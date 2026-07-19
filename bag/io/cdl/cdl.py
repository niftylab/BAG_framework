# -*- coding: utf-8 -*-

"""Reader for preprocessed CDL schematic template netlists."""

from collections import OrderedDict
import shlex

from .annotations import (
    merge_metadata,
    parse_full_line_annotation,
    parse_pin_info,
    split_inline_annotation,
)
from .core import CdlParseError, NetlistCell, NetlistInstance, NetlistLibrary


_FIXED_TERMINALS = {
    'M': ['D', 'G', 'S', 'B'],
    'R': ['PLUS', 'MINUS'],
    'C': ['PLUS', 'MINUS'],
    'D': ['PLUS', 'MINUS'],
}


def _strip_quotes(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('\'', '"'):
        return value[1:-1]
    return value


def _tokenize(statement, source, line_no):
    lexer = shlex.shlex(statement, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ''
    try:
        return list(lexer)
    except ValueError as err:
        raise CdlParseError(
            'Cannot tokenize CDL statement: {}'.format(err),
            source=source,
            line_no=line_no,
        )


def _split_positionals_and_parameters(tokens):
    positionals = []
    parameters = OrderedDict()
    in_parameters = False
    for token in tokens:
        upper = token.upper()
        if upper in ('PARAMS:', 'PARAM:'):
            in_parameters = True
            continue
        if '=' in token:
            in_parameters = True
            name, value = token.split('=', 1)
            if not name:
                raise ValueError('Parameter name cannot be empty.')
            if name in parameters:
                raise ValueError('Duplicate parameter {!r}.'.format(name))
            parameters[name] = _strip_quotes(value)
        elif in_parameters:
            raise ValueError(
                'Expected name=value parameter, got {!r}.'.format(token)
            )
        else:
            positionals.append(token)
    return positionals, parameters


class CdlParser(object):
    """Parse annotated, preprocessed CDL template netlists."""

    _CELL_METADATA_KEYS = frozenset(['lib_name'])
    _INSTANCE_METADATA_KEYS = frozenset(['lib_name', 'terminals'])
    _SUPPORTED_ELEMENTS = frozenset(['M', 'X', 'R', 'C', 'D'])

    def __init__(self, strict=True):
        self.strict = strict

    def parse_file(self, file_name):
        """Parse a netlist file."""
        with open(file_name, 'r') as stream:
            return self.parse(stream.read(), source=file_name)

    def parse(self, text, source='<string>'):
        """Parse netlist text and return a :class:`NetlistLibrary`."""
        self._source = source
        self._library = NetlistLibrary(source=source)
        self._parsed_cells = []
        self._current_cell = None
        self._pending_code = None
        self._pending_metadata = {}
        self._pending_line = None

        for line_no, raw_line in enumerate(text.splitlines(), 1):
            stripped = raw_line.strip()
            pin_info = parse_pin_info(
                raw_line, source=source, line_no=line_no
            )
            if pin_info is not None:
                self._flush_pending()
                self._apply_pin_info(pin_info, line_no)
                continue

            full_annotation = parse_full_line_annotation(
                raw_line, source=source, line_no=line_no
            )
            if full_annotation is not None:
                self._flush_pending()
                self._apply_cell_metadata(full_annotation, line_no)
                continue

            if not stripped or stripped.startswith('*'):
                self._flush_pending()
                continue

            is_continuation = raw_line.lstrip().startswith('+')
            code_line = raw_line.lstrip()[1:] if is_continuation else raw_line
            code, metadata = split_inline_annotation(
                code_line, source=source, line_no=line_no
            )

            if is_continuation:
                if self._pending_code is None:
                    raise CdlParseError(
                        'Continuation line has no preceding statement.',
                        source=source,
                        line_no=line_no,
                    )
                if code.strip():
                    self._pending_code += ' ' + code.strip()
                if metadata:
                    merge_metadata(
                        self._pending_metadata,
                        metadata,
                        source=source,
                        line_no=line_no,
                    )
            else:
                self._flush_pending()
                self._pending_code = code.strip()
                self._pending_metadata = dict(metadata or {})
                self._pending_line = line_no

        self._flush_pending()
        if self._current_cell is not None:
            raise CdlParseError(
                'Missing .ENDS for subcircuit {!r}.'.format(
                    self._current_cell.cell_name
                ),
                source=source,
                line_no=self._current_cell.line_no,
            )

        self._finalize_library()
        return self._library

    def _flush_pending(self):
        if self._pending_code:
            self._parse_statement(
                self._pending_code,
                self._pending_metadata,
                self._pending_line,
            )
        self._pending_code = None
        self._pending_metadata = {}
        self._pending_line = None

    def _parse_statement(self, statement, metadata, line_no):
        tokens = _tokenize(statement, self._source, line_no)
        if not tokens:
            return

        keyword = tokens[0].upper()
        if keyword == '.SUBCKT':
            self._start_subckt(tokens, metadata, line_no)
        elif keyword == '.ENDS':
            self._end_subckt(tokens, metadata, line_no)
        elif keyword.startswith('.'):
            if metadata:
                raise CdlParseError(
                    'BAG annotation is not supported on directive {}.'.format(tokens[0]),
                    source=self._source,
                    line_no=line_no,
                )
        elif self._current_cell is not None:
            self._parse_instance(tokens, metadata, line_no)
        elif metadata:
            raise CdlParseError(
                'Instance BAG annotation appears outside a .SUBCKT.',
                source=self._source,
                line_no=line_no,
            )

    def _start_subckt(self, tokens, metadata, line_no):
        if self._current_cell is not None:
            raise CdlParseError(
                'Nested .SUBCKT definitions are not supported.',
                source=self._source,
                line_no=line_no,
            )
        if len(tokens) < 2:
            raise CdlParseError(
                '.SUBCKT requires a cell name.',
                source=self._source,
                line_no=line_no,
            )
        self._validate_metadata_keys(
            metadata, self._CELL_METADATA_KEYS, 'cell', line_no
        )
        try:
            pins, parameters = _split_positionals_and_parameters(tokens[2:])
        except ValueError as err:
            raise CdlParseError(
                str(err), source=self._source, line_no=line_no
            )

        self._current_cell = NetlistCell(
            tokens[1],
            pins,
            parameters=parameters,
            lib_name=metadata.get('lib_name', ''),
            metadata=metadata,
            line_no=line_no,
        )

    def _end_subckt(self, tokens, metadata, line_no):
        if metadata:
            raise CdlParseError(
                'BAG annotation is not supported on .ENDS.',
                source=self._source,
                line_no=line_no,
            )
        if self._current_cell is None:
            raise CdlParseError(
                '.ENDS appears outside a .SUBCKT.',
                source=self._source,
                line_no=line_no,
            )
        if len(tokens) > 1 and tokens[1] != self._current_cell.cell_name:
            raise CdlParseError(
                '.ENDS name {!r} does not match .SUBCKT {!r}.'.format(
                    tokens[1], self._current_cell.cell_name
                ),
                source=self._source,
                line_no=line_no,
            )
        self._parsed_cells.append(self._current_cell)
        self._current_cell = None

    def _apply_cell_metadata(self, metadata, line_no):
        if self._current_cell is None:
            raise CdlParseError(
                '* @BAG annotation appears outside a .SUBCKT.',
                source=self._source,
                line_no=line_no,
            )
        self._validate_metadata_keys(
            metadata, self._CELL_METADATA_KEYS, 'cell', line_no
        )
        merged = dict(self._current_cell.metadata)
        merge_metadata(
            merged, metadata, source=self._source, line_no=line_no
        )
        self._current_cell.metadata = merged
        if 'lib_name' in metadata:
            if self._current_cell.lib_name and (
                    self._current_cell.lib_name != metadata['lib_name']):
                raise CdlParseError(
                    'Conflicting BAG annotation value for lib_name.',
                    source=self._source,
                    line_no=line_no,
                )
            self._current_cell.lib_name = metadata['lib_name']

    def _apply_pin_info(self, pin_directions, line_no):
        if self._current_cell is None:
            raise CdlParseError(
                '*.PININFO appears outside a .SUBCKT.',
                source=self._source,
                line_no=line_no,
            )
        if self._current_cell.has_pin_info:
            raise CdlParseError(
                'Duplicate *.PININFO annotation.',
                source=self._source,
                line_no=line_no,
            )
        try:
            self._current_cell.set_pin_directions(pin_directions)
        except ValueError as err:
            raise CdlParseError(
                str(err), source=self._source, line_no=line_no
            )

        missing_pins = [
            pin for pin in self._current_cell.pins
            if pin not in pin_directions
        ]
        if missing_pins:
            raise CdlParseError(
                'PININFO is missing subcircuit pins: {}.'.format(
                    ', '.join(missing_pins)
                ),
                source=self._source,
                line_no=line_no,
            )
        self._current_cell.has_pin_info = True

    def _parse_instance(self, tokens, metadata, line_no):
        element_type = tokens[0][0].upper()
        if element_type not in self._SUPPORTED_ELEMENTS:
            if self.strict:
                raise CdlParseError(
                    'Unsupported CDL element type {!r}.'.format(element_type),
                    source=self._source,
                    line_no=line_no,
                )
            return
        self._validate_metadata_keys(
            metadata, self._INSTANCE_METADATA_KEYS, 'instance', line_no
        )

        try:
            positionals, parameters = _split_positionals_and_parameters(tokens[1:])
        except ValueError as err:
            raise CdlParseError(
                str(err), source=self._source, line_no=line_no
            )
        node_count = 4 if element_type == 'M' else 2
        if element_type == 'X':
            if len(positionals) < 2:
                self._raise_bad_instance(tokens[0], line_no)
            nodes = positionals[:-1]
            cell_name = positionals[-1]
            terminals = metadata.get('terminals')
        else:
            if len(positionals) != node_count + 1:
                self._raise_bad_instance(tokens[0], line_no)
            nodes = positionals[:node_count]
            cell_name = positionals[node_count]
            terminals = metadata.get(
                'terminals', _FIXED_TERMINALS[element_type]
            )

        if terminals is not None and len(terminals) != len(nodes):
            raise CdlParseError(
                'Instance {!r} has {} nodes but {} terminal names.'.format(
                    tokens[0], len(nodes), len(terminals)
                ),
                source=self._source,
                line_no=line_no,
            )

        instance = NetlistInstance(
            tokens[0],
            element_type,
            cell_name,
            nodes,
            parameters=parameters,
            lib_name=metadata.get('lib_name', ''),
            terminals=terminals,
            metadata=metadata,
            line_no=line_no,
        )
        try:
            self._current_cell.add_instance(instance)
        except ValueError as err:
            raise CdlParseError(
                str(err), source=self._source, line_no=line_no
            )

    def _raise_bad_instance(self, instance_name, line_no):
        raise CdlParseError(
            'Malformed preprocessed CDL instance {!r}.'.format(instance_name),
            source=self._source,
            line_no=line_no,
        )

    def _validate_metadata_keys(self, metadata, allowed, scope, line_no):
        unknown = sorted(set(metadata.keys()) - allowed)
        if unknown:
            raise CdlParseError(
                'Unsupported {} BAG annotation keys: {}.'.format(
                    scope, ', '.join(unknown)
                ),
                source=self._source,
                line_no=line_no,
            )

    def _finalize_library(self):
        by_cell_name = {}
        for cell in self._parsed_cells:
            if self.strict and cell.pins and not cell.has_pin_info:
                raise CdlParseError(
                    'Subcircuit {!r} has no PININFO annotation.'.format(
                        cell.cell_name
                    ),
                    source=self._source,
                    line_no=cell.line_no,
                )
            if not cell.lib_name:
                raise CdlParseError(
                    'Subcircuit {!r} has no BAG lib_name annotation.'.format(
                        cell.cell_name
                    ),
                    source=self._source,
                    line_no=cell.line_no,
                )
            by_cell_name.setdefault(cell.cell_name, []).append(cell)
            try:
                self._library.add_cell(cell)
            except ValueError as err:
                raise CdlParseError(
                    str(err), source=self._source, line_no=cell.line_no
                )

        for cell in self._parsed_cells:
            for instance in cell.instances.values():
                if instance.element_type == 'X' and instance.terminals is None:
                    candidates = by_cell_name.get(instance.cell_name, [])
                    if instance.lib_name:
                        candidates = [
                            candidate for candidate in candidates
                            if candidate.lib_name == instance.lib_name
                        ]
                    if len(candidates) != 1:
                        raise CdlParseError(
                            'Cannot resolve terminal order for instance {!r} '
                            'referencing {!r}.'.format(
                                instance.name, instance.cell_name
                            ),
                            source=self._source,
                            line_no=instance.line_no,
                        )
                    child = candidates[0]
                    instance.terminals = list(child.pins)
                    instance.terminal_directions = dict(child.pin_directions)
                    if not instance.lib_name:
                        instance.lib_name = child.lib_name
                elif not instance.lib_name:
                    if instance.element_type == 'X':
                        instance.lib_name = cell.lib_name
                    elif self.strict:
                        raise CdlParseError(
                            'Instance {!r} has no BAG lib_name annotation.'.format(
                                instance.name
                            ),
                            source=self._source,
                            line_no=instance.line_no,
                        )
