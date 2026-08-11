# -*- coding: utf-8 -*-

"""Direct command-line simulator interfaces.

Unlike :class:`bag.interface.ocean.OceanInterface`, which drives a
simulation through an ADE assembler view inside an ocean/Virtuoso process,
the interfaces in this family run a *simulator binary directly* on a
netlist deck -- no Virtuoso, no ADE session, no skill server.  They are
therefore immune to the ADE event-loop/setupdb failure modes and run in
any headless environment where the simulator binary is available.

The common contract (see :class:`DirectSimInterface`):

* ``sim_config['netlist']`` names the source deck.  ``{work_dir}``,
  ``{lib}``, and ``{cell}`` are substituted, so the default for
  :class:`~bag.interface.spectre.SpectreInterface` points at the netlist
  the ADE-L netlist step leaves in
  ``simulation/<cell>/spectre/config/netlist/``.  ``ensure_netlist``
  provisions that deck on demand through the database interface's
  ``create_netlist`` (netlist-only ADE-L session, no simulation run), so a
  prior ADE-L *run* is no longer required.
* The deck is copied into a fresh save directory; entries of
  ``sim_config['params']`` override parameter values inside the copy.
* ``setup_sim_process`` returns the standard ProcInfo tuple, so the
  existing :class:`~bag.interface.simulator.SimProcessManager` subprocess
  machinery (and ``async_run_simulation``) work unchanged.
* Measurement expressions registered in ADE outputs are **not** evaluated
  -- these interfaces deliver raw simulator outputs plus small parsers
  (psfascii traces, HSPICE ``.mt#`` tables, ngspice ``.meas`` logs); the
  caller post-processes waveforms in Python.
"""

from typing import Any, Dict, List, Optional, Tuple

import os
import re

import bag.io
from .simulator import SimProcessManager


class DirectSimInterface(SimProcessManager):
    """Base class for direct command-line simulator interfaces.

    Parameters
    ----------
    tmp_dir : str
        temporary file directory for SimAccess.
    sim_config : Dict[str, Any]
        the simulation configuration dictionary.  Common keys:

        ``command``
            the simulator executable (subclass default if omitted).
        ``netlist``
            source deck path template; ``{work_dir}``, ``{lib}``, and
            ``{cell}`` are substituted.
        ``params``
            optional {name: value} overrides patched into the deck copy.
        ``env`` / ``cwd``
            optional subprocess environment / working directory.
    """

    #: subclass default simulator executable.
    default_command = ''
    #: subclass default netlist template.
    default_netlist = ''
    #: name of the deck copy inside the save directory.
    deck_name = 'input.ckt'

    def __init__(self, tmp_dir, sim_config):
        # type: (str, Dict[str, Any]) -> None
        SimProcessManager.__init__(self, tmp_dir, sim_config)

    def format_parameter_value(self, param_config, precision):
        # type: (Dict[str, Any], int) -> str
        """Single-value parameters only; sweeps are deck-level here."""
        fmt = '%.{}e'.format(precision)
        if param_config['type'] == 'single':
            return fmt % param_config['value']
        raise ValueError('%s only supports single-value parameters; '
                         'express sweeps in the deck itself.'
                         % type(self).__name__)

    def netlist_path(self, lib, cell):
        # type: (str, str) -> str
        """Return the source deck path template resolved for a testbench."""
        template = self.sim_config.get('netlist', self.default_netlist)
        work_dir = os.environ.get('BAG_WORK_DIR', '.')
        return template.format(work_dir=work_dir, lib=lib, cell=cell)

    def resolve_netlist(self, lib, cell):
        # type: (str, str) -> str
        """Return the source deck path for the given testbench."""
        path = self.netlist_path(lib, cell)
        if not os.path.isfile(path):
            raise ValueError('%s: netlist deck not found: %s'
                             % (type(self).__name__, path))
        return path

    def ensure_netlist(self, db, lib, cell, refresh=None):
        # type: (Any, str, str, Optional[str]) -> str
        """Ensure the source deck exists, provisioning it through ``db``.

        Parameters
        ----------
        db : bag.interface.database.DbAccess
            database interface whose ``create_netlist`` provisions the deck
            (the ADE-L skill interface).
        lib : str
            testbench library name.
        cell : str
            testbench cell name.
        refresh : Optional[str]
            deck refresh policy; defaults to
            ``sim_config['netlist_refresh']`` (``'auto'`` if unset).

            ``'never'``
                only verify the deck exists.
            ``'auto'``
                recreate when the deck is missing or older than the
                testbench cell's own OA views (schematic, config, saved
                state).  DUT-side edits are *not* seen -- after
                regenerating the DUT use ``'always'``.
            ``'always'``
                recreate unconditionally.

        Returns
        -------
        deck : str
            the netlist deck path.
        """
        if refresh is None:
            refresh = self.sim_config.get('netlist_refresh', 'auto')
        if refresh not in ('never', 'auto', 'always'):
            raise ValueError('%s: unknown netlist_refresh policy: %s'
                             % (type(self).__name__, refresh))
        if refresh == 'never':
            return self.resolve_netlist(lib, cell)

        deck = self.netlist_path(lib, cell)
        stale = refresh == 'always' or not os.path.isfile(deck)
        if not stale:
            # deck-vs-OA-source staleness: both stamps come from the same
            # file server, so comparing them is NFS-safe.  Skipped when the
            # database interface cannot resolve OA library paths.
            get_lib_path = getattr(db, 'get_library_path', None)
            lib_path = get_lib_path(lib) if get_lib_path is not None else None
            if lib_path is not None:
                cell_dir = os.path.join(lib_path, cell)
                deck_mtime = os.path.getmtime(deck)
                for dirpath, _dirnames, filenames in os.walk(cell_dir):
                    for fname in filenames:
                        try:
                            src_mtime = os.path.getmtime(
                                os.path.join(dirpath, fname))
                        except OSError:
                            continue
                        if src_mtime > deck_mtime:
                            stale = True
                            break
                    if stale:
                        break
        if stale:
            return db.create_netlist(lib, cell)
        return deck

    def patch_parameters(self, text, params):
        # type: (str, Dict[str, Any]) -> str
        """Override parameter assignments inside the deck text."""
        for name, value in params.items():
            pat = re.compile(r'(?<![\w.])(%s\s*=\s*)[^\s\\]+' % re.escape(name))
            text, n_sub = pat.subn(lambda m: m.group(1) + str(value), text)
            if n_sub == 0:
                raise ValueError('%s: parameter %s not found in deck.'
                                 % (type(self).__name__, name))
        return text

    def build_command(self, save_dir, log_fname):
        # type: (str, str) -> List[str]
        """Return the simulator argv; deck/log paths are save_dir relative."""
        raise NotImplementedError

    def setup_sim_process(self, lib, cell, outputs, precision, sim_tag):
        # type: (str, str, Dict[str, str], int, Optional[str]) -> Tuple
        sim_tag = sim_tag or type(self).__name__
        src = self.resolve_netlist(lib, cell)
        save_dir = bag.io.make_temp_dir(prefix='%s_data' % sim_tag,
                                        parent_dir=self.tmp_dir)
        text = bag.io.read_file(src)
        params = self.sim_config.get('params') or {}
        if params:
            text = self.patch_parameters(text, params)
        bag.io.write_file(os.path.join(save_dir, self.deck_name), text)

        log_fname = os.path.join(save_dir, 'sim_output.log')
        sim_cmd = self.build_command(save_dir, log_fname)
        env = self.sim_config.get('env', None)
        # run inside save_dir so all simulator outputs land there.
        return sim_cmd, log_fname, env, save_dir, save_dir

    def setup_load_process(self, lib, cell, hist_name, outputs, precision):
        raise NotImplementedError(
            '%s has no simulation history; parse the save directory of the '
            'run instead.' % type(self).__name__)


