# -*- coding: utf-8 -*-

"""ADE session flavors for the skill database interface.

Virtuoso has several simulation front-ends (ADE-XL, ADE-L, Maestro), each
driven through its own SKILL function family
(``run_scripts/bag_adexl_session.il`` / ``bag_adel_session.il``).  Each
flavor lives in its own session class here;
:class:`~bag.interface.skill.SkillInterface` delegates every testbench
operation to the session selected by the ``testbench.flavor`` entry of the
database configuration (falling back to the interface class default).
"""

from typing import List, Dict, Optional, Tuple

import glob
import os
import sqlite3
import time

import yaml

import bag
from .skill import to_skill_list_str


def stimuli_to_spec(stimuli):
    """Encode a Testbench stimulus request as the SKILL-side spec list.

    ``None`` (no request) maps to an empty list, which the SKILL side
    treats as "leave the setup untouched"; an empty sequence maps to
    ``['clear']`` (empty the testbench's stimulus file); a non-empty
    sequence of spectre lines maps to ``['set', line, ...]``.
    """
    if stimuli is None:
        return []
    stimuli = list(stimuli)
    if not stimuli:
        return ['clear']
    return ['set'] + stimuli


class AdeSession(object):
    """Base class for one ADE flavor's testbench operations.

    Parameters
    ----------
    db : :class:`bag.interface.skill.SkillInterface`
        the database interface used to evaluate skill expressions.
    """

    #: flavor name used in ``db_config['testbench']['flavor']``.
    flavor = ''

    def __init__(self, db):
        self.db = db

    @property
    def db_config(self):
        return self.db.db_config

    @property
    def tmp_dir(self):
        return self.db.tmp_dir

    def _eval_skill(self, expr, input_files=None, out_file=None):
        return self.db._eval_skill(expr, input_files=input_files, out_file=out_file)

    def configure_testbench(self, tb_lib, tb_cell):
        raise NotImplementedError('%s does not implement configure_testbench.'
                                  % type(self).__name__)

    def get_testbench_info(self, tb_lib, tb_cell):
        raise NotImplementedError('%s does not implement get_testbench_info.'
                                  % type(self).__name__)

    def update_testbench(self, lib, cell, parameters, sim_envs, config_rules,
                         env_parameters):
        raise NotImplementedError('%s does not implement update_testbench.'
                                  % type(self).__name__)

    def run_simulation(self, lib, cell, res_file_name=None):
        raise NotImplementedError(
            '%s does not run simulations through the database interface; '
            'use the simulation interface instead.' % type(self).__name__)

    def create_netlist(self, lib, cell):
        raise NotImplementedError(
            '%s does not create standalone netlist decks; only the ADE-L '
            'flavor implements create_netlist.' % type(self).__name__)


