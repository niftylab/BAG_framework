# -*- coding: utf-8 -*-

"""This module implements LVS/RCX using Calibre and stream out from Virtuoso.
"""

from typing import TYPE_CHECKING, Optional, List, Tuple, Dict, Any, Sequence

import fnmatch
import json
import os
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


# noinspection PyUnusedLocal
def _drc_rule_counts(content):
    # type: (str) -> Dict[str, int]
    """Return the final Calibre result count for each RULECHECK."""
    counts = {}
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


def _load_drc_waivers(policy_file, policy_profile, runset_file, lib_name):
    # type: (str, str, str, str) -> Tuple[bool, Dict[str, str]]
    policy = read_yaml(policy_file)
    if not isinstance(policy, dict):
        raise ValueError('DRC policy root must be a mapping.')
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

    waivers = {}
    for entry in profile.get('waive', []):
        if not isinstance(entry, dict) or not entry.get('rule'):
            raise ValueError('Each DRC waiver requires a rule.')
        reason = entry.get('reason')
        if not reason:
            raise ValueError(
                'DRC waiver {} requires a reason.'.format(entry['rule'])
            )
        waivers[str(entry['rule'])] = str(reason)
    return True, waivers


# noinspection PyUnusedLocal
def drc_passed(retcode, log_file, summary_file, policy_file=None,
               policy_profile=None, runset_file=None, lib_name=None,
               cell_name=None, run_dir=None):
    # type: (int, str, str, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]) -> Tuple[bool, str]
    """Check Calibre DRC and optionally apply an auditable rule waiver policy."""
    audit_dir = run_dir or os.path.dirname(summary_file)
    audit_file = os.path.join(audit_dir, 'drc_policy_result.json')
    result = {
        'schema_version': 1,
        'library': lib_name,
        'cell': cell_name,
        'runset': runset_file,
        'summary_file': summary_file,
        'execution_log': log_file,
        'policy_file': policy_file,
        'policy_profile': policy_profile,
        'raw_passed': False,
        'policy_passed': False,
        'raw_violation_count': None,
        'waived_violation_count': 0,
        'remaining_violation_count': None,
        'rule_counts': {},
        'waived_rules': {},
        'remaining_rules': {},
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

        if bool(policy_file) != bool(policy_profile):
            raise ValueError(
                'Both drc_policy_file and drc_policy_profile are required.'
            )

        waivers = {}
        in_scope = False
        if policy_file:
            in_scope, waivers = _load_drc_waivers(
                policy_file, policy_profile, runset_file, lib_name
            )
        result['policy_in_scope'] = in_scope

        waived_rules = {
            rule: count
            for rule, count in rule_counts.items()
            if rule in waivers
        }
        remaining_rules = {
            rule: count
            for rule, count in rule_counts.items()
            if rule not in waivers
        }
        waived_count = min(raw_count, sum(waived_rules.values()))
        remaining_count = raw_count - waived_count
        result['waived_rules'] = waived_rules
        result['waiver_reasons'] = {
            rule: waivers[rule] for rule in waived_rules
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
                 drc_policy_file=None, drc_policy_profile=None, **kwargs):

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

        with open_temp(prefix='lvsLog', dir=run_dir, delete=False) as logf:
            log_file = logf.name

        # generate new runset
        runset_content = self.modify_lvs_runset(
            run_dir, lib_name, cell_name, lay_view, lay_file,
            sch_file, lvs_params_actual,
            source_lib_name=source_lib_name,
            source_cell_name=source_cell_name,
        )

        # save runset
        with open_temp(dir=run_dir, delete=False) as runset_file:
            runset_fname = runset_file.name
            runset_file.write(runset_content)

        cmd = ['calibre', '-gui', '-lvs', '-runset', runset_fname, '-batch']

        flow_list.append((cmd, log_file, None, run_dir, lvs_passed))

        return flow_list

    def setup_rcx_flow(self, lib_name, cell_name, sch_view='schematic', lay_view='layout',
                       params=None, **kwargs):
        # type: (str, str, str, str, Optional[Dict[str, Any]], Any) -> Sequence[FlowInfo]

        # update default RCX parameters.
        rcx_params_actual = self.default_rcx_params.copy()
        if params is not None:
            rcx_params_actual.update(params)

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

        # Check if gds layout is provided
        gds_layout_path = kwargs.pop('gds_layout_path', None)

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
        if rules_file and not os.path.isabs(rules_file):
            drc_options['drcRulesFile'] = os.path.normpath(
                os.path.join(self.drc_run_dir, rules_file)
            )

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
                          source_cell_name=None):
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

        Returns
        -------
        content : str
            the new runset content.
        """
        # convert runset content to dictionary
        lvs_options = {}
        for line in readlines_iter(self.lvs_runset):
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
