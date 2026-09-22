# -*- coding: utf-8 -*-

"""Tests for the ADE-XL/maestro run guards against undispatched jobs.

A tracked setup names ``<tb>_active`` as its test state, but only the
authored ``<tb>_import`` state is kept in version control.  In a checkout
where the setup has never run, the run submission then never reaches the
ICRP job, which only logs "timed out after no activity".  The run path
seeds the missing state from the authored one, and the job-log scan treats
an idle timeout of a job that never received a test as a failure.
"""

import os

from bag.interface.ade import AdexlSession, restore_missing_test_states

SETUP_DB = '''\
<?xml version="1.0"?>
<setupdb version="6">data
	<active>Active Setup
		<tests>
			<test>tb_x
				<tool>OCEAN</tool>
				<tooloptions>
					<option>path
						<value>$AXL_SETUPDB_DIR/test_states</value>
					</option>
					<option>state
						<value>{state}</value>
					</option>
				</tooloptions>
				<origoptions>
					<option>path
						<value>$AXL_SETUPDB_DIR/test_states</value>
					</option>
					<option>state
						<value>tb_x_import</value>
					</option>
				</origoptions>
			</test>
		</tests>
	</active>
</setupdb>
'''


def _make_view(root, state='tb_x_active'):
    view_dir = os.path.join(str(root), 'tb_x', 'maestro')
    import_dir = os.path.join(view_dir, 'test_states', 'tb_x_import')
    os.makedirs(import_dir)
    with open(os.path.join(import_dir, 'state.ocn'), 'w') as handle:
        handle.write('desVar("pvdd" "0.9")\n')
    with open(os.path.join(view_dir, 'data.sdb'), 'w') as handle:
        handle.write(SETUP_DB.format(state=state))
    return view_dir


def test_missing_active_state_is_seeded_from_import(tmp_path):
    view_dir = _make_view(tmp_path)
    active = os.path.join(view_dir, 'test_states', 'tb_x_active')

    assert restore_missing_test_states(view_dir) == [active]
    with open(os.path.join(active, 'state.ocn')) as handle:
        assert handle.read() == 'desVar("pvdd" "0.9")\n'
    # the authored state is left alone
    assert os.path.isdir(os.path.join(view_dir, 'test_states', 'tb_x_import'))


def test_existing_active_state_is_not_overwritten(tmp_path):
    view_dir = _make_view(tmp_path)
    active = os.path.join(view_dir, 'test_states', 'tb_x_active')
    os.makedirs(active)
    with open(os.path.join(active, 'state.ocn'), 'w') as handle:
        handle.write('desVar("pvdd" "0.8")\n')

    assert restore_missing_test_states(view_dir) == []
    with open(os.path.join(active, 'state.ocn')) as handle:
        assert handle.read() == 'desVar("pvdd" "0.8")\n'


def test_setup_that_runs_the_authored_state_needs_nothing(tmp_path):
    # a freshly authored setup names the _import state as the active one
    view_dir = _make_view(tmp_path, state='tb_x_import')

    assert restore_missing_test_states(view_dir) == []
    assert os.listdir(os.path.join(view_dir, 'test_states')) == ['tb_x_import']


def test_view_without_setup_db_is_ignored(tmp_path):
    assert restore_missing_test_states(str(tmp_path)) == []


def _write_job_log(root, name, lines):
    log_dir = os.path.join(str(root), 'logs_user', 'logs0')
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, name)
    with open(path, 'w') as handle:
        handle.write('\n'.join(lines) + '\n')
    return path


def test_idle_timeout_of_undispatched_job_fails_the_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = AdexlSession(db=None)
    _write_job_log(tmp_path, 'Job1.log', [
        r'\o *Info*    Client has finished starting ... ',
        r'\o Job 1 timed out after no activity in 300 seconds.',
    ])

    reason = session._scan_job_logs({})
    assert reason is not None
    assert 'timed out after no activity' in reason
    assert 'never received a test' in reason


def test_idle_timeout_after_the_job_ran_is_not_a_failure(tmp_path, monkeypatch):
    # multi-point runs leave finished jobs idle until they time out
    monkeypatch.chdir(tmp_path)
    session = AdexlSession(db=None)
    _write_job_log(tmp_path, 'Job2.log', [
        r'\o *Info*    Configuring the session ...',
        r'\o INFO (ADE-3071): Simulation completed successfully.',
        r'\o Job 2 timed out after no activity in 300 seconds.',
    ])

    assert session._scan_job_logs({}) is None


def test_error_lines_still_fail_the_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = AdexlSession(db=None)
    _write_job_log(tmp_path, 'Job3.log', [
        r'\o ERROR (OSSHNL-116): cannot descend into the DUT.',
    ])

    assert 'OSSHNL-116' in session._scan_job_logs({})