class AdexlSession(AdeSession):
    """ADE-XL session flavor (``bag_adexl_session.il`` function family).

    ``run_simulation`` drives the view's saved setup through
    ``axlRunSimulation`` in the live Virtuoso session.  The run is
    asynchronous on the SKILL side (ICRP job sessions execute the points
    and the main session saves the history), so completion is detected
    here by watching the view's history result database instead of
    blocking inside SKILL -- a blocking SKILL call would starve the very
    event processing that finishes the run.  Requires a display-attached
    Virtuoso: without a provisioned job policy, ICRP jobs are only
    dispatched in GUI sessions.
    """

    flavor = 'adexl'

    #: cellview holding the ADE setup (:class:`MaestroSession` differs).
    tb_view = 'adexl'
    #: subdirectories of ``<cell>/<tb_view>/results`` that may hold the run
    #: history databases ('data' for adexl; maestro views use 'maestro', or
    #: 'data' when the setup was authored through ocean-XL).  All candidates
    #: are polled, so whichever one the run writes is picked up.
    results_subdirs = ('data',)
    #: seconds to wait for the run history database before giving up.
    sim_timeout = 3600.0
    #: seconds between history database polls.
    sim_poll_interval = 5.0
    #: seconds to keep polling after the first job-log error line, in case
    #: the run still completes (e.g. only some points failed).
    sim_err_grace = 30.0
    #: SKILL entry point ``update_testbench`` drives (:class:`MaestroSession`
    #: overrides it with the maestro-view wrapper).
    modify_fn = 'modify_testbench'
    #: glob (relative to the workspace root the client runs from) of the
    #: ICRP job logs watched for run errors.  Runs that die before writing
    #: a history database (netlist errors, license failures) only surface
    #: here; without this the poll would sit out the full timeout.
    job_log_glob = os.path.join('logs_*', 'logs*', 'Job*.log')

    def configure_testbench(self, tb_lib, tb_cell):
        """Update testbench state for the given testbench.

        This method fill in process-specific information for the given testbench.

        Parameters
        ----------
        tb_lib : str
            testbench library name.
        tb_cell : str
            testbench cell name.

        Returns
        -------
        cur_env : str
            the current simulation environment.
        envs : list[str]
            a list of available simulation environments.
        parameters : dict[str, str]
            a list of testbench parameter values, represented as string.
        """
        tb_config = self.db_config['testbench']

        cmd = ('instantiate_testbench("{tb_cell}" "{targ_lib}" ' +
               '"{config_libs}" "{config_views}" "{config_stops}" ' +
               '"{default_corner}" "{corner_file}" {def_files} ' +
               '"{tech_lib}" {result_file})')
        cmd = cmd.format(tb_cell=tb_cell,
                         targ_lib=tb_lib,
                         config_libs=tb_config['config_libs'],
                         config_views=tb_config['config_views'],
                         config_stops=tb_config['config_stops'],
                         default_corner=tb_config['default_env'],
                         corner_file=tb_config['env_file'],
                         def_files=to_skill_list_str(tb_config['def_files']),
                         tech_lib=self.db_config['schematic']['tech_lib'],
                         result_file='{result_file}')
        output = yaml.load(self._eval_skill(cmd, out_file='result_file'), Loader=yaml.FullLoader)
        return tb_config['default_env'], output['corners'], output['parameters'], output['outputs']

    def get_testbench_info(self, tb_lib, tb_cell):
        """Returns information about an existing testbench.

        Parameters
        ----------
        tb_lib : str
            testbench library.
        tb_cell : str
            testbench cell.

        Returns
        -------
        cur_envs : list[str]
            the current simulation environments.
        envs : list[str]
            a list of available simulation environments.
        parameters : dict[str, str]
            a list of testbench parameter values, represented as string.
        outputs : dict[str, str]
            a list of testbench output expressions.
        """
        cmd = 'get_testbench_info("{tb_lib}" "{tb_cell}" {result_file})'
        cmd = cmd.format(tb_lib=tb_lib,
                         tb_cell=tb_cell,
                         result_file='{result_file}')
        output = yaml.load(self._eval_skill(cmd, out_file='result_file'), Loader=yaml.FullLoader)
        return output['enabled_corners'], output['corners'], output['parameters'], output['outputs']

    def update_testbench(self,
                         lib,  # type: str
                         cell,  # type: str
                         parameters,  # type: Dict[str, str]
                         sim_envs,  # type: List[str]
                         config_rules,  # type: List[List[str]]
                         env_parameters,  # type: List[List[Tuple[str, str]]]
                         stimuli=None,  # type: Optional[List[str]]
                         ):
        # type: (...) -> None
        """Update the given testbench configuration.

        Parameters
        ----------
        lib : str
            testbench library.
        cell : str
            testbench cell.
        parameters : Dict[str, str]
            testbench parameters.
        sim_envs : List[str]
            list of enabled simulation environments.
        config_rules : List[List[str]]
            config view mapping rules, list of (lib, cell, view) rules.
        env_parameters : List[List[Tuple[str, str]]]
            list of param/value list for each simulation environment.
        stimuli : Optional[List[str]]
            spectre lines to inject through the ADE stimulus file
            (Setup -> Simulation Files).  None leaves the setup's
            stimulus file untouched; an empty list empties it.
        """

        cmd = ('%s("%s" "%s" {conf_rules} {run_opts} '
               '{sim_envs} {params} {env_params} "%s" {stimuli})'
               % (self.modify_fn, lib, cell, self.tb_view))
        in_files = {'conf_rules': config_rules,
                    'run_opts': [],
                    'sim_envs': sim_envs,
                    'params': list(parameters.items()),
                    'env_params': list(zip(sim_envs, env_parameters)),
                    'stimuli': stimuli_to_spec(stimuli),
                    }
        self._eval_skill(cmd, input_files=in_files)

    def run_simulation(self, lib, cell, res_file_name=None):
        """Run the testbench's saved ADE-XL setup and return its outputs.

        Parameters
        ----------
        lib : str
            testbench library.
        cell : str
            testbench cell.
        res_file_name : str or None
            unused (results are read from the history database); kept for
            signature compatibility with :class:`AdelSession`.

        Returns
        -------
        results : dict[str, float]
            evaluated output expression values.  For multi-point runs
            (several corners/sweep points) each value is a dict keyed by
            the run's point ID instead of a scalar.
        """
        lib_path = self._eval_skill('ddGetObj("%s")~>readPath' % lib).strip().strip('"')
        if not os.path.isdir(lib_path):
            raise Exception('run_simulation: cannot resolve library path '
                            'of %s (got %r)' % (lib, lib_path))
        rdb_dirs = [os.path.join(lib_path, cell, self.tb_view, 'results', sub)
                    for sub in self.results_subdirs]

        # snapshot the history databases before submitting so completion is
        # detected as a change against this baseline.  Comparing file mtimes
        # against the local clock does not work here: NFS stamps the files
        # with the file server's clock, which can differ from the client
        # host's by minutes.
        baseline = self._rdb_snapshot(rdb_dirs)
        log_sizes = self._job_log_sizes()
        session_name = self._eval_skill(
            'adexl_start_simulation("%s" "%s" "%s")'
            % (lib, cell, self.tb_view)).strip().strip('"')
        try:
            return self._wait_for_results(rdb_dirs, baseline, log_sizes,
                                          lib, cell)
        finally:
            self._eval_skill('adexl_close_simulation("%s")' % session_name)

    @staticmethod
    def _rdb_snapshot(rdb_dirs):
        """Return {path: (mtime, size)} for the history databases."""
        snap = {}
        for rdb_dir in rdb_dirs:
            for fname in glob.glob(os.path.join(rdb_dir, '*.rdb')):
                try:
                    st = os.stat(fname)
                except OSError:
                    continue
                snap[fname] = (st.st_mtime, st.st_size)
        return snap

    def _job_log_sizes(self):
        """Return {path: size} for the ICRP job logs."""
        sizes = {}
        for fname in glob.glob(self.job_log_glob):
            try:
                sizes[fname] = os.path.getsize(fname)
            except OSError:
                continue
        return sizes

    def _scan_job_logs(self, log_sizes):
        """Return the first error line appended to a job log, or None.

        ``log_sizes`` tracks how far each log has been read and is updated
        in place.
        """
        for fname in glob.glob(self.job_log_glob):
            offset = log_sizes.get(fname, 0)
            try:
                with open(fname, 'r', errors='replace') as stream:
                    stream.seek(offset)
                    chunk = stream.read()
                    log_sizes[fname] = stream.tell()
            except OSError:
                continue
            for line in chunk.splitlines():
                if 'ERROR (' in line or '*Error*' in line:
                    return '%s: %s' % (fname, line.strip())
        return None

    def _wait_for_results(self, rdb_dirs, baseline, log_sizes, lib, cell):
        """Poll the history databases until one changes and is readable.

        Job logs are watched alongside: after the first error line the
        deadline shrinks to a short grace period, so runs that die without
        writing a history database fail fast instead of sitting out the
        full timeout.
        """
        deadline = time.time() + self.sim_timeout
        fail_reason = None
        while time.time() < deadline:
            time.sleep(self.sim_poll_interval)
            for fname, state in sorted(self._rdb_snapshot(rdb_dirs).items()):
                if baseline.get(fname) == state:
                    continue
                results = self._read_history_results(fname)
                if results is not None:
                    return results
            if fail_reason is None:
                fail_reason = self._scan_job_logs(log_sizes)
                if fail_reason is not None:
                    deadline = min(deadline,
                                   time.time() + self.sim_err_grace)
        if fail_reason is not None:
            raise Exception('adexl run for %s__%s failed: %s'
                            % (lib, cell, fail_reason))
        raise Exception(
            'adexl run for %s__%s produced no history result database in '
            '%s within %g seconds; check the ADE-XL job logs '
            '(%s in the workspace).'
            % (lib, cell, ' / '.join(rdb_dirs), self.sim_timeout,
               self.job_log_glob))

    @staticmethod
    def _read_history_results(rdb_file):
        """Read evaluated outputs from a history rdb (SQLite) file.

        Returns None while the database is still being written or has no
        result rows yet; raises if the run recorded evaluation errors.
        """
        try:
            con = sqlite3.connect('file:%s?mode=ro' % rdb_file, uri=True)
        except sqlite3.Error:
            return None
        try:
            try:
                rows = con.execute(
                    'SELECT v.pointID, r.name, v.value, v.errorID '
                    'FROM result r JOIN resultValue v '
                    'ON r.resultID = v.resultID').fetchall()
            except sqlite3.Error:
                return None
            if not rows:
                return None
            # maestro histories also list saved signal traces (rows named
            # after nets, with no value); only output expressions carry
            # values ('wave' for waveform outputs).
            valued = [row for row in rows if row[2] not in (None, '')]
            if not valued:
                # an output whose evaluation failed has errorID set and no
                # value; a run where EVERY output failed leaves no valued
                # rows at all, and returning None here would make the poll
                # sit out the full timeout instead of failing fast (hit
                # 2026-08-12, a too-large injected load broke every
                # measurement).
                failed = sorted({name for _p, name, _v, err in rows
                                 if err is not None})
                if failed:
                    raise Exception('adexl run recorded evaluation errors '
                                    'for outputs: %s (see %s)'
                                    % (', '.join(failed), rdb_file))
                return None
            rows = valued
            points = sorted({p for p, _n, _v, _e in rows})
            if len(points) <= 1:
                return {name: value for _p, name, value, _e in rows}
            multi = {}
            for point, name, value, _err in rows:
                multi.setdefault(name, {})[point] = value
            return multi
        finally:
            con.close()


