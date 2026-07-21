import importlib
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from bag.interface.cdl import CdlInterface
from bag.io.cdl import CdlParser
from bag.util.cache import ClassImporter


DATA_DIR = Path(__file__).parents[1] / 'io' / 'cdl' / 'data'
PROJECT_ROOT = Path(__file__).parents[2]


def make_config(source_file, output_root):
    return dict(
        default_lib_path=str(output_root),
        schematic=dict(
            exclude_libraries=['BAG_prim', 'basic', 'analogLib'],
        ),
        cdl=dict(
            source_files=[str(source_file)],
            strict=True,
        ),
    )


def test_cdl_interface_indexes_and_serializes(tmp_path):
    output_root = tmp_path / 'BagModules'
    output_root.mkdir()
    interface = CdlInterface(
        None, make_config(DATA_DIR / 'buffer2.sp', output_root)
    )

    assert interface.get_cells_in_library('logic_templates') == [
        'inv', 'buffer2'
    ]
    assert interface.get_cells_in_library('missing') == []

    info = yaml.safe_load(
        interface.parse_schematic_template('logic_templates', 'buffer2')
    )
    assert info['lib_name'] == 'logic_templates'
    assert info['pins'] == ['IN', 'OUT', 'VDD', 'VSS']
    assert info['instances']['XINV1']['cell_name'] == 'inv'
    assert info['instances']['XINV1']['parameters']['wn'] == '2*wn'

    with pytest.raises(ValueError, match='was not found'):
        interface.parse_schematic_template('logic_templates', 'missing')


def test_cdl_interface_writes_structural_implementation(tmp_path):
    output_root = tmp_path / 'generated_netlists'
    output_root.mkdir()
    interface = CdlInterface(
        None, make_config(DATA_DIR / 'buffer2.sp', output_root)
    )

    inv_content = (
        'logic_templates',
        'inv',
        'inv_array',
        {'IN': 'IN', 'OUT': 'Z', 'VDD': 'VDD', 'VSS': 'VSS'},
        {
            'MN0': [
                {
                    'name': 'MN0A',
                    'lib_name': 'BAG_prim',
                    'cell_name': 'nmos4_lvt',
                    'params': {'w': '2*wn', 'nf': 4},
                    'term_mapping': {'D': 'Z'},
                },
                {
                    'name': 'MN0B',
                    'lib_name': 'BAG_prim',
                    'cell_name': 'nmos4_lvt',
                    'params': {'w': 'wn', 'nf': 2},
                    'term_mapping': {'D': 'MID'},
                },
            ],
            'MP0': [],
        },
        [['BIAS', 'input']],
    )
    top_content = (
        'logic_templates',
        'buffer2',
        'buffer_generated',
        {'IN': 'IN', 'OUT': 'OUT', 'VDD': 'VDD', 'VSS': 'VSS'},
        {
            'XINV0': [
                {
                    'name': 'XINV0',
                    'lib_name': 'logic_generated',
                    'cell_name': 'inv_array',
                    'params': {},
                    'term_mapping': {},
                },
            ],
            'XINV1': [],
        },
        [],
    )

    interface.instantiate_schematic(
        'logic_generated',
        [inv_content, top_content],
        lib_path=str(output_root),
    )

    library_dir = output_root / 'logic_generated'
    inv_text = (library_dir / 'inv_array.sp').read_text(encoding='utf-8')
    top_text = (
        library_dir / 'buffer_generated.sp'
    ).read_text(encoding='utf-8')

    assert '.SUBCKT inv_array BIAS IN Z VDD VSS' in inv_text
    assert '*.PININFO BIAS:I IN:I Z:O VDD:B VSS:B' in inv_text
    assert (
        "MN0A Z IN VSS VSS nmos4_lvt "
        "w='2*wn' l='lch' nf=4"
    ) in inv_text
    assert "MN0B MID IN VSS VSS nmos4_lvt" in inv_text
    assert 'MP0' not in inv_text
    assert '.ENDS inv_array' in inv_text

    assert '.include "inv_array.sp"' in top_text
    assert '*.PININFO IN:I OUT:O VDD:B VSS:B' in top_text
    assert 'XINV0 IN MID VDD VSS inv_array' in top_text
    assert 'XINV1' not in top_text
    assert '.ENDS buffer_generated' in top_text


