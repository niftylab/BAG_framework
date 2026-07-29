# -*- coding: utf-8 -*-

"""Local schematic database interface for annotated CDL templates."""

from collections import OrderedDict
import glob
import os

import yaml

from ..io.cdl import (
    CdlBundleBuilder,
    CdlParser,
    CdlTemplateWriter,
    CdlWriter,
)
from .netlist import NetlistInterface


def _to_plain(value):
    if isinstance(value, dict):
        return dict((key, _to_plain(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return [_to_plain(item) for item in value]
    return value


def _dump_yaml(value):
    plain = _to_plain(value)
    try:
        return yaml.safe_dump(
            plain,
            default_flow_style=False,
            sort_keys=False,
        )
    except TypeError:
        # PyYAML versions supported by early BAG2 releases do not expose
        # the sort_keys keyword.
        return yaml.safe_dump(plain, default_flow_style=False)


class CdlInterface(NetlistInterface):
    """Expose annotated CDL files through the BAG schematic DB API."""

    def __init__(self, tmp_dir, db_config):
        NetlistInterface.__init__(self, tmp_dir, db_config)
        cdl_config = db_config.get('cdl', {})
        self._strict = cdl_config.get('strict', True)
        output_config = cdl_config.get('output', {})
        self._writer = CdlWriter(
            extension=output_config.get('extension', '.sp'),
            line_length=output_config.get('line_length', 100),
            primitive_wrappers=output_config.get(
                'primitive_wrappers', {}
            ),
        )
        template_config = cdl_config.get('template', {})
        self._template_writer = CdlTemplateWriter(
            extension=template_config.get('extension', '.cdl'),
            line_length=template_config.get('line_length', 100),
        )
        self._template_root = os.path.abspath(os.path.expandvars(
            cdl_config.get('template_root', '')
        )) if cdl_config.get('template_root') else ''
        self._source_files = [
            os.path.abspath(os.path.expandvars(path))
            for path in cdl_config.get('source_files', [])
        ]
        self._external_subckts = list(
            cdl_config.get('external_subckts', [])
        )
        self._cells = OrderedDict()
        self._implementation_paths = {}
        self._load_sources()

    @property
    def source_files(self):
        return list(self._source_files)

    @property
    def template_root(self):
        """Return the optional root containing imported BAG CDL templates."""
        return self._template_root

    def _load_sources(self):
        parser = CdlParser(strict=self._strict)
        if self._template_root:
            pattern = os.path.join(
                self._template_root, '*', 'cdl_templates',
                '*' + self._template_writer.extension,
            )
            for template_file in sorted(glob.glob(pattern)):
                self._load_source_file(parser, template_file)
        for source_file in self._source_files:
            self._load_source_file(parser, source_file, skip_existing=True)
        self._resolve_hierarchical_directions()

    def _load_source_file(self, parser, source_file, skip_existing=False):
        parsed = parser.parse_file(source_file)
        for key, cell in parsed.cells.items():
            if key in self._cells:
                if skip_existing:
                    continue
                old_source = self._cells[key][0]
                raise ValueError(
                    'Duplicate CDL template {}/{} in {} and {}.'
                    .format(key[0], key[1], old_source, source_file)
                )
            self._cells[key] = (source_file, cell)

    def _resolve_hierarchical_directions(self):
        """Recover child pin directions from separately stored templates."""
        for _, parent in self._cells.values():
            for instance in parent.instances.values():
                if instance.element_type != 'X':
                    continue
                child_entry = self._cells.get(
                    (instance.lib_name, instance.cell_name)
                )
                if child_entry is None or instance.terminals is None:
                    continue
                child = child_entry[1]
                if len(instance.terminals) != len(child.pins):
                    continue
                connections = instance.connections
                if set(connections) == set(child.pins):
                    instance.terminals = list(child.pins)
                    instance.nodes = [
                        connections[terminal] for terminal in child.pins
                    ]
                    instance.terminal_directions = dict(
                        child.pin_directions
                    )
                    continue
                instance.terminal_directions = dict(
                    (terminal, child.pin_directions[child_pin])
                    for terminal, child_pin in zip(instance.terminals, child.pins)
                )

    def _refresh_library_from_sources(self, lib_name):
        """Use explicitly configured source files when refreshing an import."""
        if not self._source_files:
            return

        parser = CdlParser(strict=self._strict)
        refreshed = {}
        for source_file in self._source_files:
            parsed = parser.parse_file(source_file)
            for key, cell in parsed.cells.items():
                if key[0] != lib_name:
                    continue
                if key in refreshed:
                    raise ValueError(
                        'Duplicate CDL template {}/{} in {} and {}.'
                        .format(key[0], key[1], refreshed[key][0], source_file)
                    )
                refreshed[key] = (source_file, cell)
        self._cells.update(refreshed)
        self._resolve_hierarchical_directions()

    def import_design_library(self, lib_name, dsn_db, new_lib_path):
        """Import BAG data and refresh its annotated CDL templates."""
        self._refresh_library_from_sources(lib_name)
        NetlistInterface.import_design_library(
            self, lib_name, dsn_db, new_lib_path
        )

    def import_sch_cellview(self, lib_name, cell_name, dsn_db, new_lib_path):
        """Import one BAG cell and refresh its annotated CDL template."""
        self._refresh_library_from_sources(lib_name)
        NetlistInterface.import_sch_cellview(
            self, lib_name, cell_name, dsn_db, new_lib_path
        )

    def _import_design(self, lib_name, cell_name, imported_cells, dsn_db,
                       new_lib_path):
        """Import BAG files, then persist the annotated CDL template."""
        import_key = '{}__{}'.format(lib_name, cell_name)
        if import_key in imported_cells:
            return
        NetlistInterface._import_design(
            self, lib_name, cell_name, imported_cells, dsn_db, new_lib_path
        )
        key = (lib_name, cell_name)
        if key not in self._cells:
            return

        root_path = dsn_db.get_library_path(lib_name) or new_lib_path
        template_dir = os.path.join(root_path, lib_name, 'cdl_templates')
        template_file = self._template_writer.write_cell(
            template_dir, self._cells[key][1]
        )
        self._cells[key] = (template_file, self._cells[key][1])

    def get_cells_in_library(self, lib_name):
        """Return annotated cells belonging to ``lib_name``."""
        return [
            cell_name for (cur_lib, cell_name) in self._cells.keys()
            if cur_lib == lib_name
        ]

    def parse_schematic_template(self, lib_name, cell_name):
        """Return one CDL template in BAG ``netlist_info`` YAML form."""
        key = (lib_name, cell_name)
        if key not in self._cells:
            raise ValueError(
                'CDL template {}/{} was not found in configured sources.'
                .format(lib_name, cell_name)
            )
        return _dump_yaml(self._cells[key][1].to_schematic_info())

    def create_implementation(self, lib_name, template_list, change_list,
                              lib_path=''):
        """Write concrete BAG schematic implementations as CDL files."""
        if len(template_list) != len(change_list):
            raise ValueError(
                'template_list and change_list must have the same length.'
            )

        library_path = self.create_library(lib_name, lib_path)
        self._implementation_paths[lib_name] = library_path
        output_files = []
        for template_info, change in zip(template_list, change_list):
            if len(template_info) != 3:
                raise ValueError(
                    'Template information must contain library, template '
                    'cell, and implementation cell names.'
                )
            template_lib, template_cell, impl_cell = template_info
            key = (template_lib, template_cell)
            if key not in self._cells:
                raise ValueError(
                    'CDL template {}/{} was not found in configured '
                    'sources.'.format(template_lib, template_cell)
                )
            output_files.append(
                self._writer.write_cell(
                    library_path,
                    lib_name,
                    self._cells[key][1],
                    impl_cell,
                    change,
                )
            )
        return output_files

    def get_implementation_path(self, lib_name, cell_name,
                                 require_exists=True):
        """Return the concrete CDL path generated for one implementation."""
        if (
                not cell_name
                or os.path.basename(cell_name) != cell_name):
            raise ValueError(
                'Implementation cell name cannot contain path separators.'
            )
        library_path = self._implementation_paths.get(
            lib_name,
            os.path.join(self.default_lib_path, lib_name),
        )
        output_file = os.path.abspath(os.path.join(
            library_path, cell_name + self._writer.extension
        ))
        if require_exists and not os.path.isfile(output_file):
            raise ValueError(
                'CDL implementation {}/{} has not been generated: {}'
                .format(lib_name, cell_name, output_file)
            )
        return output_file

    def create_lvcdl_bundle(self, lib_name, cell_name, bundle_root):
        """Build a top-specific, library-qualified LVCDL source bundle."""
        builder = CdlBundleBuilder(
            source_resolver=lambda child_lib, child_cell:
            self.get_implementation_path(
                child_lib,
                child_cell,
                require_exists=False,
            ),
            extension=self._writer.extension,
            external_subckts=self._external_subckts,
            subckt_overrides=(
                self._writer.get_primitive_wrapper_subckts()
            ),
        )
        return builder.build(lib_name, cell_name, bundle_root)

    def delete_cellviews(self, lib_name, cell_view_list):
        """Delete generated CDL files for schematic-like cell views."""
        library_path = self._implementation_paths.get(
            lib_name,
            os.path.join(self.default_lib_path, lib_name),
        )
        for cell_name, view_name in cell_view_list:
            if view_name not in ('schematic', 'symbol', 'netlist'):
                continue
            if (
                    not cell_name
                    or os.path.basename(cell_name) != cell_name):
                raise ValueError(
                    'Implementation cell name cannot contain path separators.'
                )
            output_file = os.path.join(
                library_path, cell_name + self._writer.extension
            )
            if os.path.isfile(output_file):
                os.remove(output_file)
            dependency_file = self._writer.get_dependency_path(output_file)
            if os.path.isfile(dependency_file):
                os.remove(dependency_file)