def parse_psfascii_traces(psf_file):
    # type: (str) -> Dict[str, List[float]]
    """Parse a psfascii transient file into {signal: values} lists.

    The sweep variable (``time``) is included under its own name.  Complex
    or non-scalar traces are skipped.
    """
    traces = {}  # type: Dict[str, List[float]]
    in_value = False
    with open(psf_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line == 'VALUE':
                in_value = True
                continue
            if line == 'END':
                in_value = False
                continue
            if not in_value or not line.startswith('"'):
                continue
            try:
                name, val = line.split(None, 1)
            except ValueError:
                continue
            name = name.strip('"')
            try:
                traces.setdefault(name, []).append(float(val))
            except ValueError:
                pass
    return traces


def measure_period(time_vals, sig_vals, threshold=0.5, edge='rising',
                   t_start=0.0):
    # type: (List[float], List[float], float, str, float) -> float
    """Average period between threshold crossings after ``t_start``.

    Linear-interpolates each crossing; raises ValueError when fewer than
    two qualifying crossings exist.
    """
    crossings = []
    for i in range(1, len(time_vals)):
        lo, hi = sig_vals[i - 1], sig_vals[i]
        rising = lo < threshold <= hi
        falling = lo >= threshold > hi
        if (edge == 'rising' and rising) or (edge == 'falling' and falling):
            frac = (threshold - lo) / (hi - lo)
            tc = time_vals[i - 1] + frac * (time_vals[i] - time_vals[i - 1])
            if tc > t_start:
                crossings.append(tc)
    if len(crossings) < 2:
        raise ValueError('fewer than two %s crossings after %g s'
                         % (edge, t_start))
    periods = [b - a for a, b in zip(crossings, crossings[1:])]
    return sum(periods) / len(periods)


def parse_hspice_measures(mt_file):
    # type: (str) -> Dict[str, float]
    """Parse an HSPICE ``.mt#``/``.ms#`` measure table into {name: value}.

    Handles the classic wrapped-column text format ($DATA1 header, title
    line, column-name tokens, then numeric tokens).
    """
    names = []  # type: List[str]
    values = []  # type: List[float]
    with open(mt_file, 'r') as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith('$') or s.startswith('.TITLE') \
                    or s.lower().startswith('.title'):
                continue
            for tok in s.split():
                try:
                    values.append(float(tok))
                except ValueError:
                    names.append(tok)
    result = {}
    for i, name in enumerate(names):
        if i < len(values):
            result[name] = values[i]
    return result


def parse_ngspice_measures(log_file):
    # type: (str) -> Dict[str, float]
    """Parse ``name = value`` measure results from an ngspice batch log."""
    pat = re.compile(r'^\s*([A-Za-z_]\w*)\s*=\s*([-+0-9.eE]+)')
    result = {}
    with open(log_file, 'r') as f:
        for line in f:
            m = pat.match(line)
            if m:
                try:
                    result[m.group(1)] = float(m.group(2))
                except ValueError:
                    pass
    return result
