# -*- coding: utf-8 -*-
"""Bounding box of a GDSII cell, hierarchy included, with the standard library only.

The DRC waiver policy needs the extent of the judged cell to tell a violation on
the cell's edge (geometry the parent completes) from one inside it.  The extent
is the union of drawn shapes, followed through SREF/AREF placements (reflection,
rotation, magnification), either over every layer or over one layer such as the
PDK's prBoundary, which the primitive templates draw exactly over their
placement box.  TEXT records do not count.  PATH records are widened by half
their width.

Only the records needed for the extent are decoded; everything else is skipped by
length.  Coordinates come back in micrometres.
"""

import math
import struct

_HEADER = 0x00
_BGNLIB = 0x01
_UNITS = 0x03
_ENDLIB = 0x04
_BGNSTR = 0x05
_STRNAME = 0x06
_ENDSTR = 0x07
_BOUNDARY = 0x08
_PATH = 0x09
_SREF = 0x0A
_AREF = 0x0B
_TEXT = 0x0C
_LAYER = 0x0D
_DATATYPE = 0x0E
_WIDTH = 0x0F
_XY = 0x10
_ENDEL = 0x11
_SNAME = 0x12
_COLROW = 0x13
_STRANS = 0x1A
_MAG = 0x1B
_ANGLE = 0x1C
_BOX = 0x2D
_BOXTYPE = 0x2E


def _real8(data):
    # type: (bytes) -> float
    """Decode one GDSII 8-byte real (excess-64 base-16 exponent, 56-bit mantissa)."""
    if len(data) < 8:
        raise ValueError('GDS real8 record is truncated.')
    first = data[0]
    sign = -1.0 if first & 0x80 else 1.0
    exponent = (first & 0x7F) - 64
    mantissa = 0
    for byte in data[1:8]:
        mantissa = (mantissa << 8) | byte
    return sign * mantissa * (16.0 ** exponent) / float(1 << 56)


def _records(data):
    """Yield (record_type, body) for every record in the file."""
    pos = 0
    size = len(data)
    while pos + 4 <= size:
        length, rtype = struct.unpack('>HB', data[pos:pos + 3])
        if length < 4:
            raise ValueError('GDS record with length < 4 at offset {}.'.format(pos))
        yield rtype, data[pos + 4:pos + length]
        pos += length
        if rtype == _ENDLIB:
            return


def _points(body):
    count = len(body) // 8
    vals = struct.unpack('>' + 'i' * (2 * count), body[:8 * count])
    return [(vals[2 * i], vals[2 * i + 1]) for i in range(count)]


def _parse(data):
    """Return (um_per_dbu, {cell: {'boxes': [(layer, dtype, (x0,y0,x1,y1))], 'refs': [...]}})."""
    um_per_dbu = None
    cells = {}
    cur = None
    elem = None
    for rtype, body in _records(data):
        if rtype == _UNITS:
            um_per_dbu = _real8(body[8:16]) * 1e6
        elif rtype == _STRNAME:
            cur = {'boxes': [], 'refs': []}
            cells[body.rstrip(b'\x00').decode('ascii', 'replace')] = cur
        elif rtype == _ENDSTR:
            cur = None
        elif cur is None:
            continue
        elif rtype in (_BOUNDARY, _PATH, _SREF, _AREF, _TEXT, _BOX):
            elem = {'type': rtype, 'width': 0, 'reflect': False, 'angle': 0.0,
                    'mag': 1.0, 'cols': 1, 'rows': 1, 'xy': [], 'sname': None,
                    'layer': None, 'dtype': 0}
        elif elem is None:
            continue
        elif rtype == _LAYER:
            elem['layer'] = struct.unpack('>h', body[:2])[0]
        elif rtype in (_DATATYPE, _BOXTYPE):
            elem['dtype'] = struct.unpack('>h', body[:2])[0]
        elif rtype == _WIDTH:
            elem['width'] = struct.unpack('>i', body[:4])[0]
        elif rtype == _SNAME:
            elem['sname'] = body.rstrip(b'\x00').decode('ascii', 'replace')
        elif rtype == _STRANS:
            elem['reflect'] = bool(struct.unpack('>H', body[:2])[0] & 0x8000)
        elif rtype == _MAG:
            elem['mag'] = _real8(body)
        elif rtype == _ANGLE:
            elem['angle'] = _real8(body)
        elif rtype == _COLROW:
            elem['cols'], elem['rows'] = struct.unpack('>hh', body[:4])
        elif rtype == _XY:
            elem['xy'] = _points(body)
        elif rtype == _ENDEL:
            _finish(cur, elem)
            elem = None
    if um_per_dbu is None:
        raise ValueError('GDS file has no UNITS record.')
    return um_per_dbu, cells


def _finish(cell, elem):
    xy = elem['xy']
    if not xy:
        return
    etype = elem['type']
    if etype in (_BOUNDARY, _BOX, _PATH):
        xs = [p[0] for p in xy]
        ys = [p[1] for p in xy]
        half = abs(elem['width']) // 2 if etype == _PATH else 0
        cell['boxes'].append((elem['layer'], elem['dtype'],
                              (min(xs) - half, min(ys) - half, max(xs) + half, max(ys) + half)))
    elif etype in (_SREF, _AREF) and elem['sname']:
        cell['refs'].append(elem)


