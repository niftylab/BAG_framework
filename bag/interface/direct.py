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
  provisions that deck on demand: ``sim_config['netlist_source']`` picks
  between a netlist-only ADE-L session through the database interface's
  ``create_netlist`` (``'ade'``, the default) and the standalone ``si``
  netlister (``'si'``), which regenerates the circuit body without any
  ADE session and splices it back into the assembled deck (see
  :meth:`DirectSimInterface.create_netlist_with_si`).  Either way a prior
  ADE-L *run* is not required, but the ``si`` path still needs one ADE-L
  netlist step ever to have produced the deck (its control section --
  analyses, model includes, design variables -- comes from ADE).
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
import subprocess

import bag.io
from .simulator import SimProcessManager


class SiNetlistPrereqError(ValueError):
    """The si netlist path is missing one of its splice inputs.

    Raised before ``si`` is even invoked: the deck, the ``netlist`` body
    file, or ``si.env`` does not exist (or the deck no longer embeds the
    body verbatim).  ``ensure_netlist`` treats this as "provision through
    ADE-L instead" when a database interface is available, because these
    inputs only exist after one ADE-L netlist step.
    """


class SiNetlistError(Exception):
    """The ``si`` netlist run itself failed (bad exit code, timeout).

    Unlike :class:`SiNetlistPrereqError` this is *not* recoverable by
    falling back to ADE-L -- the OA design most likely does not netlist,
    and ADE would only fail the same way later.
    """


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
        ``netlist_source``
            how ``ensure_netlist`` refreshes a stale deck: ``'ade'``
            (default; netlist-only ADE-L session through the database
            interface) or ``'si'`` (standalone ``si`` netlister, see
            :meth:`create_netlist_with_si`).
        ``si_command`` / ``si_args`` / ``si_timeout`` / ``si_cwd``
            ``si`` invocation overrides: the executable (string or argv
            prefix list), extra trailing arguments, run timeout in
            seconds, and the working directory ``si`` resolves ``cds.lib``
            from (defaults to ``$BAG_WORK_DIR``).
    """

    #: subclass default simulator executable.
    default_command = ''
    #: subclass default netlist template.
    default_netlist = ''
    #: name of the deck copy inside the save directory.
    deck_name = 'input.ckt'
    #: default ``si`` executable of the si netlist path.
    default_si_command = 'si'
    #: seconds to wait for the ``si`` netlist run.
    si_timeout = 300.0
    #: name of the circuit body file the netlister writes next to the deck.
    netlist_body_name = 'netlist'
    #: name of the netlister configuration file next to the deck.
    si_env_name = 'si.env'

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
        """Ensure the source deck exists, refreshing it when stale.

        Parameters
        ----------
        db : bag.interface.database.DbAccess or None
            database interface whose ``create_netlist`` provisions the deck
            (the ADE-L skill interface).  With
            ``sim_config['netlist_source'] == 'si'`` it is only used as the
            first-provisioning fallback and may be None once the deck
            exists.
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
                recreate when the deck is stale.  The ADE source compares
                the deck against the testbench cell's own OA views, so
                DUT-side edits are *not* seen -- use ``'always'`` after
                regenerating the DUT.  The si source instead lets the
                netlister's own incremental check walk the whole design
                hierarchy, so DUT edits *are* picked up.
            ``'always'``
                recreate unconditionally (the si source forces a full
                renetlist via ``simNotIncremental``).

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
        source = self.sim_config.get('netlist_source', 'ade')
        if source not in ('ade', 'si'):
            raise ValueError('%s: unknown netlist_source: %s'
                             % (type(self).__name__, source))
        if refresh == 'never':
            return self.resolve_netlist(lib, cell)

        if source == 'si':
            try:
                return self.create_netlist_with_si(
                    lib, cell, force=(refresh == 'always'))
            except SiNetlistPrereqError:
                # the splice inputs only exist after one ADE-L netlist
                # step; provision through ADE-L when we can, else let the
                # prerequisite error explain what is missing.
                create = getattr(db, 'create_netlist', None)
                if create is None:
                    raise
                return create(lib, cell)

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

    def create_netlist_with_si(self, lib, cell, force=False):
        # type: (str, str, bool) -> str
        """Refresh the deck's circuit body with the standalone ``si`` netlister.

        ADE assembles the deck (``input.scs``) by concatenating the
        netlister's circuit output (the ``netlist`` file next to it) with
        an ADE-owned control section (design variables, model includes,
        analyses).  Only the circuit body goes stale when the design is
        regenerated, and the netlister that produces it is the same OSS
        netlister ``si -batch -command netlist`` drives from the ``si.env``
        ADE leaves in the netlist directory.  So: rerun ``si`` on that
        directory, then splice the regenerated body back into the deck in
        place of the old one.  No ADE session, no skill server -- but the
        deck, body, and ``si.env`` must exist from one prior ADE-L netlist
        step (:class:`SiNetlistPrereqError` otherwise).

        Parameters
        ----------
        lib : str
            testbench library name (only used for error messages; the
            netlist directory identifies the design).
        cell : str
            testbench cell name.
        force : bool
            True to force a full renetlist (``simNotIncremental``) instead
            of the netlister's incremental timestamp check.

        Returns
        -------
        deck : str
            the netlist deck path.
        """
        deck = self.netlist_path(lib, cell)
        nl_dir = os.path.dirname(deck)
        body_path = os.path.join(nl_dir, self.netlist_body_name)
        env_path = os.path.join(nl_dir, self.si_env_name)
        for path, desc in ((deck, 'netlist deck'),
                           (body_path, 'circuit body file'),
                           (env_path, 'si.env')):
            if not os.path.isfile(path):
                raise SiNetlistPrereqError(
                    '%s: %s not found: %s (the si netlist path needs one '
                    'prior ADE-L netlist step for %s__%s).'
                    % (type(self).__name__, desc, path, lib, cell))
        deck_text = bag.io.read_file(deck)
        old_body = bag.io.read_file(body_path)
        if not old_body.strip() or old_body not in deck_text:
            raise SiNetlistPrereqError(
                '%s: %s is not embedded verbatim in %s; the deck was '
                'hand-edited or assembled differently, so the si splice '
                'cannot locate the circuit section.'
                % (type(self).__name__, body_path, deck))
        self._run_si(nl_dir, force=force)
        new_body = bag.io.read_file(body_path)
        if new_body != old_body:
            bag.io.write_file(deck, deck_text.replace(old_body, new_body, 1))
        return deck

    def _run_si(self, nl_dir, force=False):
        # type: (str, bool) -> None
        """Run ``si -batch -command netlist`` on the netlist directory.

        ``si`` reads ``si.env`` from the run directory and resolves
        ``cds.lib`` from its working directory, so the subprocess runs in
        ``sim_config['si_cwd']`` (default ``$BAG_WORK_DIR``, the workspace
        root).  With ``force`` the run directory's ``si.env`` temporarily
        gets ``simNotIncremental = 't`` (restored afterwards) -- there is
        no command-line equivalent.
        """
        env_path = os.path.join(nl_dir, self.si_env_name)
        original = None
        if force:
            original = bag.io.read_file(env_path)
            patched, n_sub = re.subn(r'(?m)^\s*simNotIncremental\s*=.*$',
                                     "simNotIncremental = 't", original)
            if n_sub == 0:
                patched = patched.rstrip('\n') + "\nsimNotIncremental = 't\n"
            bag.io.write_file(env_path, patched)
        try:
            cmd_cfg = self.sim_config.get('si_command', self.default_si_command)
            if isinstance(cmd_cfg, (list, tuple)):
                cmd = list(cmd_cfg)
            else:
                cmd = [cmd_cfg]
            cmd += [nl_dir, '-batch', '-command', 'netlist']
            cmd += list(self.sim_config.get('si_args', ()))
            cwd = (self.sim_config.get('si_cwd')
                   or os.environ.get('BAG_WORK_DIR', '.'))
            timeout = float(self.sim_config.get('si_timeout', self.si_timeout))
            try:
                proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise SiNetlistError(
                    '%s: si netlist run timed out after %g s in %s '
                    '(command: %s)'
                    % (type(self).__name__, timeout, nl_dir, ' '.join(cmd)))
            output = proc.stdout.decode('utf-8', errors='replace')
            if proc.returncode != 0 or '*Error*' in output:
                tail = '\n'.join(output.splitlines()[-20:])
                raise SiNetlistError(
                    '%s: si netlist run failed (exit %d) in %s; last '
                    'output:\n%s\n(see si.log in the run directory)'
                    % (type(self).__name__, proc.returncode, nl_dir, tail))
        finally:
            if original is not None:
                bag.io.write_file(env_path, original)

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
