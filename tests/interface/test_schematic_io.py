import copy
from pathlib import Path
import sys
import types

import pytest
import yaml

from bag.interface.cdl import CdlInterface
from bag.interface.schematic import SchematicInterface
from bag.design.module import ModuleDB


SOURCE = Path(__file__).parents[1] / 'io/cdl/data/inv.sp'


@pytest.mark.parametrize('source', ['cdl', 'oa'])
@pytest.mark.parametrize('output', ['cdl', 'oa'])
def test_routes_use_selected_source_and_writer(tmp_path, monkeypatch, source, output):
    db_config = {
        'class': 'test.OA', 'default_lib_path': str(tmp_path),
        'schematic': {'exclude_libraries': ['BAG_prim']},
        'cdl': {'source_files': [str(SOURCE)]},
        'schematic_io': {'input': source, 'output': output,
                         'cdl_oa_renderer': 'test_sch_renderer:render'},
    }
    electrical = CdlInterface(None, db_config)
    oa_info = yaml.safe_load(electrical.parse_schematic_template('logic_templates', 'inv'))
    oa_info['instances']['MN0']['instpins']['G']['net_name'] = 'OA_INPUT'
    calls = []

    class OA:
        def __init__(self, *args):
            pass

        def parse_schematic_template(self, lib, cell):
            calls.append(('read_oa', lib, cell))
            return yaml.safe_dump(oa_info)

        def create_implementation(self, lib, templates, changes, lib_path=''):
            calls.append(('write_oa', lib, templates, changes))
            return ['oa-result']

    def render(oa, path, cell, runtime):
        assert cell.instances['MN0'].connections['G'] == 'IN'
        calls.append(('render', cell.lib_name, cell.cell_name))
        return 'runtime_templates', cell.cell_name

    monkeypatch.setattr('bag.interface.schematic._load_class', lambda name: OA)
    monkeypatch.setitem(sys.modules, 'test_sch_renderer', types.SimpleNamespace(render=render))
    db = SchematicInterface(object() if 'oa' in (source, output) else None, None, db_config)
    info = db.read_source_info('logic_templates', 'inv')
    assert info['instances']['MN0']['instpins']['G']['net_name'] == ('IN' if source == 'cdl' else 'OA_INPUT')
    # BagProject/batch_schematic enters here, not through create_implementation.
    db.instantiate_schematic('generated', [
        ('logic_templates', 'inv', 'inv_out', {}, {}, []),
    ])
    result = db.create_implementation('generated', [('logic_templates', 'inv', 'inv_out')], [{}])
    if output == 'cdl':
        text = Path(result[0]).read_text()
        assert ('OA_INPUT' in text) == (source == 'oa')
        assert not any(call[0] == 'write_oa' for call in calls)
    else:
        assert result == ['oa-result']
        written = next(call for call in calls if call[0] == 'write_oa')
        assert written[2][0][0] == ('runtime_templates' if source == 'cdl' else 'logic_templates')
    assert any(call[0] == 'read_oa' for call in calls) == (source == 'oa')
    assert any(call[0] == 'render' for call in calls) == (source == 'cdl' and output == 'oa')


def test_module_reads_cdl_topology_but_keeps_bag_instance_aliases(tmp_path):
    source = tmp_path / 'source.cdl'
    source.write_text(SOURCE.read_text().replace('MN0', 'MIMN0'))
    config = {'class': 'bag.interface.cdl.CdlInterface',
              'default_lib_path': str(tmp_path),
              'schematic': {'exclude_libraries': []},
              'cdl': {'source_files': [str(source)]},
              'schematic_io': {'input': 'cdl', 'output': 'cdl'}}
    backend = SchematicInterface(None, None, config)
    info = backend.read_source_info('logic_templates', 'inv')
    stale = copy.deepcopy(info)
    stale['instances']['IMN0'] = stale['instances'].pop('MIMN0')
    stale['instances']['IMN0']['instpins']['G']['net_name'] = 'STALE'
    info_file = tmp_path / 'inv.yaml'
    info_file.write_text(yaml.safe_dump(stale))
    db = ModuleDB('', None, [], prj=types.SimpleNamespace(impl_db=backend))
    selected = db.read_schematic_info(str(info_file))
    assert selected['instances']['IMN0']['instpins']['G']['net_name'] == 'IN'
    assert 'MIMN0' in backend.read_source_info('logic_templates', 'inv')['instances']


def test_oa_selection_never_silently_uses_cdl_when_server_is_missing():
    with pytest.raises(ValueError, match='BAG server'):
        SchematicInterface(None, None, {'schematic_io': {'input': 'oa', 'output': 'cdl'}})
