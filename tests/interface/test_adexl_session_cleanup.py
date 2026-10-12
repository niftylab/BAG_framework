"""Every assembler setup-database open must be bracketed by a close that
also runs on the error path.

An error between an "a"-mode open and the close on the last line of a
procedure left the view open for writing; every later open of the same
view then failed with ADEXL-9072 / "Cannot load adexl database" until
Virtuoso was restarted (2026-09-23, tutorial 5-8 student runs 2026-10).
The SKILL cannot run here, so this checks the structure of the files
the server loads (start_bag_ICADV12d3.il -> bag_adexl_session.il,
bag_maestro_session.il).
"""
import os
import re

import pytest

RUN_SCRIPTS = os.path.join(os.path.dirname(__file__), '..', '..', 'run_scripts')
ADEXL = os.path.join(RUN_SCRIPTS, 'bag_adexl_session.il')
MAESTRO = os.path.join(RUN_SCRIPTS, 'bag_maestro_session.il')

#: procedures allowed to call open_adexl_session directly: the bracket
#: itself, and the run submission whose close is adexl_close_simulation.
DIRECT_OPENERS = {'bag_with_adexl_session', 'adexl_start_simulation'}

#: setup edits/reads that must go through the bracket.
BRACKETED = {
    ADEXL: ['instantiate_testbench', 'get_testbench_info', 'modify_testbench'],
    MAESTRO: ['maestro_get_testbench_info'],
}


def strip(text):
    """Drop strings and ; comments so parens and names are code only."""
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
    return re.sub(r';[^\n]*', '', text)


def procedures(text):
    """{name: body} for every top-level procedure( in a SKILL file."""
    out = {}
    for m in re.finditer(r'^procedure\(\s*(\w+)', text, re.M):
        start = m.start()
        depth, i = 0, m.end() - len(m.group(1)) - 1
        # walk from the "procedure(" paren to its match
        i = text.index('(', start)
        while i < len(text):
            if text[i] == '(':
                depth += 1
            elif text[i] == ')':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out[m.group(1)] = text[start:i + 1]
    return out


@pytest.fixture(scope='module')
def code():
    return {path: strip(open(path, encoding='utf-8').read()) for path in (ADEXL, MAESTRO)}


def test_parens_balance(code):
    for path, text in code.items():
        assert text.count('(') == text.count(')'), path


def test_only_the_bracket_and_run_submission_open_directly(code):
    for path, text in code.items():
        for name, body in procedures(text).items():
            if 'open_adexl_session(' in body and name != 'open_adexl_session':
                assert name in DIRECT_OPENERS, (path, name)


@pytest.mark.parametrize('path, name', [(p, n) for p, ns in BRACKETED.items() for n in ns])
def test_setup_edits_use_the_bracket(code, path, name):
    body = procedures(code[path])[name]
    assert 'bag_with_adexl_session(' in body, name
    # the bracket closes; the body must not close by hand (a second close
    # of a released handle is what the bracket guards against)
    assert 'axlCloseSetupDB(' not in body, name
    assert 'axlCloseSession(' not in body, name


def test_bracket_closes_after_errset_and_reraises(code):
    body = procedures(code[ADEXL])['bag_with_adexl_session']
    run = body.index('errset(funcall(body')
    close = body.index('bag_close_adexl_session(session sdb)')
    reraise = body.rindex('error(')
    assert run < close < reraise


def test_run_submission_closes_when_axlRunSimulation_fails(code):
    body = procedures(code[ADEXL])['adexl_start_simulation']
    assert 'errset(axlRunSimulation(' in body
    assert body.index('errset(axlRunSimulation(') < body.index('bag_close_adexl_session(session sdb)')
    # the reopened session handle must be the one closed on failure
    reopen = body.index('session_sdb = open_adexl_session', body.index('when( null(mode)'))
    assert 'session = car(session_sdb)' in body[reopen:]


def test_set_run_options_leaves_closing_to_the_bracket(code):
    body = procedures(code[ADEXL])['set_run_options']
    assert 'axlCloseSession(' not in body
