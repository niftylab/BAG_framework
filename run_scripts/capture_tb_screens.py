# -*- coding: utf-8 -*-
"""Capture testbench schematic / ADE-L / waveform screens to PNG.

Read-only tutorial/report helper: opens the testbench windows through the
BAG skill server, captures each with X11 ``xwd``, converts to PNG and closes
everything it opened (see bag.interface.capture). Run on the Virtuoso host
from the workspace root::

    start_bag.sh ./BAG_framework/run_scripts/capture_tb_screens.py -- \\
        clkdiv_testbenches tb_c2mos_div2 \\
        --psf-dir ./simulation/tb_c2mos_div2/spectre/config/psf \\
        --signals CLKI CLKO0 CLKO1 \\
        --out-dir ./runtime/tmp/manual/tb_captures

(the ``--`` keeps IPython from parsing the script's own options)

Requires existing tran results for the waveform shot (run the testbench
first); skip it with ``--no-wave``. The ADE state is loaded but never saved,
and the schematic opens in read mode.
"""

import argparse
import functools
import os

from bag.core import BagProject
from bag.interface.capture import TestbenchScreenCapture

print = functools.partial(print, flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tb_lib', help='testbench library name')
    parser.add_argument('tb_cell', help='testbench cell name')
    parser.add_argument('--config-view', default='config',
                        help='config view opened by the ADE-L session')
    parser.add_argument('--state', default='spectre_state1',
                        help='saved ADE state view to load')
    parser.add_argument('--psf-dir', default='',
                        help='existing psf results directory for the waveform plot')
    parser.add_argument('--signals', nargs='+', default=[],
                        help='net names to plot from the psf results')
    parser.add_argument('--out-dir', default='.',
                        help='directory the PNG files are written to')
    parser.add_argument('--no-sch', dest='cap_sch', action='store_false',
                        default=True, help='skip the schematic capture')
    parser.add_argument('--no-adel', dest='cap_adel', action='store_false',
                        default=True, help='skip the ADE-L capture')
    parser.add_argument('--no-wave', dest='cap_wave', action='store_false',
                        default=True, help='skip the waveform capture')
    parser.add_argument('--keep-open', action='store_true', default=False,
                        help='leave the opened windows on screen afterwards')
    return parser.parse_args()


def run_main(prj, args):
    os.makedirs(args.out_dir, exist_ok=True)
    cap = TestbenchScreenCapture(prj)
    print('capturing on display %s' % cap.display)
    # pre-existing windows are never captured or closed
    viva_before = {w[0] for w in cap.list_windows(r'Visualization & Analysis')}

    try:
        if args.cap_sch:
            out = os.path.join(args.out_dir, '%s_schematic.png' % args.tb_cell)
            cap.open_schematic(args.tb_lib, args.tb_cell)
            cap.capture_by_title(
                r'Schematic Editor L Reading: %s %s' % (args.tb_lib, args.tb_cell),
                out, raise_skill='hiRaiseWindow(__bag_cap_sch)')
            print('[sch]  %s' % out)

        if args.cap_adel:
            out = os.path.join(args.out_dir, '%s_adel.png' % args.tb_cell)
            cap.open_adel_state(args.tb_lib, args.tb_cell,
                                config_view=args.config_view,
                                ade_state=args.state)
            cap.capture_by_title(
                r'ADE L \(\d+\) - %s %s' % (args.tb_lib, args.tb_cell),
                out, raise_skill='hiRaiseWindow(__bag_cap_adel_win)')
            print('[adel] %s' % out)

        if args.cap_wave:
            if not args.psf_dir or not args.signals:
                print('[wave] skipped: --psf-dir and --signals are required')
            else:
                out = os.path.join(args.out_dir, '%s_waveform.png' % args.tb_cell)
                cap.plot_transient(args.psf_dir, args.signals)
                cap.capture_by_title(r'Visualization & Analysis', out,
                                     exclude=viva_before)
                print('[wave] %s' % out)
    finally:
        if args.keep_open:
            print('[keep-open] windows left on screen')
        else:
            cap.close()
            leftover = [w for w in cap.list_windows(r'Visualization & Analysis')
                        if w[0] not in viva_before]
            if leftover:
                print('[warn] ViVA windows still open: %s' % leftover)


if __name__ == '__main__':
    args = parse_args()
    local_dict = locals()
    bprj = local_dict.get('bprj', BagProject())
    run_main(bprj, args)
