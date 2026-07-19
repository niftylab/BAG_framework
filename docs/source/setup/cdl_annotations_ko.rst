Annotated CDL 템플릿 가져오기
=============================

입력 CDL netlist는 BAG 가져오기 전에 다음 전처리가 끝나 있어야 합니다.

* 참조 model/subcircuit 이름은 BAG ``cell_name``\ 과 같아야 합니다.
* parameter 이름은 BAG에서 사용할 이름으로 정규화되어 있어야 합니다.
* ``.SUBCKT`` 선언의 pin 순서가 계층 instance의 terminal 순서입니다.

Cell과 instance annotation
--------------------------

``.SUBCKT`` 안의 ``* @BAG`` JSON 주석은 BAG library를 정의합니다::

    .SUBCKT inv IN OUT VDD VSS PARAMS: wn=1u wp=2u lch=45n nf=2
    * @BAG {"lib_name":"logic_templates"}
    *.PININFO IN:I OUT:O VDD:B VSS:B

``*.PININFO``\ 가 BAG pin 방향의 유일한 입력입니다. ``I``, ``O``, ``B``\ 는 각각
``input``, ``output``, ``inputOutput``\ 으로 변환합니다. strict mode에서는 모든
``.SUBCKT`` pin을 한 번씩 정확히 선언해야 합니다. cell ``@BAG`` payload에서 허용되는
key는 ``lib_name``뿐이며 ``pin_directions``\ 는 사용하지 않습니다.

각 device 뒤의 ``$ @BAG`` JSON 주석은 참조 BAG library를 정의합니다.
model/subcircuit 이름 자체를 ``cell_name``\ 으로 사용하며 parameter 이름과 수식은
변경하지 않습니다::

    MN0 OUT IN VSS VSS nmos4_lvt w='wn' l='lch' nf='nf' $ @BAG {"lib_name":"BAG_prim"}

MOS terminal 순서는 ``D, G, S, B``\ 이고 resistor, capacitor, diode는
``PLUS, MINUS``\ 입니다. ``X`` instance는 참조 ``.SUBCKT``의 pin 순서를
사용합니다. 표준 순서를 사용할 수 없는 device만 ``terminals`` annotation을
추가합니다.

가져오기 명령
-------------

Virtuoso server 없이 다음 명령으로 BAG template과 annotated CDL template을
생성합니다::

    python run_scripts/import_cdl.py logic_templates.sp \
        --library logic_templates \
        --output-root ./BagModules \
        --register ./bag_libs.def

이 명령은 다음 파일을 생성합니다.

* ``<output-root>/<library>/netlist_info/<cell>.yaml``
* ``<output-root>/<library>/cdl_templates/<cell>.cdl``
* ``<output-root>/<library>/<cell>.py``
* ``<output-root>/<library>/__init__.py``
* library가 아직 없을 때 ``bag_libs.def`` 항목

CDL template은 cell/instance ``@BAG`` annotation과 ``*.PININFO``\ 를 보존하므로
원본 combined source file 없이 다시 읽을 수 있습니다. ``source_files``\ 에 원본이
명시된 상태에서 재-import하면 YAML과 CDL template을 함께 갱신하며, 기존 Python
generator는 덮어쓰지 않습니다. 같은 library가 ``bag_libs.def``\ 에 다른 경로로
등록되어 있으면 기존 정의를 변경하지 않고 오류를 반환합니다.

``BAG_prim``, ``basic``, ``analogLib``\ 은 기본적으로 외부 library로 취급합니다.
추가 외부 library는 ``--exclude-library`` 옵션을 반복해서 지정합니다.

BagProject에서 CDL 사용
-----------------------

프로젝트 API로 가져오려면 별도의 BAG 설정 파일에서 로컬 CDL database
class를 선택합니다.

.. literalinclude:: examples/bag_config_cdl.yaml
    :language: yaml

위 예제는 simulator 연동 없이 database 동작만 설정하므로 ``socket``\ 과
``simulation`` 설정이 필요하지 않습니다. 프로젝트를 만들기 전에 설정에
지정된 ``gen_libs`` 디렉터리를 생성하고 Virtuoso 프로젝트와 같은 import
API를 사용합니다::

    from bag.core import BagProject

    project = BagProject(bag_config_path='bag_config_cdl.yaml')
    project.import_design_library('logic_templates')
    project.close_bag_server()

기존 Virtuoso 설정 파일은 변경하지 않고, 공정이나 툴 환경에 맞는 설정 파일을
``BAG_CONFIG_PATH``\ 로 선택합니다. Virtuoso database class는 이전과 동일하게
socket과 server를 사용합니다.

``database.cdl.template_root``\ 는 BAG module root를 가리킵니다. CDL backend는
시작할 때 이 경로 아래의 ``<library>/cdl_templates/*.cdl``\ 을 찾아 읽고,
``source_files``\ 에 같은 cell이 있어도 imported template을 우선 사용합니다.
원본 source로 library를 갱신하려면 원본 파일을 ``source_files``\ 에 둔 채
``import_design_library()``\ 를 호출합니다.

기존 BAG template에서 CDL 생성
-------------------------------

이미 존재하는 BAG ``netlist_info`` YAML도 Virtuoso 없이 annotated CDL template으로
변환할 수 있습니다::

    python run_scripts/export_cdl_templates.py \
        /path/to/BagModules/logic_templates/netlist_info --dry-run
    python run_scripts/export_cdl_templates.py \
        /path/to/BagModules/logic_templates/netlist_info

출력 경로의 기본값은 같은 library의 ``cdl_templates``\ 입니다. 4-terminal
``BAG_prim`` MOS는 MOS element로, 같은 library의 hierarchy와 외부 master는 terminal
순서를 명시한 annotated ``X`` instance로 출력합니다. 전기적 CDL device가 아닌
``basic`` symbol instance(``ipin``, ``iopin``\ 등)는 출력에서 제외합니다.

구현 netlist 생성
-----------------

CDL backend는 일반적인 BAG schematic 생성 API를 지원합니다::

    instance = project.new_schematic_instance(
        lib_name='logic_templates',
        cell_name='inv',
        params=dict(),
    )
    project.batch_schematic(
        'logic_generated',
        [instance],
        name_list=['inv_generated'],
    )

예제 설정에서는 다음 경로에 결과가 생성됩니다::

    ${BAG_WORK_DIR}/gen_libs/logic_generated/inv_generated.sp

writer는 가져온 template을 복사한 다음 BAG의 pin 이름 변경과 추가, instance
삭제와 array, master 교체, terminal 재연결, parameter 변경을 적용합니다.
생성된 subcircuit가 같은 implementation library의 하위 subcircuit를 참조하면
해당 ``.sp`` 파일을 자동으로 include합니다. 외부 device model 및 subcircuit
library는 simulation deck에서 별도로 include해야 합니다.

생성 결과의 BAG pin 방향은 CDL 호환 ``*.PININFO``\ 로 다시 출력됩니다.

``database.cdl.output.extension``\ 은 생성 파일 확장자이며 기본값은
``.sp``\ 입니다. ``database.cdl.output.line_length``\ 는 continuation
line 기준이며 기본값은 100자입니다.

같은 implementation을 다시 생성하면 기존 파일을 교체합니다. ADE testbench
설정과 simulator 실행은 이 database writer의 범위에 포함되지 않습니다.
