"""Independent schematic source and implementation formats.

The input database supplies topology, the output database implements the BAG
changes.  In particular, an OA input is read from the live cellview, not from a
possibly stale netlist_info file, and CDL-to-OA uses derived runtime templates.
"""

import copy
import importlib
from pathlib import Path

import yaml

from .cdl import CdlInterface
from .database import DbAccess
from ..io.cdl import load_schematic_library


def _load_class(name):
    module, attribute = name.rsplit('.', 1)
    return getattr(importlib.import_module(module), attribute)


def validate_io(config):
    for key in ('input', 'output'):
        if config.get(key) not in ('cdl', 'oa'):
            raise ValueError('schematic_io.{} must be cdl or oa'.format(key))
    return 'oa' in (config['input'], config['output'])


class SchematicInterface:
    """Compose existing BAG interfaces without modifying the input libraries."""

    def __init__(self, dealer, tmp_dir, db_config):
        selection = db_config['schematic_io']
        needs_oa = validate_io(selection)
        self.input_format = selection['input']
        self.output_format = selection['output']
        self.oa = None
        self.cdl = None
        self._source_info = {}
        self._oa_templates = {}
        self._rendering = set()
        self._renderer = selection.get('cdl_oa_renderer')
        if needs_oa:
            if dealer is None:
                raise ValueError('OA schematic input/output requires a BAG server')
            cls_name = selection.get('oa_class', db_config['class'])
            if cls_name == 'bag.interface.cdl.CdlInterface':
                cls_name = 'bag.interface.skill.SkillInterface'
            self.oa = _load_class(cls_name)(dealer, tmp_dir, db_config)
        if 'cdl' in (self.input_format, self.output_format):
            cdl_config = copy.deepcopy(db_config)
            if self.input_format == 'oa':
                # OA is authoritative. Never let configured CDL override it.
                cdl_config.setdefault('cdl', {}).update(
                    source_files=[], template_root='')
            self.cdl = CdlInterface(tmp_dir, cdl_config)
        self.source = self.cdl if self.input_format == 'cdl' else self.oa
        self.output = self.cdl if self.output_format == 'cdl' else self.oa

    def __getattr__(self, name):
        return getattr(self.output, name)

    def instantiate_schematic(self, lib_name, content_list, lib_path=''):
        # Bind the shared formatter to this router. Delegating this call to the
        # output backend would bypass source preparation and template rendering.
        return DbAccess.instantiate_schematic(self, lib_name, content_list, lib_path)

    def parse_schematic_template(self, lib_name, cell_name):
        return self.source.parse_schematic_template(lib_name, cell_name)

    def get_cells_in_library(self, lib_name):
        return self.source.get_cells_in_library(lib_name)

    def import_design_library(self, *args, **kwargs):
        return self.source.import_design_library(*args, **kwargs)

    def import_sch_cellview(self, *args, **kwargs):
        return self.source.import_sch_cellview(*args, **kwargs)

    def read_source_info(self, lib_name, cell_name, instance_names=None):
        key = (lib_name, cell_name)
        if key not in self._source_info:
            info = yaml.safe_load(self.parse_schematic_template(*key))
            if (info.get('lib_name'), info.get('cell_name')) != key:
                raise ValueError('Schematic source identity mismatch: {}'.format(key))
            self._source_info[key] = info
        info = copy.deepcopy(self._source_info[key])
        if self.input_format == 'cdl' and instance_names:
            # CDL adds an M/X prefix when a BAG instance name lacks one.
            # YAML supplies naming aliases only; topology still comes from CDL.
            for name in instance_names:
                if name not in info['instances']:
                    aliases = [candidate for candidate in info['instances']
                               if candidate[1:] == name]
                    if len(aliases) == 1:
                        info['instances'][name] = info['instances'].pop(aliases[0])
        return info

    def _prepare_cdl_output(self, lib_name, cell_name):
        key = (lib_name, cell_name)
        if key in self.cdl._cells:
            return
        info = self.read_source_info(*key)
        info_dir = Path(self.cdl.tmp_dir) / 'oa_source' / lib_name
        info_dir.mkdir(parents=True, exist_ok=True)
        info_file = info_dir / (cell_name + '.yaml')
        info_file.write_text(yaml.safe_dump(info), encoding='utf-8')
        for cell in load_schematic_library(info_dir):
            self.cdl._cells[(cell.lib_name, cell.cell_name)] = (str(info_file), cell)

    def _prepare_oa_output(self, lib_name, cell_name):
        key = (lib_name, cell_name)
        if key not in self._oa_templates:
            if not self._renderer:
                raise ValueError('CDL-to-OA requires schematic_io.cdl_oa_renderer')
            module, function = self._renderer.split(':', 1)
            render = getattr(importlib.import_module(module), function)
            source, cell = self.cdl._cells[key]
            if key in self._rendering:
                raise ValueError('Recursive CDL hierarchy: {}'.format(key))
            self._rendering.add(key)
            try:
                cell = copy.deepcopy(cell)
                for instance in cell.instances.values():
                    child = (instance.lib_name, instance.cell_name)
                    if (child in self.cdl._cells and instance.lib_name != 'BAG_prim'
                            and instance.lib_name not in self.cdl.exc_libs):
                        instance.lib_name, instance.cell_name = self._prepare_oa_output(*child)
                self._oa_templates[key] = render(
                    self.oa, source, cell, self.cdl.tmp_dir)
            finally:
                self._rendering.remove(key)
        return self._oa_templates[key]

    def create_implementation(self, lib_name, template_list, change_list, lib_path=''):
        if len(template_list) != len(change_list):
            raise ValueError('template_list and change_list must have the same length')
        templates = []
        changes = copy.deepcopy(change_list)
        for index, (source_lib, source_cell, implementation_cell) in enumerate(template_list):
            # Always read the selected input, including OA-to-OA.
            self.read_source_info(source_lib, source_cell)
            if self.input_format == 'oa' and self.output_format == 'cdl':
                self._prepare_cdl_output(source_lib, source_cell)
            elif self.input_format == 'cdl' and self.output_format == 'oa':
                cell = self.cdl._cells[(source_lib, source_cell)][1]
                inst_list = []
                for name, replacements in changes[index].get('inst_list', []):
                    if name not in cell.instances:
                        aliases = [candidate for candidate in cell.instances
                                   if candidate[1:] == name]
                        if len(aliases) == 1:
                            name = aliases[0]
                    inst_list.append((name, replacements))
                changes[index]['inst_list'] = inst_list
                source_lib, source_cell = self._prepare_oa_output(source_lib, source_cell)
            templates.append((source_lib, source_cell, implementation_cell))
        return self.output.create_implementation(
            lib_name, templates, changes, lib_path=lib_path)