def test_cdl_interface_uses_child_subckt_pin_order(tmp_path):
    source = tmp_path / 'hierarchy.cdl'
    source.write_text(
        '.SUBCKT child A B C\n'
        '* @BAG {"lib_name":"logic_templates"}\n'
        '*.PININFO A:B B:B C:B\n'
        '.ENDS child\n'
        '.SUBCKT parent P Q R\n'
        '* @BAG {"lib_name":"logic_templates"}\n'
        '*.PININFO P:B Q:B R:B\n'
        'XU0 netB netC netA child '
        '$ @BAG {"lib_name":"logic_templates",'
        '"terminals":["B","C","A"]}\n'
        '.ENDS parent\n',
        encoding='utf-8',
    )
    output_root = tmp_path / 'generated'
    output_root.mkdir()
    interface = CdlInterface(None, make_config(source, output_root))

    interface.instantiate_schematic(
        'logic_generated',
        [(
            'logic_templates',
            'parent',
            'parent_impl',
            {'P': 'P', 'Q': 'Q', 'R': 'R'},
            {
                'XU0': [{
                    'name': 'XU0',
                    'lib_name': 'logic_generated',
                    'cell_name': 'child_impl',
                    'params': {},
                    'term_mapping': {},
                }],
            },
            [],
        )],
        lib_path=str(output_root),
    )

    output = (
        output_root / 'logic_generated' / 'parent_impl.sp'
    ).read_text(encoding='utf-8')
    assert 'XU0 netA netB netC child_impl' in output


def test_cdl_writer_output_options_and_validation(tmp_path):
    output_root = tmp_path / 'generated_netlists'
    output_root.mkdir()
    config = make_config(DATA_DIR / 'inv.sp', output_root)
    config['cdl']['output'] = {
        'extension': '.hsp',
        'line_length': 40,
    }
    interface = CdlInterface(None, config)
    identity_pins = {
        'IN': 'IN',
        'OUT': 'OUT',
        'VDD': 'VDD',
        'VSS': 'VSS',
    }

    interface.instantiate_schematic(
        'logic_generated',
        [(
            'logic_templates',
            'inv',
            'inv_wrapped',
            identity_pins,
            {},
            [],
        )],
        lib_path=str(output_root),
    )

    output_file = (
        output_root / 'logic_generated' / 'inv_wrapped.hsp'
    )
    output = output_file.read_text(encoding='utf-8')
    assert '\n+ ' in output
    assert '.ENDS inv_wrapped' in output
    dependency_file = Path(str(output_file) + '.deps.json')
    assert dependency_file.is_file()

    interface.delete_cellviews(
        'logic_generated', [('inv_wrapped', 'schematic')]
    )
    assert not output_file.exists()
    assert not dependency_file.exists()

    with pytest.raises(ValueError, match='unknown terminals'):
        interface.instantiate_schematic(
            'logic_generated',
            [(
                'logic_templates',
                'inv',
                'inv_invalid',
                identity_pins,
                {
                    'MN0': [{
                        'name': 'MN0',
                        'lib_name': 'BAG_prim',
                        'cell_name': 'nmos4_lvt',
                        'params': {},
                        'term_mapping': {'BAD': 'net'},
                    }],
                },
                [],
            )],
            lib_path=str(output_root),
        )


