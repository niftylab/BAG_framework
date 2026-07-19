from pathlib import Path

import pytest

from bag.io.cdl import (
    CdlParseError,
    CdlParser,
    CdlTemplateWriter,
    load_schematic_library,
)


DATA_DIR = Path(__file__).parent / 'data'


def test_parse_inverter():
    library = CdlParser().parse_file(str(DATA_DIR / 'inv.sp'))
    assert library.get_cells_in_library('logic_templates') == ['inv']

    inv = library.get_cell('inv', 'logic_templates')
    assert inv.pins == ['IN', 'OUT', 'VDD', 'VSS']
    assert inv.pin_directions == {
        'IN': 'input',
        'OUT': 'output',
        'VDD': 'inputOutput',
        'VSS': 'inputOutput',
    }
    assert inv.parameters == {
        'wn': '1u',
        'wp': '2u',
        'lch': '45n',
        'nf': '2',
    }

    mn0 = inv.instances['MN0']
    assert mn0.lib_name == 'BAG_prim'
    assert mn0.cell_name == 'nmos4_lvt'
    assert mn0.connections == {
        'D': 'OUT',
        'G': 'IN',
        'S': 'VSS',
        'B': 'VSS',
    }
    assert mn0.parameters == {'w': 'wn', 'l': 'lch', 'nf': 'nf'}

    schematic_info = inv.to_schematic_info()
    assert schematic_info['instances']['MP0']['cell_name'] == 'pmos4_lvt'
    assert schematic_info['instances']['MP0']['parameters']['w'] == 'wp'


def test_parse_hierarchy_and_continuations():
    library = CdlParser().parse_file(str(DATA_DIR / 'buffer2.sp'))
    assert library.get_cells_in_library('logic_templates') == ['inv', 'buffer2']

    inv = library.get_cell('inv', 'logic_templates')
    assert inv.instances['MN0'].parameters == {
        'w': 'wn',
        'l': 'lch',
        'nf': 'nf',
    }
    assert inv.instances['MP0'].parameters['m'] == '1'

    buffer2 = library.get_cell('buffer2', 'logic_templates')
    xinv1 = buffer2.instances['XINV1']
    assert xinv1.cell_name == 'inv'
    assert xinv1.terminals == ['IN', 'OUT', 'VDD', 'VSS']
    assert xinv1.connections == {
        'IN': 'MID',
        'OUT': 'OUT',
        'VDD': 'VDD',
        'VSS': 'VSS',
    }
    assert xinv1.terminal_directions['IN'] == 'input'
    assert xinv1.terminal_directions['OUT'] == 'output'
    assert xinv1.parameters['wn'] == '2*wn'
    assert xinv1.parameters['wp'] == '2*wp'


def test_inline_subckt_annotation():
    text = """
.SUBCKT passthrough A B $ @BAG {"lib_name":"test_templates"}
*.PININFO A:I B:O
R0 A B res_ideal r=1k $ @BAG {"lib_name":"BAG_prim"}
.ENDS passthrough
"""
    cell = CdlParser().parse(text).get_cell(
        'passthrough', 'test_templates'
    )
    assert cell.pin_directions == {'A': 'input', 'B': 'output'}
    assert cell.instances['R0'].cell_name == 'res_ideal'


def test_pin_info_is_required_in_strict_mode():
    text = """
.SUBCKT passthrough A B
* @BAG {"lib_name":"test_templates"}
*.PININFO A:I B:O
R0 A B res_ideal r=1k $ @BAG {"lib_name":"BAG_prim"}
.ENDS passthrough
"""
    cell = CdlParser().parse(text).get_cell(
        'passthrough', 'test_templates'
    )
    assert cell.pin_directions == {'A': 'input', 'B': 'output'}

    missing_pin_info = text.replace('*.PININFO A:I B:O\n', '')
    with pytest.raises(CdlParseError, match='has no PININFO'):
        CdlParser().parse(missing_pin_info, source='missing_pininfo.sp')


