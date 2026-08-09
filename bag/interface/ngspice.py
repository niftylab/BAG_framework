# -*- coding: utf-8 -*-

"""Direct ngspice command-line interface.

Runs the ngspice binary in batch mode on a SPICE deck, without Virtuoso
or ADE.  Note that foundry PDK models shipped in spectre syntax cannot be
read by ngspice, so this interface targets model-independent decks
(behavioral testbenches, ideal-element fixtures, exported model cards).
``.meas`` results are read from the batch log with
:func:`bag.interface.direct.parse_ngspice_measures`; ``wrdata`` output
files land in the save directory.

Example configuration::

    simulation:
      class: "bag.interface.ngspice.NgspiceInterface"
      netlist: "{work_dir}/simulation/{cell}/ngspice/input.sp"
      params: {pper: 1.0e-10}
"""

from typing import List

from .direct import DirectSimInterface


class NgspiceInterface(DirectSimInterface):
    """Runs the ngspice binary in batch mode on a SPICE deck."""

    default_command = 'ngspice'
    default_netlist = '{work_dir}/simulation/{cell}/ngspice/input.sp'
    deck_name = 'input.sp'

    def build_command(self, save_dir, log_fname):
        # type: (str, str) -> List[str]
        cmd = [self.sim_config.get('command', self.default_command),
               '-b', '-o', 'ngspice.log',
               self.deck_name]
        cmd.extend(self.sim_config.get('extra_args', []))
        return cmd
