# -*- coding: utf-8 -*-

"""CDL serialization for concrete BAG schematic implementations."""

from collections import OrderedDict
import json
import os
import re

from ..file import write_file


_NUMBER_RE = re.compile(
    r'^[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))'
    r'(?:e[+-]?\d+|[a-z][a-z0-9]*)?$',
    re.IGNORECASE,
)
_BUS_RANGE_RE = re.compile(
    r'^(?P<base>.+)<(?P<first>-?\d+):(?P<last>-?\d+)>$'
)
_REPLICATION_RE = re.compile(
    r'^<\*(?P<count>\d+)>(?P<value>.+)$'
)
_PININFO_CODES = {
    'input': 'I',
    'output': 'O',
    'inputOutput': 'B',
}


def _ordered_items(value, description):
    if hasattr(value, 'items'):
        items = list(value.items())
    else:
        items = list(value or [])

    answer = OrderedDict()
    for key, item in items:
        if key in answer:
            raise ValueError(
                'Duplicate {} key {!r}.'.format(description, key)
            )
        answer[key] = item
    return answer


def _validate_identifier(value, description):
    if not isinstance(value, str) or not value:
        raise ValueError('{} must be a non-empty string.'.format(description))
    if any(character.isspace() for character in value):
        raise ValueError(
            '{} {!r} cannot contain whitespace.'.format(description, value)
        )
    if '\n' in value or '\r' in value:
        raise ValueError(
            '{} {!r} cannot contain a newline.'.format(description, value)
        )
    return value


def _format_parameter_value(value):
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, (int, float)):
        return repr(value)
    if not isinstance(value, str):
        raise ValueError(
            'Unsupported CDL parameter value {!r}.'.format(value)
        )

    value = value.strip()
    if not value:
        return "''"
    if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ('\'', '"')):
        return value
    if _NUMBER_RE.match(value):
        return value
    if '\'' in value or '\n' in value or '\r' in value:
        raise ValueError(
            'CDL expression contains an unsupported quote or newline: '
            '{!r}.'.format(value)
        )
    return "'{}'".format(value)


def _expand_bus_name(value):
    """Expand one trailing CDBA bus range while preserving its direction."""
    value = _validate_identifier(value, 'CDL name')
    match = _BUS_RANGE_RE.match(value)
    if match is None:
        return [value]

    first = int(match.group('first'))
    last = int(match.group('last'))
    step = 1 if last >= first else -1
    base = match.group('base')
    return [
        '{}<{}>'.format(base, index)
        for index in range(first, last + step, step)
    ]


def _expand_net_expression(value):
    """Expand CDBA concatenation, replication, and simple bus ranges."""
    value = _validate_identifier(value, 'Net name')
    answer = []
    for part in value.split(','):
        if not part:
            raise ValueError(
                'Net expression {!r} contains an empty concatenation item.'
                .format(value)
            )
        replication = _REPLICATION_RE.match(part)
        if replication is not None:
            count = int(replication.group('count'))
            expanded = _expand_net_expression(replication.group('value'))
            for _ in range(count):
                answer.extend(expanded)
        else:
            answer.extend(_expand_bus_name(part))
    return answer


