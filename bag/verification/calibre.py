# -*- coding: utf-8 -*-

"""This module implements LVS/RCX using Calibre and stream out from Virtuoso.
"""

from typing import TYPE_CHECKING, Optional, List, Tuple, Dict, Any, Sequence

import fnmatch
import json
import os
import sys
import tempfile
import re
import subprocess
import shutil

from .virtuoso import VirtuosoChecker
from ..io import read_file, read_yaml, open_temp, readlines_iter

if TYPE_CHECKING:
    from .base import FlowInfo


# noinspection PyUnusedLocal
def _all_pass(retcode, log_file):
    return True


# noinspection PyUnusedLocal
def lvs_passed(retcode, log_file):
    # type: (int, str) -> Tuple[bool, str]
    """Check if LVS passed

    Parameters
    ----------
    retcode : int
        return code of the LVS process.
    log_file : str
        log file name.

    Returns
    -------
    success : bool
        True if LVS passed.
    log_file : str
        the log file name.
    """
    if not os.path.isfile(log_file):
        return False, ''

    test_str = 'LVS completed. CORRECT.'
    LogCheck = subprocess.Popen(['grep', '-i', test_str, log_file], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout, stderr = LogCheck.communicate()

    return stdout.decode() != '', log_file


LVS_LEVEL_ENV = 'BAG_LVS_LEVEL'


def _load_lvs_policy(policy_file):
    # type: (str) -> Dict[str, Any]
    policy = read_yaml(policy_file)
    if not isinstance(policy, dict):
        raise ValueError('LVS policy root must be a mapping.')
    version = policy.get('version', 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError('LVS policy version must be a positive integer.')
    return policy


def resolve_lvs_policy(policy_file, policy_level):
    # type: (Optional[str], Optional[str]) -> Optional[Dict[str, Any]]
    """Map a hierarchy level (cell/block/...) to a profile and its runset overrides.

    LVS has no per-rule markers to waive after the run, so a level never edits
    the verdict: it selects how Calibre is run -- ``runset:`` overrides (for
    example whether ``name:`` colon labels are virtually connected) and
    ``svrf:`` statements added to the comparison (for example ``LVS IGNORE
    DEVICE PIN MN B`` so a tapless cell's floating bodies are not compared) --
    and the verdict stays Calibre's own CORRECT/INCORRECT.  The ``levels:`` map lives in the policy
    file, so a requester names only a level and never an override.  A level
    the file does not declare is an error (fail closed).  Without a level the
    policy is not applied and the runset is unchanged.
    """
    if not policy_level:
        return None
    if not policy_file:
        raise ValueError(
            'LVS level {} needs lvs_policy_file in the checker config.'.format(policy_level)
        )
    policy = _load_lvs_policy(policy_file)
    levels = policy.get('levels')
    if not isinstance(levels, dict) or not levels:
        raise ValueError('LVS policy declares no levels map.')
    profile_name = levels.get(policy_level)
    if not isinstance(profile_name, str) or not profile_name:
        raise ValueError('LVS policy level not found: {}'.format(policy_level))
    profile = (policy.get('profiles') or {}).get(profile_name)
    if not isinstance(profile, dict):
        raise ValueError('LVS policy profile not found: {}'.format(profile_name))
    runset = profile.get('runset') or {}
    if not isinstance(runset, dict):
        raise ValueError('LVS policy profile {} runset must be a mapping.'.format(profile_name))
    overrides = {}
    for key, value in runset.items():
        if not isinstance(key, str) or not key:
            raise ValueError('LVS policy profile {} has an empty runset key.'.format(profile_name))
        if value is None or isinstance(value, (dict, list)):
            raise ValueError(
                'LVS policy profile {} runset {} must be a scalar.'.format(profile_name, key)
            )
        overrides[key] = value
    svrf = profile.get('svrf') or []
    if not isinstance(svrf, list) or not all(
            isinstance(cmd, str) and cmd.strip() and not re.search(r'[{}\n]', cmd)
            for cmd in svrf):
        raise ValueError(
            'LVS policy profile {} svrf must be a list of one-line SVRF statements '
            'without braces.'.format(profile_name)
        )
    fail_on_vc = profile.get('fail_on_virtual_connect', False)
    if not isinstance(fail_on_vc, bool):
        raise ValueError(
            'LVS policy profile {} fail_on_virtual_connect must be true or false.'.format(
                profile_name)
        )
    svrf = [cmd.strip() for cmd in svrf]
    if fail_on_vc and 'VIRTUAL CONNECT REPORT YES' not in svrf:
        # Without the report an empty .ext would look like "no rail was split".
        svrf.append('VIRTUAL CONNECT REPORT YES')
    return {
        'policy_file': policy_file,
        'policy_version': policy.get('version', 1),
        'level': policy_level,
        'profile': profile_name,
        'description': profile.get('description'),
        'runset_overrides': overrides,
        'svrf': svrf,
        'fail_on_virtual_connect': fail_on_vc,
    }


_VC_SECTION_RE = re.compile(r'^Extraction Errors and Warnings for cell "([^"]+)"')
_VC_NOTE_RE = re.compile(
    r'Virtually connected "([^"]+)" at \(([-\d.]+),([-\d.]+)\) on layer (\S+)'
    r' and \(([-\d.]+),([-\d.]+)\) on layer (\S+)'
)


def lvs_virtual_connections(ext_report):
    # type: (str) -> List[Dict[str, Any]]
    """Virtual connections Calibre made, from a ``<cell>.lvs.report.ext`` text.

    Each ``NOTE: Virtually connected "VDD" at (x,y) on layer L and (x,y) on
    layer L`` line (written under ``VIRTUAL CONNECT REPORT YES``) is one pair of
    same-named pieces that were joined by name, not by metal.
    """
    found = []
    cell = None
    for line in ext_report.splitlines():
        section = _VC_SECTION_RE.match(line.strip())
        if section:
            cell = section.group(1)
            continue
        note = _VC_NOTE_RE.search(line)
        if note:
            found.append({
                'cell': cell,
                'net': note.group(1),
                'points_um': [[float(note.group(2)), float(note.group(3))],
                              [float(note.group(5)), float(note.group(6))]],
                'layers': [note.group(4), note.group(7)],
            })
    return found


def merge_svrf_commands(value, commands):
    # type: (Optional[str], Sequence[str]) -> str
    """Add SVRF statements to a Calibre Interactive ``lvsSVRFCmds`` Tcl list.

    The tech runset already carries commands (``{SOURCE CASE YES} {LAYOUT CASE
    YES} {} {}``); those are kept, empty slots are filled first, and a
    statement already present is not repeated.
    """
    items = re.findall(r'\{([^{}]*)\}', value or '')
    present = {item.strip() for item in items if item.strip()}
    for cmd in commands:
        if cmd in present:
            continue
        present.add(cmd)
        for idx, item in enumerate(items):
            if not item.strip():
                items[idx] = cmd
                break
        else:
            items.append(cmd)
    return ' '.join('{%s}' % item for item in items)


def _same_runset_value(left, right):
    # type: (Any, Any) -> bool
    return str(left).strip() == str(right).strip()


def lvs_policy_passed(retcode, log_file, policy, run_dir, lib_name, cell_name,
                      report_file=None):
    # type: (int, str, Dict[str, Any], str, str, str, Optional[str]) -> Tuple[bool, str]
    """Calibre's own LVS verdict plus an audit of the level the run was made at.

    With ``fail_on_virtual_connect`` the level may only make the verdict
    stricter: a CORRECT run still fails when Calibre joined same-named pieces
    by name (``Virtually connected`` notes in ``<cell>.lvs.report.ext``), and
    it fails closed when that report, or the report statement in the control
    file, is missing.
    """
    passed, log = lvs_passed(retcode, log_file)
    calibre_passed = bool(passed)
    vc_result = None
    if policy.get('fail_on_virtual_connect'):
        vc_result = {'checked': False, 'connections': [], 'error': None}
        ext_file = os.path.join(run_dir, '%s.lvs.report.ext' % cell_name)
        ctl_file = os.path.join(run_dir, '_calibre.lvs_')
        vc_result['ext_report'] = ext_file
        try:
            if not os.path.isfile(ctl_file) or \
                    'VIRTUAL CONNECT REPORT YES' not in read_file(ctl_file):
                raise RuntimeError('control file lacks VIRTUAL CONNECT REPORT YES')
            if not os.path.isfile(ext_file):
                raise RuntimeError('extraction report is missing')
            vc_result['connections'] = lvs_virtual_connections(read_file(ext_file))
            vc_result['checked'] = True
        except Exception as err:  # fail closed
            vc_result['error'] = '{}: {}'.format(type(err).__name__, err)
        if not vc_result['checked'] or vc_result['connections']:
            passed = False
    result = {
        'schema_version': 1,
        'library': lib_name,
        'cell': cell_name,
        'policy_file': policy['policy_file'],
        'policy_version': policy['policy_version'],
        'policy_level': policy['level'],
        'policy_profile': policy['profile'],
        'runset_overrides': policy['runset_overrides'],
        'svrf': policy.get('svrf', []),
        'execution_log': log_file,
        'report_file': report_file,
        'retcode': retcode,
        'calibre_passed': calibre_passed,
        'virtual_connect_check': vc_result,
        'passed': bool(passed),
    }
    os.makedirs(run_dir, exist_ok=True)
    audit_file = os.path.join(run_dir, 'lvs_policy_result.json')
    with open(audit_file, 'w', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write('\n')
    if os.path.isfile(log_file):
        with open(log_file, 'a', encoding='utf-8') as stream:
            stream.write(
                '\nBAG LVS policy result: {} (level={}, profile={}, calibre_passed={}, '
                'virtual_connections={}, passed={})\n'.format(
                    audit_file, policy['level'], policy['profile'], calibre_passed,
                    ('-' if vc_result is None else
                     len(vc_result['connections']) if vc_result['checked'] else 'unchecked'),
                    bool(passed),
                )
            )
    return passed, log


def _format_calibre_runset_value(value):
    # type: (Any) -> str
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (list, tuple)):
        return '[ {} ]'.format(
            ', '.join(json.dumps(item) for item in value)
        )
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value)


def _replace_calibre_runset_assignment(content, key, value):
    # type: (str, str, Any) -> str
    pattern = re.compile(
        r'^(\s*{}\s*=\s*).*$'.format(re.escape(key)),
        flags=re.MULTILINE,
    )
    content, count = pattern.subn(
        lambda match: match.group(1) + _format_calibre_runset_value(value),
        content,
    )
    if count != 1:
        raise ValueError(
            'Expected one assignment for {!r} in modern Calibre runset; '
            'found {}.'.format(key, count)
        )
    return content


# noinspection PyUnusedLocal
def _drc_rule_counts(content):
    # type: (str) -> Dict[str, int]
    """Return top-level Calibre result counts for each RULECHECK."""
    counts = {}
    by_cell = re.search(
        r'^\s*-+\s*RULECHECK\s+RESULTS\s+STATISTICS\s*'
        r'\(BY\s+CELL\)\s*$',
        content,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if by_cell:
        content = content[:by_cell.start()]
    pattern = re.compile(
        r'^\s*RULECHECK\s+(.+?)\s+(?:\.+\s*)?'
        r'TOTAL\s+Result\s+Count\s*=\s*(\d+)'
        r'(?:\s+\(\d+\))?\s*$',
        flags=re.IGNORECASE | re.MULTILINE,
    )
    for match in pattern.finditer(content):
        counts[match.group(1).strip()] = int(match.group(2))
    return counts


def _matches_any(value, patterns):
    # type: (str, Sequence[str]) -> bool
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


_DRC_WAIVER_WHERE = ('anywhere', 'boundary')


def _load_drc_policy(policy_file):
    # type: (str) -> Dict[str, Any]
    policy = read_yaml(policy_file)
    if not isinstance(policy, dict):
        raise ValueError('DRC policy root must be a mapping.')
    version = policy.get('version', 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError('DRC policy version must be a positive integer.')
    return policy


def _resolve_drc_profile(policy, policy_level, policy_profile):
    # type: (Dict[str, Any], Optional[str], Optional[str]) -> str
    """Map a hierarchy level (cell/block/chip) to a profile name through ``levels:``.

    The map lives in the policy file so that a requester names only a level and
    never a waiver list.  A level the file does not declare is an error (the
    judgement fails closed), and without a level the profile is used directly.
    """
    if policy_level:
        if policy.get('version', 1) < 2:
            if policy_level == 'cell' and policy_profile:
                # A version 1 file knows only the cell-level profile; that is
                # what level "cell" means, so the named profile stands.
                return policy_profile
            raise ValueError(
                'DRC policy levels need version 2 (got level {}).'.format(policy_level)
            )
        levels = policy.get('levels')
        if not isinstance(levels, dict) or not levels:
            raise ValueError('DRC policy declares no levels map.')
        profile = levels.get(policy_level)
        if not isinstance(profile, str) or not profile:
            raise ValueError(
                'DRC policy level not found: {}'.format(policy_level)
            )
        if policy_profile and policy_profile != profile:
            raise ValueError(
                'DRC policy level {} maps to {}, not {}.'.format(
                    policy_level, profile, policy_profile
                )
            )
        return profile
    if not policy_profile:
        raise ValueError('DRC policy needs a profile or a level.')
    return policy_profile


def _load_drc_waivers(policy_file, policy_profile, runset_file, lib_name,
                      policy=None):
    # type: (str, str, str, str, Optional[Dict[str, Any]]) -> Tuple[bool, Dict[str, Dict[str, Any]]]
    """Return (in_scope, {rule: {'reason', 'where', 'margin_um'}}) for a profile.

    ``where`` is ``anywhere`` (the rule's whole count is waived, version 1
    behaviour) or ``boundary`` (version 2: only markers that touch the judged
    cell's extent within ``margin_um`` are waived; the rest stay violations).
    """
    if policy is None:
        policy = _load_drc_policy(policy_file)
    version = policy.get('version', 1)
    profiles = policy.get('profiles', {})
    profile = profiles.get(policy_profile)
    if not isinstance(profile, dict):
        raise ValueError(
            'DRC policy profile not found: {}'.format(policy_profile)
        )

    scope = profile.get('scope', {})
    runsets = scope.get('runsets', ['*'])
    libraries = scope.get('libraries', ['*'])
    if not isinstance(runsets, list) or not isinstance(libraries, list):
        raise ValueError('DRC policy scope runsets/libraries must be lists.')
    runset_name = os.path.basename(runset_file) if runset_file else ''
    in_scope = (
        _matches_any(runset_name, runsets)
        and _matches_any(lib_name or '', libraries)
    )
    if not in_scope:
        return False, {}

    default_margin = profile.get('boundary_margin_nm', 0)
    if isinstance(default_margin, bool) or not isinstance(default_margin, (int, float)) \
            or default_margin < 0:
        raise ValueError('DRC policy boundary_margin_nm must be a non-negative number.')

    waivers = {}
    for entry in profile.get('waive', []) or []:
        if not isinstance(entry, dict) or not entry.get('rule'):
            raise ValueError('Each DRC waiver requires a rule.')
        reason = entry.get('reason')
        if not reason:
            raise ValueError(
                'DRC waiver {} requires a reason.'.format(entry['rule'])
            )
        where = entry.get('where', 'anywhere')
        if where not in _DRC_WAIVER_WHERE:
            raise ValueError(
                'DRC waiver {} has unknown where: {}'.format(entry['rule'], where)
            )
        margin = entry.get('margin_nm', default_margin)
        if isinstance(margin, bool) or not isinstance(margin, (int, float)) or margin < 0:
            raise ValueError(
                'DRC waiver {} margin_nm must be a non-negative number.'.format(entry['rule'])
            )
        if where != 'anywhere' and version < 2:
            raise ValueError(
                'DRC waiver {} uses where: {} which needs policy version 2.'.format(
                    entry['rule'], where
                )
            )
        waivers[str(entry['rule'])] = {
            'reason': str(reason),
            'where': where,
            'margin_um': float(margin) / 1000.0,
        }
    return True, waivers


def _drc_results_markers(content, rule_names):
    # type: (str, Sequence[str]) -> Dict[str, List[Tuple[float, float, float, float]]]
    """Marker bounding boxes (micrometres) per rule from a Calibre ASCII results file.

    A rule block is the rule name on its own line followed by the
    ``<n> <count> <count> <timestamp>`` line; markers inside it are
    ``<p|e> <id> <npoints>`` followed by the vertices (``x y`` per line for a
    polygon, ``x1 y1 x2 y2`` for an edge pair).  Description lines never match
    the marker header and are skipped.
    """
    wanted = set(rule_names)
    lines = content.splitlines()
    if not lines:
        return {}
    header = lines[0].split()
    precision = 1000.0
    if len(header) > 1:
        try:
            precision = float(header[1])
        except ValueError:
            pass
    marker_re = re.compile(r'^([pe])\s+(\d+)\s+(\d+)')
    count_re = re.compile(r'^\d+\s+\d+\s+\d+(\s|$)')
    num_re = re.compile(r'^-?\d+$')
    out = {}  # type: Dict[str, List[Tuple[float, float, float, float]]]
    cur = None
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if (stripped and not marker_re.match(lines[i]) and i + 1 < len(lines)
                and count_re.match(lines[i + 1].strip())):
            cur = stripped if stripped in wanted else None
            if cur is not None:
                out.setdefault(cur, [])
            i += 2
            continue
        match = marker_re.match(lines[i])
        if match and cur is not None:
            npts = int(match.group(3))
            pts = []
            for k in range(1, npts + 1):
                if i + k < len(lines):
                    q = lines[i + k].split()
                    if len(q) in (2, 4) and all(num_re.match(v) for v in q):
                        for j in range(0, len(q), 2):
                            pts.append((int(q[j]) / precision, int(q[j + 1]) / precision))
            if pts:
                xs = [pt[0] for pt in pts]
                ys = [pt[1] for pt in pts]
                out[cur].append((min(xs), min(ys), max(xs), max(ys)))
            i += npts + 1
            continue
        i += 1
    return out


def _marker_depth(marker, extent):
    # type: (Tuple[float, float, float, float], Tuple[float, float, float, float]) -> float
    """How far inside the extent the marker starts: its smallest distance to any edge.

    Zero or negative means the marker touches or crosses the cell edge.
    """
    return min(marker[0] - extent[0], marker[1] - extent[1],
               extent[2] - marker[2], extent[3] - marker[3])


def _touches_boundary(marker, extent, margin_um):
    # type: (Tuple[float, float, float, float], Tuple[float, float, float, float], float) -> bool
    """True when the marker comes within ``margin_um`` of any edge of the extent."""
    return _marker_depth(marker, extent) <= margin_um


def _drc_profile_extent_layer(policy, policy_profile):
    # type: (Dict[str, Any], str) -> Optional[str]
    """The profile's ``extent_layer`` (e.g. ``"108/0"``, the PDK prBoundary) or None."""
    profile = (policy.get('profiles') or {}).get(policy_profile) or {}
    layer = profile.get('extent_layer')
    if layer is None:
        return None
    from .gds_extent import parse_layer
    lay, dt = parse_layer(layer)
    return '{}/{}'.format(lay, dt)


def _find_drc_results_file(run_dir, cell_name):
    # type: (str, Optional[str]) -> Optional[str]
    candidates = []
    if cell_name:
        candidates.append(os.path.join(run_dir, '{}.drc.results'.format(cell_name)))
    candidates.append(os.path.join(run_dir, 'drc.results'))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


# noinspection PyUnusedLocal
def drc_passed(retcode, log_file, summary_file, policy_file=None,
               policy_profile=None, runset_file=None, lib_name=None,
               cell_name=None, run_dir=None, policy_level=None,
               gds_file=None, results_file=None):
    # type: (int, str, str, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]) -> Tuple[bool, str]
    """Check Calibre DRC and optionally apply an auditable rule waiver policy.

    ``policy_level`` (``cell``/``block``/...) selects the profile through the
    policy file's ``levels:`` map; ``policy_profile`` names it directly.  A
    waiver with ``where: boundary`` is applied per marker against the extent of
    the judged cell read from ``gds_file`` (default ``<run_dir>/layout.gds``),
    using the markers in ``results_file`` (default ``<run_dir>/<cell>.drc.results``).
    When the extent or the rule's markers are unavailable such a waiver does not
    apply and the rule's whole count stays a violation.
    """
    audit_dir = run_dir or os.path.dirname(summary_file)
    audit_file = os.path.join(audit_dir, 'drc_policy_result.json')
    result = {
        'schema_version': 2,
        'library': lib_name,
        'cell': cell_name,
        'runset': runset_file,
        'summary_file': summary_file,
        'execution_log': log_file,
        'policy_file': policy_file,
        'policy_profile': policy_profile,
        'policy_level': policy_level,
        'raw_passed': False,
        'policy_passed': False,
        'raw_violation_count': None,
        'waived_violation_count': 0,
        'remaining_violation_count': None,
        'rule_counts': {},
        'waived_rules': {},
        'remaining_rules': {},
        'boundary_waivers': {},
        'remaining_markers': {},
        'extent': None,
        'extent_source': 'unavailable',
    }

    try:
        if retcode != 0:
            raise RuntimeError(
                'Calibre DRC exited with status {}.'.format(retcode)
            )
        if not os.path.isfile(summary_file):
            raise RuntimeError('Calibre DRC summary file is missing.')

        content = read_file(summary_file)
        matches = re.findall(
            r'TOTAL\s+(?:DRC\s+)?RESULTS?\s+GENERATED\s*[:=]\s*(\d+)',
            content,
            flags=re.IGNORECASE,
        )
        if not matches:
            raise RuntimeError(
                'Calibre DRC total result count was not found.'
            )

        raw_count = int(matches[-1])
        rule_counts = {
            rule: count
            for rule, count in _drc_rule_counts(content).items()
            if count
        }
        result['raw_violation_count'] = raw_count
        result['raw_passed'] = raw_count == 0
        result['rule_counts'] = rule_counts

        if bool(policy_file) != bool(policy_profile or policy_level):
            raise ValueError(
                'drc_policy_file needs drc_policy_profile or a policy level, and vice versa.'
            )

        waivers = {}  # type: Dict[str, Dict[str, Any]]
        in_scope = False
        if policy_file:
            policy = _load_drc_policy(policy_file)
            policy_profile = _resolve_drc_profile(policy, policy_level, policy_profile)
            result['policy_profile'] = policy_profile
            in_scope, waivers = _load_drc_waivers(
                policy_file, policy_profile, runset_file, lib_name, policy=policy
            )
        result['policy_in_scope'] = in_scope

        boundary_rules = [
            rule for rule in rule_counts
            if rule in waivers and waivers[rule]['where'] == 'boundary'
        ]
        extent = None
        markers = {}  # type: Dict[str, List[Tuple[float, float, float, float]]]
        if not boundary_rules:
            # No boundary-scoped rule fired, so the extent was never needed.
            result['extent_source'] = 'not_needed'
        else:
            extent_layer = _drc_profile_extent_layer(policy, policy_profile)
            result['extent_layer'] = extent_layer
            gds_path = gds_file or (os.path.join(audit_dir, 'layout.gds') if audit_dir else None)
            if gds_path and os.path.isfile(gds_path) and cell_name:
                try:
                    from .gds_extent import gds_cell_extent
                    if extent_layer is not None:
                        try:
                            # The placement box: the PDK prBoundary the templates draw.
                            extent = gds_cell_extent(gds_path, cell_name, layer=extent_layer)
                            result['extent_source'] = 'layer {}'.format(extent_layer)
                        except ValueError as err:
                            # Nothing on that layer (a cell drawn without templates):
                            # fall back to every drawn shape and say so.
                            result['extent_layer_error'] = str(err)
                    if extent is None:
                        extent = gds_cell_extent(gds_path, cell_name)
                        result['extent_source'] = 'all_layers'
                    result['extent'] = list(extent)
                except Exception as err:  # fail closed: boundary waivers stay violations
                    extent = None
                    result['extent_source'] = 'unavailable'
                    result['extent_error'] = '{}: {}'.format(type(err).__name__, err)
            results_path = results_file or _find_drc_results_file(audit_dir, cell_name)
            if results_path and os.path.isfile(results_path):
                markers = _drc_results_markers(read_file(results_path), boundary_rules)
                result['results_file'] = results_path

        waived_rules = {}
        remaining_rules = {}
        for rule, count in rule_counts.items():
            waiver = waivers.get(rule)
            if waiver is None:
                remaining_rules[rule] = count
                continue
            if waiver['where'] == 'anywhere':
                waived_rules[rule] = count
                continue
            # where: boundary -- judged per marker, never per summary count.
            margin = waiver['margin_um']
            rule_markers = markers.get(rule)
            entry = {
                'summary_count': count,
                'marker_count': len(rule_markers) if rule_markers is not None else None,
                'margin_um': margin,
            }
            if extent is None:
                entry['scope'] = 'extent_unavailable'
                remaining_rules[rule] = count
            elif not rule_markers:
                entry['scope'] = 'no_markers'
                remaining_rules[rule] = count
            else:
                waived = [m for m in rule_markers if _touches_boundary(m, extent, margin)]
                kept = [m for m in rule_markers if not _touches_boundary(m, extent, margin)]
                entry['scope'] = 'boundary'
                # Depth statistics: how deep the waived markers reach and how
                # close the kept ones come. A margin is right when the two sets
                # are clearly apart on known-clean cells.
                entry['waived_depth_max_um'] = (
                    round(max(_marker_depth(m, extent) for m in waived), 6) if waived else None
                )
                entry['remaining_depth_min_um'] = (
                    round(min(_marker_depth(m, extent) for m in kept), 6) if kept else None
                )
                if len(rule_markers) == count:
                    waived_count = len(waived)
                else:
                    # Summary and results disagree: never waive more than the
                    # summary counts and never fewer violations than the
                    # interior markers seen.
                    waived_count = min(count, len(waived))
                    if count - waived_count < len(kept):
                        waived_count = max(0, count - len(kept))
                remaining_count = count - waived_count
                if waived_count:
                    waived_rules[rule] = waived_count
                if remaining_count:
                    remaining_rules[rule] = remaining_count
                    result['remaining_markers'][rule] = [
                        [round(v, 6) for v in m] for m in kept
                    ]
            entry['waived'] = waived_rules.get(rule, 0)
            entry['remaining'] = remaining_rules.get(rule, 0)
            result['boundary_waivers'][rule] = entry

        waived_count = min(raw_count, sum(waived_rules.values()))
        remaining_count = raw_count - waived_count
        result['waived_rules'] = waived_rules
        result['waiver_reasons'] = {
            rule: waivers[rule]['reason'] for rule in waived_rules
        }
        result['remaining_rules'] = remaining_rules
        result['waived_violation_count'] = waived_count
        result['remaining_violation_count'] = remaining_count
        result['policy_passed'] = remaining_count == 0
    except Exception as err:
        result['error'] = '{}: {}'.format(type(err).__name__, err)
        result['policy_passed'] = False

    os.makedirs(audit_dir, exist_ok=True)
    with open(audit_file, 'w', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write('\n')
    if os.path.isfile(log_file):
        with open(log_file, 'a', encoding='utf-8') as stream:
            stream.write(
                '\nBAG DRC policy result: {} (policy_passed={})\n'.format(
                    audit_file, result['policy_passed']
                )
            )

    return result['policy_passed'], log_file


# noinspection PyUnusedLocal
def query_passed(retcode, log_file):
    # type: (int, str) -> Tuple[bool, str]
    """Check if query passed

    Parameters
    ----------
    retcode : int
        return code of the query process.
    log_file : str
        log file name.

    Returns
    -------
    success : bool
        True if query passed.
    log_file : str
        the log file name.
    """
    if not os.path.isfile(log_file):
        return False, ''

    test_str = 'OK: Terminating.'
    LogCheck = subprocess.Popen(['grep', '-i', test_str, log_file], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout, stderr = LogCheck.communicate()

    return stdout.decode() != '', log_file


class Calibre(VirtuosoChecker):
    """A subclass of VirtuosoChecker that uses Calibre for verification.

    Parameters
    ----------
    tmp_dir : string
        temporary directory to save files in.
    lvs_run_dir : str
        the LVS run directory.
    lvs_runset : str
        the LVS runset filename.
    drc_run_dir : str
        the DRC run directory.
    drc_runset : str
        the DRC runset filename.
    drc_policy_file : str
        optional YAML policy containing scoped cell-level DRC waivers.
    drc_policy_profile : str
        profile name selected from ``drc_policy_file``.
    lvs_policy_file : str
        optional YAML policy mapping an LVS hierarchy level (``cell``/``block``)
        to runset overrides.  Applied only when a run names a level
        (``lvs_level=`` or the ``BAG_LVS_LEVEL`` environment variable).
    rcx_run_dir : str
        the RCX run directory.
    rcx_runset : str
        the RCX runset filename.
    source_added_file : str
        the Calibre source.added file location.  Environment variable is supported.
        Default value is '$DK/Calibre/lvs/source.added'.
    rcx_mode : str
        the RC extraction mode.  Either 'pex' or 'xact' or 'starrc'.  Defaults to 'pex'.
    xact_rules : str
        the XACT rules file name.
    """

    def __init__(self, tmp_dir, lvs_run_dir, lvs_runset, rcx_run_dir, rcx_runset,
                 source_added_file='$DK/Calibre/lvs/source.added', rcx_mode='pex',
                 xact_rules='', drc_run_dir=None, drc_runset=None,
                 drc_policy_file=None, drc_policy_profile=None,
                 lvs_policy_file=None, **kwargs):

        max_workers = kwargs.get('max_workers', None)
        cancel_timeout = kwargs.get('cancel_timeout_ms', None)
        rcx_params = kwargs.get('rcx_params', {})
        lvs_params = kwargs.get('lvs_params', {})
        rcx_link_files = kwargs.get('rcx_link_files', None)

        if cancel_timeout is not None:
            cancel_timeout /= 1e3

        VirtuosoChecker.__init__(self, tmp_dir, max_workers, cancel_timeout, source_added_file)

        self.default_rcx_params = rcx_params
        self.default_lvs_params = lvs_params
        self.lvs_run_dir = os.path.abspath(rcx_run_dir if (rcx_mode == 'starrc' or rcx_mode == 'qrc') else lvs_run_dir)
        self.lvs_runset = lvs_runset
        self.drc_run_dir = (
            os.path.abspath(drc_run_dir) if drc_run_dir else None
        )
        self.drc_runset = drc_runset
        self.drc_policy_file = drc_policy_file
        self.drc_policy_profile = drc_policy_profile
        self.lvs_policy_file = lvs_policy_file
        self.rcx_run_dir = os.path.abspath(rcx_run_dir)
        self.rcx_runset = rcx_runset
        self.rcx_link_files = rcx_link_files
        self.xact_rules = xact_rules
        self.rcx_mode = rcx_mode

    def get_rcx_netlists(self, lib_name, cell_name):
        # type: (str, str) -> List[str]
        """Returns a list of generated extraction netlist file names.

        Parameters
        ----------
        lib_name : str
            library name.
        cell_name : str
            cell_name

        Returns
        -------
        netlists : List[str]
            a list of generated extraction netlist file names.  The first index is the main netlist.
        """
        # PVS generate schematic cellviews directly.
        if self.rcx_mode == 'starrc' or self.rcx_mode == 'qrc':
            return ['%s.spf' % cell_name]
        else:
            return ['%s.pex.netlist' % cell_name,
                    # '%s.pex.netlist.pex' % cell_name,
                    # '%s.pex.netlist.%s.pxi' % (cell_name, cell_name),
                    ]

    def setup_drc_flow(self, lib_name, cell_name):
        # type: (str, str) -> Sequence[FlowInfo]
        if not self.drc_run_dir or not self.drc_runset:
            raise ValueError(
                'Calibre DRC requires drc_run_dir and drc_runset.'
            )

        run_dir = os.path.join(self.drc_run_dir, lib_name, cell_name)
        os.makedirs(run_dir, exist_ok=True)
        lay_file = os.path.join(run_dir, 'layout.gds')

        cmd, log_file, env, cwd = self.setup_export_layout(
            lib_name, cell_name, lay_file, 'layout', None
        )
        flow_list = [(cmd, log_file, env, cwd, _all_pass)]

        summary_name = '%s.drc.summary' % cell_name
        summary_file = os.path.join(run_dir, summary_name)
        with open_temp(prefix='drcLog', dir=run_dir, delete=False) as logf:
            drc_log_file = logf.name

        runset_content = self.modify_drc_runset(
            run_dir, lib_name, cell_name, lay_file
        )
        with open_temp(
                prefix='drcRunset', dir=run_dir, delete=False) as runset_file:
            runset_fname = runset_file.name
            runset_file.write(runset_content)

        cmd = ['calibre', '-gui', '-drc', '-runset', runset_fname, '-batch']
        flow_list.append(
            (
                cmd,
                drc_log_file,
                None,
                run_dir,
                lambda rc, lf: drc_passed(
                    rc,
                    lf,
                    summary_file,
                    policy_file=self.drc_policy_file,
                    policy_profile=self.drc_policy_profile,
                    runset_file=self.drc_runset,
                    lib_name=lib_name,
                    cell_name=cell_name,
                    run_dir=run_dir,
                ),
            )
        )
        return flow_list

    def setup_lvs_flow(self, lib_name, cell_name, sch_view='schematic', lay_view='layout',
                       params=None, **kwargs):
        # type: (str, str, str, str, Optional[Dict[str, Any]], Any) -> Sequence[FlowInfo]

        run_dir = os.path.join(self.lvs_run_dir, lib_name, cell_name)
        os.makedirs(run_dir, exist_ok=True)
        lay_file, sch_file = self._get_lay_sch_files(run_dir)

        # add schematic/layout export to flow
        flow_list = []

        # Check if gds layout is provided
        gds_layout_path = kwargs.pop('gds_layout_path', None)
        source_netlist_path = kwargs.pop('source_netlist_path', None)
        source_lib_name = kwargs.pop('source_lib_name', lib_name)
        source_cell_name = kwargs.pop('source_cell_name', cell_name)
        lvs_level = kwargs.pop('lvs_level', None) or os.environ.get(LVS_LEVEL_ENV) or None
        # Resolve before any export so an unknown level costs no tool run.
        lvs_policy = resolve_lvs_policy(
            getattr(self, 'lvs_policy_file', None), lvs_level
        )

        # If not provided the gds layout, need to export layout
        if not gds_layout_path:
            cmd, log, env, cwd = self.setup_export_layout(lib_name, cell_name, lay_file, lay_view, None)
            flow_list.append((cmd, log, env, cwd, _all_pass))
        # If provided gds layout, do not export layout, just copy gds
        else:
            if not os.path.exists(gds_layout_path):
                raise ValueError(f'gds_layout_path does not exist: {gds_layout_path}')
            with open_temp(prefix='copy', dir=run_dir, delete=True) as f:
                copy_log_file = f.name
            copy_cmd = ['cp', gds_layout_path, os.path.abspath(lay_file)]
            flow_list.append((copy_cmd, copy_log_file, None, None, _all_pass))

        if source_netlist_path:
            if not os.path.exists(source_netlist_path):
                raise ValueError(
                    'source_netlist_path does not exist: {}'
                    .format(source_netlist_path)
                )
            source_netlist_path = os.path.abspath(source_netlist_path)
            source_netlist_dir = os.path.dirname(source_netlist_path)
            with open_temp(prefix='copySourceTree', dir=run_dir,
                           delete=True) as f:
                copy_tree_log_file = f.name
            copy_tree_cmd = [
                'cp', '-R', os.path.join(source_netlist_dir, '.'),
                os.path.abspath(run_dir),
            ]
            flow_list.append(
                (copy_tree_cmd, copy_tree_log_file, None, None, _all_pass)
            )
            with open_temp(prefix='copySource', dir=run_dir, delete=True) as f:
                copy_log_file = f.name
            copy_cmd = [
                'cp', source_netlist_path,
                os.path.abspath(sch_file),
            ]
            flow_list.append(
                (copy_cmd, copy_log_file, None, None, _all_pass)
            )
        else:
            cmd, log, env, cwd = self.setup_export_schematic(
                lib_name, cell_name, sch_file, sch_view, None
            )
            flow_list.append((cmd, log, env, cwd, _all_pass))

        lvs_params_actual = self.default_lvs_params.copy()
        if params is not None:
            lvs_params_actual.update(params)
        if lvs_policy is not None:
            # The level owns these keys: a caller may repeat a value but never
            # loosen it, the same way a DRC requester cannot pick waivers.
            for key, value in lvs_policy['runset_overrides'].items():
                if key in lvs_params_actual and not _same_runset_value(
                        lvs_params_actual[key], value):
                    raise ValueError(
                        'LVS level {} sets {}={}; the run asked for {}.'.format(
                            lvs_policy['level'], key, value, lvs_params_actual[key]
                        )
                    )
                lvs_params_actual[key] = value

        with open_temp(prefix='lvsLog', dir=run_dir, delete=False) as logf:
            log_file = logf.name

        # generate new runset
        runset_content = self.modify_lvs_runset(
            run_dir, lib_name, cell_name, lay_view, lay_file,
            sch_file, lvs_params_actual,
            source_lib_name=source_lib_name,
            source_cell_name=source_cell_name,
            extra_svrf=lvs_policy['svrf'] if lvs_policy is not None else None,
        )

        # save runset
        with open_temp(dir=run_dir, delete=False) as runset_file:
            runset_fname = runset_file.name
            runset_file.write(runset_content)

        cmd = ['calibre', '-gui', '-lvs', '-runset', runset_fname, '-batch']

        if lvs_policy is None:
            flow_list.append((cmd, log_file, None, run_dir, lvs_passed))
        else:
            report_file = os.path.join(run_dir, '%s.lvs.report' % cell_name)
            flow_list.append(
                (
                    cmd,
                    log_file,
                    None,
                    run_dir,
                    lambda rc, lf: lvs_policy_passed(
                        rc, lf, lvs_policy, run_dir, lib_name, cell_name,
                        report_file=report_file,
                    ),
                )
            )

        return flow_list

    RCX_FILE_INPUT_VERSION = 1

    def setup_rcx_flow(self, lib_name, cell_name, sch_view='schematic', lay_view='layout',
                       params=None, **kwargs):
        # type: (str, str, str, str, Optional[Dict[str, Any]], Any) -> Sequence[FlowInfo]

        # update default RCX parameters.
        rcx_params_actual = self.default_rcx_params.copy()
        if params is not None:
            rcx_params_actual.update(params)

        gds_layout_path = kwargs.pop('gds_layout_path', None)
        source_netlist_path = kwargs.pop('source_netlist_path', None)
        source_lib_name = kwargs.pop('source_lib_name', lib_name)
        source_cell_name = kwargs.pop('source_cell_name', cell_name)
        for key, path in [('gds_layout_path', gds_layout_path), ('source_netlist_path', source_netlist_path)]:
            if path is not None and not os.path.isfile(path):
                raise ValueError('{} is not a file: {}'.format(key, path))
        if source_netlist_path is not None:
            from .input_files import cdl_files
            cdl_files(source_netlist_path)  # Validate includes before scheduling any export.
        if (source_lib_name != lib_name or source_cell_name != cell_name) and self.rcx_mode != 'pex':
            raise ValueError('Distinct RCX source identifiers require the Calibre PEX backend')

        run_dir = os.path.join(self.rcx_run_dir, lib_name, cell_name)
        os.makedirs(run_dir, exist_ok=True)

        # make symlinks
        query_input = None
        if self.rcx_link_files:
            for source_file in self.rcx_link_files:
                base_name = os.path.basename(source_file)
                targ_file = os.path.join(run_dir, base_name)
                if 'query' in base_name:
                    query_input = targ_file
                if not os.path.exists(targ_file):
                    os.symlink(source_file, targ_file)

        lay_file, sch_file = self._get_lay_sch_files(run_dir)
        with open_temp(prefix='rcxLog', dir=run_dir, delete=False) as logf:
            log_file = logf.name
        flow_list = []
        if source_netlist_path is not None:
            # Never overwrite the caller's source if it is already in this run directory.
            sch_file = os.path.join(tempfile.mkdtemp(prefix='rcx_source_', dir=run_dir), 'schematic.net')

        # If not provided the gds layout, need to export layout
        if not gds_layout_path:
            cmd, log, env, cwd = self.setup_export_layout(lib_name, cell_name, lay_file, lay_view, None)
            flow_list.append((cmd, log, env, cwd, _all_pass))
        # If provided gds layout, do not export layout, just copy gds
        else:
            if not os.path.exists(gds_layout_path):
                raise ValueError(f'gds_layout_path does not exist: {gds_layout_path}')
            with open_temp(prefix='copy', dir=run_dir, delete=True) as f:
                copy_log_file = f.name
            if not (os.path.isfile(lay_file) and os.path.samefile(gds_layout_path, lay_file)):
                copy_cmd = ['cp', gds_layout_path, os.path.abspath(lay_file)]
                flow_list.append((copy_cmd, copy_log_file, None, None, lambda rc, log: rc == 0))

        if source_netlist_path is not None:
            with open_temp(prefix='copySource', dir=run_dir, delete=False) as f:
                copy_log = f.name
            cmd = [sys.executable, '-m', 'bag.verification.input_files',
                   os.path.abspath(source_netlist_path), os.path.abspath(sch_file)]
            flow_list.append((cmd, copy_log, None, None, lambda rc, log: rc == 0))
        else:
            cmd, log, env, cwd = self.setup_export_schematic(lib_name, cell_name, sch_file, sch_view,
                                                             None)
            flow_list.append((cmd, log, env, cwd, _all_pass))

        if self.rcx_mode == 'starrc' or self.rcx_mode == 'qrc':
            # check if LVS was run prior to run_rcx
            sp_file = os.path.join(run_dir, cell_name + '.sp')
            if not os.path.isfile(sp_file):
                raise Exception('Did you forget to do run_lvs first?')

            # now query the LVS file using query.input
            with open_temp(prefix='queryLog', dir=run_dir, delete=False) as queryf:
                query_file = queryf.name

            if query_input is None:
                query_input = os.path.join(run_dir, 'query.input')
            cmd = ['calibre', '-query_input', query_input,
                   '-query', os.path.join(run_dir, 'svdb'), cell_name]
            flow_list.append((cmd, query_file, None, run_dir,
                              lambda rc, lf: query_passed(rc, lf)[0]))

            if self.rcx_mode == 'starrc':
                # generate new cmd for StarXtract
                cmd_content, result = self.modify_starrc_cmd(run_dir, lib_name, cell_name,
                                                             rcx_params_actual, query_input, sch_file)

                # save cmd for StarXtract
                with open_temp(dir=run_dir, delete=False) as cmd_file:
                    cmd_fname = cmd_file.name
                    cmd_file.write(cmd_content)

                cmd = ['StarXtract', cmd_fname]
            else:
                # generate new cmd for QRC
                cmd_content, result = self.modify_qrc_cmd(run_dir, cell_name, rcx_params_actual, sch_file)
                # save cmd for QRC
                with open_temp(dir=run_dir, delete=False) as cmd_file:
                    cmd_fname = cmd_file.name
                    cmd_file.write(cmd_content)

                cmd = ['qrc', '-64', '-cmd', cmd_fname]
        elif self.rcx_mode == 'pex':
            if source_netlist_path is not None:
                rcx_params_actual['pexSourceLibrary'] = source_lib_name
                rcx_params_actual['pexSourcePrimary'] = source_cell_name
            # generate new runset
            runset_content, result = self.modify_pex_runset(run_dir, lib_name, cell_name, lay_view,
                                                            lay_file, sch_file, rcx_params_actual)

            # save runset
            with open_temp(dir=run_dir, delete=False) as runset_file:
                runset_fname = runset_file.name
                runset_file.write(runset_content)

            # remove old svdb directory
            svdb_dir = os.path.join(run_dir, 'svdb')
            if os.path.exists(svdb_dir) and os.path.isdir(svdb_dir):
                shutil.rmtree(svdb_dir)

            cmd = ['calibre', '-gui', '-pex', '-runset', runset_fname, '-batch']
        else:
            # generate new runset
            runset_content, result = self.modify_xact_rules(run_dir, cell_name, lay_file, sch_file,
                                                            rcx_params_actual)

            # save runset
            with open_temp(dir=run_dir, delete=False) as runset_file:
                runset_fname = runset_file.name
                runset_file.write(runset_content)

            with open_temp(prefix='lvsLog', dir=run_dir, delete=False) as lvsf:
                lvs_file = lvsf.name

            num_cores = rcx_params_actual.get('num_cores', 2)
            cmd = ['calibre', '-lvs', '-hier', '-turbo', '%d' % num_cores, '-nowait', runset_fname]
            flow_list.append(
                (cmd, lvs_file, None, run_dir, lambda rc, lf: lvs_passed(rc, lf)[0]))

            extract_mode = rcx_params_actual.get('extract_mode', 'rcc')
            cmd = ['calibre', '-xact', '-3d', '-%s' % extract_mode, '-turbo', '%d' % num_cores,
                   runset_fname]

        # noinspection PyUnusedLocal
        def rcx_passed(retcode, log_fname):
            if not os.path.isfile(result):
                return None, log_fname

            if self.rcx_mode in ['qrc', 'pex']:
                if self.rcx_mode == 'qrc':
                    test_str = ' terminated normally  *****'
                else:
                    test_str = ' Errors  =  0'
                LogCheck = subprocess.Popen(['grep', '-i', test_str, log_fname], stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT)
                stdout, stderr = LogCheck.communicate()

                if stdout.decode() == '':
                    return None, log_fname

            return result, log_fname

        flow_list.append((cmd, log_file, None, run_dir, rcx_passed))
        return flow_list

    @classmethod
    def _get_lay_sch_files(cls, run_dir):
        lay_file = os.path.join(run_dir, 'layout.gds')
        sch_file = os.path.join(run_dir, 'schematic.net')
        return lay_file, sch_file

    def modify_drc_runset(self, run_dir, lib_name, cell_name, gds_file):
        # type: (str, str, str, str) -> str
        """Return a cell-specific Calibre DRC runset."""
        drc_options = {}
        for line in readlines_iter(self.drc_runset):
            key, val = line.split(':', 1)
            key = key.strip('*')
            drc_options[key] = val.strip()

        rules_file = drc_options.get('drcRulesFile')
        if rules_file:
            # Tech runsets carry the rule deck as $BAG_WORK_DIR/... or ~/...
            # so no per-user absolute path lands in git. Expand those before
            # deciding whether the value is relative to the DRC run dir;
            # otherwise "$BAG_WORK_DIR/..." is joined under drc_run_dir and
            # Calibre exits with "Rules File not found" and no summary.
            rules_file = os.path.expanduser(os.path.expandvars(rules_file))
            if not os.path.isabs(rules_file):
                rules_file = os.path.join(self.drc_run_dir, rules_file)
            drc_options['drcRulesFile'] = os.path.normpath(rules_file)

        drc_options['drcRunDir'] = run_dir
        drc_options['drcLayoutPaths'] = gds_file
        drc_options['drcLayoutPrimary'] = cell_name
        drc_options['drcLayoutLibrary'] = lib_name
        drc_options['drcLayoutView'] = 'layout'
        drc_options['drcResultsFile'] = '%s.drc.results' % cell_name
        drc_options['drcSummaryFile'] = '%s.drc.summary' % cell_name
        drc_options['cmnFDILayoutLibrary'] = lib_name
        drc_options['cmnFDILayoutView'] = 'layout'
        drc_options['cmnFDIDEFLayoutPath'] = '%s.def' % cell_name

        return ''.join(
            ('*%s: %s\n' % (key, val) for key, val in drc_options.items())
        )

    def modify_lvs_runset(self, run_dir, lib_name, cell_name, lay_view, gds_file, netlist,
                          lvs_params, source_lib_name=None,
                          source_cell_name=None, extra_svrf=None):
        # type: (str, str, str, str, str, str, Dict[str, Any]) -> str
        """Modify the given LVS runset file.

        Parameters
        ----------
        run_dir : str
            the run directory.
        lib_name : str
            the library name.
        cell_name : str
            the cell name.
        lay_view : str
            the layout view.
        gds_file : str
            the layout gds file name.
        netlist : str
            the schematic netlist file.
        lvs_params : Dict[str, Any]
            override LVS parameters.
        extra_svrf : Sequence[str], optional
            SVRF statements (from an LVS policy level) merged into the legacy
            runset's ``lvsSVRFCmds`` list, keeping its existing commands.

        Returns
        -------
        content : str
            the new runset content.
        """
        runset_content = read_file(self.lvs_runset)
        if re.search(r'^\s*lvs\.[A-Za-z0-9_.]+\s*=', runset_content,
                     flags=re.MULTILINE):
            if extra_svrf:
                raise ValueError(
                    'LVS policy svrf statements need a legacy (*key: value) runset; '
                    '{} is in the modern format.'.format(self.lvs_runset)
                )
            source_lib_name = source_lib_name or lib_name
            source_cell_name = source_cell_name or cell_name
            spice_file = os.path.join(run_dir, '%s.sp' % cell_name)
            replacements = {
                'lvs.runDir.value': run_dir,
                'lvs.layout.layoutFile.value': gds_file,
                'cmn.layout.defFiles.value': ['%s.def' % cell_name],
                'lvs.layout.topCellLibrary.value': lib_name,
                'cmn.layout.topCellLibraryFDI.value': lib_name,
                'lvs.layout.topCell.value': cell_name,
                'lvs.layout.topCellView.value': lay_view,
                'cmn.layout.topCellViewFDI.value': lay_view,
                'lvs.layout.layoutNetlist.value': spice_file,
                'lvs.source2.sourceFile.value': spice_file,
                'lvs.source2.topCellLibrary.value': lib_name,
                'lvs.source2.topCellLibraryFDI.value': lib_name,
                'lvs.source2.topCell.value': cell_name,
                'lvs.source2.topCellFDI.value': cell_name,
                'lvs.source.sourceFile.value': netlist,
                'lvs.source.topCellLibrary.value': source_lib_name,
                'lvs.source.topCellLibraryFDI.value': source_lib_name,
                'lvs.source.topCell.value': source_cell_name,
                'lvs.source.topCellFDI.value': source_cell_name,
                'lvs.reports.lvsReport.value': '%s.lvs.report' % cell_name,
                'lvs.ercdb.resultsFile.value': '%s.erc.results' % cell_name,
                'lvs.ercSummaryReport.report.value':
                    '%s.erc.summary' % cell_name,
            }

            legacy_to_modern = {
                'lvsRunDir': 'lvs.runDir.value',
                'lvsLayoutPaths': 'lvs.layout.layoutFile.value',
                'lvsLayoutPrimary': 'lvs.layout.topCell.value',
                'lvsLayoutLibrary': 'lvs.layout.topCellLibrary.value',
                'lvsLayoutView': 'lvs.layout.topCellView.value',
                'lvsSourcePath': 'lvs.source.sourceFile.value',
                'lvsSourcePrimary': 'lvs.source.topCell.value',
                'lvsSourceLibrary': 'lvs.source.topCellLibrary.value',
                'lvsSpiceFile': 'lvs.layout.layoutNetlist.value',
                'lvsERCDatabase': 'lvs.ercdb.resultsFile.value',
                'lvsERCSummaryFile': 'lvs.ercSummaryReport.report.value',
                'lvsReportFile': 'lvs.reports.lvsReport.value',
            }
            for key, value in lvs_params.items():
                replacements[legacy_to_modern.get(key, key)] = value

            for key, value in replacements.items():
                runset_content = _replace_calibre_runset_assignment(
                    runset_content, key, value
                )
            return runset_content

        # convert legacy runset content to dictionary
        lvs_options = {}
        for line in runset_content.splitlines():
            key, val = line.split(':', 1)
            key = key.strip('*')
            lvs_options[key] = val.strip()

        # override parameters
        lvs_options['lvsRunDir'] = run_dir
        lvs_options['lvsLayoutPaths'] = gds_file
        lvs_options['lvsLayoutPrimary'] = cell_name
        lvs_options['lvsLayoutLibrary'] = lib_name
        lvs_options['lvsLayoutView'] = lay_view
        lvs_options['lvsSourcePath'] = netlist
        lvs_options['lvsSourcePrimary'] = source_cell_name or cell_name
        lvs_options['lvsSourceLibrary'] = source_lib_name or lib_name
        lvs_options['lvsSpiceFile'] = os.path.join(run_dir, '%s.sp' % cell_name)
        lvs_options['lvsERCDatabase'] = '%s.erc.results' % cell_name
        lvs_options['lvsERCSummaryFile'] = '%s.erc.summary' % cell_name
        lvs_options['lvsReportFile'] = '%s.lvs.report' % cell_name
        lvs_options['lvsMaskDBFile'] = '%s.maskdb' % cell_name
        lvs_options['cmnFDILayoutLibrary'] = lib_name
        lvs_options['cmnFDILayoutView'] = lay_view
        lvs_options['cmnFDIDEFLayoutPath'] = '%s.def' % cell_name

        lvs_options.update(lvs_params)
        if extra_svrf:
            lvs_options['lvsSVRFCmds'] = merge_svrf_commands(
                lvs_options.get('lvsSVRFCmds'), extra_svrf
            )
            # Calibre Interactive reads lvsSVRFCmds only as SVRF text.
            lvs_options['lvsIncludeCmdsType'] = 'SVRF'

        return ''.join(('*%s: %s\n' % (key, val) for key, val in lvs_options.items()))

    def modify_pex_runset(self, run_dir, lib_name, cell_name, lay_view, gds_file, netlist,
                          rcx_params):
        # type: (str, str ,str, str, str, str, Dict[str, Any]) -> Tuple[str, str]
        """Modify the given RCX runset file.

        Parameters
        ----------
        run_dir : str
            the run directory.
        lib_name : str
            the library name.
        cell_name : str
            the cell name.
        lay_view : str
            the layout view.
        gds_file : str
            the layout gds file name.
        netlist : str
            the schematic netlist file.
        rcx_params : Dict[str, Any]
            override RCX parameters.

        Returns
        -------
        content : str
            the new runset content.
        output_name : str
            the extracted netlist file.
        """
        # convert runset content to dictionary
        rcx_options = {}
        for line in readlines_iter(self.rcx_runset):
            key, val = line.split(':', 1)
            key = key.strip('*')
            rcx_options[key] = val.strip()

        output_name = '%s.pex.netlist' % cell_name

        rules_file = rcx_options.get('pexRulesFile')
        if rules_file:
            # Same treatment as the DRC runset: tech runsets name the rule
            # deck as $BAG_TECH_CONFIG_DIR/... so no per-user absolute path
            # lands in git.  Expand before deciding whether the value is
            # relative to the RCX run dir; otherwise Calibre exits within
            # seconds with "Rules File ... is not a file".
            rules_file = os.path.expanduser(os.path.expandvars(rules_file))
            if not os.path.isabs(rules_file):
                rules_file = os.path.join(self.rcx_run_dir, rules_file)
            rcx_options['pexRulesFile'] = os.path.normpath(rules_file)

        # override parameters
        rcx_options['pexRunDir'] = run_dir
        rcx_options['pexLayoutPaths'] = gds_file
        rcx_options['pexLayoutPrimary'] = cell_name
        rcx_options['pexLayoutLibrary'] = lib_name
        rcx_options['pexLayoutView'] = lay_view
        rcx_options['pexSourcePath'] = netlist
        rcx_options['pexSourcePrimary'] = cell_name
        rcx_options['pexSourceLibrary'] = lib_name
        rcx_options['pexReportFile'] = '%s.lvs.report' % cell_name
        rcx_options['pexPexNetlistFile'] = output_name
        rcx_options['pexPexReportFile'] = '%s.pex.report' % cell_name
        rcx_options['pexMaskDBFile'] = '%s.maskdb' % cell_name
        rcx_options['cmnFDILayoutLibrary'] = lib_name
        rcx_options['cmnFDILayoutView'] = lay_view
        rcx_options['cmnFDIDEFLayoutPath'] = '%s.def' % cell_name

        rcx_options['pexPexNetlistType'] = rcx_params.pop('netlist_type', 'RCC')
        rcx_options['pexPexGroundNameValue'] = rcx_params.pop('ground_name_value', 'VSS')

        rcx_options.update(rcx_params)

        content = ''.join(('*%s: %s\n' % (key, val) for key, val in rcx_options.items()))
        return content, os.path.join(run_dir, output_name)

    def modify_xact_rules(self, run_dir, cell_name, gds_file, netlist, xact_params):
        # type: (str, str, str, str, Dict[str, Any]) -> Tuple[str, str]
        """Modify the given XACT runset file.

        Parameters
        ----------
        run_dir : str
            the run directory.
        cell_name : str
            the cell name.
        gds_file : str
            the layout gds file name.
        netlist : str
            the schematic netlist file.
        xact_params : Dict[str, Any]
            additional XACT parameters.

        Returns
        -------
        content : str
            the new runset content.
        output_name : str
            the extracted netlist file.
        """
        substrate_name = xact_params.get('substrate_name', 'VSS')
        power_names = xact_params.get('power_names', 'VDD')
        ground_names = xact_params.get('ground_names', 'VSS')

        output_name = '%s.pex.netlist' % cell_name
        content = self.render_string_template(read_file(self.xact_rules),
                                              dict(
                                                  cell_name=cell_name,
                                                  gds_file=gds_file,
                                                  netlist=netlist,
                                                  substrate_name=substrate_name,
                                                  power_names=power_names,
                                                  ground_names=ground_names,
                                                  output_name=output_name,
                                              ))

        return content, os.path.join(run_dir, output_name)

    def modify_starrc_cmd(self, run_dir, lib_name, cell_name, starrc_params, query_input, sch_file):
        # type: (str, str, str, Dict[str, Any], str, str) -> Tuple[str, str]
        """Modify the cmd file.

        Parameters
        ----------
        run_dir : str
            the run directory.
        lib_name : str
            the library name.
        cell_name : str
            the cell name.
        starrc_params : Dict[str, Any]
            override StarRC parameters.
        query_input : str
            the path to query.input file
        sch_file : str
            the schematic netlist

        Returns
        -------
        starrc_cmd : str
            the new StarXtract cmd file.
        output_name : str
            the extracted netlist file.
        """
        output_name = '%s.spf' % cell_name
        if 'CDSLIBPATH' in os.environ:
            cds_lib_path = os.path.abspath(os.path.join(os.environ['CDSLIBPATH'], 'cds.lib'))
        else:
            cds_lib_path = os.path.abspath('./cds.lib')
        content = self.render_string_template(read_file(self.rcx_runset),
                                              dict(
                                                  cell_name=cell_name,
                                                  query_input=query_input,
                                                  extract_type=starrc_params['extract'].get('type', 'RCc'),
                                                  netlist_format=starrc_params.get('netlist_format',
                                                                                   'SPF'),
                                                  sch_file=sch_file,
                                                  cds_lib=cds_lib_path,
                                                  lib_name=lib_name,
                                                  run_dir=run_dir,
                                                  skew=starrc_params.get('skew', 'tt'),
                                              ))

        return content, os.path.join(run_dir, output_name)

    def modify_qrc_cmd(self, run_dir, cell_name, qrc_params, sch_file):
        # type: (str, str, Dict[str, Any], str) -> Tuple[str, str]
        """Modify the cmd file.

        Parameters
        ----------
        run_dir : str
            the run directory.
        cell_name : str
            the cell name.
        qrc_params : Dict[str, Any]
            override QRC parameters.
        sch_file : str
            the schematic netlist

        Returns
        -------
        qrc_cmd : str
            the new QRC cmd file.
        output_name : str
            the extracted netlist file.
        """
        output_name = '%s.spf' % cell_name
        if 'CDSLIBPATH' in os.environ:
            cds_lib_path = os.path.abspath(os.path.join(os.environ['CDSLIBPATH'], 'cds.lib'))
        else:
            cds_lib_path = os.path.abspath('./cds.lib')
        content = self.render_string_template(read_file(self.rcx_runset),
                                              dict(
                                                  cell_name=cell_name,
                                                  netlist_format=qrc_params.get('netlist_format',
                                                                                'spf'),
                                                  extract_type=qrc_params['extract'].get('type', 'rc_coupled'),
                                                  sch_file=sch_file,
                                                  cds_lib=cds_lib_path,
                                                  skew=qrc_params.get('skew', 'tt'),
                                                  temp=qrc_params.get('temp', '25'),
                                              ))

        return content, os.path.join(run_dir, output_name)
