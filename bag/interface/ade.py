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
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
import warnings
import xml.etree.ElementTree as ElementTree

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


def _test_options(test, section):
    """Return {option name: value} of one ``<test>`` options section."""
    options = {}
    node = test.find(section)
    if node is None:
        return options
    for option in node.findall('option'):
        name = (option.text or '').strip()
        value = option.findtext('value')
        if name and value is not None:
            options[name] = value.strip()
    return options


def restore_missing_test_states(view_dir):
    # type: (str) -> List[str]
    """Recreate the active test states a setup database names but lacks.

    An ADE-XL/maestro setup (``<view_dir>/data.sdb``) runs each test from
    the state named by its ``tooloptions`` (``<tb>_active`` once the setup
    has been opened), while the authored state stays under ``origoptions``
    (``<tb>_import``).  Only the ``_import`` state is kept in version
    control: the ``_active`` one is rewritten by every run.  In a checkout
    where the setup has never run, the active state directory is therefore
    missing, and the run submission then never dispatches -- the ICRP job
    only logs "timed out after no activity".  Seeding the missing state
    from the authored one reproduces what a first run in the authoring
    checkout had.

    Parameters
    ----------
    view_dir : str
        the setup cellview directory (``<lib>/<cell>/<adexl|maestro>``).

    Returns
    -------
    restored : list[str]
        the test state directories created.
    """
    try:
        root = ElementTree.parse(os.path.join(view_dir, 'data.sdb')).getroot()
    except (OSError, ElementTree.ParseError):
        return []
    restored = []
    for test in root.iterfind('./active/tests/test'):
        cur = _test_options(test, 'tooloptions')
        orig = _test_options(test, 'origoptions')
        state, orig_state = cur.get('state'), orig.get('state')
        if not state or not orig_state or state == orig_state:
            continue
        path = cur.get('path', '').replace('$AXL_SETUPDB_DIR', view_dir)
        orig_path = orig.get('path', '').replace('$AXL_SETUPDB_DIR', view_dir)
        if '$' in path or '$' in orig_path:
            continue
        target = os.path.normpath(os.path.join(path, state))
        source = os.path.normpath(os.path.join(orig_path, orig_state))
        if os.path.exists(target) or not os.path.isdir(source):
            continue
        shutil.copytree(source, target)
        restored.append(target)
    return restored


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
    #: job-log substrings that mark a failed run.
    job_log_error_marks = ('ERROR (', '*Error*')
    #: an idle ICRP job logs this before it is killed.  It is harmless after
    #: the job has run its points, but a job the main session never handed
    #: a test to (no ``job_log_configured_mark``) means the run was never
    #: dispatched, with no ERROR line anywhere.
    job_log_idle_mark = 'timed out after no activity'
    job_log_configured_mark = 'Configuring the session'

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
               '"{tech_lib}" {result_file} {corner_spec})')
        cmd = cmd.format(tb_cell=tb_cell,
                         targ_lib=tb_lib,
                         config_libs=tb_config['config_libs'],
                         config_views=tb_config['config_views'],
                         config_stops=tb_config['config_stops'],
                         default_corner=tb_config['default_env'],
                         corner_file=tb_config['env_file'],
                         def_files=to_skill_list_str(tb_config['def_files']),
                         tech_lib=self.db_config['schematic']['tech_lib'],
                         result_file='{result_file}',
                         corner_spec='{corner_spec}')
        in_files = {'corner_spec': self.read_corner_spec(tb_config)}
        output = yaml.load(self._eval_skill(cmd, input_files=in_files,
                                            out_file='result_file'),
                           Loader=yaml.FullLoader)
        return tb_config['default_env'], output['corners'], output['parameters'], output['outputs']

    @staticmethod
    def read_corner_spec(tb_config):
        # type: (Dict[str, Any]) -> List[List[str]]
        """Read the central corner definitions named by ``corner_file``.

        ADE-XL's own ``axlLoadCorners`` accepts a corner setup database and
        imports nothing from it, so the corners are applied by SKILL instead;
        this turns the file into rows of
        ``[name, model file, section, enabled, temperature]``.  A missing or
        unset file yields no rows, which leaves every setup as it is.
        """
        path = tb_config.get('corner_file', '')
        if not path:
            return []
        real = os.path.expandvars(path)
        if not os.path.isfile(real):
            return []
        with open(real, 'r') as spec_file:
            text = spec_file.read()
        block = re.search(r'<corners>.*?</corners>', text, re.DOTALL)
        if block is None:
            return []
        rows = []
        corners = re.findall(r'<corner([^>]*)>([A-Za-z_]\w*)(.*?)'
                             r'(?=<corner[^>]*>|</corners>)',
                             block.group(0), re.DOTALL)
        for attrs, name, body in corners:
            if name == '_default':
                continue
            files = re.findall(r'<modelfile>(.*?)</modelfile>', body, re.DOTALL)
            if not files:
                continue
            sections = re.findall(r'<modelsection>(.*?)</modelsection>', body,
                                  re.DOTALL)
            temps = re.findall(r'<var>temperature\s*<value>(.*?)</value>', body,
                               re.DOTALL)
            rows.append([name,
                         files[0].strip(),
                         sections[0].strip().strip('"') if sections else '',
                         '1' if 'enabled="1"' in attrs else '0',
                         temps[0].strip() if temps else ''])
        return rows

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
               '{sim_envs} {params} {env_params} "%s" {stimuli} {corner_spec})'
               % (self.modify_fn, lib, cell, self.tb_view))
        in_files = {'conf_rules': config_rules,
                    'run_opts': [],
                    'sim_envs': sim_envs,
                    'params': list(parameters.items()),
                    'env_params': list(zip(sim_envs, env_parameters)),
                    'stimuli': stimuli_to_spec(stimuli),
                    # the maestro flavor never runs instantiate_testbench,
                    # so the central corners are applied here too
                    'corner_spec': self.read_corner_spec(
                        self.db_config['testbench']),
                    }
        self._restore_test_states(lib, cell)
        self._eval_skill(cmd, input_files=in_files)

    def _lib_path(self, lib):
        """Return the library directory the live session resolves."""
        lib_path = self._eval_skill('ddGetObj("%s")~>readPath' % lib).strip().strip('"')
        if not os.path.isdir(lib_path):
            raise Exception('cannot resolve library path of %s (got %r)'
                            % (lib, lib_path))
        return lib_path

    def _restore_test_states(self, lib, cell, lib_path=None):
        """Seed active test states missing from this checkout.

        See :func:`restore_missing_test_states`; must run before the setup
        is opened for a write or a run submission.
        """
        if lib_path is None:
            lib_path = self._lib_path(lib)
        view_dir = os.path.join(lib_path, cell, self.tb_view)
        for target in restore_missing_test_states(view_dir):
            print('restored missing test state %s from the authored setup'
                  % os.path.relpath(target, view_dir), flush=True)

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
        lib_path = self._lib_path(lib)
        self._restore_test_states(lib, cell, lib_path)
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
                if any(mark in line for mark in self.job_log_error_marks):
                    return '%s: %s' % (fname, line.strip())
                if (self.job_log_idle_mark in line
                        and not self._job_was_configured(fname)):
                    return ('%s: %s (the job never received a test; check '
                            'that the setup\'s test states exist)'
                            % (fname, line.strip()))
        return None

    def _job_was_configured(self, fname):
        """Return True if the job log shows a test handed to the job."""
        try:
            with open(fname, 'r', errors='replace') as stream:
                return self.job_log_configured_mark in stream.read()
        except OSError:
            return True

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
            # the session is closed as soon as results are returned, which
            # kills every point still simulating ("SPECTRE-25 ... the current
            # ADE session is lost").  Wait for all points, and fail on a
            # point that stopped without values instead of returning the
            # other points' results as if the run had passed.
            status = AdexlSession._point_status(con)
            if status is not None:
                if any(stop in (None, '') for _c, stop in status.values()):
                    return None
                valued_points = {p for p, _n, _v, _e in rows}
                dead = sorted(p for p in status if p not in valued_points)
                if dead:
                    raise Exception(
                        'adexl run point(s) %s stopped without results%s '
                        '(see %s)'
                        % (', '.join('%d (corner %s)'
                                     % (p, status[p][0] or 'nominal')
                                     for p in dead),
                           AdexlSession._run_error_messages(con), rdb_file))
            points = sorted({p for p, _n, _v, _e in rows})
            if len(points) <= 1:
                return {name: value for _p, name, value, _e in rows}
            multi = {}
            for point, name, value, _err in rows:
                multi.setdefault(name, {})[point] = value
            return multi
        finally:
            con.close()

    @staticmethod
    def _point_status(con):
        """Return {pointID: (corner name, stopTime)} for every run point.

        Returns None for a history database without the point/status
        tables, so older schemas keep the first-result behavior.  A point
        with no status row yet has not started and counts as unfinished.
        """
        try:
            rows = con.execute(
                'SELECT p.pointID, c.name, s.stopTime FROM point p '
                'LEFT JOIN corner c ON p.cornerID = c.cornerID '
                'LEFT JOIN testStatus s ON s.pointID = p.pointID').fetchall()
        except sqlite3.Error:
            return None
        if not rows:
            return None
        status = {}
        for point, corner, stop in rows:
            # several tests per point give several rows; unfinished wins
            if status.get(point, (None, ''))[1] is not None:
                status[point] = (corner, stop)
        return status

    @staticmethod
    def _run_error_messages(con):
        """Return the run's recorded error messages as a short suffix."""
        try:
            msgs = [row[0] for row in con.execute('SELECT message FROM error')]
        except sqlite3.Error:
            return ''
        lines = []
        for msg in msgs:
            if msg in (None, 'running', 'done'):
                continue
            for line in str(msg).splitlines():
                line = line.strip()
                if 'ERROR' in line:
                    lines.append(line)
                    break
            else:
                lines.append(str(msg).strip().splitlines()[0])
        return (': ' + ' | '.join(lines)) if lines else ''


