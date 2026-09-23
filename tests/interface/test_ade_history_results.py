# -*- coding: utf-8 -*-

"""Tests for reading ADE-XL/maestro run results from the history rdb.

A run can simulate several points (the nominal "_default" corner next to an
enabled corner, sweeps).  The runner polls the history database and closes
the session once results are returned, so returning after the FIRST point
finishes kills the others ("SPECTRE-25: ... the current ADE session is
lost") and hides their failures: on 2026-09-23 a maestro run returned the
nominal point's values while its top_tt point had failed (SFE-675) or was
still running.  Results are only returned once every point has stopped, and
a point that stopped without values is an error.
"""

import sqlite3

import pytest

from bag.interface.ade import AdexlSession

SCHEMA = '''
CREATE TABLE point (pointID INTEGER, designPointNumber INTEGER, merit TEXT,
                    cornerID INTEGER, groupID INTEGER);
CREATE TABLE corner (cornerID INTEGER, name TEXT);
CREATE TABLE error (errorID INTEGER, message TEXT);
CREATE TABLE testStatus (pointID INTEGER, testID INTEGER, hostID INTEGER,
                         startTime TEXT, stopTime TEXT, statusCode INTEGER,
                         errorID INTEGER, origPointID INTEGER,
                         memoryUsed TEXT, cpuCount INTEGER);
CREATE TABLE result (resultID INTEGER, name TEXT);
CREATE TABLE resultValue (resultID INTEGER, pointID INTEGER, value,
                          errorID INTEGER);
'''

RUNNING, DONE = 1, 2


def _make_rdb(path, points, errors=()):
    """Write a history rdb.

    ``points`` maps pointID -> (corner name, stopTime or None, {output: value}).
    """
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    con.executemany('INSERT INTO error VALUES (?, ?)',
                    [(RUNNING, 'running'), (DONE, 'done')] + list(errors))
    outputs = sorted({name for _c, _s, vals in points.values() for name in vals})
    con.executemany('INSERT INTO result VALUES (?, ?)',
                    [(idx, name) for idx, name in enumerate(outputs, 1)])
    for pid, (corner, stop, vals) in points.items():
        con.execute('INSERT INTO corner VALUES (?, ?)', (pid, corner))
        con.execute('INSERT INTO point VALUES (?, 1, NULL, ?, NULL)', (pid, pid))
        con.execute('INSERT INTO testStatus VALUES (?, 1, 1, ?, ?, ?, ?, ?, NULL, 1)',
                    (pid, '1790123389.0', stop, 3 if vals else 2,
                     DONE if vals else RUNNING, pid))
        for name, value in vals.items():
            con.execute('INSERT INTO resultValue VALUES (?, ?, ?, NULL)',
                        (outputs.index(name) + 1, pid, value))
    con.commit()
    con.close()
    return str(path)


def test_single_finished_point_returns_scalars(tmp_path):
    rdb = _make_rdb(tmp_path / 'a.rdb',
                    {1: ('top_tt', '1790123395.0', {'VOD': 0.9, 'CLK2Q': 1.4e-11})})
    assert AdexlSession._read_history_results(rdb) == {'VOD': 0.9, 'CLK2Q': 1.4e-11}


def test_waits_while_another_point_is_still_running(tmp_path):
    # the 2026-09-23 tb_double_tail_latch run: nominal done, top_tt running
    rdb = _make_rdb(tmp_path / 'a.rdb',
                    {1: ('', '1790123395.0', {'VOD': 0.9}),
                     2: ('top_tt', None, {})})
    assert AdexlSession._read_history_results(rdb) is None


def test_all_points_finished_returns_per_point_values(tmp_path):
    rdb = _make_rdb(tmp_path / 'a.rdb',
                    {1: ('', '1790123395.0', {'VOD': 0.9}),
                     2: ('top_tt', '1790123399.0', {'VOD': 0.8})})
    assert AdexlSession._read_history_results(rdb) == {'VOD': {1: 0.9, 2: 0.8}}


def test_point_stopped_without_values_is_an_error(tmp_path):
    # the morning run of 2026-09-23: top_tt died in circuit read-in
    rdb = _make_rdb(tmp_path / 'a.rdb',
                    {1: ('', '1790113560.0', {'VOD': 0.9}),
                     2: ('top_tt', '1790113565.0', {})},
                    errors=[(3, 'Simulation Error:\n ----\n ERROR (SFE-675): '
                                'toplevel.scs does not contain a valid section')])
    with pytest.raises(Exception) as info:
        AdexlSession._read_history_results(rdb)
    assert 'top_tt' in str(info.value)
    assert 'SFE-675' in str(info.value)


def test_rdb_without_status_tables_keeps_first_result_behavior(tmp_path):
    path = tmp_path / 'a.rdb'
    con = sqlite3.connect(str(path))
    con.executescript('''
        CREATE TABLE result (resultID INTEGER, name TEXT);
        CREATE TABLE resultValue (resultID INTEGER, pointID INTEGER, value,
                                  errorID INTEGER);
        INSERT INTO result VALUES (1, 'VOD');
        INSERT INTO resultValue VALUES (1, 1, 0.9, NULL);
    ''')
    con.commit()
    con.close()
    assert AdexlSession._read_history_results(str(path)) == {'VOD': 0.9}
