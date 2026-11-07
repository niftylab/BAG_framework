import os
import json
from pathlib import Path

import pytest

from bag.verification.calibre import Calibre, drc_passed
from bag.verification.icv import ICV
from bag.verification.pvs import PVS


def _make_checker(tmp_path):
    runset_dir = tmp_path / 'config'
    runset_dir.mkdir()
    runset = runset_dir / 'drc.cell.runset'
    runset.write_text(
        '*drcRulesFile: ../Calibre/drc/calibre.drc.cell\n'
        '*drcRunDir: ./rundir_drc\n'
        '*drcLayoutPaths: old.gds\n'
        '*drcLayoutPrimary: old_cell\n'
        '*drcLayoutLibrary: old_lib\n'
        '*drcLayoutView: layout\n'
        '*drcResultsFile: old.drc.results\n'
        '*drcSummaryFile: old.drc.summary\n',
        encoding='utf-8',
    )
    checker = object.__new__(Calibre)
    checker.drc_run_dir = str(tmp_path / 'workspace' / 'rundir_drc')
    checker.drc_runset = str(runset)
    checker.drc_policy_file = None
    checker.drc_policy_profile = None
    checker.setup_export_layout = lambda *args: (
        ['export-layout'], str(tmp_path / 'layout.log'), None, None
    )
    return checker


def test_setup_drc_flow_exports_layout_and_generates_cell_runset(tmp_path):
    checker = _make_checker(tmp_path)

    flow = checker.setup_drc_flow('logic_generated', 'inv_lvs_2x')

    assert len(flow) == 2
    assert flow[0][0][0] == 'export-layout'
    assert flow[1][0][:3] == ['calibre', '-gui', '-drc']
    assert flow[1][0][-1] == '-batch'

    run_dir = (
        Path(checker.drc_run_dir) / 'logic_generated' / 'inv_lvs_2x'
    )
    assert Path(flow[1][3]) == run_dir

    generated_runset = Path(flow[1][0][4])
    content = generated_runset.read_text(encoding='utf-8')
    assert '*drcRunDir: {}'.format(run_dir) in content
    assert '*drcLayoutPaths: {}'.format(run_dir / 'layout.gds') in content
    assert '*drcLayoutPrimary: inv_lvs_2x' in content
    assert '*drcLayoutLibrary: logic_generated' in content
    assert '*drcResultsFile: inv_lvs_2x.drc.results' in content
    assert '*drcSummaryFile: inv_lvs_2x.drc.summary' in content
    assert (
        '*drcRulesFile: {}'.format(
            Path(checker.drc_run_dir).parent
            / 'Calibre' / 'drc' / 'calibre.drc.cell'
        )
        in content
    )


@pytest.mark.parametrize(
    'summary,total_passed',
    [
        ('TOTAL DRC Results Generated: 0\n', True),
        ('TOTAL RESULTS GENERATED = 4\n', False),
    ],
)
def test_drc_passed_parses_violation_count(tmp_path, summary, total_passed):
    log_path = tmp_path / 'calibre.log'
    log_path.write_text('calibre output\n', encoding='utf-8')
    summary_path = tmp_path / 'cell.drc.summary'
    summary_path.write_text(summary, encoding='utf-8')

    assert drc_passed(0, str(log_path), str(summary_path)) == (
        total_passed,
        str(log_path),
    )


def test_drc_passed_keeps_execution_log_on_tool_or_summary_failure(tmp_path):
    log_path = tmp_path / 'calibre.log'
    log_path.write_text('calibre output\n', encoding='utf-8')
    summary_path = tmp_path / 'cell.drc.summary'
    summary_path.write_text(
        'TOTAL DRC Results Generated: 0\n',
        encoding='utf-8',
    )

    assert drc_passed(2, str(log_path), str(summary_path)) == (
        False,
        str(log_path),
    )
    assert drc_passed(
        0, str(log_path), str(tmp_path / 'missing.summary')
    ) == (False, str(log_path))

    summary_path.write_text('unrecognized summary\n', encoding='utf-8')
    assert drc_passed(0, str(log_path), str(summary_path)) == (
        False,
        str(log_path),
    )