def test_cdl_writer_accepts_bag_instance_names_and_pin_symbols(tmp_path):
    source = tmp_path / 'inv.cdl'
    source.write_text(
        '.SUBCKT inv I O VDD VSS\n'
        '* @BAG {"lib_name":"logic_templates"}\n'
        '*.PININFO I:I O:O VDD:B VSS:B\n'
        'MIN0 O I VSS VSS nmos4_fast w=220n l=30n nf=2 '
        '$ @BAG {"lib_name":"BAG_prim"}\n'
        '.ENDS inv\n',
        encoding='utf-8',
    )
    output_root = tmp_path / 'generated'
    output_root.mkdir()
    config = make_config(source, output_root)
    config['cdl']['output'] = {
        'primitive_wrappers': {
            'BAG_prim/nmos4_fast': {
                'terminals': ['B', 'D', 'G', 'S'],
                'body': 'MM0 D G S B nch_ulvt_mac l=l w=w m=1*nf nf=1',
            },
        },
    }
    interface = CdlInterface(None, config)

    interface.instantiate_schematic(
        'logic_generated',
        [(
            'logic_templates',
            'inv',
            'inv_lvs_2x',
            {'I': 'I', 'O': 'O', 'VDD': 'VDD', 'VSS': 'VSS'},
            {
                'IN0': [{
                    'name': 'IN0',
                    'lib_name': 'BAG_prim',
                    'cell_name': 'nmos4_fast',
                    'params': {'nf': 4},
                    'term_mapping': {},
                }],
                'PIN0': [{
                    'name': 'PIN0',
                    'lib_name': 'basic',
                    'cell_name': 'ipin',
                    'params': {},
                    'term_mapping': {},
                }],
            },
            [],
        )],
        lib_path=str(output_root),
    )

    output = (
        output_root / 'logic_generated' / 'inv_lvs_2x.sp'
    ).read_text(encoding='utf-8')
    assert '.SUBCKT nmos4_fast B D G S' in output
    assert 'MM0 D G S B nch_ulvt_mac' in output
    assert 'XIN0 VSS O I VSS nmos4_fast' in output
    assert 'nf=4' in output
    assert 'PIN0' not in output


def test_import_design_library_creates_bag_templates(tmp_path):
    output_root = tmp_path / 'BagModules'
    output_root.mkdir()
    lib_defs = tmp_path / 'bag_libs.def'
    lib_defs.write_text('', encoding='utf-8')

    interface = CdlInterface(
        None, make_config(DATA_DIR / 'buffer2.sp', output_root)
    )
    registry = ClassImporter(str(lib_defs))
    interface.import_design_library(
        'logic_templates', registry, str(output_root)
    )

    package = output_root / 'logic_templates'
    assert (package / '__init__.py').is_file()
    assert (package / 'inv.py').is_file()
    assert (package / 'buffer2.py').is_file()
    assert (package / 'netlist_info' / 'inv.yaml').is_file()
    assert (package / 'netlist_info' / 'buffer2.yaml').is_file()
    inv_template = package / 'cdl_templates' / 'inv.cdl'
    buffer_template = package / 'cdl_templates' / 'buffer2.cdl'
    assert inv_template.is_file()
    assert buffer_template.is_file()

    info = yaml.safe_load(
        (package / 'netlist_info' / 'inv.yaml').read_text(encoding='utf-8')
    )
    assert info['instances']['MN0']['lib_name'] == 'BAG_prim'
    assert info['instances']['MN0']['parameters'] == {
        'w': 'wn',
        'l': 'lch',
        'nf': 'nf',
    }
    assert '* @BAG {"lib_name":"logic_templates"}' in inv_template.read_text(
        encoding='utf-8'
    )
    assert '*.PININFO IN:I OUT:O VDD:B VSS:B' in inv_template.read_text(
        encoding='utf-8'
    )
    assert '$ @BAG {"lib_name":"logic_templates","terminals":' in (
        buffer_template.read_text(encoding='utf-8')
    )
    parsed_template = CdlParser().parse_file(str(buffer_template))
    parsed_buffer = parsed_template.get_cell('buffer2', 'logic_templates')
    assert parsed_buffer.instances['XINV0'].terminals == [
        'IN', 'OUT', 'VDD', 'VSS'
    ]
    assert lib_defs.read_text(encoding='utf-8').strip() == (
        'logic_templates {}'.format(output_root.resolve())
    )

    restored = CdlInterface(
        None,
        dict(
            default_lib_path=str(tmp_path / 'restored_output'),
            schematic=dict(exclude_libraries=['BAG_prim', 'basic', 'analogLib']),
            cdl=dict(template_root=str(output_root), strict=True),
        ),
    )
    assert restored.source_files == []
    assert set(restored.get_cells_in_library('logic_templates')) == {
        'inv', 'buffer2'
    }
    restored_info = yaml.safe_load(
        restored.parse_schematic_template('logic_templates', 'buffer2')
    )
    assert restored_info['instances']['XINV0']['instpins']['IN']['direction'] == (
        'input'
    )
    assert restored_info['instances']['XINV0']['instpins']['OUT']['direction'] == (
        'output'
    )
    restored.instantiate_schematic(
        'logic_restored',
        [(
            'logic_templates',
            'inv',
            'inv_restored',
            {'IN': 'IN', 'OUT': 'OUT', 'VDD': 'VDD', 'VSS': 'VSS'},
            {},
            [],
        )],
        lib_path=str(tmp_path / 'restored_output'),
    )
    assert (
        tmp_path / 'restored_output' / 'logic_restored' / 'inv_restored.sp'
    ).is_file()

    importlib.invalidate_caches()
    module = importlib.import_module('logic_templates.inv')
    assert hasattr(module, 'logic_templates__inv')


