# -*- coding: utf-8 -*-

"""Direct Spectre command-line interface.

Re-runs a spectre-format netlist (by default the ADE-L deck in
``simulation/<cell>/spectre/config/netlist/``) through the spectre binary,
without Virtuoso or ADE.  The deck is provisioned on demand with
``ensure_netlist(db, lib, cell)``, which drives the netlist-only ADE-L
session (``create_netlist`` on the skill database interface) when the deck
is missing or stale -- no prior ADE-L simulation run is required.
Design-variable overrides come from ``sim_config['params']`` and are
patched into the ``parameters`` statement of the deck copy.  Results are
written as psfascii under ``<save_dir>/psf`` and parsed with
:func:`bag.interface.direct.parse_psfascii_traces`.

Example configuration::

    simulation:
      class: "bag.interface.spectre.SpectreInterface"
      kwargs: {}
      netlist: "{work_dir}/simulation/{cell}/spectre/config/netlist/input.scs"
      netlist_refresh: auto   # never | auto | always (see ensure_netlist)
      params: {pper: 1.0e-10}

The ``netlist`` template above is also the default deck location of the
ADE-L netlist step (``AdelSession.netlist_path``); override both together
(``database.testbench.netlist_path``) when using a nonstandard layout.
"""

from typing import List

from .direct import DirectSimInterface


class SpectreInterface(DirectSimInterface):
    """Runs the spectre binary directly on a spectre-format deck."""

    default_command = 'spectre'
    default_netlist = ('{work_dir}/simulation/{cell}/spectre/config/'
                       'netlist/input.scs')
    deck_name = 'input.scs'

    def build_command(self, save_dir, log_fname):
        # type: (str, str) -> List[str]
        cmd = [self.sim_config.get('command', self.default_command),
               self.deck_name,
               '-format', self.sim_config.get('raw_format', 'psfascii'),
               '-raw', 'psf',
               '=log', 'spectre.log']
        cmd.extend(self.sim_config.get('extra_args', []))
        return cmd
