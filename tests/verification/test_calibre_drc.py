from pathlib import Path

import pytest

from bag.verification.calibre import Calibre, drc_passed
from bag.verification.icv import ICV
from bag.verification.pvs import PVS


def _make_checker(tmp_path):
    runset_dir = tmp_path / 'config'
    runset_dir.mkdir()
    runset = runset_dir / 'drc.cell.runset'
    runset.write_text(
        '*drcRulesFile: ../Calibre/drc/calibre.drc.cell\n'
        '*drcRunDir: ./rundir_drc\n'
        '*drcLayoutPaths: old.gds\n'
        '*drcLayoutPrimary: old_cell\n'
        '*drcLayoutLibrary: old_lib\n'
        '*drcLayoutView: layout\n'
        '*drcResultsFile: old.drc.results\n'
        '*drcSummaryFile: old.drc.summary\n',
        encoding='utf-8',
    )
    checker = object.__new__(Calibre)
    checker.drc_run_dir = str(tmp_path / 'workspace' / 'rundir_drc')
    checker.drc_runset = str(runset)
    checker.setup_export_layout = lambda *args: (
        ['export-layout'], str(tmp_path / 'layout.log'), None, None
    )
    return checker


def test_setup_drc_flow_exports_layout_and_generates_cell_runset(tmp_path):
    checker = _make_checker(tmp_path)

    flow = checker.setup_drc_flow('logic_generated', 'inv_lvs_2x')

    assert len(flow) == 2
    assert flow[0][0][0] == 'export-layout'
    assert flow[1][0][:3] == ['calibre', '-gui', '-drc']
    assert flow[1][0][-1] == '-batch'

    run_dir = (
        Path(checker.drc_run_dir) / 'logic_generated' / 'inv_lvs_2x'
    )
    assert Path(flow[1][3]) == run_dir

    generated_runset = Path(flow[1][0][4])
    content = generated_runset.read_text(encoding='utf-8')
    assert '*drcRunDir: {}'.format(run_dir) in content
    assert '*drcLayoutPaths: {}'.format(run_dir / 'layout.gds') in content
    assert '*drcLayoutPrimary: inv_lvs_2x' in content
    assert '*drcLayoutLibrary: logic_generated' in content
    assert '*drcResultsFile: inv_lvs_2x.drc.results' in content
    assert '*drcSummaryFile: inv_lvs_2x.drc.summary' in content
    assert (
        '*drcRulesFile: {}'.format(
            Path(checker.drc_run_dir).parent
            / 'Calibre' / 'drc' / 'calibre.drc.cell'
        )
        in content
    )


@pytest.mark.parametrize(
    'summary,total_passed',
    [
        ('TOTAL DRC Results Generated: 0\n', True),
        ('TOTAL RESULTS GENERATED = 4\n', False),
    ],
)
def test_drc_passed_parses_violation_count(tmp_path, summary, total_passed):
    log_path = tmp_path / 'calibre.log'
    log_path.write_text('calibre output\n', encoding='utf-8')
    summary_path = tmp_path / 'cell.drc.summary'
    summary_path.write_text(summary, encoding='utf-8')

    assert drc_passed(0, str(log_path), str(summary_path)) == (
        total_passed,
        str(log_path),
    )


def test_drc_passed_keeps_execution_log_on_tool_or_summary_failure(tmp_path):
    log_path = tmp_path / 'calibre.log'
    log_path.write_text('calibre output\n', encoding='utf-8')
    summary_path = tmp_path / 'cell.drc.summary'
    summary_path.write_text(
        'TOTAL DRC Results Generated: 0\n',
        encoding='utf-8',
    )

    assert drc_passed(2, str(log_path), str(summary_path)) == (
        False,
        str(log_path),
    )
    assert drc_passed(
        0, str(log_path), str(tmp_path / 'missing.summary')
    ) == (False, str(log_path))

    summary_path.write_text('unrecognized summary\n', encoding='utf-8')
    assert drc_passed(0, str(log_path), str(summary_path)) == (
        False,
        str(log_path),
    )


@pytest.mark.parametrize('checker_cls', [PVS, ICV])
def test_non_calibre_backends_explicitly_reject_drc(checker_cls):
    checker = object.__new__(checker_cls)

    with pytest.raises(NotImplementedError, match=checker_cls.__name__):
        checker.setup_drc_flow('logic_generated', 'inv_lvs_2x')
