from pathlib import Path

from bag.verification.pvs import PVS


def _make_checker(tmp_path):
    checker = object.__new__(PVS)
    checker.lvs_run_dir = str(tmp_path / 'runs')
    checker.lvs_rule_file = str(tmp_path / 'rules.rul')
    checker.default_lvs_params = {}
    checker.modify_lvs_runset = (
        lambda run_dir, cell_name, params: 'runset\n'
    )
    checker.setup_export_layout = lambda *args: (
        ['export-layout'], str(tmp_path / 'layout.log'), None, None
    )
    checker.setup_export_schematic = lambda *args: (
        ['export-schematic'], str(tmp_path / 'schematic.log'), None, None
    )
    return checker


def _flow_commands(flow):
    return [step[0] for step in flow]


def test_setup_lvs_flow_uses_external_cdl_source(tmp_path):
    checker = _make_checker(tmp_path)
    source_dir = tmp_path / 'cdl'
    source_dir.mkdir()
    source = source_dir / 'top.sp'
    source.write_text(
        '.include "libs/child.sp"\n.subckt top A B\n.ends\n',
        encoding='utf-8',
    )
    (source_dir / 'libs').mkdir()
    (source_dir / 'libs' / 'child.sp').write_text(
        '.subckt child A B\n.ends\n', encoding='utf-8'
    )

    flow = checker.setup_lvs_flow(
        'layout_lib', 'layout_top',
        source_netlist_path=str(source),
        source_cell_name='top',
    )
    commands = _flow_commands(flow)

    assert ['export-schematic'] not in commands
    copy_cmds = [cmd for cmd in commands if cmd and cmd[0] == 'cp']
    # one copies the source directory tree, one the top netlist itself
    assert any(cmd[1] == '-R' for cmd in copy_cmds)
    assert any(str(source).replace('\\', '/') in cmd[1].replace('\\', '/')
               for cmd in copy_cmds if cmd[1] != '-R')
    pvs_cmd = commands[-1]
    assert pvs_cmd[0] == 'pvs'
    idx = pvs_cmd.index('-source_top_cell')
    assert pvs_cmd[idx + 1] == 'top'


def test_setup_lvs_flow_still_exports_schematic_by_default(tmp_path):
    checker = _make_checker(tmp_path)
    flow = checker.setup_lvs_flow('layout_lib', 'layout_top')
    commands = _flow_commands(flow)
    assert ['export-schematic'] in commands
    pvs_cmd = commands[-1]
    idx = pvs_cmd.index('-source_top_cell')
    assert pvs_cmd[idx + 1] == 'layout_top'
