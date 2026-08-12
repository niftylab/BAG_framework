# -*- coding: utf-8 -*-

"""Base database interface for local circuit-netlist libraries."""

import abc
import os

from .database import DbAccess


class NetlistInterface(DbAccess, metaclass=abc.ABCMeta):
    """A :class:`DbAccess` implementation backed by local netlist files.

    Subclasses implement source indexing and template parsing, and can
    override implementation export when a format-specific writer is
    available.
    """

    requires_server = False

    def __init__(self, tmp_dir, db_config):
        DbAccess.__init__(self, tmp_dir, db_config)

    def close(self):
        """A local file interface has no server to terminate."""
        pass

    @abc.abstractmethod
    def parse_schematic_template(self, lib_name, cell_name):
        pass

    @abc.abstractmethod
    def get_cells_in_library(self, lib_name):
        pass

    def create_library(self, lib_name, lib_path=''):
        root_path = os.path.abspath(lib_path or self.default_lib_path)
        library_path = os.path.join(root_path, lib_name)
        os.makedirs(library_path, exist_ok=True)
        return library_path

    def create_implementation(self, lib_name, template_list, change_list, lib_path=''):
        raise NotImplementedError(
            'This netlist backend has no implementation writer.'
        )

    def configure_testbench(self, tb_lib, tb_cell):
        raise NotImplementedError(
            'A local netlist database does not implement ADE testbenches.'
        )

    def get_testbench_info(self, tb_lib, tb_cell):
        raise NotImplementedError(
            'A local netlist database does not implement ADE testbenches.'
        )

    def update_testbench(self, lib, cell, parameters, sim_envs,
                         config_rules, env_parameters, stimuli=None):
        raise NotImplementedError(
            'A local netlist database does not implement ADE testbenches.'
        )

    def instantiate_layout_pcell(self, lib_name, cell_name, view_name,
                                 inst_lib, inst_cell, params, pin_mapping):
        raise NotImplementedError(
            'A local netlist database does not implement layout PCells.'
        )

    def instantiate_layout(self, lib_name, view_name, via_tech, layout_list):
        raise NotImplementedError(
            'A local netlist database does not implement layouts.'
        )

    def release_write_locks(self, lib_name, cell_view_list):
        """Local netlist files do not use Virtuoso cell-view locks."""
        pass

    def delete_cellviews(self, lib_name, cell_view_list):
        raise NotImplementedError(
            'Deleting generated netlist cell views is not implemented yet.'
        )

    def create_schematic_from_netlist(self, netlist, lib_name, cell_name,
                                      sch_view=None, **kwargs):
        raise NotImplementedError(
            'Creating an extracted schematic is not supported by this backend.'
        )

    def create_verilog_view(self, verilog_file, lib_name, cell_name, **kwargs):
        raise NotImplementedError(
            'Creating a Verilog CAD view is not supported by this backend.'
        )