class AdelSession(AdexlSession):
    """ADE-L session flavor (``bag_adel_session.il`` function family).

    The testbench is opened in place: the OA schematic, config view, and
    the saved ADE state (``spectre_state1``) must already exist in the
    testbench library.  ``run_simulation`` drives ``adel_run_simulation``
    through the skill server.

    Inherits :class:`AdexlSession` so ``get_testbench_info`` keeps its
    historical (ADE-XL path) behavior, matching the old
    ``ADELSkillInterface(SkillInterface)`` override relationship.
    """

    flavor = 'adel'

    lib_name = None   # library name of the last configured testbench
    cell_name = None  # cell name of the last configured testbench

    def configure_testbench(self, tb_lib, tb_cell):
        """Update testbench state for the given testbench.

        This method fill in process-specific information for the given testbench.

        Parameters
        ----------
        tb_lib : str
            testbench library name.
        tb_cell : str
            testbench cell name.

        Returns
        -------
        cur_env : str
            the current simulation environment.
        envs : list[str]
            a list of available simulation environments.
        parameters : dict[str, str]
            a list of testbench parameter values, represented as string.
        """
        self.lib_name = tb_lib
        self.cell_name = tb_cell
        # mirrored on the interface for backward compatibility.
        self.db.lib_name = tb_lib
        self.db.cell_name = tb_cell

        tb_config = self.db_config['testbench']

        cmd = ('adel_instantiate_testbench("{tb_cell}" "{targ_lib}" ' +
               '"{config_libs}" "{config_views}" "{config_stops}" ' +
               '"{default_corner}" "{corner_file}" {def_files} ' +
               '"{tech_lib}" {result_file})')
        cmd = cmd.format(tb_cell=tb_cell,
                         targ_lib=tb_lib,
                         config_libs=tb_config['config_libs'],
                         config_views=tb_config['config_views'],
                         config_stops=tb_config['config_stops'],
                         default_corner=tb_config['default_env'],
                         corner_file=tb_config['env_file'],
                         def_files=to_skill_list_str(tb_config['def_files']),
                         tech_lib=self.db_config['schematic']['tech_lib'],
                         result_file='{result_file}')
        output = yaml.load(self._eval_skill(cmd, out_file='result_file'), Loader=yaml.FullLoader)
        return tb_config['default_env'], output['corners'], output['parameters'], output['outputs']

    #: cellview holding the saved ADE-L setup state.
    tb_ade_view = 'spectre_state1'

    def update_testbench(self,
                         lib,  # type: str
                         cell,  # type: str
                         parameters,  # type: Dict[str, str]
                         sim_envs,  # type: List[str]
                         config_rules,  # type: List[List[str]]
                         env_parameters,  # type: List[List[Tuple[str, str]]]
                         stimuli=None,  # type: Optional[List[str]]
                         ):
        # type: (...) -> None
        """Update the given testbench configuration.

        Parameters
        ----------
        lib : str
            testbench library.
        cell : str
            testbench cell.
        parameters : Dict[str, str]
            testbench parameters.
        sim_envs : List[str]
            list of enabled simulation environments.
        config_rules : List[List[str]]
            config view mapping rules, list of (lib, cell, view) rules.
        env_parameters : List[List[Tuple[str, str]]]
            list of param/value list for each simulation environment.
        stimuli : Optional[List[str]]
            spectre lines to inject through the ADE stimulus file
            (Setup -> Simulation Files).  None leaves the setup's
            stimulus file untouched; an empty list empties it.
        """

        tb_config = self.db_config['testbench']
        corner_file = tb_config['env_file']
        cmd = ('adel_modify_testbench("%s" "%s" {conf_rules} {run_opts} "%s" '
               '{sim_envs} {params} {env_params} "%s" {stimuli})'
               % (lib, cell, corner_file, self.tb_ade_view))
        in_files = {'conf_rules': config_rules,
                    'run_opts': [],
                    'sim_envs': sim_envs,
                    'params': list(parameters.items()),
                    'env_params': list(zip(sim_envs, env_parameters)),
                    'stimuli': stimuli_to_spec(stimuli),
                    }
        self._eval_skill(cmd, input_files=in_files)

    #: seconds to wait for the netlist deck after adel_create_netlist.
    netlist_timeout = 300.0
    #: seconds between netlist deck polls.
    netlist_poll_interval = 1.0
    #: deck path template; ``testbench.netlist_path`` in the database
    #: configuration overrides it.  The default matches both the ADE-L
    #: project layout and bag.interface.spectre.SpectreInterface.
    netlist_path_template = ('{work_dir}/simulation/{cell}/spectre/config/'
                             'netlist/input.scs')

    def netlist_path(self, lib, cell):
        """Return the deck path the ADE-L netlist step writes."""
        template = self.db_config['testbench'].get(
            'netlist_path', self.netlist_path_template)
        work_dir = os.environ.get('BAG_WORK_DIR', '.')
        return template.format(work_dir=work_dir, lib=lib, cell=cell)

    def create_netlist(self, lib, cell):
        """(Re)create the ADE-L netlist deck without running the simulation.

        Drives ``adel_create_netlist`` (``bag_adel_session.il``), which
        opens the saved config-view/state session and recreates the deck the
        direct simulator interfaces (:mod:`bag.interface.direct`) re-run
        outside Virtuoso.  Completion is detected as a change of the deck
        file against a pre-call ``(mtime, size)`` snapshot -- both stamps
        come from the same file server, so comparing them sidesteps the
        NFS-vs-local clock skew that rules out deadline-style mtime checks.

        Returns
        -------
        deck : str
            the netlist deck path.
        """
        deck = self.netlist_path(lib, cell)
        try:
            before = (os.path.getmtime(deck), os.path.getsize(deck))
        except OSError:
            before = None
        self._eval_skill('adel_create_netlist("%s" "%s")' % (lib, cell))
        elapsed = 0.0
        while elapsed <= self.netlist_timeout:
            try:
                after = (os.path.getmtime(deck), os.path.getsize(deck))
            except OSError:
                after = None
            if after is not None and after != before:
                return deck
            time.sleep(self.netlist_poll_interval)
            elapsed += self.netlist_poll_interval
        if before is not None:
            raise Exception(
                'adel_create_netlist left %s unchanged for %g s; the '
                'netlister may have skipped an up-to-date deck (only the '
                'incremental sevNetlist call is available on this ADE '
                'build).' % (deck, self.netlist_timeout))
        raise Exception('adel_create_netlist did not produce %s within %g s'
                        % (deck, self.netlist_timeout))

    def run_simulation(self, lib, cell, res_file_name=None):
        """Run ADE-L simulation"""
        if res_file_name is None:
            res_file_name = 'sim_results.yaml'
        save_dir = bag.io.make_temp_dir(prefix='adel_data', parent_dir=self.tmp_dir)
        save_full_path = save_dir + '/' + res_file_name
        cmd = ('adel_run_simulation("%s" "%s" "%s")' % (lib, cell, save_full_path))
        self._eval_skill(cmd)
        if os.path.exists(save_full_path):
            with open(save_full_path, 'r') as stream:
                results = yaml.load(stream, Loader=yaml.FullLoader)
        return results


