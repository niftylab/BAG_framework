import json
from pathlib import Path

import pytest

from bag.verification import calibre
from bag.verification.calibre import Calibre, LVS_LEVEL_ENV, resolve_lvs_policy

POLICY = """\
version: 1
levels:
  cell: bag_cell
  block: bag_block
profiles:
  bag_cell:
    description: Parent joins split colon rails and supplies taps.
    runset:
      cmnVConnectColon: 1
    svrf:
      - LVS IGNORE DEVICE PIN MN B
      - LVS IGNORE DEVICE PIN MP B
  bag_block:
    description: Rails must close in metal inside the block.
    runset:
      cmnVConnectColon: 1
    fail_on_virtual_connect: true
"""

EXT_SPLIT = """\
Extraction Errors and Warnings for cell "qmux_cml"
--------------------------------------------------

NOTE:  Virtually connected "VDD" at (13.91,1.2) on layer 132 and (13.91,3.6) on layer 132
NOTE:  Virtually connected "VSS" at (13.91,0) on layer 132 and (13.91,2.4) on layer 132

WARNING: Invalid PATHCHK request "! POWER": no POWER nets present, operation aborted.
"""

EXT_CLEAN = """\
Extraction Errors and Warnings for cell "nmos4_fast_tap"
--------------------------------------------------------
"""


def _make_checker(tmp_path, policy=POLICY):
    tmp_path.mkdir(parents=True, exist_ok=True)
    runset = tmp_path / 'lvs.runset'
    runset.write_text(
        '*lvsLayoutPrimary: old_layout\n'
        '*lvsSourcePrimary: old_source\n'
        '*cmnVConnectColon: 1\n'
        '*lvsIncludeCmdsType: SVRF\n'
        '*lvsSVRFCmds: {SOURCE CASE YES} {LAYOUT CASE YES} {} {}\n',
        encoding='utf-8',
    )
    checker = object.__new__(Calibre)
    checker.lvs_run_dir = str(tmp_path / 'runs')
    checker.lvs_runset = str(runset)
    checker.default_lvs_params = {}
    checker.lvs_policy_file = None
    if policy is not None:
        policy_file = tmp_path / 'lvs_policy.yaml'
        policy_file.write_text(policy, encoding='utf-8')
        checker.lvs_policy_file = str(policy_file)
    checker.setup_export_layout = lambda *args: (
        ['export-layout'], str(tmp_path / 'layout.log'), None, None
    )
    checker.setup_export_schematic = lambda *args: (
        ['export-schematic'], str(tmp_path / 'schematic.log'), None, None
    )
    return checker


def _runset(flow):
    return Path(flow[-1][0][4]).read_text(encoding='utf-8')


@pytest.fixture(autouse=True)
def _no_level_env(monkeypatch):
    monkeypatch.delenv(LVS_LEVEL_ENV, raising=False)


def test_no_level_leaves_runset_and_verdict_unchanged(tmp_path):
    with_policy = _make_checker(tmp_path / 'a')
    without_policy = _make_checker(tmp_path / 'b', policy=None)
    flow_a = with_policy.setup_lvs_flow('lib', 'top')
    flow_b = without_policy.setup_lvs_flow('lib', 'top')
    strip = lambda text, root: text.replace(str(root), '<root>')
    assert strip(_runset(flow_a), tmp_path / 'a') == strip(_runset(flow_b), tmp_path / 'b')
    assert flow_a[-1][4] is calibre.lvs_passed


def test_block_level_keeps_colon_names_and_asks_for_the_report(tmp_path):
    checker = _make_checker(tmp_path)
    content = _runset(checker.setup_lvs_flow('lib', 'top', lvs_level='block'))
    # Colon names stay joined so VDD: matches VDD; the join itself is judged.
    assert '*cmnVConnectColon: 1\n' in content
    assert ('*lvsSVRFCmds: {SOURCE CASE YES} {LAYOUT CASE YES} '
            '{VIRTUAL CONNECT REPORT YES} {}\n') in content


def test_cell_level_keeps_colon_virtual_connect(tmp_path):
    checker = _make_checker(tmp_path)
    content = _runset(checker.setup_lvs_flow('lib', 'top', lvs_level='cell'))
    assert '*cmnVConnectColon: 1\n' in content


