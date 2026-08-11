# -*- coding: utf-8 -*-
"""Read-only screen capture of Virtuoso testbench windows.

This module drives the BAG skill server to open a testbench's schematic, an
ADE-L session with a saved state, and a ViVA plot of existing tran results,
then captures each window with X11 ``xwd`` and converts the dumps to PNG.

It must run on the host whose X display shows the Virtuoso session (the same
host ``start_bag.sh`` runs on): ``xwd``/``xwininfo`` connect to the DISPLAY
that the Virtuoso process reports via ``getShellEnvVar("DISPLAY")``.

Everything is read-only by design: the schematic opens in "r" mode, the ADE-L
state is loaded but never saved back, and plots read an existing psf
directory. ``close()`` restores the GUI to its prior window set. Tracked OA
files are not modified.

See ``run_scripts/capture_tb_screens.py`` for the command-line entry point.
"""

import re
import struct
import subprocess
import time
import zlib


def write_png(path, width, height, rgb):
    """Write an 8-bit RGB PNG without PIL.

    Parameters
    ----------
    path : str
        output PNG path.
    width : int
    height : int
    rgb : numpy.ndarray
        (height, width, 3) uint8 array.
    """
    import numpy as np

    raw = np.empty((height, width * 3 + 1), dtype=np.uint8)
    raw[:, 0] = 0  # filter type 0 per scanline
    raw[:, 1:] = rgb.reshape(height, width * 3)

    def chunk(tag, data):
        payload = tag + data
        return (struct.pack(">I", len(data)) + payload +
                struct.pack(">I", zlib.crc32(payload) & 0xffffffff))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)))
        f.write(chunk(b"IEND", b""))


def xwd_to_png(xwd_path, png_path):
    """Convert a 24/32-bit ZPixmap TrueColor XWD dump to PNG."""
    import numpy as np

    with open(xwd_path, "rb") as f:
        data = f.read()
    (header_size, file_version, _fmt, _depth, width, height, _xoff, byte_order,
     _unit, _bit_order, _pad, bits_per_pixel, bytes_per_line, _visual,
     red_mask, green_mask, blue_mask, _bits_rgb, _cmap_entries, ncolors
     ) = struct.unpack(">20I", data[:80])
    if file_version != 7:
        raise ValueError("unsupported XWD version %d" % file_version)
    if bits_per_pixel not in (24, 32):
        raise ValueError("unsupported bits_per_pixel %d" % bits_per_pixel)
    offset = header_size + ncolors * 12

    def shift(mask):
        s = 0
        while mask and not (mask & 1):
            mask >>= 1
            s += 1
        return s

    bpp = bits_per_pixel // 8
    rows = np.frombuffer(data, dtype=np.uint8, count=height * bytes_per_line,
                         offset=offset).reshape(height, bytes_per_line)
    px = rows[:, :width * bpp].reshape(height, width, bpp).astype(np.uint32)
    if byte_order == 1:  # MSB first
        val = px[:, :, 0]
        for i in range(1, bpp):
            val = (val << 8) | px[:, :, i]
    else:
        val = px[:, :, bpp - 1]
        for i in range(bpp - 2, -1, -1):
            val = (val << 8) | px[:, :, i]
    rgb = np.stack([(val & red_mask) >> shift(red_mask),
                    (val & green_mask) >> shift(green_mask),
                    (val & blue_mask) >> shift(blue_mask)],
                   axis=-1).astype(np.uint8)
    write_png(png_path, width, height, rgb)