class MaestroSession(AdexlSession):
    """Maestro (ADE Assembler) session flavor (``bag_maestro_session.il``).

    ADE Assembler is the successor to ADE-XL and drives the same ``axl*``
    SKILL API, so this reuses :class:`AdexlSession` with the ``maestro``
    cellview.  The maestro view (``maestro.sdb`` + ``active.state``) is
    authored in Virtuoso and opened in place, so there is no separate
    instantiate step: ``configure_testbench`` reads the existing setup the
    same way ``get_testbench_info`` does.  ``update_testbench`` and
    ``run_simulation`` are the inherited ADE-XL implementations, pointed at
    the ``maestro`` view.
    """

    flavor = 'maestro'

    def configure_testbench(self, tb_lib, tb_cell):
        """Read the pre-built maestro setup for the given testbench.

        Parameters
        ----------
        tb_lib : str
            testbench library name.
        tb_cell : str
            testbench cell name.

        Returns
        -------
        cur_env : str
            the current simulation environment (from the config default).
        envs : list[str]
            a list of available simulation environments.
        parameters : dict[str, str]
            testbench parameter values, as strings.
        outputs : dict[str, str]
            testbench output expressions.
        """
        _enabled, corners, params, outputs = self.get_testbench_info(tb_lib, tb_cell)
        default_env = self.db_config['testbench']['default_env']
        return default_env, corners, params, outputs

    def get_testbench_info(self, tb_lib, tb_cell):
        """Returns corner/parameter/output information of a maestro testbench.

        Parameters
        ----------
        tb_lib : str
            testbench library.
        tb_cell : str
            testbench cell.

        Returns
        -------
        cur_envs : list[str]
            the currently enabled simulation environments.
        envs : list[str]
            a list of available simulation environments.
        parameters : dict[str, str]
            testbench parameter values, as strings.
        outputs : dict[str, str]
            testbench output expressions.
        """
        cmd = 'maestro_get_testbench_info("{tb_lib}" "{tb_cell}" {result_file})'
        cmd = cmd.format(tb_lib=tb_lib, tb_cell=tb_cell, result_file='{result_file}')
        output = yaml.load(self._eval_skill(cmd, out_file='result_file'), Loader=yaml.FullLoader)
        return output['enabled_corners'], output['corners'], output['parameters'], output['outputs']

    #: setup writes go through the maestro-view wrapper of the ADE-XL
    #: modify path (``bag_maestro_session.il``); the write itself is the
    #: inherited :meth:`AdexlSession.update_testbench`.
    modify_fn = 'maestro_modify_testbench'

    #: maestro views share the axl run submission inherited from
    #: :class:`AdexlSession`.  The maestro setup-database writes that
    #: crash IC618 headlessly (sdbaccess.cpp:514) do not reproduce in a
    #: display-attached session: a write-mode open of the maestro view and
    #: the run-history save both work there (verified 2026-08-10), which
    #: is the same environment the run submission requires anyway for ICRP
    #: job dispatch.
    tb_view = 'maestro'
    #: assembler-authored maestro histories live under results/maestro;
    #: ocean-XL-authored maestro views keep the adexl layout (results/data).
    results_subdirs = ('maestro', 'data')