def test_level_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(LVS_LEVEL_ENV, 'block')
    checker = _make_checker(tmp_path)
    assert 'VIRTUAL CONNECT REPORT YES' in _runset(checker.setup_lvs_flow('lib', 'top'))


def test_keyword_level_wins_over_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(LVS_LEVEL_ENV, 'block')
    checker = _make_checker(tmp_path)
    content = _runset(checker.setup_lvs_flow('lib', 'top', lvs_level='cell'))
    assert 'IGNORE DEVICE PIN' in content
    assert 'VIRTUAL CONNECT REPORT' not in content


def test_unknown_level_fails_before_any_tool_runs(tmp_path):
    checker = _make_checker(tmp_path)
    with pytest.raises(ValueError, match='level not found: chip'):
        checker.setup_lvs_flow('lib', 'top', lvs_level='chip')
    assert not (tmp_path / 'runs' / 'lib' / 'top').exists() or not any(
        (tmp_path / 'runs' / 'lib' / 'top').iterdir()
    )


def test_level_without_policy_file_is_an_error(tmp_path):
    checker = _make_checker(tmp_path, policy=None)
    with pytest.raises(ValueError, match='needs lvs_policy_file'):
        checker.setup_lvs_flow('lib', 'top', lvs_level='block')


def test_caller_cannot_loosen_a_level(tmp_path):
    checker = _make_checker(tmp_path)
    with pytest.raises(ValueError, match='LVS level block sets cmnVConnectColon=1'):
        checker.setup_lvs_flow('lib', 'top', lvs_level='block',
                               params={'cmnVConnectColon': 0})
    # Repeating the level's own value is fine.
    content = _runset(checker.setup_lvs_flow('lib', 'top', lvs_level='block',
                                             params={'cmnVConnectColon': '1'}))
    assert '*cmnVConnectColon: 1\n' in content


def test_policy_rejects_non_scalar_override(tmp_path):
    policy = tmp_path / 'p.yaml'
    policy.write_text(
        'levels: {block: b}\nprofiles:\n  b:\n    runset:\n      cmnVConnectColon: [0]\n',
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='must be a scalar'):
        resolve_lvs_policy(str(policy), 'block')


def test_cell_level_adds_bulk_ignore_and_keeps_tech_commands(tmp_path):
    checker = _make_checker(tmp_path)
    content = _runset(checker.setup_lvs_flow('lib', 'top', lvs_level='cell'))
    assert ('*lvsSVRFCmds: {SOURCE CASE YES} {LAYOUT CASE YES} '
            '{LVS IGNORE DEVICE PIN MN B} {LVS IGNORE DEVICE PIN MP B}\n') in content
    assert '*lvsIncludeCmdsType: SVRF\n' in content


def test_block_level_compares_bodies(tmp_path):
    checker = _make_checker(tmp_path)
    content = _runset(checker.setup_lvs_flow('lib', 'top', lvs_level='block'))
    assert 'IGNORE DEVICE PIN' not in content


def test_merge_svrf_fills_empty_slots_then_appends_without_repeats():
    merge = calibre.merge_svrf_commands
    assert merge('{A} {} {}', ['B']) == '{A} {B} {}'
    assert merge('{A} {B}', ['C', 'A']) == '{A} {B} {C}'
    assert merge(None, ['X']) == '{X}'
    assert merge('{A} {}', []) == '{A} {}'


def test_policy_rejects_multiline_or_braced_svrf(tmp_path):
    policy = tmp_path / 'p.yaml'
    policy.write_text(
        'levels: {cell: c}\nprofiles:\n  c:\n    svrf:\n      - "LVS {X}"\n',
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='one-line SVRF statements'):
        resolve_lvs_policy(str(policy), 'cell')


def test_svrf_with_modern_runset_fails_closed(tmp_path):
    checker = _make_checker(tmp_path)
    (tmp_path / 'lvs.runset').write_text(
        'lvs.runDir.value = "./old"\n', encoding='utf-8'
    )
    with pytest.raises(ValueError, match='need a legacy'):
        checker.modify_lvs_runset(str(tmp_path), 'lib', 'top', 'layout', 'l.gds',
                                  's.net', {}, extra_svrf=['LVS IGNORE DEVICE PIN MN B'])


