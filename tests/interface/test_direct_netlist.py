# -*- coding: utf-8 -*-

"""Tests for the netlist provisioning of the direct simulator interfaces.

The si netlist path is exercised end-to-end against a fake ``si``
executable (a python script standing in for the Cadence standalone
netlister): it must be invoked with the documented argv, see the
force-mode ``si.env`` patch, and its regenerated circuit body must be
spliced back into the assembled deck without touching the ADE-owned
control section.
"""

import sys

import pytest

from bag.interface.direct import SiNetlistError, SiNetlistPrereqError
from bag.interface.spectre import SpectreInterface

HEADER = ('// deck header\n'
          'simulator lang=spectre\n'
          'parameters pper=100p\n'
          'include "/models/toplevel.scs" section=top_tt\n')
BODY_OLD = ('subckt inv in out\n'
            'ends inv\n'
            'I0 (a b) inv\n')
BODY_NEW = ('subckt inv2 in out\n'
            'ends inv2\n'
            'I0 (a b) inv2\n')
FOOTER = ('tran tran stop=10n\n'
          'saveOptions options save=allpub\n')
SI_ENV = ('simSimulator = "spectre"\n'
          'simNotIncremental = nil\n')

# Stand-in for ``si``: records its invocation, snapshots the si.env it was
# run against, then rewrites/keeps/fails per the fake_mode.txt marker.
FAKE_SI = '''\
import os, shutil, sys

nl_dir = sys.argv[1]
assert sys.argv[2:5] == ['-batch', '-command', 'netlist'], sys.argv
with open(os.path.join(nl_dir, 'si_invoked.txt'), 'a') as f:
    f.write(' '.join(sys.argv[1:]) + '\\n')
shutil.copy(os.path.join(nl_dir, 'si.env'),
            os.path.join(nl_dir, 'si.env.seen'))
mode_path = os.path.join(nl_dir, 'fake_mode.txt')
mode = 'rewrite'
if os.path.exists(mode_path):
    with open(mode_path) as f:
        mode = f.read().strip()
if mode == 'fail':
    sys.stdout.write('*Error* netlist blew up\\n')
    sys.exit(1)
if mode == 'rewrite':
    with open(os.path.join(nl_dir, 'new_body.txt')) as f:
        new_body = f.read()
    with open(os.path.join(nl_dir, 'netlist'), 'w') as f:
        f.write(new_body)
sys.exit(0)
'''


class StubDb(object):
    """Database interface stub recording create_netlist calls."""

    def __init__(self, deck='ade_deck.scs'):
        self.deck = deck
        self.calls = []

    def create_netlist(self, lib, cell):
        self.calls.append((lib, cell))
        return self.deck


def make_interface(tmp_path, **extra):
    """Build a SpectreInterface over a fake ADE netlist directory."""
    script = tmp_path / 'fake_si.py'
    script.write_text(FAKE_SI)

    nl_dir = tmp_path / 'sim' / 'tb_cell' / 'spectre' / 'config' / 'netlist'
    nl_dir.mkdir(parents=True)
    (nl_dir / 'input.scs').write_text(HEADER + BODY_OLD + FOOTER)
    (nl_dir / 'netlist').write_text(BODY_OLD)
    (nl_dir / 'si.env').write_text(SI_ENV)
    (nl_dir / 'new_body.txt').write_text(BODY_NEW)

    sim_config = dict(
        netlist=str(tmp_path / 'sim' / '{cell}' / 'spectre' / 'config'
                    / 'netlist' / 'input.scs'),
        netlist_source='si',
        si_command=[sys.executable, str(script)],
        si_cwd=str(tmp_path),
    )
    sim_config.update(extra)
    (tmp_path / 'bag_tmp').mkdir()
    iface = SpectreInterface(str(tmp_path / 'bag_tmp'), sim_config)
    return iface, nl_dir