def test_drc_policy_waives_known_rules_and_writes_audit_result(tmp_path):
    log_path = tmp_path / 'calibre.log'
    log_path.write_text('calibre output\n', encoding='utf-8')
    summary_path = tmp_path / 'cell.drc.summary'
    summary_path.write_text(
        'RULECHECK PP.W.1 ........ TOTAL Result Count = 0 (0)\n'
        'RULECHECK PP.W.1 ........ TOTAL Result Count = 2 (2)\n'
        'RULECHECK LDN.EX.2 ...... TOTAL Result Count = 3 (3)\n'
        'TOTAL DRC Results Generated: 5 (5)\n',
        encoding='utf-8',
    )
    policy_path = tmp_path / 'drc_policy.yaml'
    policy_path.write_text(
        'version: 1\n'
        'profiles:\n'
        '  bag_cell:\n'
        '    scope:\n'
        '      runsets: [drc.cell.runset]\n'
        '      libraries: ["*_generated"]\n'
        '    waive:\n'
        '      - rule: PP.W.1\n'
        '        reason: resolved by parent-level well merge\n'
        '      - rule: LDN.EX.2\n'
        '        reason: resolved by parent-level boundary\n',
        encoding='utf-8',
    )

    assert drc_passed(
        0,
        str(log_path),
        str(summary_path),
        policy_file=str(policy_path),
        policy_profile='bag_cell',
        runset_file='/pdk/drc.cell.runset',
        lib_name='logic_generated',
        cell_name='inv_2x',
        run_dir=str(tmp_path),
    ) == (True, str(log_path))

    result = json.loads(
        (tmp_path / 'drc_policy_result.json').read_text(encoding='utf-8')
    )
    assert result['raw_violation_count'] == 5
    assert result['waived_violation_count'] == 5
    assert result['remaining_violation_count'] == 0
    assert result['raw_passed'] is False
    assert result['policy_passed'] is True
    assert result['waived_rules'] == {'LDN.EX.2': 3, 'PP.W.1': 2}


def test_drc_policy_ignores_hierarchical_by_cell_rule_counts(tmp_path):
    log_path = tmp_path / 'calibre.log'
    log_path.write_text('calibre output\n', encoding='utf-8')
    summary_path = tmp_path / 'cell.drc.summary'
    summary_path.write_text(
        '--- RULECHECK RESULTS STATISTICS\n'
        'RULECHECK LUP.6 ......... TOTAL Result Count = 810 (810)\n'
        'RULECHECK M2.S.7 ........ TOTAL Result Count = 4 (4)\n'
        '--- RULECHECK RESULTS STATISTICS (BY CELL)\n'
        '    RULECHECK LUP.6 ..... TOTAL Result Count = 234 (234)\n'
        '    RULECHECK M2.S.7 .... TOTAL Result Count = 2 (2)\n'
        'TOTAL DRC Results Generated: 814 (814)\n',
        encoding='utf-8',
    )
    policy_path = tmp_path / 'drc_policy.yaml'
    policy_path.write_text(
        'version: 1\n'
        'profiles:\n'
        '  bag_cell:\n'
        '    scope:\n'
        '      runsets: [drc.cell.runset]\n'
        '      libraries: ["*_generated"]\n'
        '    waive:\n'
        '      - rule: LUP.6\n'
        '        reason: resolved by parent-level integration\n',
        encoding='utf-8',
    )

    assert drc_passed(
        0,
        str(log_path),
        str(summary_path),
        policy_file=str(policy_path),
        policy_profile='bag_cell',
        runset_file='/pdk/drc.cell.runset',
        lib_name='serializer_generated',
        cell_name='shser_32to4_2x',
        run_dir=str(tmp_path),
    ) == (False, str(log_path))

    result = json.loads(
        (tmp_path / 'drc_policy_result.json').read_text(encoding='utf-8')
    )
    assert result['rule_counts'] == {'LUP.6': 810, 'M2.S.7': 4}
    assert result['waived_rules'] == {'LUP.6': 810}
    assert result['remaining_rules'] == {'M2.S.7': 4}
    assert result['waived_violation_count'] == 810
    assert result['remaining_violation_count'] == 4