SESSION_CLASSES = {cls.flavor: cls
                   for cls in (AdexlSession, AdelSession, MaestroSession)}

#: Detection order for ``testbench.flavor: auto``: the first cellview in
#: this list that exists on the testbench cell picks the flavor.
AUTO_DETECT_VIEWS = (
    ('spectre_state1', 'adel'),
    ('maestro', 'maestro'),
    ('adexl', 'adexl'),
)


def detect_flavor(db, tb_lib, tb_cell):
    """Detect the ADE flavor of a testbench from its cellviews.

    Parameters
    ----------
    db : :class:`bag.interface.skill.SkillInterface`
        the database interface used to evaluate skill expressions.
    tb_lib : str
        testbench library name.
    tb_cell : str
        testbench cell name.

    Returns
    -------
    flavor : str or None
        the detected flavor name, or None if none of the flavor cellviews
        exist (e.g. the library is not registered in cds.lib).
    """
    checks = ' '.join('(ddGetObj("%s" "%s" "%s") && t)' % (tb_lib, tb_cell, view)
                      for view, _flavor in AUTO_DETECT_VIEWS)
    reply = db._eval_skill('list(%s)' % checks)
    tokens = reply.strip().lstrip('(').rstrip(')').split()
    for (_view, flavor), token in zip(AUTO_DETECT_VIEWS, tokens):
        if token == 't':
            return flavor
    return None


def create_ade_session(flavor, db):
    """Create the session object for the given ADE flavor name.

    Parameters
    ----------
    flavor : str
        the ADE flavor name ('adexl', 'adel', or 'maestro').
    db : :class:`bag.interface.skill.SkillInterface`
        the database interface the session drives skill commands through.

    Returns
    -------
    session : :class:`AdeSession`
        the session object.
    """
    try:
        cls = SESSION_CLASSES[flavor]
    except KeyError:
        raise ValueError('Unknown ADE session flavor: %r (choices: %s)'
                         % (flavor, ', '.join(sorted(SESSION_CLASSES))))
    return cls(db)
