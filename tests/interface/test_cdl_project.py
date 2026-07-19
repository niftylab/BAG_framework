from pathlib import Path

import yaml

import bag.core as bag_core
from bag.core import BagProject
from bag.interface.cdl import CdlInterface


DATA_DIR = Path(__file__).parents[1] / 'io' / 'cdl' / 'data'


def _write_project_config(tmp_path, database, simulation=None, socket=None):
    tech_config = tmp_path / 'tech_config.yaml'
    tech_config.write_text('{}\n', encoding='utf-8')

    lib_defs = tmp_path / 'bag_libs.def'
    lib_defs.write_text('', encoding='utf-8')

    config = {
        'database': database,
        'tech_config_path': str(tech_config),
        'lib_defs': lib_defs.name,
        'new_lib_path': str(tmp_path / 'BagModules'),
    }
    if simulation is not None:
        config['simulation'] = simulation
    if socket is not None:
        config['socket'] = socket

    config_path = tmp_path / 'bag_config.yaml'
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding='utf-8',
    )
    return config_path, lib_defs


def test_bag_project_imports_cdl_without_socket_or_simulator(
        tmp_path, monkeypatch):
    monkeypatch.setenv('BAG_WORK_DIR', str(tmp_path))
    output_root = tmp_path / 'BagModules'
    output_root.mkdir()
    implementation_root = tmp_path / 'gen_libs'
    implementation_root.mkdir()

    database = {
        'class': 'bag.interface.cdl.CdlInterface',
        'default_lib_path': str(implementation_root),
        'schematic': {
            'exclude_libraries': ['BAG_prim', 'basic', 'analogLib'],
        },
        'cdl': {
            'source_files': [str(DATA_DIR / 'buffer2.sp')],
            'strict': True,
        },
    }
    config_path, lib_defs = _write_project_config(tmp_path, database)

    project = BagProject(bag_config_path=str(config_path))
    try:
        assert isinstance(project.impl_db, CdlInterface)
        assert project.sim is None

        project.import_design_library('logic_templates')
    finally:
        project.close_bag_server()
        project.close_sim_server()

    assert (
        output_root / 'logic_templates' / 'netlist_info' / 'inv.yaml'
    ).is_file()
    assert (
        output_root / 'logic_templates' / 'netlist_info' / 'buffer2.yaml'
    ).is_file()
    assert (
        output_root / 'logic_templates' / 'cdl_templates' / 'inv.cdl'
    ).is_file()
    assert (
        output_root / 'logic_templates' / 'cdl_templates' / 'buffer2.cdl'
    ).is_file()
    assert lib_defs.read_text(encoding='utf-8').strip() == (
        'logic_templates {}'.format(output_root)
    )


def test_bag_project_batch_schematic_writes_cdl(
        tmp_path, monkeypatch):
    monkeypatch.setenv('BAG_WORK_DIR', str(tmp_path))
    source_file = tmp_path / 'runtime_templates.sp'
    source_file.write_text(
        '.SUBCKT inv IN OUT VDD VSS PARAMS: wn=1u lch=45n nf=2\n'
        '* @BAG {"lib_name":"runtime_templates"}\n'
        '*.PININFO IN:I OUT:O VDD:B VSS:B\n'
        "MN0 OUT IN VSS VSS nmos4_lvt w='wn' l='lch' nf='nf'"
        ' $ @BAG {"lib_name":"BAG_prim"}\n'
        '.ENDS inv\n',
        encoding='utf-8',
    )

    template_root = tmp_path / 'BagModules'
    template_root.mkdir()
    implementation_root = tmp_path / 'gen_libs'
    implementation_root.mkdir()
    database = {
        'class': 'bag.interface.cdl.CdlInterface',
        'default_lib_path': str(implementation_root),
        'schematic': {
            'exclude_libraries': ['BAG_prim', 'basic', 'analogLib'],
        },
        'cdl': {
            'source_files': [str(source_file)],
            'strict': True,
        },
    }
    config_path, _ = _write_project_config(tmp_path, database)

    project = BagProject(bag_config_path=str(config_path))
    try:
        project.import_design_library('runtime_templates')
        instance = project.new_schematic_instance(
            lib_name='runtime_templates',
            cell_name='inv',
            params={},
        )
        project.batch_schematic(
            'runtime_generated',
            [instance],
            name_list=['inv_generated'],
        )
        instance.implement_design(
            'runtime_generated',
            top_cell_name='inv_generated',
            overwrite=True,
        )
    finally:
        project.close_bag_server()
        project.close_sim_server()

    output_file = (
        implementation_root / 'runtime_generated' / 'inv_generated.sp'
    )
    output = output_file.read_text(encoding='utf-8')
    assert '.SUBCKT inv_generated IN OUT VDD VSS' in output
    assert "MN0 OUT IN VSS VSS nmos4_lvt w='wn' l='lch' nf='nf'" in output
    assert '.ENDS inv_generated' in output


def test_bag_project_keeps_server_database_construction(
        tmp_path, monkeypatch):
    monkeypatch.setenv('BAG_WORK_DIR', str(tmp_path))
    implementation_root = tmp_path / 'gen_libs'
    implementation_root.mkdir()

    class DummyDealer:
        def __init__(self, port, **kwargs):
            self.port = port
            self.kwargs = kwargs

    class DummyServerDatabase:
        requires_server = True

        def __init__(self, dealer, tmp_dir, db_config):
            self.dealer = dealer
            self.default_lib_path = db_config['default_lib_path']

        def close(self):
            pass

    class DummySimulation:
        def __init__(self, tmp_dir, sim_config):
            self.sim_config = sim_config

        def close(self):
            pass

    original_import = bag_core._import_class_from_str

    def import_class(class_name):
        if class_name == 'test.DummyServerDatabase':
            return DummyServerDatabase
        if class_name == 'test.DummySimulation':
            return DummySimulation
        return original_import(class_name)

    monkeypatch.setattr(bag_core, '_import_class_from_str', import_class)
    monkeypatch.setattr(bag_core, 'ZMQDealer', DummyDealer)

    database = {
        'class': 'test.DummyServerDatabase',
        'default_lib_path': str(implementation_root),
        'schematic': {'exclude_libraries': []},
    }
    simulation = {'class': 'test.DummySimulation'}
    socket = {
        'host': 'localhost',
        'port_file': 'unused.txt',
        'pipeline': 10,
    }
    config_path, _ = _write_project_config(
        tmp_path, database, simulation=simulation, socket=socket
    )

    project = BagProject(bag_config_path=str(config_path), port=4321)
    try:
        assert isinstance(project.impl_db, DummyServerDatabase)
        assert project.impl_db.dealer.port == 4321
        assert project.impl_db.dealer.kwargs == {
            'host': 'localhost',
            'pipeline': 10,
        }
        assert isinstance(project.sim, DummySimulation)
    finally:
        project.close_bag_server()
        project.close_sim_server()
