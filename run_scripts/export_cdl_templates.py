#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Export BAG ``netlist_info`` YAML files as annotated CDL templates."""

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bag.io.cdl import CdlTemplateWriter, load_schematic_library


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Export BAG netlist_info YAML files as annotated CDL.'
    )
    parser.add_argument(
        'netlist_info_dir',
        help='directory containing BAG netlist_info/*.yaml files',
    )
    parser.add_argument(
        '--output-dir',
        help='template output directory; defaults to ../cdl_templates',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='report the CDL files that would be written without writing them',
    )
    return parser.parse_args(argv)


def run_main(args):
    info_dir = Path(args.netlist_info_dir).resolve()
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir else info_dir.parent / 'cdl_templates'
    )
    cells = load_schematic_library(info_dir)
    output_files = [output_dir / (cell.cell_name + '.cdl') for cell in cells]
    if args.dry_run:
        for output_file in output_files:
            print(output_file)
        return

    writer = CdlTemplateWriter()
    for cell in cells:
        writer.write_cell(str(output_dir), cell)
    print('Exported {} CDL template(s) to {}.'.format(len(cells), output_dir))


if __name__ == '__main__':
    run_main(parse_args())
