# -*- coding: utf-8 -*-

"""Direct Spectre command-line interface.

Re-runs a spectre-format netlist (by default the one the last ADE-L run
left in ``simulation/<cell>/spectre/config/netlist/``) through the spectre
binary, without Virtuoso or ADE.  Design-variable overrides come from
``sim_config['params']`` and are patched into the ``parameters`` statement
of the deck copy.  Results are written as psfascii under ``<save_dir>/psf``
and parsed with :func:`bag.interface.direct.parse_psfascii_traces`.

Example configuration::

    simulation:
      class: "bag.interface.spectre.SpectreInterface"
      kwargs: {}
      netlist: "{work_dir}/simulation/{cell}/spectre/config/netlist/input.scs"
      params: {pper: 1.0e-10}
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