def _block_judge(tmp_path, ext=None, ctl='VIRTUAL CONNECT REPORT YES\n'):
    checker = _make_checker(tmp_path)
    flow = checker.setup_lvs_flow('lib', 'top', lvs_level='block')
    run_dir = Path(flow[-1][3])
    if ctl is not None:
        (run_dir / '_calibre.lvs_').write_text(ctl, encoding='utf-8')
    if ext is not None:
        (run_dir / 'top.lvs.report.ext').write_text(ext, encoding='utf-8')
    log = tmp_path / 'lvs.log'
    return flow[-1][4], run_dir, log


def _audit(run_dir):
    return json.loads((run_dir / 'lvs_policy_result.json').read_text(encoding='utf-8'))


def test_cell_verdict_is_calibre_verdict_and_writes_audit(tmp_path):
    checker = _make_checker(tmp_path)
    flow = checker.setup_lvs_flow('lib', 'top', lvs_level='cell')
    judge, run_dir = flow[-1][4], Path(flow[-1][3])
    log = tmp_path / 'lvs.log'
    log.write_text('LVS completed. INCORRECT. See report.\n', encoding='utf-8')
    assert judge(0, str(log))[0] is False
    audit = _audit(run_dir)
    assert audit['policy_profile'] == 'bag_cell'
    assert audit['svrf'] == ['LVS IGNORE DEVICE PIN MN B', 'LVS IGNORE DEVICE PIN MP B']
    assert audit['virtual_connect_check'] is None
    log.write_text('LVS completed. CORRECT. See report.\n', encoding='utf-8')
    assert judge(0, str(log))[0] is True
    assert 'BAG LVS policy result' in log.read_text(encoding='utf-8')


def test_block_correct_without_virtual_connects_passes(tmp_path):
    judge, run_dir, log = _block_judge(tmp_path, ext=EXT_CLEAN)
    log.write_text('LVS completed. CORRECT. See report.\n', encoding='utf-8')
    assert judge(0, str(log))[0] is True
    check = _audit(run_dir)['virtual_connect_check']
    assert check['checked'] is True and check['connections'] == []


def test_block_correct_with_virtual_connects_fails(tmp_path):
    judge, run_dir, log = _block_judge(tmp_path, ext=EXT_SPLIT)
    log.write_text('LVS completed. CORRECT. See report.\n', encoding='utf-8')
    assert judge(0, str(log))[0] is False
    audit = _audit(run_dir)
    assert audit['calibre_passed'] is True and audit['passed'] is False
    nets = [(c['cell'], c['net']) for c in audit['virtual_connect_check']['connections']]
    assert nets == [('qmux_cml', 'VDD'), ('qmux_cml', 'VSS')]
    assert audit['virtual_connect_check']['connections'][0]['points_um'] == [
        [13.91, 1.2], [13.91, 3.6]]
    assert 'virtual_connections=2' in log.read_text(encoding='utf-8')


def test_block_check_never_turns_incorrect_into_pass(tmp_path):
    judge, run_dir, log = _block_judge(tmp_path, ext=EXT_CLEAN)
    log.write_text('LVS completed. INCORRECT. See report.\n', encoding='utf-8')
    assert judge(0, str(log))[0] is False


def test_block_fails_closed_without_ext_report(tmp_path):
    judge, run_dir, log = _block_judge(tmp_path, ext=None)
    log.write_text('LVS completed. CORRECT. See report.\n', encoding='utf-8')
    assert judge(0, str(log))[0] is False
    assert 'extraction report is missing' in _audit(run_dir)['virtual_connect_check']['error']


def test_block_fails_closed_when_report_statement_missing(tmp_path):
    judge, run_dir, log = _block_judge(tmp_path, ext=EXT_CLEAN, ctl='SOURCE CASE YES\n')
    log.write_text('LVS completed. CORRECT. See report.\n', encoding='utf-8')
    assert judge(0, str(log))[0] is False
    assert 'lacks VIRTUAL CONNECT REPORT' in _audit(run_dir)['virtual_connect_check']['error']


def test_policy_rejects_non_bool_fail_on_virtual_connect(tmp_path):
    policy = tmp_path / 'p.yaml'
    policy.write_text(
        'levels: {block: b}\nprofiles:\n  b:\n    fail_on_virtual_connect: "yes"\n',
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='must be true or false'):
        resolve_lvs_policy(str(policy), 'block')
