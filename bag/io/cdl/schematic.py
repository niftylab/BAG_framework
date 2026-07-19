# -*- coding: utf-8 -*-

"""Conversion from BAG ``netlist_info`` YAML to annotated CDL templates."""

from collections import OrderedDict
from pathlib import Path
import re

import yaml

from .core import NetlistCell, NetlistInstance


_BASIC_SYMBOLS = frozenset(['ipin', 'opin', 'iopin', 'noConn'])
_MOS_TERMINALS = ['D', 'G', 'S', 'B']


def _net_references_pin(net_name, pin_name):
    pin_base = pin_name.split('<', 1)[0]
    if not pin_base:
        return False
    return re.search(
        r'(?<![A-Za-z0-9_]){}(?:<[^>]*>)?(?![A-Za-z0-9_])'.format(
            re.escape(pin_base)
        ),
        str(net_name),
        flags=re.IGNORECASE,
    ) is not None


def _infer_pin_directions(info, pins):
    answer = {}
    instances = info.get('instances', {}) or {}
    for pin in pins:
        observed = set()
        for attrs in instances.values():
            for pin_info in (attrs.get('instpins', {}) or {}).values():
                if _net_references_pin(pin_info.get('net_name', ''), pin):
                    observed.add(pin_info.get('direction', 'inputOutput'))

        if not observed:
            answer[pin] = 'inputOutput'
        elif 'inputOutput' in observed or len(observed) > 1:
            answer[pin] = 'inputOutput'
        elif 'output' in observed:
            answer[pin] = 'output'
        else:
            answer[pin] = 'input'
    return answer


def _get_pin_directions(info, pins, source):
    directions = info.get('pin_directions', [])
    if isinstance(directions, dict):
        missing = [pin for pin in pins if pin not in directions]
        if missing:
            raise ValueError(
                '{} is missing pin directions: {}.'.format(
                    source, ', '.join(missing)
                )
            )
        return dict((pin, directions[pin]) for pin in pins)

    if not directions:
        return _infer_pin_directions(info, pins)
    if len(directions) != len(pins):
        raise ValueError(
            '{} has {} pins but {} pin directions.'.format(
                source, len(pins), len(directions)
            )
        )
    return dict(zip(pins, directions))


def _get_element_info(lib_name, cell_name, cells):
    if lib_name == 'BAG_prim':
        lower_name = cell_name.lower()
        if lower_name.startswith('nmos4') or lower_name.startswith('pmos4'):
            return 'M', list(_MOS_TERMINALS)

    return 'X', None


def _normalize_instance_name(name, element_type):
    if not name:
        raise ValueError('BAG instance name cannot be empty.')
    if name[0].upper() == element_type:
        return name
    return element_type + name


def load_schematic_library(netlist_info_dir):
    """Load one BAG library's ``netlist_info`` directory as CDL cells.

    Four-terminal ``BAG_prim`` MOS devices are emitted as MOS elements.
    Subcircuits in the same library and external masters are emitted as
    explicitly annotated ``X`` instances.  ``basic`` symbol instances are
    intentionally omitted because they do not represent electrical devices
    in a CDL netlist.
    """
    info_dir = Path(netlist_info_dir)
    yaml_files = sorted(info_dir.glob('*.yaml'))
    if not yaml_files:
        raise ValueError('No netlist_info YAML files found in {}.'.format(info_dir))

    raw_cells = OrderedDict()
    cells = OrderedDict()
    for yaml_file in yaml_files:
        with yaml_file.open('r') as stream:
            info = yaml.safe_load(stream)
        if not isinstance(info, dict):
            continue

        lib_name = info.get('lib_name', '')
        cell_name = info.get('cell_name', '')
        pins = list(info.get('pins', []))
        if not lib_name or not cell_name:
            raise ValueError(
                '{} is missing lib_name or cell_name.'.format(yaml_file)
            )
        key = (lib_name, cell_name)
        if key in raw_cells:
            raise ValueError('Duplicate BAG cell {}/{}.'.format(*key))

        cell = NetlistCell(
            cell_name,
            pins,
            parameters=info.get('parameters', {}),
            lib_name=lib_name,
            pin_directions=_get_pin_directions(info, pins, yaml_file),
            line_no=1,
        )
        cell.has_pin_info = True
        raw_cells[key] = info
        cells[key] = cell

    if not cells:
        raise ValueError(
            'No BAG netlist_info cell objects found in {}.'.format(info_dir)
        )

    for key, info in raw_cells.items():
        parent = cells[key]
        for name, attrs in info.get('instances', {}).items():
            lib_name = attrs.get('lib_name', '')
            cell_name = attrs.get('cell_name', '')
            if lib_name == 'basic' and cell_name in _BASIC_SYMBOLS:
                continue
            if not lib_name or not cell_name:
                raise ValueError(
                    'Instance {!r} in {}/{} is missing library or cell name.'
                    .format(name, parent.lib_name, parent.cell_name)
                )

            element_type, terminals = _get_element_info(
                lib_name, cell_name, cells
            )
            instpins = attrs.get('instpins', {})
            if terminals is None:
                terminals = list(instpins)
            if not terminals:
                raise ValueError(
                    'Instance {!r} in {}/{} has no electrical terminals.'
                    .format(name, parent.lib_name, parent.cell_name)
                )
            missing = [terminal for terminal in terminals if terminal not in instpins]
            extra = sorted(set(instpins) - set(terminals))
            if missing or extra:
                problems = []
                if missing:
                    problems.append('missing {}'.format(', '.join(missing)))
                if extra:
                    problems.append('unknown {}'.format(', '.join(extra)))
                raise ValueError(
                    'Instance {!r} in {}/{} has incompatible terminals: {}.'
                    .format(
                        name, parent.lib_name, parent.cell_name,
                        '; '.join(problems),
                    )
                )

            nodes = [instpins[terminal]['net_name'] for terminal in terminals]
            directions = dict(
                (terminal, instpins[terminal].get('direction', 'inputOutput'))
                for terminal in terminals
            )
            instance = NetlistInstance(
                _normalize_instance_name(name, element_type),
                element_type,
                cell_name,
                nodes,
                parameters=OrderedDict(
                    (parameter, value)
                    for parameter, value in (attrs.get('parameters', {}) or {}).items()
                    if value is not None
                ),
                lib_name=lib_name,
                terminals=terminals,
                terminal_directions=directions,
                metadata=dict(lib_name=lib_name),
            )
            parent.add_instance(instance)

    return list(cells.values())