def test_import_preserves_existing_python_generator(tmp_path):
    output_root = tmp_path / 'BagModules'
    package = output_root / 'logic_templates'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('', encoding='utf-8')
    inv_file = package / 'inv.py'
    inv_file.write_text('# user implementation\n', encoding='utf-8')
    lib_defs = tmp_path / 'bag_libs.def'
    lib_defs.write_text(
        'logic_templates {}\n'.format(output_root.resolve()),
        encoding='utf-8',
    )

    interface = CdlInterface(
        None, make_config(DATA_DIR / 'inv.sp', output_root)
    )
    registry = ClassImporter(str(lib_defs))
    interface.import_design_library(
        'logic_templates', registry, str(output_root)
    )
    assert inv_file.read_text(encoding='utf-8') == '# user implementation\n'
    assert (package / 'netlist_info' / 'inv.yaml').is_file()


def test_library_definition_rejects_conflicting_path(tmp_path):
    lib_defs = tmp_path / 'bag_libs.def'
    lib_defs.write_text('', encoding='utf-8')
    registry = ClassImporter(str(lib_defs))

    first = tmp_path / 'first'
    second = tmp_path / 'second'
    registry.append_library('logic_templates', str(first))
    registry.append_library('logic_templates', str(first))
    with pytest.raises(ValueError, match='already defined'):
        registry.append_library('logic_templates', str(second))

    assert len(lib_defs.read_text(encoding='utf-8').splitlines()) == 1


def test_import_cdl_cli(tmp_path):
    output_root = tmp_path / 'BagModules'
    lib_defs = tmp_path / 'bag_libs.def'
    command = [
        sys.executable,
        str(PROJECT_ROOT / 'run_scripts' / 'import_cdl.py'),
        str(DATA_DIR / 'buffer2.sp'),
        '--library', 'logic_templates',
        '--output-root', str(output_root),
        '--register', str(lib_defs),
    ]
    result = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    assert 'Imported 2 cell(s)' in result.stdout
    assert (
        output_root / 'logic_templates' / 'netlist_info' / 'buffer2.yaml'
    ).is_file()


def test_cdl_interface_returns_generated_netlist_path(tmp_path):
    output_root = tmp_path / 'generated_netlists'
    output_root.mkdir()
    interface = CdlInterface(
        None, make_config(DATA_DIR / 'inv.sp', output_root)
    )

    output_files = interface.create_implementation(
        'logic_generated',
        [('logic_templates', 'inv', 'inv_generated')],
        [dict(
            pin_map={'IN': 'IN', 'OUT': 'OUT', 'VDD': 'VDD', 'VSS': 'VSS'},
            inst_list=[],
            new_pins=[],
        )],
    )

    assert interface.get_implementation_path(
        'logic_generated', 'inv_generated'
    ) == output_files[0]
    with pytest.raises(ValueError, match='has not been generated'):
        interface.get_implementation_path('logic_generated', 'missing')


