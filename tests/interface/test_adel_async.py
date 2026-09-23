"""ADE-L must release SKILL between polls and clean up every exit path."""
import yaml

import pytest

from bag.interface.ade import AdelSession


class Database:
    def __init__(self, tmp_path, replies):
        self.tmp_dir = str(tmp_path)
        self.replies = iter(replies)
        self.calls = []

    def _eval_skill(self, expr, **kwargs):
        self.calls.append(expr)
        if expr.startswith('adel_poll_simulation'):
            return yaml.safe_dump(next(self.replies))
        return 't'


def session(tmp_path, replies):
    db = Database(tmp_path, replies)
    result = AdelSession(db)
    result.sim_poll_interval = 0
    result.sim_error_grace = 0
    return result, db


def test_pending_then_complete_returns_current_outputs_and_closes(tmp_path):
    run, db = session(tmp_path, [{'status': 'pending'},
                               {'status': 'complete', 'outputs': {'period': 4e-10}}])
    assert run.run_simulation('lib', 'cell') == {'period': 4e-10}
    assert db.calls[0].startswith('adel_start_simulation')
    assert sum(c.startswith('adel_poll_simulation') for c in db.calls) == 2
    assert db.calls[-1].startswith('adel_close_simulation')
    assert not any(c.startswith('adel_run_simulation') for c in db.calls)


def test_error_reports_cause_and_closes(tmp_path):
    run, db = session(tmp_path, [{'status': 'error', 'detail': 'ERROR (SFE-675)'}])
    with pytest.raises(RuntimeError, match='SFE-675'):
        run.run_simulation('lib', 'cell')
    assert db.calls[-1].startswith('adel_close_simulation')


def test_timeout_is_bounded_and_closes(tmp_path):
    run, db = session(tmp_path, [])
    run.sim_timeout = 0
    with pytest.raises(TimeoutError, match='lib/cell'):
        run.run_simulation('lib', 'cell')
    assert db.calls[-1].startswith('adel_close_simulation')


def test_missing_evaluated_output_is_not_success(tmp_path):
    run, db = session(tmp_path, [{'status': 'complete', 'outputs': {'period': None}}])
    with pytest.raises(RuntimeError, match='period'):
        run.run_simulation('lib', 'cell')
    assert db.calls[-1].startswith('adel_close_simulation')


def test_transport_failure_still_attempts_cleanup(tmp_path):
    run, db = session(tmp_path, [])
    with pytest.raises(StopIteration):
        run.run_simulation('lib', 'cell')
    assert db.calls[-1].startswith('adel_close_simulation')


def test_success_during_error_grace_wins(tmp_path):
    run, db = session(tmp_path, [{'status': 'error', 'detail': 'recoverable'},
                               {'status': 'complete', 'outputs': {'period': 4e-10}}])
    run.sim_error_grace = 10
    assert run.run_simulation('lib', 'cell') == {'period': 4e-10}
    assert db.calls[-1].startswith('adel_close_simulation')


def test_cleanup_failure_does_not_mask_simulation_error(tmp_path):
    run, db = session(tmp_path, [{'status': 'error', 'detail': 'bad model'}])
    original = db._eval_skill
    def evaluate(expr, **kwargs):
        if expr.startswith('adel_close_simulation'):
            raise OSError('connection lost')
        return original(expr, **kwargs)
    db._eval_skill = evaluate
    with pytest.warns(UserWarning, match='cleanup failed'):
        with pytest.raises(RuntimeError, match='bad model'):
            run.run_simulation('lib', 'cell')


def test_submission_failure_still_attempts_cleanup(tmp_path):
    run, db = session(tmp_path, [])
    original = db._eval_skill
    def evaluate(expr, **kwargs):
        if expr.startswith('adel_start_simulation'):
            raise RuntimeError('netlist failed')
        return original(expr, **kwargs)
    db._eval_skill = evaluate
    with pytest.raises(RuntimeError, match='netlist failed'):
        run.run_simulation('lib', 'cell')
    assert db.calls[-1].startswith('adel_close_simulation')
