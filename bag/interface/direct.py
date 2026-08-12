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
            interface), ``'si'`` (standalone ``si`` batch netlister), or
            ``'ocean'`` (headless ``ocean -nograph`` ``createNetlist()``
            session).  The si and ocean sources share the body-splice
            machinery of :meth:`create_netlist_with_si`.  Note ``si``
            proved unusable on installs whose ``Virtuoso_Spectre``
            license feature is expired (the encrypted OSS hnl driver
            exits silently); ``ocean`` runs the same OSS netlister under
            an available ADE/MMSIM license and is the verified choice
            there.
        ``si_command`` / ``si_args`` / ``si_timeout`` / ``si_cwd``
            standalone-netlister invocation overrides: the ``si``
            executable (string or argv prefix list), extra trailing
            arguments, run timeout in seconds, and the working directory
            the netlister resolves ``cds.lib`` from (defaults to
            ``$BAG_WORK_DIR``).  ``si_timeout``/``si_cwd`` also apply to
            the ocean source.
        ``ocean_command`` / ``ocean_args``
            ocean-source overrides: the ``ocean`` executable (string or
            argv prefix list) and extra arguments inserted before
            ``-replay``.
    """

    #: subclass default simulator executable.
    default_command = ''
    #: subclass default netlist template.
    default_netlist = ''
    #: name of the deck copy inside the save directory.
    deck_name = 'input.ckt'
    #: default ``si`` executable of the si netlist path.
    default_si_command = 'si'
    #: default ``ocean`` executable of the ocean netlist path.
    default_ocean_command = 'ocean'
    #: seconds to wait for a standalone netlist run (si or ocean).
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
        if source not in ('ade', 'si', 'ocean'):
            raise ValueError('%s: unknown netlist_source: %s'
                             % (type(self).__name__, source))
        if refresh == 'never':
            return self.resolve_netlist(lib, cell)

        if source in ('si', 'ocean'):
            try:
                return self.create_netlist_with_si(
                    lib, cell, force=(refresh == 'always'), runner=source)
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

    def create_netlist_with_si(self, lib, cell, force=False, runner='si'):
        # type: (str, str, bool, str) -> str
        """Refresh the deck's circuit body with a standalone netlist run.

        ADE assembles the deck (``input.scs``) by concatenating the
        netlister's circuit output (the ``netlist`` file next to it) with
        an ADE-owned control section (design variables, model includes,
        analyses).  Only the circuit body goes stale when the design is
        regenerated, and it can be rebuilt without an ADE session: rerun
        the OSS netlister on the netlist directory, then splice the
        regenerated body back into the deck in place of the old one.  Two
        runners drive the netlister:

        ``'si'``
            ``si -batch -command netlist`` on the ``si.env`` ADE leaves in
            the netlist directory.
        ``'ocean'``
            a generated ``ocean -nograph`` script whose ``createNetlist()``
            regenerates the same directory (needs no ``si.env``; the
            design identity comes from the netlist path and the ocean
            session holds an ADE/MMSIM license instead of the
            ``Virtuoso_Spectre`` feature ``si`` insists on).

        No skill server either way -- but the deck and body must exist
        from one prior ADE-L netlist step (:class:`SiNetlistPrereqError`
        otherwise).  The regenerated body may name subcircuits differently
        from the ADE run (OSS uniquification depends on the session); the
        deck stays self-consistent because instance lines and subckt
        definitions are both inside the body.

        Parameters
        ----------
        lib : str
            testbench library name (the ocean runner netlists this
            library; the si runner takes it from ``si.env``).
        cell : str
            testbench cell name.
        force : bool
            True to force a full renetlist (``simNotIncremental`` /
            ``?recreateAll t``) instead of the netlister's incremental
            timestamp check.
        runner : str
            ``'si'`` or ``'ocean'``.

        Returns
        -------
        deck : str
            the netlist deck path.
        """
        if runner not in ('si', 'ocean'):
            raise ValueError('%s: unknown standalone netlist runner: %s'
                             % (type(self).__name__, runner))
        deck = self.netlist_path(lib, cell)
        nl_dir = os.path.dirname(deck)
        body_path = os.path.join(nl_dir, self.netlist_body_name)
        env_path = os.path.join(nl_dir, self.si_env_name)
        required = [(deck, 'netlist deck'), (body_path, 'circuit body file')]
        if runner == 'si':
            required.append((env_path, 'si.env'))
        for path, desc in required:
            if not os.path.isfile(path):
                raise SiNetlistPrereqError(
                    '%s: %s not found: %s (the standalone netlist path '
                    'needs one prior ADE-L netlist step for %s__%s).'
                    % (type(self).__name__, desc, path, lib, cell))
        deck_text = bag.io.read_file(deck)
        old_body = bag.io.read_file(body_path)
        if not old_body.strip() or old_body not in deck_text:
            raise SiNetlistPrereqError(
                '%s: %s is not embedded verbatim in %s; the deck was '
                'hand-edited or assembled differently, so the splice '
                'cannot locate the circuit section.'
                % (type(self).__name__, body_path, deck))
        if runner == 'si':
            self._run_si(nl_dir, force=force)
        else:
            self._run_ocean(nl_dir, lib, cell, force=force)
        new_body = bag.io.read_file(body_path)
        if new_body == old_body:
            new_deck = deck_text
        else:
            new_deck = deck_text.replace(old_body, new_body, 1)
        # compare against the CURRENT on-disk deck, not the pre-run
        # snapshot: the ocean runner reassembles input.scs itself (without
        # the ADE control section), so the spliced deck must be restored
        # even when the body came back unchanged.
        if bag.io.read_file(deck) != new_deck:
            bag.io.write_file(deck, new_deck)
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
            self._run_netlister_cmd(cmd, nl_dir, 'si')
        finally:
            if original is not None:
                bag.io.write_file(env_path, original)

    def _run_ocean(self, nl_dir, lib, cell, force=False):
        # type: (str, str, str, bool) -> None
        """Regenerate the netlist directory through ``ocean -nograph``.

        Writes a throwaway ocean script whose ``createNetlist()`` targets
        the same project layout the deck lives in
        (``<project>/<cell>/<simulator>/<view>/netlist``); simulator, view,
        and project directory are recovered from the netlist path.  The
        subprocess runs in ``sim_config['si_cwd']`` (default
        ``$BAG_WORK_DIR``) so cds.lib and the workspace ``.cdsenv``
        resolve as usual.
        """
        view_dir, _nl = os.path.split(os.path.abspath(nl_dir))
        sim_dir, view_name = os.path.split(view_dir)
        cell_dir, sim_name = os.path.split(sim_dir)
        proj_dir, path_cell = os.path.split(cell_dir)
        if path_cell != cell:
            raise SiNetlistPrereqError(
                '%s: netlist path %s does not follow the '
                '<project>/<cell>/<simulator>/<view>/netlist layout for '
                'cell %s; the ocean runner cannot recover the project '
                'directory.' % (type(self).__name__, nl_dir, cell))
        script = (
            'envSetVal("asimenv.startup" "projectDir" \'string "%s")\n'
            'simulator(\'%s)\n'
            'design("%s" "%s" "%s")\n'
            'ok = createNetlist(?recreateAll %s ?display nil)\n'
            'unless(ok exit(1))\n'
            'exit\n'
            % (proj_dir, sim_name, lib, cell, view_name,
               't' if force else 'nil'))
        script_dir = bag.io.make_temp_dir(prefix='ocean_netlist',
                                          parent_dir=self.tmp_dir)
        script_path = os.path.join(script_dir, 'netlist.ocn')
        bag.io.write_file(script_path, script)
        cmd_cfg = self.sim_config.get('ocean_command',
                                      self.default_ocean_command)
        if isinstance(cmd_cfg, (list, tuple)):
            cmd = list(cmd_cfg)
        else:
            cmd = [cmd_cfg]
        cmd += ['-nograph', '-log', os.path.join(script_dir, 'ocean.log')]
        cmd += list(self.sim_config.get('ocean_args', ()))
        cmd += ['-replay', script_path]
        self._run_netlister_cmd(cmd, nl_dir, 'ocean')

    def _run_netlister_cmd(self, cmd, nl_dir, what):
        # type: (List[str], str, str) -> None
        """Run a standalone netlister command and raise on failure."""
        cwd = (self.sim_config.get('si_cwd')
               or os.environ.get('BAG_WORK_DIR', '.'))
        timeout = float(self.sim_config.get('si_timeout', self.si_timeout))
        try:
            proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise SiNetlistError(
                '%s: %s netlist run timed out after %g s for %s '
                '(command: %s)'
                % (type(self).__name__, what, timeout, nl_dir, ' '.join(cmd)))
        output = proc.stdout.decode('utf-8', errors='replace')
        if proc.returncode != 0 or '*Error*' in output:
            tail = '\n'.join(output.splitlines()[-20:])
            raise SiNetlistError(
                '%s: %s netlist run failed (exit %d) for %s; last '
                'output:\n%s'
                % (type(self).__name__, what, proc.returncode, nl_dir, tail))

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