def test_si_refresh_splices_new_body_into_deck(tmp_path):
    iface, nl_dir = make_interface(tmp_path)
    deck = iface.ensure_netlist(None, 'tb_lib', 'tb_cell')
    assert deck == str(nl_dir / 'input.scs')
    assert (nl_dir / 'input.scs').read_text() == HEADER + BODY_NEW + FOOTER
    # exactly one si invocation, pointed at the netlist directory.
    invocations = (nl_dir / 'si_invoked.txt').read_text().splitlines()
    assert len(invocations) == 1
    assert invocations[0].startswith(str(nl_dir))


def test_si_up_to_date_leaves_deck_untouched(tmp_path):
    iface, nl_dir = make_interface(tmp_path)
    (nl_dir / 'fake_mode.txt').write_text('noop')
    deck = iface.ensure_netlist(None, 'tb_lib', 'tb_cell')
    assert deck == str(nl_dir / 'input.scs')
    assert (nl_dir / 'input.scs').read_text() == HEADER + BODY_OLD + FOOTER


def test_si_always_forces_full_renetlist_and_restores_si_env(tmp_path):
    iface, nl_dir = make_interface(tmp_path, netlist_refresh='always')
    iface.ensure_netlist(None, 'tb_lib', 'tb_cell')
    # the netlister ran against a si.env with the incremental check off...
    assert "simNotIncremental = 't" in (nl_dir / 'si.env.seen').read_text()
    # ...and the ADE-authored si.env was put back afterwards.
    assert (nl_dir / 'si.env').read_text() == SI_ENV


def test_missing_si_env_falls_back_to_ade_provisioning(tmp_path):
    iface, nl_dir = make_interface(tmp_path)
    (nl_dir / 'si.env').unlink()
    db = StubDb()
    assert iface.ensure_netlist(db, 'tb_lib', 'tb_cell') == db.deck
    assert db.calls == [('tb_lib', 'tb_cell')]
    assert not (nl_dir / 'si_invoked.txt').exists()


def test_hand_edited_deck_falls_back_to_ade_provisioning(tmp_path):
    iface, nl_dir = make_interface(tmp_path)
    (nl_dir / 'input.scs').write_text(HEADER + 'X1 (a b) other\n' + FOOTER)
    db = StubDb()
    assert iface.ensure_netlist(db, 'tb_lib', 'tb_cell') == db.deck
    assert db.calls == [('tb_lib', 'tb_cell')]


def test_missing_prereq_without_db_raises(tmp_path):
    iface, nl_dir = make_interface(tmp_path)
    (nl_dir / 'si.env').unlink()
    with pytest.raises(SiNetlistPrereqError):
        iface.ensure_netlist(None, 'tb_lib', 'tb_cell')


def test_si_run_failure_raises_without_ade_fallback(tmp_path):
    iface, nl_dir = make_interface(tmp_path)
    (nl_dir / 'fake_mode.txt').write_text('fail')
    db = StubDb()
    with pytest.raises(SiNetlistError):
        iface.ensure_netlist(db, 'tb_lib', 'tb_cell')
    assert db.calls == []
    assert (nl_dir / 'input.scs').read_text() == HEADER + BODY_OLD + FOOTER


def test_refresh_never_only_checks_existence(tmp_path):
    iface, nl_dir = make_interface(tmp_path, netlist_refresh='never')
    deck = iface.ensure_netlist(None, 'tb_lib', 'tb_cell')
    assert deck == str(nl_dir / 'input.scs')
    assert not (nl_dir / 'si_invoked.txt').exists()
    (nl_dir / 'input.scs').unlink()
    with pytest.raises(ValueError):
        iface.ensure_netlist(None, 'tb_lib', 'tb_cell')


def test_ade_source_provisions_missing_deck_through_db(tmp_path):
    iface, nl_dir = make_interface(tmp_path, netlist_source='ade')
    (nl_dir / 'input.scs').unlink()
    db = StubDb()
    assert iface.ensure_netlist(db, 'tb_lib', 'tb_cell') == db.deck
    assert db.calls == [('tb_lib', 'tb_cell')]


def test_unknown_netlist_source_rejected(tmp_path):
    iface, _nl_dir = make_interface(tmp_path, netlist_source='ocean')
    with pytest.raises(ValueError):
        iface.ensure_netlist(None, 'tb_lib', 'tb_cell')
