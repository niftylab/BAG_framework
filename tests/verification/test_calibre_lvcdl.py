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


def test_modify_lvs_runset_supports_modern_calibre_format(tmp_path):
    runset = tmp_path / 'lvs.runset'
    runset.write_text(
        '// modern Calibre runset\n'
        'lvs.runDir.value = "./old"\n'
        'lvs.layout.layoutFile.value = "old.gds"\n'
        'cmn.layout.defFiles.value = [ "old.def" ]\n'
        'lvs.layout.topCellLibrary.value = "old_layout_lib"\n'
        'cmn.layout.topCellLibraryFDI.value = "old_layout_lib"\n'
        'lvs.layout.topCell.value = "old_layout"\n'
        'lvs.layout.topCellView.value = "old_layout_view"\n'
        'cmn.layout.topCellViewFDI.value = "old_layout_view"\n'
        'lvs.layout.layoutNetlist.value = "old.sp"\n'
        'lvs.source2.sourceFile.value = "old_source.net"\n'
        'lvs.source2.topCellLibrary.value = "old_source_lib"\n'
        'lvs.source2.topCellLibraryFDI.value = "old_source_lib"\n'
        'lvs.source2.topCell.value = "old_source"\n'
        'lvs.source2.topCellFDI.value = "old_source"\n'
        'lvs.source.sourceFile.value = "old_source.net"\n'
        'lvs.source.topCellLibrary.value = "old_source_lib"\n'
        'lvs.source.topCellLibraryFDI.value = "old_source_lib"\n'
        'lvs.source.topCell.value = "old_source"\n'
        'lvs.source.topCellFDI.value = "old_source"\n'
        'lvs.reports.lvsReport.value = "old.lvs.report"\n'
        'lvs.traceProperty.parameters = [\n'
        '    [ false, "\\"R\\"(opppcres) \\"r\\" \\"r\\" 0.1 0.1"],\n'
        ']\n'
        'lvs.ercdb.resultsFile.value = "old.erc.results"\n'
        'lvs.ercSummaryReport.report.value = "old.erc.summary"\n',
        encoding='utf-8',
    )
    checker = object.__new__(Calibre)
    checker.lvs_runset = str(runset)
    run_dir = str(tmp_path / 'runs' / 'layout_lib' / 'layout_top')
    gds_file = str(tmp_path / 'layout.gds')
    netlist = str(tmp_path / 'schematic.net')

    content = checker.modify_lvs_runset(
        run_dir,
        'layout_lib',
        'layout_top',
        'layout',
        gds_file,
        netlist,
        {},
        source_lib_name='source_lib',
        source_cell_name='source_top',
    )

    assert 'lvs.runDir.value = "{}"'.format(run_dir.replace('\\', '\\\\')) in content
    assert 'lvs.layout.layoutFile.value = "{}"'.format(
        gds_file.replace('\\', '\\\\')
    ) in content
    assert 'lvs.source.sourceFile.value = "{}"'.format(
        netlist.replace('\\', '\\\\')
    ) in content
    assert 'lvs.layout.topCell.value = "layout_top"' in content
    assert 'lvs.source.topCell.value = "source_top"' in content
    assert 'lvs.source.topCellLibrary.value = "source_lib"' in content
    assert 'lvs.reports.lvsReport.value = "layout_top.lvs.report"' in content
    assert '[ false, "\\"R\\"(opppcres) \\"r\\" \\"r\\" 0.1 0.1"]' in content
