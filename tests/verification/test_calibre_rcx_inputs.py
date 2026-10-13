from pathlib import Path

import pytest

from bag.verification.calibre import Calibre
from bag.verification.input_files import cdl_files, stage_cdl


def checker(tmp_path):
    runset = tmp_path/'pex.runset'
    runset.write_text('*pexPexNetlistFormat: CALIBREVIEW\n')
    result = object.__new__(Calibre)
    result.rcx_run_dir = str(tmp_path/'runs')
    result.rcx_runset = str(runset)
    result.rcx_mode = 'pex'
    result.rcx_link_files = []
    result.default_rcx_params = {}
    result.setup_export_layout = lambda *a: (['export-layout'], 'layout.log', None, None)
    result.setup_export_schematic = lambda *a: (['export-schematic'], 'source.log', None, None)
    return result


@pytest.mark.parametrize('external_layout', [False, True])
@pytest.mark.parametrize('external_source', [False, True])
def test_rcx_input_formats_are_independent(tmp_path, external_layout, external_source):
    instance = checker(tmp_path)
    gds = tmp_path/'input.gds'; gds.write_bytes(b'gds')
    cdl = tmp_path/'source.sp'; cdl.write_text('.subckt cell A B\n.ends\n')
    options = {}
    if external_layout: options['gds_layout_path'] = str(gds)
    if external_source: options['source_netlist_path'] = str(cdl)
    flow = instance.setup_rcx_flow('lib', 'cell', **options)
    commands = [s[0] for s in flow]
    assert any(c[0]=='export-layout' for c in commands) == (not external_layout)
    assert any(c[0]=='export-schematic' for c in commands) == (not external_source)
    runset = Path(flow[-1][0][4]).read_text()
    assert '*pexSourcePrimary: cell' in runset


@pytest.mark.parametrize('argument', ['source_netlist_path', 'gds_layout_path'])
def test_rcx_rejects_missing_input_files_before_scheduling_export(tmp_path, argument):
    instance = checker(tmp_path)
    with pytest.raises(ValueError, match=argument):
        instance.setup_rcx_flow('lib', 'cell', **{argument: str(tmp_path/'missing')})


def test_rcx_source_identifiers_reach_the_runset(tmp_path):
    instance = checker(tmp_path)
    cdl = tmp_path/'source.sp'; cdl.write_text('.subckt other A B\n.ends\n')
    flow = instance.setup_rcx_flow('layout_lib', 'cell', source_netlist_path=str(cdl),
                                   source_lib_name='source_lib', source_cell_name='other')
    content = Path(flow[-1][0][4]).read_text()
    assert '*pexSourcePrimary: other' in content
    assert '*pexSourceLibrary: source_lib' in content


def test_copy_failure_stops_before_calibre(tmp_path):
    instance=checker(tmp_path)
    gds=tmp_path/'input.gds';gds.write_bytes(b'gds')
    cdl=tmp_path/'source.sp';cdl.write_text('.subckt cell A B\n.ends\n')
    flow=instance.setup_rcx_flow('lib','cell',gds_layout_path=str(gds),source_netlist_path=str(cdl))
    assert flow[0][4](1,'copy-failure.log') is False
    assert flow[1][4](1,'stage-failure.log') is False


def test_database_rejects_unsupported_file_input_checker():
    import asyncio
    from types import SimpleNamespace
    from bag.interface.database import DbAccess
    with pytest.raises(ValueError,match='not supported'):
        asyncio.run(DbAccess.async_run_rcx(SimpleNamespace(checker=object()),'lib','cell',source_netlist_path='input.sp'))


def test_nested_include_bundle_is_staged_without_unrelated_files(tmp_path):
    source = tmp_path/'source.sp'
    child = tmp_path/'child'/'child.sp'; child.parent.mkdir()
    leaf = tmp_path/'leaf.sp'
    source.write_text('.include "child/child.sp"\n.subckt top A B\n.ends\n')
    child.write_text(".include '../leaf.sp'\n.subckt child A B\n.ends\n")
    leaf.write_text('.subckt leaf A B\n.ends\n')
    (tmp_path/'unrelated.txt').write_text('do not copy')
    destination = tmp_path/'run'/'schematic.net'
    stage_cdl(source, destination)
    assert sorted(p.name for p in cdl_files(destination)) == ['child.sp','leaf.sp','schematic.net']
    assert not list(destination.parent.rglob('unrelated.txt'))


def test_missing_includes_fail_before_a_run_directory_is_created(tmp_path):
    instance = checker(tmp_path)
    source = tmp_path/'source.sp'; source.write_text('.include "missing.sp"\n')
    with pytest.raises(ValueError, match='include'):
        instance.setup_rcx_flow('lib', 'cell', source_netlist_path=str(source))
    assert not Path(instance.rcx_run_dir).exists()


def test_portable_cdl_bundle_survives_relocation(tmp_path):
    import shutil
    source = tmp_path/'src'/'source.sp'; source.parent.mkdir()
    source.write_text('.include "child.sp"\n')
    (source.parent/'child.sp').write_text('.lib corner\n.endl\n')
    stage_cdl(source, tmp_path/'package'/'source.sp', absolute_includes=False)
    shutil.move(str(tmp_path/'package'), str(tmp_path/'remote'))
    assert len(cdl_files(tmp_path/'remote'/'source.sp')) == 2


def test_reusing_a_source_in_the_run_directory_does_not_modify_it(tmp_path):
    import subprocess
    instance=checker(tmp_path)
    directory=Path(instance.rcx_run_dir)/'lib'/'cell';directory.mkdir(parents=True)
    source=directory/'schematic.net';source.write_text('.include "child.sp"\n')
    (directory/'child.sp').write_text('.subckt child A B\n.ends\n')
    before=source.read_bytes()
    flow=instance.setup_rcx_flow('lib','cell',source_netlist_path=str(source))
    command=next(s[0] for s in flow if 'bag.verification.input_files' in s[0])
    completed=subprocess.run(command,capture_output=True,text=True)
    assert completed.returncode==0,completed.stderr
    assert source.read_bytes()==before
    assert Path(command[-1])!=source
    assert len(cdl_files(command[-1]))==2
