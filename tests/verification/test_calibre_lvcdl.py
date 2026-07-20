from pathlib import Path

from bag.verification.calibre import Calibre


def _make_checker(tmp_path):
    runset = tmp_path / 'lvs.runset'
    runset.write_text(
        '*lvsLayoutPrimary: old_layout\n'
        '*lvsSourcePrimary: old_source\n'
        '*lvsSourceLibrary: old_lib\n',
        encoding='utf-8',
    )
    checker = object.__new__(Calibre)
    checker.lvs_run_dir = str(tmp_path / 'runs')
    checker.lvs_runset = str(runset)
    checker.default_lvs_params = {}
    checker.setup_export_layout = lambda *args: (
        ['export-layout'], str(tmp_path / 'layout.log'), None, None
    )
    checker.setup_export_schematic = lambda *args: (
        ['export-schematic'], str(tmp_path / 'schematic.log'), None, None
    )
    return checker


def test_setup_lvs_flow_uses_external_cdl_and_distinct_source_cell(tmp_path):
    checker = _make_checker(tmp_path)
    source_dir = tmp_path / 'cdl'
    source_dir.mkdir()
    source = source_dir / 'generated.sp'
    source.write_text(
        '.include "child.sp"\n'
        '.subckt schematic_top A B\n.ends\n',
        encoding='utf-8',
    )
    (source_dir / 'child.sp').write_text(
        '.subckt child A B\n.ends\n',
        encoding='utf-8',
    )

    flow = checker.setup_lvs_flow(
        'layout_lib',
        'layout_top',
        source_netlist_path=str(source),
        source_lib_name='source_lib',
        source_cell_name='schematic_top',
    )

    assert flow[0][0][0] == 'export-layout'
    assert flow[1][0][0] == 'cp'
    assert flow[1][0][1] == '-R'
    assert Path(flow[1][0][2]) == source_dir / '.'
    assert flow[2][0][0] == 'cp'
    assert Path(flow[2][0][1]) == source
    assert all(step[0][0] != 'export-schematic' for step in flow)

    runset_path = flow[-1][0][4]
    content = Path(runset_path).read_text(encoding='utf-8')
    assert '*lvsLayoutPrimary: layout_top' in content
    assert '*lvsSourcePrimary: schematic_top' in content
    assert '*lvsSourceLibrary: source_lib' in content


def test_setup_lvs_flow_rejects_missing_external_cdl(tmp_path):
    checker = _make_checker(tmp_path)

    try:
        checker.setup_lvs_flow(
            'layout_lib',
            'layout_top',
            source_netlist_path=str(tmp_path / 'missing.sp'),
        )
    except ValueError as err:
        assert 'source_netlist_path does not exist' in str(err)
    else:
        raise AssertionError('missing source CDL was accepted')
