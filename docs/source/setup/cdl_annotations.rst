Annotated CDL template format
=============================

BAG can represent schematic template metadata in preprocessed CDL
netlists.  The preprocessor must make every referenced model or subcircuit
name equal to its BAG ``cell_name`` and normalize parameter names before this
parser is invoked.

Cell annotations
----------------

A ``* @BAG`` JSON comment inside a ``.SUBCKT`` defines its BAG library::

    .SUBCKT inv IN OUT VDD VSS PARAMS: wn=1u wp=2u lch=45n nf=2
    * @BAG {"lib_name":"logic_templates"}
    *.PININFO IN:I OUT:O VDD:B VSS:B

``*.PININFO`` is the only source of BAG pin directions.  ``I``, ``O``, and
``B`` map to ``input``, ``output``, and ``inputOutput``, respectively.  In
strict mode it must list every ``.SUBCKT`` pin exactly once.  The supported
cell annotation key is ``lib_name``; ``pin_directions`` is not accepted in a
``@BAG`` payload.

Instance annotations
--------------------

An inline ``$ @BAG`` JSON comment defines the library of the instance master::

    MN0 OUT IN VSS VSS nmos4_lvt w='wn' l='lch' nf='nf' $ @BAG {"lib_name":"BAG_prim"}

The referenced CDL model or subcircuit name, ``nmos4_lvt`` in this example,
is the BAG ``cell_name``.  Parameter names and expressions are preserved
without mapping.

The parser uses the standard terminal order ``D, G, S, B`` for MOS devices and
``PLUS, MINUS`` for resistor, capacitor, and diode devices.  An exceptional
device can specify its terminal order explicitly::

    X0 A B C custom_device p=1 $ @BAG {"lib_name":"custom_prim","terminals":["A","B","C"]}

For an ``X`` instance without explicit ``terminals``, the parser uses the pin
order of the referenced ``.SUBCKT`` definition.

Validation
----------

Ordinary ``*`` and ``$`` comments are ignored.  Invalid JSON, conflicting
metadata, malformed or incomplete ``PININFO``, unknown pins, duplicate
instances, unresolved hierarchical terminal orders, and missing library
annotations are errors in strict mode.  Parser errors contain the input file
name and physical line number.

Importing templates
-------------------

Use the offline import script to create BAG ``netlist_info`` YAML files,
Python generator skeletons, annotated CDL templates, and a ``bag_libs.def``
entry without starting a Virtuoso server::

    python run_scripts/import_cdl.py logic_templates.sp \
        --library logic_templates \
        --output-root ./BagModules \
        --register ./bag_libs.def

``BAG_prim``, ``basic``, and ``analogLib`` are treated as external libraries
by default and are not recursively imported.  Additional external libraries
can be supplied with repeated ``--exclude-library`` options.  Each imported
cell also writes a self-contained annotated CDL file to
``<library>/cdl_templates/<cell>.cdl``.  It preserves the cell and instance
``@BAG`` annotations and ``*.PININFO``, so it can be loaded without the
original combined source file.  Re-importing from an explicitly configured
``source_files`` entry refreshes both the YAML and the CDL template but
preserves an existing Python generator.  If a library is already registered
at a different path, the import fails instead of changing the existing
definition.

Using CDL through BagProject
----------------------------

For project-level integration, select the local CDL database class in a
separate BAG configuration file:

.. literalinclude:: examples/bag_config_cdl.yaml
    :language: yaml

The example configures database operations without simulator integration, so
neither ``socket`` nor ``simulation`` is required.  Create the configured
``gen_libs`` directory before constructing the project, then use the same
import API as a Virtuoso-backed project::

    from bag.core import BagProject

    project = BagProject(bag_config_path='bag_config_cdl.yaml')
    project.import_design_library('logic_templates')
    project.close_bag_server()

Keep the normal Virtuoso configuration unchanged and select the appropriate
file with ``BAG_CONFIG_PATH`` for each process or tool environment.  A
Virtuoso database class still uses its socket and server as before.

``database.cdl.template_root`` points at the BAG module root.  On startup the
CDL backend discovers ``<library>/cdl_templates/*.cdl`` below this root and
uses those imported templates before duplicate entries in ``source_files``.
To refresh an imported library, call ``import_design_library()`` with its
original source file still listed; explicit source files are used for that
refresh operation.

Exporting existing BAG templates
--------------------------------

Existing BAG ``netlist_info`` YAML files can also be converted to annotated
CDL templates without Virtuoso::

    python run_scripts/export_cdl_templates.py \
        /path/to/BagModules/logic_templates/netlist_info --dry-run
    python run_scripts/export_cdl_templates.py \
        /path/to/BagModules/logic_templates/netlist_info

The output defaults to the sibling ``cdl_templates`` directory.  Four-terminal
``BAG_prim`` MOS cells are emitted as MOS elements; same-library hierarchy and
external masters are emitted as annotated ``X`` instances with explicit
terminal order.  The exporter omits ``basic`` symbol instances such as
``ipin`` and ``iopin``, which do not correspond to electrical CDL devices.

Writing implementation netlists
-------------------------------

The CDL backend implements the normal BAG schematic generation path::

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

With the example configuration, the result is written to::

    ${BAG_WORK_DIR}/gen_libs/logic_generated/inv_generated.sp

The writer copies the imported template and applies BAG pin renaming and
addition, instance deletion and arraying, master replacement, terminal
reconnection, and parameter changes.  A generated subcircuit automatically
includes direct child subcircuits from the same implementation library.
External device model and subcircuit libraries remain the responsibility of
the simulation deck.

It emits a CDL-compatible ``*.PININFO`` from the resulting BAG pin directions.

``database.cdl.output.extension`` selects the generated filename
extension and defaults to ``.sp``.
``database.cdl.output.line_length`` selects the continuation-line limit
and defaults to 100 characters.

Generated files are replaced when the same implementation is written again.
ADE testbench configuration and simulator execution are outside this
database writer's scope.
