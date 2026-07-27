BAG Calibre DRC 설정
====================

``BagProject.run_drc(lib_name, cell_name)``는 지정한 셀의 ``layout`` view를
GDS로 export한 뒤 Calibre DRC를 실행한다. 반환값은 ``run_lvs()``와 같은
``(passed, log_path)`` 형식이다. ``passed``는 violation이 하나도 없을 때만
참이며, ``log_path``는 Calibre 실행 stdout/stderr 로그를 가리킨다.

Calibre checker에는 다음 설정을 추가한다.

.. code-block:: yaml

    checker:
      checker_cls: "bag.verification.calibre.Calibre"
      drc_run_dir: "${BAG_WORK_DIR}/rundir_drc"
      drc_runset: "${BAG_TECH_CONFIG_DIR}/workspace_setup/drc.cell.runset"

DRC 실행마다 ``drc_run_dir/<library>/<cell>`` 디렉터리를 만들고, 원본
runset의 library, cell, GDS, 결과 데이터베이스와 summary 경로를 현재
실행에 맞게 바꾼 임시 runset을 사용한다.

현재 DRC 실행 backend는 Calibre만 지원한다. PVS 또는 ICV checker에서
``run_drc()``를 호출하면 ``NotImplementedError``가 발생한다.

실제 DRC가 정상적으로 끝났더라도 상위 레벨에서 해소되는 boundary rule
등의 violation이 있으면 ``passed``는 거짓일 수 있다. 이 경우 실행 자체의
성공 여부는 반환된 실행 로그와 셀 실행 디렉터리의 DRC summary/result
database를 함께 확인한다.