def _transform(point, reflect, angle, mag):
    x, y = point
    if reflect:
        y = -y
    if mag != 1.0:
        x, y = x * mag, y * mag
    if angle:
        rad = math.radians(angle)
        c, s = math.cos(rad), math.sin(rad)
        x, y = x * c - y * s, x * s + y * c
    return x, y


def _union(boxes):
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _extent(cells, name, layer, memo, trail):
    """Union bbox (dbu) of ``name`` on ``layer`` ((l, d) or None for all), or None."""
    key = (name, layer)
    if key in memo:
        return memo[key]
    if name in trail:
        raise ValueError('GDS hierarchy loops through {}.'.format(name))
    cell = cells.get(name)
    if cell is None:
        raise KeyError('GDS structure not found: {}'.format(name))
    boxes = [box for lay, dt, box in cell['boxes'] if layer is None or (lay, dt) == layer]
    for ref in cell['refs']:
        child = _extent(cells, ref['sname'], layer, memo, trail + (name,))
        if child is None:
            continue
        corners = [(child[0], child[1]), (child[2], child[1]),
                   (child[2], child[3]), (child[0], child[3])]
        placed = [_transform(p, ref['reflect'], ref['angle'], ref['mag']) for p in corners]
        local = _union([(p[0], p[1], p[0], p[1]) for p in placed])
        for ox, oy in _array_origins(ref):
            boxes.append((local[0] + ox, local[1] + oy, local[2] + ox, local[3] + oy))
    result = _union(boxes)
    memo[key] = result
    return result


def _array_origins(ref):
    xy = ref['xy']
    if ref['type'] == _SREF or len(xy) < 3:
        return [xy[0]]
    cols, rows = max(ref['cols'], 1), max(ref['rows'], 1)
    (x0, y0), (xc, yc), (xr, yr) = xy[0], xy[1], xy[2]
    dcx, dcy = (xc - x0) / float(cols), (yc - y0) / float(cols)
    drx, dry = (xr - x0) / float(rows), (yr - y0) / float(rows)
    # Only the four corner placements can bound the array.
    return [(x0 + dcx * i + drx * j, y0 + dcy * i + dry * j)
            for i in (0, cols - 1) for j in (0, rows - 1)]


def _layer_keys(cells, name, seen, trail):
    """Every (layer, dtype) drawn anywhere under ``name``."""
    if name in seen or name in trail:
        return
    seen.add(name)
    cell = cells.get(name)
    if cell is None:
        raise KeyError('GDS structure not found: {}'.format(name))
    for lay, dt, _ in cell['boxes']:
        seen.add((lay, dt))
    for ref in cell['refs']:
        _layer_keys(cells, ref['sname'], seen, trail + (name,))


def parse_layer(spec):
    # type: (object) -> Tuple[int, int]
    """``"108/0"``, ``"108"``, ``(108, 0)`` or ``108`` -> (108, 0)."""
    if isinstance(spec, (list, tuple)) and len(spec) == 2:
        return int(spec[0]), int(spec[1])
    if isinstance(spec, int) and not isinstance(spec, bool):
        return spec, 0
    text = str(spec).strip()
    if '/' in text:
        lay, dt = text.split('/', 1)
        return int(lay), int(dt)
    return int(text), 0


def _load(gds_file):
    with open(gds_file, 'rb') as stream:
        data = stream.read()
    if len(data) < 4 or struct.unpack('>HB', data[:3])[1] != _HEADER:
        raise ValueError('Not a GDSII stream file: {}'.format(gds_file))
    return _parse(data)


def _um(box, um_per_dbu):
    return tuple(round(v * um_per_dbu, 6) for v in box)


def gds_cell_extent(gds_file, cell_name, layer=None):
    # type: (str, str, object) -> Tuple[float, float, float, float]
    """Return (x0, y0, x1, y1) in micrometres of ``cell_name`` with its hierarchy.

    With ``layer`` (``"108/0"``, ``(108, 0)``) only shapes on that layer count,
    e.g. the PDK prBoundary the templates draw over their placement box.  Raises
    ``KeyError`` when the structure is missing and ``ValueError`` when the file is
    not a GDSII stream or nothing is drawn (on that layer).
    """
    um_per_dbu, cells = _load(gds_file)
    key = parse_layer(layer) if layer is not None else None
    box = _extent(cells, cell_name, key, {}, ())
    if box is None:
        what = ' on layer {}/{}'.format(*key) if key else ''
        raise ValueError('GDS structure {} draws no geometry{}.'.format(cell_name, what))
    return _um(box, um_per_dbu)


def gds_layer_extents(gds_file, cell_name):
    # type: (str, str) -> Dict[str, Tuple[float, float, float, float]]
    """Per-layer extents ``{"<layer>/<dtype>": (x0, y0, x1, y1)}`` in micrometres.

    Used to measure how far each layer of a template reaches beyond its
    placement box (the overhang that sizes boundary waiver margins).
    """
    um_per_dbu, cells = _load(gds_file)
    keys = set()
    _layer_keys(cells, cell_name, keys, ())
    out = {}
    memo = {}
    for key in sorted(k for k in keys if isinstance(k, tuple)):
        box = _extent(cells, cell_name, key, memo, ())
        if box is not None:
            out['{}/{}'.format(*key)] = _um(box, um_per_dbu)
    return out