def test_cell_annotation_rejects_pin_directions():
    text = """
.SUBCKT passthrough A B
* @BAG {"lib_name":"test_templates","pin_directions":{"A":"input"}}
*.PININFO A:I B:O
.ENDS passthrough
"""
    with pytest.raises(CdlParseError, match='Unsupported cell BAG annotation keys'):
        CdlParser().parse(text, source='invalid_pin_directions.sp')


@pytest.mark.parametrize(
    'text, expected',
    [
        (
            '.SUBCKT inv A B\n'
            '* @BAG {"lib_name":"logic_templates"}\n'
            '*.PININFO BAD:I B:O\n'
            '.ENDS inv\n',
            'unknown pin',
        ),
        (
            '.SUBCKT top A B\n'
            '* @BAG {"lib_name":"logic_templates"}\n'
            '*.PININFO A:I B:O\n'
            'X0 A B missing\n'
            '.ENDS top\n',
            'Cannot resolve terminal order',
        ),
        (
            '.SUBCKT inv A B\n'
            '*.PININFO A:I B:O\n'
            'R0 A B res_ideal r=1k $ @BAG {"lib_name":"BAG_prim"}\n'
            '.ENDS inv\n',
            'has no BAG lib_name',
        ),
    ],
)
def test_strict_validation_errors(text, expected):
    with pytest.raises(CdlParseError) as err:
        CdlParser().parse(text, source='invalid.sp')
    assert expected in str(err.value)
    assert 'invalid.sp:' in str(err.value)


def test_export_bag_netlist_info_as_annotated_cdl(tmp_path):
    info_dir = tmp_path / 'netlist_info'
    info_dir.mkdir()
    (info_dir / 'inv.yaml').write_text(
        '''lib_name: logic_templates
cell_name: inv
pins: [I, O, VDD, VSS]
pin_directions: [input, output, inputOutput, inputOutput]
instances:
  IN0:
    lib_name: BAG_prim
    cell_name: nmos4_fast
    instpins:
      D: {net_name: O, direction: inputOutput}
      G: {net_name: I, direction: inputOutput}
      S: {net_name: VSS, direction: inputOutput}
      B: {net_name: VSS, direction: inputOutput}
    parameters: {w: 220n, l: 40n}
  PIN0:
    lib_name: basic
    cell_name: ipin
    instpins: {}
    parameters: {}
''',
        encoding='utf-8',
    )
    (info_dir / 'top.yaml').write_text(
        '''lib_name: logic_templates
cell_name: top
pins: [I, O, VDD, VSS]
instances:
  I0:
    lib_name: logic_templates
    cell_name: inv
    instpins:
      I: {net_name: I, direction: input}
      O: {net_name: O, direction: output}
      VDD: {net_name: VDD, direction: inputOutput}
      VSS: {net_name: VSS, direction: inputOutput}
    parameters: {}
''',
        encoding='utf-8',
    )

    cells = load_schematic_library(info_dir)
    assert [cell.cell_name for cell in cells] == ['inv', 'top']
    assert cells[1].pin_directions == {
        'I': 'input',
        'O': 'output',
        'VDD': 'inputOutput',
        'VSS': 'inputOutput',
    }
    writer = CdlTemplateWriter()
    output_dir = tmp_path / 'cdl_templates'
    for cell in cells:
        writer.write_cell(str(output_dir), cell)

    inv_text = (output_dir / 'inv.cdl').read_text(encoding='utf-8')
    top_text = (output_dir / 'top.cdl').read_text(encoding='utf-8')
    assert 'MIN0 O I VSS VSS nmos4_fast' in inv_text
    assert 'PIN0' not in inv_text
    assert '$ @BAG {"lib_name":"logic_templates","terminals":' in top_text
    assert CdlParser().parse_file(str(output_dir / 'inv.cdl')).get_cell(
        'inv', 'logic_templates'
    ).pins == ['I', 'O', 'VDD', 'VSS']
    assert CdlParser().parse_file(str(output_dir / 'top.cdl')).get_cell(
        'top', 'logic_templates'
    ).instances['XI0'].terminals == ['I', 'O', 'VDD', 'VSS']
