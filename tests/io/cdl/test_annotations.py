import pytest

from bag.io.cdl.annotations import (
    merge_metadata,
    parse_full_line_annotation,
    parse_pin_info,
    split_inline_annotation,
)
from bag.io.cdl.core import CdlParseError


def test_full_line_annotation():
    metadata = parse_full_line_annotation(
        '  * @BAG {"lib_name":"logic_templates"}',
        source='sample.sp',
        line_no=4,
    )
    assert metadata == {'lib_name': 'logic_templates'}
    assert parse_full_line_annotation('* ordinary comment') is None


def test_inline_annotation_and_ordinary_comment():
    code, metadata = split_inline_annotation(
        'MN0 D G S B nmos4_lvt w=1u $ @BAG {"lib_name":"BAG_prim"}'
    )
    assert code == 'MN0 D G S B nmos4_lvt w=1u'
    assert metadata == {'lib_name': 'BAG_prim'}

    code, metadata = split_inline_annotation(
        'MP0 D G S B pmos4_lvt w=2u $ ordinary comment'
    )
    assert code == 'MP0 D G S B pmos4_lvt w=2u'
    assert metadata is None


def test_dollar_inside_expression_is_not_a_comment():
    code, metadata = split_inline_annotation(
        "X0 A B child expression='left$right'"
    )
    assert code == "X0 A B child expression='left$right'"
    assert metadata is None


def test_invalid_annotation_reports_location():
    with pytest.raises(CdlParseError) as err:
        parse_full_line_annotation(
            '* @BAG {bad json}', source='broken.sp', line_no=12
        )
    assert 'broken.sp:12:' in str(err.value)


def test_pin_info_annotation():
    directions = parse_pin_info(
        '*.PININFO IN:I OUT:O VDD:B VSS:B',
        source='sample.sp',
        line_no=5,
    )
    assert directions == {
        'IN': 'input',
        'OUT': 'output',
        'VDD': 'inputOutput',
        'VSS': 'inputOutput',
    }
    assert parse_pin_info('* ordinary comment') is None

    with pytest.raises(CdlParseError, match='Invalid PININFO entry'):
        parse_pin_info('*.PININFO IN:I OUT', source='sample.sp', line_no=6)

    with pytest.raises(CdlParseError, match='Duplicate PININFO'):
        parse_pin_info('*.PININFO IN:I IN:O', source='sample.sp', line_no=7)


def test_metadata_merge_rejects_conflict():
    metadata = {'options': {'mode': 'strict'}}
    merge_metadata(
        metadata,
        {'options': {'line_length': 100}},
    )
    assert metadata['options'] == {
        'mode': 'strict',
        'line_length': 100,
    }

    with pytest.raises(CdlParseError):
        merge_metadata(metadata, {'options': {'mode': 'permissive'}})
