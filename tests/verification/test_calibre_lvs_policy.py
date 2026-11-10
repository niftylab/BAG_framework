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
    description: Rails must close inside the block.
    runset:
      cmnVConnectColon: 0
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


def test_block_level_turns_colon_virtual_connect_off(tmp_path):
    checker = _make_checker(tmp_path)
    content = _runset(checker.setup_lvs_flow('lib', 'top', lvs_level='block'))
    assert '*cmnVConnectColon: 0\n' in content
    assert '*cmnVConnectColon: 1' not in content


def test_cell_level_keeps_colon_virtual_connect(tmp_path):
    checker = _make_checker(tmp_path)
    content = _runset(checker.setup_lvs_flow('lib', 'top', lvs_level='cell'))
    assert '*cmnVConnectColon: 1\n' in content


def test_level_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(LVS_LEVEL_ENV, 'block')
    checker = _make_checker(tmp_path)
    assert '*cmnVConnectColon: 0\n' in _runset(checker.setup_lvs_flow('lib', 'top'))


def test_keyword_level_wins_over_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(LVS_LEVEL_ENV, 'block')
    checker = _make_checker(tmp_path)
    content = _runset(checker.setup_lvs_flow('lib', 'top', lvs_level='cell'))
    assert '*cmnVConnectColon: 1\n' in content


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
    with pytest.raises(ValueError, match='LVS level block sets cmnVConnectColon=0'):
        checker.setup_lvs_flow('lib', 'top', lvs_level='block',
                               params={'cmnVConnectColon': 1})
    # Repeating the level's own value is fine.
    content = _runset(checker.setup_lvs_flow('lib', 'top', lvs_level='block',
                                             params={'cmnVConnectColon': '0'}))
    assert '*cmnVConnectColon: 0\n' in content


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
    assert '*lvsSVRFCmds: {SOURCE CASE YES} {LAYOUT CASE YES} {} {}\n' in content
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


def test_policy_verdict_is_calibre_verdict_and_writes_audit(tmp_path):
    checker = _make_checker(tmp_path)
    flow = checker.setup_lvs_flow('lib', 'top', lvs_level='block')
    judge = flow[-1][4]
    run_dir = Path(flow[-1][3])

    log = tmp_path / 'lvs.log'
    log.write_text('LVS completed. INCORRECT. See report.\n', encoding='utf-8')
    passed, _ = judge(0, str(log))
    assert passed is False
    audit = json.loads((run_dir / 'lvs_policy_result.json').read_text(encoding='utf-8'))
    assert audit['policy_level'] == 'block'
    assert audit['policy_profile'] == 'bag_block'
    assert audit['runset_overrides'] == {'cmnVConnectColon': 0}
    assert audit['passed'] is False
    assert 'BAG LVS policy result' in log.read_text(encoding='utf-8')

    log.write_text('LVS completed. CORRECT. See report.\n', encoding='utf-8')
    passed, _ = judge(0, str(log))
    assert passed is True
    audit = json.loads((run_dir / 'lvs_policy_result.json').read_text(encoding='utf-8'))
    assert audit['passed'] is True
