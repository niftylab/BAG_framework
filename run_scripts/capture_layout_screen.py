# -*- coding: utf-8 -*-
"""Capture a layout view's editor window to PNG.

Read-only counterpart of capture_tb_screens.py for layout cells: opens the
layout in read mode through the BAG skill server, zoom-fits, captures the
window with X11 ``xwd`` and closes it (see bag.interface.capture). Run on
the Virtuoso host from the workspace root::

    start_bag.sh ./BAG_framework/run_scripts/capture_layout_screen.py -- \\
        clkdiv_generated c2mos_div2_8x --out-dir ./runtime/tmp/manual/captures

(the ``--`` keeps IPython from parsing the script's own options)
"""

import argparse
import functools
import os

from bag.core import BagProject
from bag.interface.capture import TestbenchScreenCapture

print = functools.partial(print, flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('lib', help='layout library name')
    parser.add_argument('cell', help='layout cell name')
    parser.add_argument('--view', default='layout', help='layout view name')
    parser.add_argument('--width', type=int, default=1400,
                        help='capture window width in pixels')
    parser.add_argument('--height', type=int, default=1000,
                        help='capture window height in pixels')
    parser.add_argument('--out-dir', default='.',
                        help='directory the PNG file is written to')
    parser.add_argument('--keep-open', action='store_true', default=False,
                        help='leave the layout window on screen afterwards')
    return parser.parse_args()


def run_main(prj, args):
    os.makedirs(args.out_dir, exist_ok=True)
    cap = TestbenchScreenCapture(prj)
    print('capturing on display %s' % cap.display)
    out = os.path.join(args.out_dir, '%s_%s.png' % (args.cell, args.view))
    try:
        cap.open_layout(args.lib, args.cell, view=args.view,
                        size=(args.width, args.height))
        cap.capture_by_title(
            r'Layout Suite \w+ Reading: %s %s %s' % (args.lib, args.cell, args.view),
            out, raise_skill='hiRaiseWindow(__bag_cap_lay)')
        print('[layout] %s' % out)
    finally:
        if args.keep_open:
            print('[keep-open] window left on screen')
        else:
            cap.close()


if __name__ == '__main__':
    args = parse_args()
    local_dict = locals()
    bprj = local_dict.get('bprj', BagProject())
    run_main(bprj, args)
