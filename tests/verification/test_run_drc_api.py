import asyncio
from types import SimpleNamespace

import pytest

import bag.core as core_module
from bag.core import BagProject
from bag.interface.database import DbAccess


class _Checker:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def async_run_drc(self, lib_name, cell_name):
        self.calls.append((lib_name, cell_name))
        return self.result


def test_database_run_drc_delegates_to_checker():
    checker = _Checker((True, '/tmp/drc.log'))
    db = SimpleNamespace(checker=checker)

    result = asyncio.run(
        DbAccess.async_run_drc(db, 'logic_generated', 'inv_lvs_2x')
    )

    assert result == (True, '/tmp/drc.log')
    assert checker.calls == [('logic_generated', 'inv_lvs_2x')]


def test_bag_project_run_drc_returns_database_result(monkeypatch):
    project = object.__new__(BagProject)
    project.impl_db = SimpleNamespace(
        async_run_drc=lambda *args: 'drc-coro'
    )
    monkeypatch.setattr(
        core_module,
        'batch_async_task',
        lambda tasks: [(True, '/tmp/drc.log')],
    )

    result = project.run_drc('logic_generated', 'inv_lvs_2x')

    assert result == (True, '/tmp/drc.log')


def test_bag_project_run_drc_propagates_unsupported_backend(monkeypatch):
    project = object.__new__(BagProject)
    project.impl_db = SimpleNamespace(
        async_run_drc=lambda *args: 'drc-coro'
    )
    error = NotImplementedError('PVS does not support DRC')
    monkeypatch.setattr(
        core_module,
        'batch_async_task',
        lambda tasks: [error],
    )

    with pytest.raises(NotImplementedError, match='PVS'):
        project.run_drc('logic_generated', 'inv_lvs_2x')
