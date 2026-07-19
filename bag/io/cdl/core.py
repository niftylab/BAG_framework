# -*- coding: utf-8 -*-

"""Format-neutral data structures used by circuit netlist readers."""

from collections import OrderedDict


class CdlParseError(ValueError):
    """An error raised while parsing an annotated CDL netlist."""

    def __init__(self, message, source='<string>', line_no=None):
        self.message = message
        self.source = source
        self.line_no = line_no
        if line_no is None:
            text = '{}: {}'.format(source, message)
        else:
            text = '{}:{}: {}'.format(source, line_no, message)
        ValueError.__init__(self, text)


class NetlistInstance(object):
    """A format-neutral schematic instance."""

    def __init__(self, name, element_type, cell_name, nodes, parameters=None,
                 lib_name='', terminals=None, terminal_directions=None,
                 metadata=None, line_no=None):
        self.name = name
        self.element_type = element_type.upper()
        self.lib_name = lib_name
        self.cell_name = cell_name
        self.nodes = list(nodes)
        self.parameters = OrderedDict(parameters or [])
        self.terminals = None if terminals is None else list(terminals)
        self.terminal_directions = dict(terminal_directions or {})
        self.metadata = dict(metadata or {})
        self.line_no = line_no

    @property
    def connections(self):
        """Return an ordered terminal-to-net mapping."""
        if self.terminals is None:
            raise ValueError('Terminal names are unresolved for instance {}.'.format(self.name))
        return OrderedDict(zip(self.terminals, self.nodes))

    def to_schematic_info(self):
        """Return this instance in BAG ``netlist_info`` form."""
        instpins = OrderedDict()
        for terminal, net_name in self.connections.items():
            instpins[terminal] = dict(
                direction=self.terminal_directions.get(terminal, 'inputOutput'),
                net_name=net_name,
                num_bits=1,
            )

        ans = dict(
            lib_name=self.lib_name,
            cell_name=self.cell_name,
            instpins=instpins,
        )
        if self.parameters:
            ans['parameters'] = OrderedDict(self.parameters)
        return ans


class NetlistCell(object):
    """A format-neutral subcircuit definition."""

    _VALID_PIN_DIRECTIONS = frozenset(['input', 'output', 'inputOutput'])

    def __init__(self, cell_name, pins, parameters=None, lib_name='',
                 pin_directions=None, metadata=None, line_no=None):
        self.lib_name = lib_name
        self.cell_name = cell_name
        self.pins = list(pins)
        self.parameters = OrderedDict(parameters or [])
        self.pin_directions = dict((pin, 'inputOutput') for pin in self.pins)
        if pin_directions:
            self.set_pin_directions(pin_directions)
        self.has_pin_info = False
        self.instances = OrderedDict()
        self.metadata = dict(metadata or {})
        self.line_no = line_no

    def set_pin_directions(self, pin_directions):
        """Validate and apply pin direction metadata."""
        for pin, direction in pin_directions.items():
            if pin not in self.pin_directions:
                raise ValueError(
                    'Pin direction metadata references unknown pin {!r}.'.format(pin)
                )
            if direction not in self._VALID_PIN_DIRECTIONS:
                raise ValueError(
                    'Invalid direction {!r} for pin {!r}.'.format(direction, pin)
                )
            self.pin_directions[pin] = direction

    def add_instance(self, instance):
        """Add an instance, rejecting duplicate instance names."""
        if instance.name in self.instances:
            raise ValueError('Duplicate instance name {!r}.'.format(instance.name))
        self.instances[instance.name] = instance

    def to_schematic_info(self):
        """Return this cell in BAG ``netlist_info`` form."""
        ans = dict(
            lib_name=self.lib_name,
            cell_name=self.cell_name,
            pins=list(self.pins),
            instances=OrderedDict(
                (name, inst.to_schematic_info())
                for name, inst in self.instances.items()
            ),
        )
        if self.parameters:
            ans['parameters'] = OrderedDict(self.parameters)
        ans['pin_directions'] = OrderedDict(
            (pin, self.pin_directions[pin]) for pin in self.pins
        )
        return ans


class NetlistLibrary(object):
    """A collection of parsed subcircuits."""

    def __init__(self, source='<string>'):
        self.source = source
        self.cells = OrderedDict()

    def add_cell(self, cell):
        """Add a cell using ``(lib_name, cell_name)`` as its identity."""
        key = (cell.lib_name, cell.cell_name)
        if key in self.cells:
            raise ValueError(
                'Duplicate cell {}/{}.'.format(cell.lib_name, cell.cell_name)
            )
        self.cells[key] = cell

    def get_cell(self, cell_name, lib_name=None):
        """Return a cell, requiring an unambiguous name when library is omitted."""
        if lib_name is not None:
            return self.cells[(lib_name, cell_name)]

        matches = [
            cell for (cur_lib, cur_cell), cell in self.cells.items()
            if cur_cell == cell_name
        ]
        if not matches:
            raise KeyError(cell_name)
        if len(matches) > 1:
            raise KeyError(
                'Cell name {!r} is ambiguous; specify lib_name.'.format(cell_name)
            )
        return matches[0]

    def get_cells_in_library(self, lib_name):
        """Return cell names in definition order for the given library."""
        return [
            cell_name for (cur_lib, cell_name) in self.cells.keys()
            if cur_lib == lib_name
        ]