def test_cdl_interface_builds_library_qualified_lvcdl_bundle(tmp_path):
    output_root = tmp_path / 'generated_netlists'
    output_root.mkdir()
    interface = CdlInterface(
        None, make_config(DATA_DIR / 'buffer2.sp', output_root)
    )

    interface.create_implementation(
        'logic_generated',
        [('logic_templates', 'inv', 'inv_2x')],
        [dict(
            name='inv_2x',
            pin_map={},
            inst_list=[],
            new_pins=[],
        )],
    )
    interface.create_implementation(
        'adc_generated',
        [('logic_templates', 'buffer2', 'adc_top')],
        [dict(
            name='adc_top',
            pin_map={},
            inst_list={
                'XINV0': [{
                    'name': 'XINV0',
                    'lib_name': 'logic_generated',
                    'cell_name': 'inv_2x',
                    'params': {},
                    'term_mapping': {},
                }],
                'XINV1': [],
            },
            new_pins=[],
        )],
    )

    bundle_top = Path(interface.create_lvcdl_bundle(
        'adc_generated',
        'adc_top',
        tmp_path / 'bundles',
    ))
    bundle_dir = tmp_path / 'bundles' / 'adc_generated' / 'adc_top'
    copied_top = bundle_dir / 'libs' / 'adc_generated' / 'adc_top.sp'
    copied_child = (
        bundle_dir / 'libs' / 'logic_generated' / 'inv_2x.sp'
    )

    assert bundle_top == bundle_dir / 'top.sp'
    assert copied_top.is_file()
    assert copied_child.is_file()
    assert bundle_top.read_text(encoding='utf-8').splitlines() == [
        '* BAG LVCDL library-qualified source bundle',
        '.include "libs/logic_generated/inv_2x.sp"',
        '.include "libs/adc_generated/adc_top.sp"',
    ]
    assert '.include' not in copied_top.read_text(encoding='utf-8').lower()

    manifest = yaml.safe_load(
        (bundle_dir / 'manifest.json').read_text(encoding='utf-8')
    )
    assert manifest['top'] == {
        'lib_name': 'adc_generated',
        'cell_name': 'adc_top',
        'path': 'libs/adc_generated/adc_top.sp',
    }
    assert [
        (entry['lib_name'], entry['cell_name'])
        for entry in manifest['cells']
    ] == [
        ('logic_generated', 'inv_2x'),
        ('adc_generated', 'adc_top'),
    ]
    assert manifest['preflight']['missing_subckts'] == []


def test_cdl_interface_bundle_rejects_missing_cross_library_child(tmp_path):
    output_root = tmp_path / 'generated_netlists'
    output_root.mkdir()
    interface = CdlInterface(
        None, make_config(DATA_DIR / 'buffer2.sp', output_root)
    )
    interface.create_implementation(
        'adc_generated',
        [('logic_templates', 'buffer2', 'adc_top')],
        [dict(
            name='adc_top',
            pin_map={},
            inst_list={
                'XINV0': [{
                    'name': 'XINV0',
                    'lib_name': 'logic_generated',
                    'cell_name': 'missing_inv',
                    'params': {},
                    'term_mapping': {},
                }],
                'XINV1': [],
            },
            new_pins=[],
        )],
    )

    with pytest.raises(
            ValueError,
            match='logic_generated/missing_inv.*has not been generated'):
        interface.create_lvcdl_bundle(
            'adc_generated',
            'adc_top',
            tmp_path / 'bundles',
        )


def test_cdl_interface_bundle_rejects_cross_library_subckt_collision(
        tmp_path):
    output_root = tmp_path / 'generated_netlists'
    output_root.mkdir()
    interface = CdlInterface(
        None, make_config(DATA_DIR / 'buffer2.sp', output_root)
    )
    interface.create_implementation(
        'first_generated',
        [('logic_templates', 'inv', 'shared')],
        [dict(
            name='shared',
            pin_map={},
            inst_list=[],
            new_pins=[],
        )],
    )
    interface.create_implementation(
        'second_generated',
        [('logic_templates', 'inv', 'shared')],
        [dict(
            name='shared',
            pin_map={'OUT': 'Z'},
            inst_list=[],
            new_pins=[],
        )],
    )
    interface.create_implementation(
        'top_generated',
        [('logic_templates', 'buffer2', 'collision_top')],
        [dict(
            name='collision_top',
            pin_map={},
            inst_list={
                'XINV0': [{
                    'name': 'XINV0',
                    'lib_name': 'first_generated',
                    'cell_name': 'shared',
                    'params': {},
                    'term_mapping': {},
                }],
                'XINV1': [{
                    'name': 'XINV1',
                    'lib_name': 'second_generated',
                    'cell_name': 'shared',
                    'params': {},
                    'term_mapping': {},
                }],
            },
            new_pins=[],
        )],
    )

    with pytest.raises(
            ValueError,
            match=r'Conflicting \.SUBCKT shared definitions'):
        interface.create_lvcdl_bundle(
            'top_generated',
            'collision_top',
            tmp_path / 'bundles',
        )