def test_drc_policy_fails_on_unknown_or_out_of_scope_rules(tmp_path):
    log_path = tmp_path / 'calibre.log'
    log_path.write_text('calibre output\n', encoding='utf-8')
    summary_path = tmp_path / 'cell.drc.summary'
    summary_path.write_text(
        'RULECHECK PP.W.1 ........ TOTAL Result Count = 2\n'
        'RULECHECK NEW.RULE ...... TOTAL Result Count = 1\n'
        'TOTAL DRC Results Generated: 3\n',
        encoding='utf-8',
    )
    policy_path = tmp_path / 'drc_policy.yaml'
    policy_path.write_text(
        'version: 1\n'
        'profiles:\n'
        '  bag_cell:\n'
        '    scope:\n'
        '      runsets: [drc.cell.runset]\n'
        '      libraries: ["*_generated"]\n'
        '    waive:\n'
        '      - rule: PP.W.1\n'
        '        reason: parent-level well merge\n',
        encoding='utf-8',
    )

    result = drc_passed(
        0,
        str(log_path),
        str(summary_path),
        policy_file=str(policy_path),
        policy_profile='bag_cell',
        runset_file='/pdk/drc.cell.runset',
        lib_name='logic_generated',
        cell_name='inv_2x',
        run_dir=str(tmp_path),
    )
    assert result == (False, str(log_path))
    audit = json.loads(
        (tmp_path / 'drc_policy_result.json').read_text(encoding='utf-8')
    )
    assert audit['waived_rules'] == {'PP.W.1': 2}
    assert audit['remaining_rules'] == {'NEW.RULE': 1}
    assert audit['remaining_violation_count'] == 1

    assert drc_passed(
        0,
        str(log_path),
        str(summary_path),
        policy_file=str(policy_path),
        policy_profile='bag_cell',
        runset_file='/pdk/drc.cell.runset',
        lib_name='third_party_lib',
        cell_name='inv_2x',
        run_dir=str(tmp_path),
    ) == (False, str(log_path))


def test_drc_policy_configuration_error_fails_closed(tmp_path):
    log_path = tmp_path / 'calibre.log'
    log_path.write_text('calibre output\n', encoding='utf-8')
    summary_path = tmp_path / 'cell.drc.summary'
    summary_path.write_text(
        'TOTAL DRC Results Generated: 0\n',
        encoding='utf-8',
    )

    assert drc_passed(
        0,
        str(log_path),
        str(summary_path),
        policy_file=str(tmp_path / 'missing.yaml'),
        policy_profile='bag_cell',
        run_dir=str(tmp_path),
    ) == (False, str(log_path))
    result = json.loads(
        (tmp_path / 'drc_policy_result.json').read_text(encoding='utf-8')
    )
    assert result['policy_passed'] is False
    assert 'error' in result


@pytest.mark.parametrize('checker_cls', [PVS, ICV])
def test_non_calibre_backends_explicitly_reject_drc(checker_cls):
    checker = object.__new__(checker_cls)

    with pytest.raises(NotImplementedError, match=checker_cls.__name__):
        checker.setup_drc_flow('logic_generated', 'inv_lvs_2x')