class CdlWriter(object):
    """Apply BAG schematic changes and write concrete CDL subcircuits."""

    def __init__(self, extension='.sp', line_length=100,
                 primitive_wrappers=None):
        if (
                not isinstance(extension, str)
                or not extension.startswith('.')
                or os.path.basename(extension) != extension):
            raise ValueError(
                'CDL output extension must be a filename extension.'
            )
        if not isinstance(line_length, int) or line_length < 40:
            raise ValueError('CDL line_length must be at least 40.')
        self.extension = extension
        self.line_length = line_length
        self.primitive_wrappers = dict(primitive_wrappers or {})

    def write_cell(self, output_dir, impl_lib, template_cell, impl_cell,
                   change):
        """Write one implemented subcircuit and return its absolute path."""
        _validate_identifier(impl_lib, 'Implementation library name')
        _validate_identifier(impl_cell, 'Implementation cell name')
        if os.path.basename(impl_cell) != impl_cell:
            raise ValueError(
                'Implementation cell name cannot contain path separators.'
            )
        if change.get('name', impl_cell) != impl_cell:
            raise ValueError(
                'Implementation cell name {!r} does not match change name '
                '{!r}.'.format(impl_cell, change.get('name'))
            )

        pins, pin_info = self._get_pins(template_cell, change)
        instance_lines, child_cells, dependencies = self._get_instances(
            impl_lib, impl_cell, template_cell, change
        )

        lines = [
            '* BAG generated CDL implementation',
            '* library: {}'.format(impl_lib),
            '* template: {}/{}'.format(
                template_cell.lib_name, template_cell.cell_name
            ),
        ]
        if self.primitive_wrappers:
            lines.append('')
            for key, wrapper in self.primitive_wrappers.items():
                wrapper_cell = key.split('/', 1)[-1]
                terminals = wrapper.get('terminals', [])
                wrapper_tokens = ['.SUBCKT', wrapper_cell]
                wrapper_tokens.extend(terminals)
                parameters = _ordered_items(
                    wrapper.get('parameters', []),
                    'primitive wrapper parameters',
                )
                if parameters:
                    wrapper_tokens.append('PARAMS:')
                    wrapper_tokens.extend(
                        '{}={}'.format(
                            name, _format_parameter_value(value)
                        )
                        for name, value in parameters.items()
                    )
                lines.extend(self._wrap_tokens(wrapper_tokens))
                lines.append(wrapper['body'])
                lines.append('.ENDS {}'.format(wrapper_cell))
                lines.append('')
        for child_cell in child_cells:
            lines.append(
                '.include "{}{}"'.format(child_cell, self.extension)
            )
        if child_cells:
            lines.append('')

        subckt_tokens = ['.SUBCKT', impl_cell] + pins
        if template_cell.parameters:
            subckt_tokens.append('PARAMS:')
            subckt_tokens.extend(
                '{}={}'.format(name, _format_parameter_value(value))
                for name, value in template_cell.parameters.items()
            )
        lines.extend(self._wrap_tokens(subckt_tokens))
        lines.append('*.PININFO {}'.format(' '.join(pin_info)))
        lines.extend(instance_lines)
        lines.append('.ENDS {}'.format(impl_cell))
        lines.append('')

        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.abspath(
            os.path.join(output_dir, impl_cell + self.extension)
        )
        write_file(output_file, '\n'.join(lines), mkdir=False)
        dependency_file = self.get_dependency_path(output_file)
        write_file(
            dependency_file,
            json.dumps(
                {
                    'schema': 'bag.cdl.dependencies.v1',
                    'lib_name': impl_lib,
                    'cell_name': impl_cell,
                    'source': os.path.basename(output_file),
                    'dependencies': [
                        {
                            'lib_name': lib_name,
                            'cell_name': cell_name,
                        }
                        for lib_name, cell_name in dependencies
                    ],
                },
                indent=2,
                sort_keys=True,
            ) + '\n',
            mkdir=False,
        )
        return output_file

    @staticmethod
    def get_dependency_path(output_file):
        """Return the dependency sidecar path for one generated CDL file."""
        return output_file + '.deps.json'

    def _get_pins(self, template_cell, change):
        pin_map = _ordered_items(change.get('pin_map', []), 'pin mapping')
        unknown_pins = sorted(set(pin_map.keys()) - set(template_cell.pins))
        if unknown_pins:
            raise ValueError(
                'Pin mapping references unknown template pins: {}.'
                .format(', '.join(unknown_pins))
            )

        pin_entries = []
        for new_pin in change.get('new_pins', []):
            if len(new_pin) != 2:
                raise ValueError('New pin entry cannot be empty.')
            pin_name = _validate_identifier(new_pin[0], 'New pin name')
            direction_code = self._get_pin_info_code(
                new_pin[1], pin_name
            )
            pin_entries.extend(
                (expanded_pin, direction_code)
                for expanded_pin in _expand_bus_name(pin_name)
            )
        for old_pin in template_cell.pins:
            mapped_pin = pin_map.get(old_pin, old_pin)
            if mapped_pin:
                pin_name = _validate_identifier(mapped_pin, 'Mapped pin name')
                direction_code = self._get_pin_info_code(
                    template_cell.pin_directions[old_pin], pin_name
                )
                pin_entries.extend(
                    (expanded_pin, direction_code)
                    for expanded_pin in _expand_bus_name(pin_name)
                )

        pins = [pin_name for pin_name, _ in pin_entries]

        duplicates = sorted(
            pin for pin in set(pins) if pins.count(pin) > 1
        )
        if duplicates:
            raise ValueError(
                'Implemented subcircuit has duplicate pins: {}.'
                .format(', '.join(duplicates))
            )
        return pins, [
            '{}:{}'.format(pin_name, direction_code)
            for pin_name, direction_code in pin_entries
        ]

    @staticmethod
    def _get_pin_info_code(direction, pin_name):
        if direction not in _PININFO_CODES:
            raise ValueError(
                'Pin {!r} has unsupported BAG direction {!r}.'.format(
                    pin_name, direction
                )
            )
        return _PININFO_CODES[direction]

    def _get_instances(self, impl_lib, impl_cell, template_cell, change):
        raw_instance_changes = _ordered_items(
            change.get('inst_list', []), 'instance change'
        )
        instance_changes = OrderedDict()
        unknown_instances = []
        for change_name, replacements in raw_instance_changes.items():
            template_name = change_name
            if template_name not in template_cell.instances:
                aliases = [
                    name for name in template_cell.instances
                    if name[1:] == change_name
                ]
                if len(aliases) == 1:
                    template_name = aliases[0]
                elif self._is_bag_schematic_symbol(change_name, replacements):
                    continue
                else:
                    unknown_instances.append(change_name)
                    continue

            element_type = template_cell.instances[template_name].element_type
            normalized_replacements = []
            for replacement in replacements:
                replacement = dict(replacement)
                name = replacement.get('name')
                if name and not name.upper().startswith(element_type):
                    replacement['name'] = element_type + name
                normalized_replacements.append(replacement)
            instance_changes[template_name] = normalized_replacements

        unknown_instances = sorted(unknown_instances)
        if unknown_instances:
            raise ValueError(
                'Instance changes reference unknown template instances: {}.'
                .format(', '.join(unknown_instances))
            )

        lines = []
        child_cells = OrderedDict()
        dependencies = OrderedDict()
        for old_name, template_instance in template_cell.instances.items():
            replacement_list = instance_changes.get(old_name)
            if replacement_list is None:
                replacement_list = [
                    dict(
                        name=old_name,
                        lib_name=template_instance.lib_name,
                        cell_name=template_instance.cell_name,
                        params=[],
                        term_mapping=[],
                    )
                ]

            for replacement in replacement_list:
                statement, child_cell, dependency = self._render_instance(
                    impl_lib, impl_cell, template_instance, replacement
                )
                lines.extend(statement)
                if child_cell is not None:
                    child_cells[child_cell] = None
                if dependency is not None:
                    dependencies[dependency] = None

        return (
            lines,
            list(child_cells.keys()),
            list(dependencies.keys()),
        )

    @staticmethod
    def _is_bag_schematic_symbol(change_name, replacements):
        """Return True for schematic-only pin and no-connect instances."""
        if not replacements:
            return change_name.upper().startswith('PIN')
        schematic_cells = {'ipin', 'opin', 'iopin', 'sympin', 'noConn'}
        return all(
            replacement.get('lib_name') == 'basic'
            and replacement.get('cell_name') in schematic_cells
            for replacement in replacements
        )

    def _render_instance(self, impl_lib, impl_cell, template_instance,
                         replacement):
        name = _validate_identifier(
            replacement.get('name'), 'Instance name'
        )
        if name[0].upper() != template_instance.element_type:
            raise ValueError(
                'Implemented instance {!r} must retain CDL element prefix '
                '{}.'.format(name, template_instance.element_type)
            )
        cell_name = _validate_identifier(
            replacement.get('cell_name'), 'Instance cell name'
        )
        lib_name = _validate_identifier(
            replacement.get('lib_name'), 'Instance library name'
        )

        connections = OrderedDict(template_instance.connections)
        term_mapping = _ordered_items(
            replacement.get('term_mapping', []), 'terminal mapping'
        )
        unknown_terminals = sorted(
            set(term_mapping.keys()) - set(connections.keys())
        )
        if unknown_terminals:
            raise ValueError(
                'Instance {!r} maps unknown terminals: {}.'.format(
                    name, ', '.join(unknown_terminals)
                )
            )
        connections.update(term_mapping)

        same_master = (
            lib_name == template_instance.lib_name
            and cell_name == template_instance.cell_name
        )
        parameters = OrderedDict(
            template_instance.parameters if same_master else []
        )
        parameters.update(
            _ordered_items(
                replacement.get('params', []), 'instance parameter'
            )
        )

        wrapper = self.primitive_wrappers.get(
            '{}/{}'.format(lib_name, cell_name)
        )
        if wrapper is None:
            terminals = template_instance.terminals
        else:
            terminals = wrapper.get('terminals', [])
            unknown_wrapper_terminals = sorted(
                set(terminals) - set(connections)
            )
            if unknown_wrapper_terminals:
                raise ValueError(
                    'Primitive wrapper {}/{} references unknown terminals: {}.'
                    .format(
                        lib_name,
                        cell_name,
                        ', '.join(unknown_wrapper_terminals),
                    )
                )
            name = 'X' + name[1:]

        tokens = [name]
        is_array_instance = len(_expand_bus_name(name)) > 1
        for terminal in terminals:
            connection = _validate_identifier(
                connections[terminal], 'Net name'
            )
            if is_array_instance:
                # Vectorized Cadence instances use range/replication
                # expressions collectively across every array element.
                # Flattening them requires expanding the instance itself.
                tokens.append(connection)
            else:
                tokens.extend(_expand_net_expression(connection))
        tokens.append(cell_name)
        for parameter, value in parameters.items():
            _validate_identifier(parameter, 'Parameter name')
            tokens.append(
                '{}={}'.format(parameter, _format_parameter_value(value))
            )

        child_cell = None
        dependency = None
        if (
                template_instance.element_type == 'X'
                and lib_name == impl_lib
                and cell_name != impl_cell):
            child_cell = cell_name
        if template_instance.element_type == 'X' and wrapper is None:
            dependency = (lib_name, cell_name)
        return self._wrap_tokens(tokens), child_cell, dependency

    def _wrap_tokens(self, tokens):
        lines = []
        current = ''
        for token in tokens:
            candidate = token if not current else '{} {}'.format(current, token)
            if current and len(candidate) > self.line_length:
                lines.append(current)
                current = '+ {}'.format(token)
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines


class CdlTemplateWriter(CdlWriter):
    """Serialize BAG template metadata as self-contained annotated CDL."""

    def __init__(self, extension='.cdl', line_length=100):
        CdlWriter.__init__(self, extension=extension, line_length=line_length)

    def write_cell(self, output_dir, template_cell):
        """Write one reusable annotated CDL template and return its path."""
        _validate_identifier(template_cell.lib_name, 'Template library name')
        _validate_identifier(template_cell.cell_name, 'Template cell name')
        if os.path.basename(template_cell.cell_name) != template_cell.cell_name:
            raise ValueError(
                'Template cell name cannot contain path separators.'
            )

        subckt_tokens = ['.SUBCKT', template_cell.cell_name]
        subckt_tokens.extend(
            _validate_identifier(pin, 'Template pin name')
            for pin in template_cell.pins
        )
        if template_cell.parameters:
            subckt_tokens.append('PARAMS:')
            subckt_tokens.extend(
                '{}={}'.format(name, _format_parameter_value(value))
                for name, value in template_cell.parameters.items()
            )

        lines = [
            '* BAG annotated CDL template',
            '* library: {}'.format(template_cell.lib_name),
        ]
        lines.extend(self._wrap_tokens(subckt_tokens))
        lines.append('* @BAG {}'.format(self._format_metadata(
            dict(lib_name=template_cell.lib_name)
        )))
        if template_cell.pins:
            lines.append('*.PININFO {}'.format(' '.join(
                '{}:{}'.format(
                    pin, self._get_pin_info_code(
                        template_cell.pin_directions[pin], pin
                    )
                )
                for pin in template_cell.pins
            )))
        for instance in template_cell.instances.values():
            lines.extend(self._render_template_instance(instance))
        lines.append('.ENDS {}'.format(template_cell.cell_name))
        lines.append('')

        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.abspath(
            os.path.join(output_dir, template_cell.cell_name + self.extension)
        )
        write_file(output_file, '\n'.join(lines), mkdir=False)
        return output_file

    def _render_template_instance(self, instance):
        _validate_identifier(instance.name, 'Template instance name')
        _validate_identifier(instance.cell_name, 'Template instance cell name')
        _validate_identifier(instance.lib_name, 'Template instance library name')

        if instance.terminals is None:
            raise ValueError(
                'Template instance {!r} has unresolved terminals.'.format(
                    instance.name
                )
            )
        if len(instance.nodes) != len(instance.terminals):
            raise ValueError(
                'Template instance {!r} has mismatched nodes and terminals.'
                .format(instance.name)
            )

        tokens = [instance.name]
        tokens.extend(
            _validate_identifier(node, 'Template net name')
            for node in instance.nodes
        )
        tokens.append(instance.cell_name)
        for parameter, value in instance.parameters.items():
            _validate_identifier(parameter, 'Template parameter name')
            tokens.append(
                '{}={}'.format(parameter, _format_parameter_value(value))
            )

        metadata = dict(lib_name=instance.lib_name)
        if instance.element_type == 'X':
            metadata['terminals'] = list(instance.terminals)
        annotation = '$ @BAG {}'.format(self._format_metadata(metadata))
        lines = self._wrap_tokens(tokens)
        if len(lines[-1]) + 1 + len(annotation) <= self.line_length:
            lines[-1] += ' ' + annotation
        else:
            lines.append('+ ' + annotation)
        return lines

    @staticmethod
    def _format_metadata(metadata):
        return json.dumps(metadata, separators=(',', ':'), sort_keys=True)
