#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Import annotated CDL templates into BAG design-module libraries."""

import argparse
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bag.interface.cdl import CdlInterface
from bag.io.file import write_file
from bag.util.cache import ClassImporter


DEFAULT_EXCLUDED_LIBRARIES = ['BAG_prim', 'basic', 'analogLib']


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Import annotated CDL templates into BAG.'
    )
    parser.add_argument(
        'source_files',
        nargs='+',
        help='annotated, preprocessed CDL template files',
    )
    parser.add_argument(
        '--library',
        required=True,
        help='BAG library to import from the annotated source files',
    )
    parser.add_argument(
        '--cell',
        help='import only this cell and its non-excluded dependencies',
    )
    parser.add_argument(
        '--output-root',
        required=True,
        help='directory containing generated BAG Python library packages',
    )
    parser.add_argument(
        '--register',
        required=True,
        help='bag_libs.def file to read and update',
    )
    parser.add_argument(
        '--exclude-library',
        action='append',
        default=list(DEFAULT_EXCLUDED_LIBRARIES),
        help='external library not recursively imported; may be repeated',
    )
    parser.add_argument(
        '--no-strict',
        action='store_true',
        help='ignore unsupported CDL element types',
    )
    return parser.parse_args(argv)


def run_main(args):
    output_root = os.path.abspath(os.path.expandvars(args.output_root))
    lib_defs = os.path.abspath(os.path.expandvars(args.register))
    source_files = [
        os.path.abspath(os.path.expandvars(path)) for path in args.source_files
    ]

    os.makedirs(output_root, exist_ok=True)
    lib_defs_parent = os.path.dirname(lib_defs)
    if lib_defs_parent:
        os.makedirs(lib_defs_parent, exist_ok=True)
    if not os.path.exists(lib_defs):
        write_file(lib_defs, '', mkdir=False)

    db_config = dict(
        default_lib_path=output_root,
        schematic=dict(
            exclude_libraries=list(dict.fromkeys(args.exclude_library)),
        ),
        cdl=dict(
            source_files=source_files,
            strict=not args.no_strict,
        ),
    )
    interface = CdlInterface(None, db_config)
    registry = ClassImporter(lib_defs)
    try:
        if args.cell:
            interface.import_sch_cellview(
                args.library, args.cell, registry, output_root
            )
            imported_cells = [args.cell]
        else:
            imported_cells = interface.get_cells_in_library(args.library)
            if not imported_cells:
                raise ValueError(
                    'No CDL templates found for BAG library {}.'
                    .format(args.library)
                )
            interface.import_design_library(
                args.library, registry, output_root
            )
    finally:
        interface.close()

    print(
        'Imported {} cell(s) from {} into {}/{}.'
        .format(
            len(imported_cells),
            ', '.join(source_files),
            output_root,
            args.library,
        )
    )


if __name__ == '__main__':
    run_main(parse_args())