def test_drc_runset_expands_environment_variables_in_rules_path(
        tmp_path, monkeypatch):
    # tsmcN28 797480f moved drcRulesFile to $BAG_WORK_DIR/Calibre/... so the
    # tech repo stops carrying per-user absolute paths. Treating that value as
    # relative joins it under drc_run_dir and Calibre never finds the rules.
    work_dir = tmp_path / 'workspace'
    monkeypatch.setenv('BAG_WORK_DIR', str(work_dir))
    checker = _make_checker(tmp_path)
    Path(checker.drc_runset).write_text(
        '*drcRulesFile: $BAG_WORK_DIR/Calibre/drc/calibre.drc.cell\n',
        encoding='utf-8',
    )

    run_dir = tmp_path / 'run'
    content = checker.modify_drc_runset(
        str(run_dir), 'logic_generated', 'inv_lvs_2x',
        str(run_dir / 'layout.gds'),
    )

    expected = os.path.normpath(
        str(work_dir / 'Calibre' / 'drc' / 'calibre.drc.cell')
    )
    assert '*drcRulesFile: {}\n'.format(expected) in content
    assert '$BAG_WORK_DIR' not in content
    assert 'rundir_drc' not in content.split('*drcRulesFile:')[1].split('\n')[0]


def test_drc_runset_expands_home_in_rules_path(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    checker = _make_checker(tmp_path)
    Path(checker.drc_runset).write_text(
        '*drcRulesFile: ~/Calibre/drc/calibre.drc.cell\n', encoding='utf-8',
    )

    content = checker.modify_drc_runset(
        str(tmp_path / 'run'), 'logic_generated', 'inv_lvs_2x',
        str(tmp_path / 'run' / 'layout.gds'),
    )

    expected = os.path.normpath(str(home / 'Calibre' / 'drc' / 'calibre.drc.cell'))
    assert '*drcRulesFile: {}\n'.format(expected) in content


# --------------------------------------------------------------------------
# Hierarchy levels and boundary-scoped waivers (policy version 2)


def _gds_real8(value):
    """Encode a float as a GDSII 8-byte real."""
    if value == 0:
        return b'\x00' * 8
    sign = 0x80 if value < 0 else 0
    value = abs(value)
    exponent = 0
    while value >= 1.0:
        value /= 16.0
        exponent += 1
    while value < 1.0 / 16.0:
        value *= 16.0
        exponent -= 1
    mantissa = int(round(value * (1 << 56)))
    return bytes([sign | (exponent + 64)]) + mantissa.to_bytes(7, 'big')


def _rec(rtype, dtype, body=b''):
    import struct
    return struct.pack('>HBB', 4 + len(body), rtype, dtype) + body


def _xy(points):
    import struct
    return b''.join(struct.pack('>ii', x, y) for x, y in points)


def _write_gds(path, cells):
    """cells: {name: {'boxes': [(x0,y0,x1,y1)], 'refs': [(name, x, y, reflect, angle)]}} in nm."""
    import struct
    out = [_rec(0x00, 0x02, struct.pack('>h', 600)),
           _rec(0x01, 0x02, b'\x00' * 24),
           _rec(0x02, 0x06, b'LIB\x00'),
           _rec(0x03, 0x05, _gds_real8(0.001) + _gds_real8(1e-9))]
    for name, body in cells.items():
        out.append(_rec(0x05, 0x02, b'\x00' * 24))
        out.append(_rec(0x06, 0x06, name.encode() + (b'\x00' if len(name) % 2 else b'')))
        for x0, y0, x1, y1 in body.get('boxes', []):
            out += [_rec(0x08, 0x00), _rec(0x0D, 0x02, struct.pack('>h', 31)),
                    _rec(0x0E, 0x02, struct.pack('>h', 0)),
                    _rec(0x10, 0x03, _xy([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])),
                    _rec(0x11, 0x00)]
        for ref, x, y, reflect, angle in body.get('refs', []):
            out += [_rec(0x0A, 0x00), _rec(0x12, 0x06, ref.encode() + (b'\x00' if len(ref) % 2 else b''))]
            if reflect or angle:
                out.append(_rec(0x1A, 0x01, struct.pack('>H', 0x8000 if reflect else 0)))
            if angle:
                out.append(_rec(0x1C, 0x05, _gds_real8(float(angle))))
            out += [_rec(0x10, 0x03, _xy([(x, y)])), _rec(0x11, 0x00)]
        out.append(_rec(0x07, 0x00))
    out.append(_rec(0x04, 0x00))
    Path(path).write_bytes(b''.join(out))


def test_gds_cell_extent_follows_placements(tmp_path):
    from bag.verification.gds_extent import gds_cell_extent
    gds = tmp_path / 'layout.gds'
    _write_gds(gds, {
        'leaf': {'boxes': [(0, 0, 100, 300)]},
        'top': {'boxes': [(0, 0, 1000, 1000)],
                # rotated 90 deg at (1000, 0): the leaf spans x in [700, 1000], y in [0, 100];
                # reflected at (-200, 0): x in [-200, -100], y in [-300, 0]
                'refs': [('leaf', 1000, 0, False, 90), ('leaf', -200, 0, True, 0)]},
    })
    assert gds_cell_extent(str(gds), 'leaf') == (0.0, 0.0, 0.1, 0.3)
    assert gds_cell_extent(str(gds), 'top') == (-0.2, -0.3, 1.0, 1.0)
    with pytest.raises(KeyError):
        gds_cell_extent(str(gds), 'missing')


POLICY_V2 = (
    'version: 2\n'
    'levels:\n'
    '  cell: bag_cell\n'
    '  block: bag_block\n'
    'profiles:\n'
    '  bag_cell:\n'
    '    scope:\n'
    '      runsets: [drc.cell.runset]\n'
    '      libraries: ["*_generated"]\n'
    '    boundary_margin_nm: 50\n'
    '    waive:\n'
    '      - rule: PP.W.1\n'
    '        reason: implant cut at the cell edge, completed by the parent\n'
    '        where: boundary\n'
    '      - rule: LDN.EX.2\n'
    '        reason: completed at the parent level\n'
    '  bag_block:\n'
    '    scope:\n'
    '      runsets: [drc.cell.runset]\n'
    '      libraries: ["*_generated"]\n'
    '    waive:\n'
    '      - rule: LDN.EX.2\n'
    '        reason: completed at chip integration\n'
)

SUMMARY_V2 = (
    'RULECHECK PP.W.1 ........ TOTAL Result Count = 3 (3)\n'
    'RULECHECK LDN.EX.2 ...... TOTAL Result Count = 1 (1)\n'
    'TOTAL DRC Results Generated: 4 (4)\n'
)

# Cell extent is (0, 0, 1, 1) um. Two PP.W.1 markers sit on the edge, one inside.
RESULTS_V2 = (
    'inv_2x 1000\n'
    'PP.W.1\n'
    '3 3 3 Nov 8 00:00:00 2026\n'
    'PP.W.1 { @ implant width\n'
    'p 1 4\n'
    '0 400\n'
    '60 400\n'
    '60 600\n'
    '0 600\n'
    'p 2 4\n'
    '400 950\n'
    '600 950\n'
    '600 1000\n'
    '400 1000\n'
    'p 3 4\n'
    '400 400\n'
    '460 400\n'
    '460 600\n'
    '400 600\n'
    'LDN.EX.2\n'
    '1 1 1 Nov 8 00:00:00 2026\n'
    'p 1 4\n'
    '500 500\n'
    '510 500\n'
    '510 510\n'
    '500 510\n'
)


def _v2_rundir(tmp_path, policy=POLICY_V2, with_gds=True, with_results=True):
    (tmp_path / 'calibre.log').write_text('calibre output\n', encoding='utf-8')
    (tmp_path / 'inv_2x.drc.summary').write_text(SUMMARY_V2, encoding='utf-8')
    results = tmp_path / 'inv_2x.drc.results'
    if with_results:
        results.write_text(RESULTS_V2, encoding='utf-8')
    elif results.exists():
        results.unlink()
    gds = tmp_path / 'layout.gds'
    if with_gds:
        _write_gds(gds, {'inv_2x': {'boxes': [(0, 0, 1000, 1000)]}})
    elif gds.exists():
        gds.unlink()
    (tmp_path / 'drc_policy.yaml').write_text(policy, encoding='utf-8')


def _judge(tmp_path, **kwargs):
    args = dict(policy_file=str(tmp_path / 'drc_policy.yaml'), runset_file='/pdk/drc.cell.runset',
                lib_name='logic_generated', cell_name='inv_2x', run_dir=str(tmp_path))
    args.update(kwargs)
    passed, _ = drc_passed(0, str(tmp_path / 'calibre.log'), str(tmp_path / 'inv_2x.drc.summary'), **args)
    return passed, json.loads((tmp_path / 'drc_policy_result.json').read_text(encoding='utf-8'))


def test_boundary_waiver_keeps_interior_markers(tmp_path):
    _v2_rundir(tmp_path)
    passed, result = _judge(tmp_path, policy_level='cell')
    assert passed is False
    assert result['policy_profile'] == 'bag_cell'
    assert result['extent'] == [0.0, 0.0, 1.0, 1.0] and result['extent_source'] == 'all_layers'
    entry = result['boundary_waivers']['PP.W.1']
    # Two markers on the edge (depth 0), one 0.4 um inside.
    assert (entry['waived_depth_max_um'], entry['remaining_depth_min_um']) == (0.0, 0.4)
    assert result['waived_rules'] == {'PP.W.1': 2, 'LDN.EX.2': 1}
    assert result['remaining_rules'] == {'PP.W.1': 1}
    assert result['remaining_violation_count'] == 1
    assert result['remaining_markers'] == {'PP.W.1': [[0.4, 0.4, 0.46, 0.6]]}
    entry = result['boundary_waivers']['PP.W.1']
    assert (entry['scope'], entry['summary_count'], entry['marker_count']) == ('boundary', 3, 3)
    assert (entry['waived'], entry['remaining'], entry['margin_um']) == (2, 1, 0.05)


def test_boundary_waiver_fails_closed_without_extent_or_markers(tmp_path):
    _v2_rundir(tmp_path, with_gds=False)
    passed, result = _judge(tmp_path, policy_level='cell')
    assert passed is False
    assert result['remaining_rules'] == {'PP.W.1': 3}
    assert result['boundary_waivers']['PP.W.1']['scope'] == 'extent_unavailable'

    _v2_rundir(tmp_path, with_results=False)
    passed, result = _judge(tmp_path, policy_level='cell')
    assert result['remaining_rules'] == {'PP.W.1': 3}
    assert result['boundary_waivers']['PP.W.1']['scope'] == 'no_markers'


def test_level_selects_the_profile_owner_side(tmp_path):
    _v2_rundir(tmp_path)
    passed, result = _judge(tmp_path, policy_level='block')
    assert result['policy_profile'] == 'bag_block'
    # bag_block does not waive the implant rule at all, edge markers included.
    assert result['remaining_rules'] == {'PP.W.1': 3}
    assert result['waived_rules'] == {'LDN.EX.2': 1}

    passed, result = _judge(tmp_path, policy_level='chip')
    assert passed is False and 'level not found' in result['error']

    # A profile given together with a level must agree with the map.
    passed, result = _judge(tmp_path, policy_level='block', policy_profile='bag_cell')
    assert passed is False and 'maps to' in result['error']


def test_version_1_policy_rejects_boundary_scope_and_levels(tmp_path):
    _v2_rundir(tmp_path, policy=POLICY_V2.replace('version: 2', 'version: 1'))
    passed, result = _judge(tmp_path, policy_profile='bag_cell')
    assert passed is False and 'version 2' in result['error']
    passed, result = _judge(tmp_path, policy_level='block', policy_profile='bag_cell')
    assert passed is False and 'version 2' in result['error']
    # Level "cell" is what a version 1 file means by its only profile: the
    # profile stands, and the policy file's where: lines are still refused.
    passed, result = _judge(tmp_path, policy_level='cell', policy_profile='bag_cell')
    assert result['policy_profile'] == 'bag_cell' and 'version 2' in result['error']


def _write_gds_layers(path, cells):
    """Like _write_gds but boxes carry their own (layer, dtype): {name: [(layer, dtype, (x0,y0,x1,y1))]}."""
    import struct
    out = [_rec(0x00, 0x02, struct.pack('>h', 600)),
           _rec(0x01, 0x02, b'\x00' * 24),
           _rec(0x02, 0x06, b'LIB\x00'),
           _rec(0x03, 0x05, _gds_real8(0.001) + _gds_real8(1e-9))]
    for name, boxes in cells.items():
        out.append(_rec(0x05, 0x02, b'\x00' * 24))
        out.append(_rec(0x06, 0x06, name.encode() + (b'\x00' if len(name) % 2 else b'')))
        for layer, dtype, (x0, y0, x1, y1) in boxes:
            out += [_rec(0x08, 0x00), _rec(0x0D, 0x02, struct.pack('>h', layer)),
                    _rec(0x0E, 0x02, struct.pack('>h', dtype)),
                    _rec(0x10, 0x03, _xy([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])),
                    _rec(0x11, 0x00)]
        out.append(_rec(0x07, 0x00))
    out.append(_rec(0x04, 0x00))
    Path(path).write_bytes(b''.join(out))


def test_extent_layer_uses_the_placement_box_and_reports_per_layer_extents(tmp_path):
    from bag.verification.gds_extent import gds_cell_extent, gds_layer_extents
    gds = tmp_path / 'layout.gds'
    # prBoundary (108/0) is the 1x1 um box; the implant overhangs it by 65 nm.
    _write_gds_layers(gds, {'inv_2x': [(108, 0, (0, 0, 1000, 1000)), (25, 0, (-65, -30, 1065, 1000))]})
    assert gds_cell_extent(str(gds), 'inv_2x') == (-0.065, -0.03, 1.065, 1.0)
    assert gds_cell_extent(str(gds), 'inv_2x', layer='108/0') == (0.0, 0.0, 1.0, 1.0)
    assert gds_layer_extents(str(gds), 'inv_2x') == {'108/0': (0.0, 0.0, 1.0, 1.0),
                                                     '25/0': (-0.065, -0.03, 1.065, 1.0)}
    with pytest.raises(ValueError):
        gds_cell_extent(str(gds), 'inv_2x', layer='3/0')

    # The policy names the layer; the marker 0.4 um inside the box stays a violation and
    # the two edge markers are waived against the prBoundary, not the implant overhang.
    _v2_rundir(tmp_path, policy=POLICY_V2.replace('boundary_margin_nm: 50\n',
                                                  'boundary_margin_nm: 50\n    extent_layer: "108/0"\n'))
    _write_gds_layers(gds, {'inv_2x': [(108, 0, (0, 0, 1000, 1000)), (25, 0, (-65, -30, 1065, 1000))]})
    passed, result = _judge(tmp_path, policy_level='cell')
    assert result['extent_source'] == 'layer 108/0' and result['extent'] == [0.0, 0.0, 1.0, 1.0]
    assert result['remaining_rules'] == {'PP.W.1': 1}

    # A cell drawn without templates has no prBoundary: fall back to every shape, and say so.
    _write_gds_layers(gds, {'inv_2x': [(25, 0, (0, 0, 1000, 1000))]})
    passed, result = _judge(tmp_path, policy_level='cell')
    assert result['extent_source'] == 'all_layers' and 'extent_layer_error' in result