class TestbenchScreenCapture(object):
    """Open and capture testbench windows through the BAG skill server.

    Parameters
    ----------
    prj : bag.BagProject
        an open BAG project (its ``impl_db`` must be a skill interface).
    display : str or None
        X display of the Virtuoso session; queried from the session itself
        when None.
    """

    def __init__(self, prj, display=None):
        self._ev = prj.impl_db._eval_skill
        self.display = (display or
                        self._ev('getShellEnvVar("DISPLAY")').strip('"'))
        self._opened_sch = False
        self._opened_adel = False

    # ------------------------------------------------------------------
    # window opening (read-only)
    # ------------------------------------------------------------------

    def open_schematic(self, lib, cell, size=(1550, 960)):
        """Open ``lib/cell`` schematic in read mode, fit and raise it."""
        self._ev('__bag_cap_sch = geOpen(?lib "%s" ?cell "%s" '
                 '?view "schematic" ?mode "r")' % (lib, cell))
        self._ev('hiResizeWindow(__bag_cap_sch list(10:60 %d:%d))'
                 % (10 + size[0], 60 + size[1]))
        self._ev('hiSetCurrentWindow(__bag_cap_sch)')
        self._ev('errset(schZoomFit(1.0 0.9) t)')
        self._opened_sch = True

    def open_adel_state(self, lib, cell, config_view="config",
                        ade_state="spectre_state1", size=(1200, 760)):
        """Open an ADE-L session on ``lib/cell`` and load ``ade_state``.

        Same steps as ``open_adel_session`` (bag_adel_session.il), but the
        window handle is taken right after ``sevStartSession`` — loading a
        state whose outputs have plot flags can pop a ViVA window and change
        what ``hiGetCurrentWindow`` returns. The state is loaded into the
        session only and never saved back.
        """
        self._ev('__bag_cap_sev = sevStartSession(?lib "%s" ?cell "%s" '
                 '?view "%s")' % (lib, cell, config_view))
        self._ev('__bag_cap_adel_win = hiGetCurrentWindow()')
        self._ev('__bag_cap_asi = asiGetSession(__bag_cap_adel_win)')
        self._ev('asiLoadState(__bag_cap_asi ?name "%s" ?option \'cellview '
                 '?lib "%s" ?cell "%s")' % (ade_state, lib, cell))
        self._ev('errset(hiResizeWindow(__bag_cap_adel_win '
                 'list(40:100 %d:%d)) t)' % (40 + size[0], 100 + size[1]))
        self._opened_adel = True

    def plot_transient(self, psf_dir, signals):
        """Plot ``signals`` from an existing psf directory in a ViVA window."""
        self._ev('openResults("%s")' % psf_dir)
        self._ev("selectResult('tran)")
        self._ev('errset(newWindow() t)')
        self._ev('plot(%s)' % ' '.join('v("%s")' % s for s in signals))

    # ------------------------------------------------------------------
    # capture
    # ------------------------------------------------------------------

    def list_windows(self, title_pattern):
        """Return [(xid, title)] of X windows whose title matches the regex."""
        out = subprocess.run(
            ["xwininfo", "-display", self.display, "-root", "-tree"],
            capture_output=True, text=True, check=True).stdout
        pat = re.compile(title_pattern)
        wins = []
        for line in out.splitlines():
            m = re.match(r'\s*(0x[0-9a-f]+) "([^"]*)"', line)
            if m and pat.search(m.group(2)):
                wins.append((m.group(1), m.group(2)))
        return wins

    def capture_window(self, xid, out_png, raise_skill=None, settle=1.0):
        """Capture one window to PNG, optionally raising it via SKILL first.

        ``xwd`` copies the window's on-screen pixels, so an obscured window
        captures whatever covers it — pass ``raise_skill`` (a SKILL
        expression such as ``"hiRaiseWindow(__bag_cap_sch)"``) to bring the
        target to the front, then wait ``settle`` seconds for the redraw.
        """
        if raise_skill:
            self._ev('errset(%s t)' % raise_skill)
            time.sleep(settle)
        xwd_path = out_png + ".xwd"
        subprocess.run(["xwd", "-display", self.display, "-id", xid,
                        "-silent", "-out", xwd_path], check=True)
        xwd_to_png(xwd_path, out_png)
        subprocess.run(["rm", "-f", xwd_path], check=False)

    def capture_by_title(self, title_pattern, out_png, raise_skill=None,
                         exclude=()):
        """Capture the first window matching the regex, skipping ``exclude`` ids."""
        wins = [w for w in self.list_windows(title_pattern)
                if w[0] not in exclude]
        if not wins:
            raise RuntimeError('no window matches %r' % title_pattern)
        self.capture_window(wins[0][0], out_png, raise_skill=raise_skill)
        return wins[0]

    # ------------------------------------------------------------------
    # cleanup
    # ------------------------------------------------------------------

    def close(self):
        """Close every window this instance opened; never saves anything."""
        if self._opened_adel:
            self._ev('errset(sevQuit(__bag_cap_sev) t)')
            self._opened_adel = False
        if self._opened_sch:
            self._ev('errset(hiCloseWindow(__bag_cap_sch) t)')
            self._opened_sch = False
        self._ev('errset(awvCloseAll() t)')