class AdelSession(AdexlSession):
    """ADE-L session flavor (``bag_adel_session.il`` function family).

    The testbench is opened in place: the OA schematic, config view, and
    the saved ADE state (``spectre_state1``) must already exist in the
    testbench library. Run submission and status polling are separate SKILL
    calls so Virtuoso can process completion events between requests.

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

    sim_error_grace = 30.0

    def run_simulation(self, lib, cell, res_file_name=None):
        """Submit ADE-L, poll without blocking its event loop, always close.

        A unique run token prevents a previous run's outputs from being read.
        Saved setup changes belong to update_testbench; running never rewrites
        the source ADE state. Cleanup discards only this run's transient state.
        """
        token = uuid.uuid4().hex
        args = ' '.join(json.dumps(value) for value in
                        (lib, cell, token, self.tb_ade_view))
        deadline = time.monotonic() + self.sim_timeout
        error_deadline = None
        failure = None
        try:
            self._eval_skill('adel_start_simulation(%s)' % args)
            while time.monotonic() < deadline:
                state = yaml.safe_load(self._eval_skill(
                    'adel_poll_simulation("%s" {result_file})' % token,
                    out_file='result_file'))
                status = state.get('status') if isinstance(state, dict) else None
                if status == 'complete':
                    results = state.get('outputs')
                    if not isinstance(results, dict) or not results:
                        raise RuntimeError('ADE-L returned no evaluated outputs')
                    missing = [name for name, value in results.items()
                               if value is None or value == 'nil']
                    if missing:
                        raise RuntimeError('ADE-L output evaluation failed: %s'
                                           % ', '.join(missing))
                    save_dir = bag.io.make_temp_dir(prefix='adel_data', parent_dir=self.tmp_dir)
                    with open(os.path.join(save_dir, res_file_name or 'sim_results.yaml'), 'w') as stream:
                        yaml.safe_dump(results, stream)
                    return results
                if status == 'error':
                    failure = state.get('detail', 'unknown ADE-L error')
                    if error_deadline is None:
                        error_deadline = time.monotonic() + self.sim_error_grace
                elif status != 'pending':
                    raise RuntimeError('Invalid ADE-L run status: %r' % state)
                if error_deadline is not None and time.monotonic() >= error_deadline:
                    raise RuntimeError('ADE-L failed for %s/%s: %s' % (lib, cell, failure))
                time.sleep(min(self.sim_poll_interval, max(0, deadline - time.monotonic())))
            raise TimeoutError('ADE-L did not complete %s/%s within %g seconds%s'
                               % (lib, cell, self.sim_timeout,
                                  ': ' + str(failure) if failure else ''))
        finally:
            # Preserve the simulation/transport exception if cleanup also fails.
            import sys
            failed = sys.exc_info()[0] is not None
            try:
                self._eval_skill('adel_close_simulation("%s")' % token)
            except Exception as exc:
                if not failed:
                    raise
                warnings.warn('ADE-L cleanup failed for %s/%s: %s' % (lib, cell, exc))


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
