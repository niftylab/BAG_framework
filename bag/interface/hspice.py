# -*- coding: utf-8 -*-

"""Direct HSPICE command-line interface.

Runs the hspice binary on an HSPICE-format deck, without Virtuoso or ADE.
The deck must be authored in HSPICE syntax (the OSS netlister's spectre
netlists are *not* accepted); parameter overrides are patched into
``.param``-style assignments of the deck copy.  ``.measure`` results land
in ``<save_dir>/hspice.mt0`` (transient) and are parsed with
:func:`bag.interface.direct.parse_hspice_measures`.

Example configuration::

    simulation:
      class: "bag.interface.hspice.HSPICEInterface"
      netlist: "{work_dir}/simulation/{cell}/hspice/input.sp"
      params: {pper: 1.0e-10}
"""

from typing import List

from .direct import DirectSimInterface


class HSPICEInterface(DirectSimInterface):
    """Runs the hspice binary directly on an HSPICE-format deck."""

    default_command = 'hspice'
    default_netlist = '{work_dir}/simulation/{cell}/hspice/input.sp'
    deck_name = 'input.sp'

    def build_command(self, save_dir, log_fname):
        # type: (str, str) -> List[str]
        cmd = [self.sim_config.get('command', self.default_command),
               '-i', self.deck_name,
               '-o', 'hspice']
        cmd.extend(self.sim_config.get('extra_args', []))
        return cmd
