# -*- coding: utf-8 -*-

"""Parser for BAG metadata embedded in CDL comments."""

from collections import OrderedDict
import json
import re

from .core import CdlParseError


_FULL_LINE_RE = re.compile(r'^\s*\*\s*@BAG\s+(.+?)\s*$')
_INLINE_RE = re.compile(r'\$\s*@BAG\s+')
_PININFO_RE = re.compile(r'^\s*\*\s*\.PININFO\b(.*?)$', re.IGNORECASE)
_PININFO_DIRECTIONS = {
    'I': 'input',
    'O': 'output',
    'B': 'inputOutput',
}


def _find_unquoted_dollar(line):
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in ('\'', '"'):
            quote = char
        elif char == '$':
            return index
    return -1


def parse_payload(payload, source='<string>', line_no=None):
    """Parse and validate one BAG annotation JSON payload."""
    try:
        value = json.loads(payload)
    except ValueError as err:
        raise CdlParseError(
            'Invalid BAG annotation JSON: {}'.format(err),
            source=source,
            line_no=line_no,
        )
    if not isinstance(value, dict):
        raise CdlParseError(
            'BAG annotation payload must be a JSON object.',
            source=source,
            line_no=line_no,
        )
    return value


def parse_full_line_annotation(line, source='<string>', line_no=None):
    """Return metadata from a ``* @BAG`` line, or ``None``."""
    match = _FULL_LINE_RE.match(line)
    if match is None:
        return None
    return parse_payload(match.group(1), source=source, line_no=line_no)


def parse_pin_info(line, source='<string>', line_no=None):
    """Parse a CDL-style ``*.PININFO`` comment, if present.

    Each entry has the form ``pin:I``, ``pin:O``, or ``pin:B``.  The
    returned mapping uses BAG's ``input``, ``output``, and ``inputOutput``
    direction names.
    """
    match = _PININFO_RE.match(line)
    if match is None:
        return None

    payload = match.group(1).strip()
    if not payload:
        raise CdlParseError(
            'PININFO requires at least one pin direction entry.',
            source=source,
            line_no=line_no,
        )

    directions = OrderedDict()
    for entry in payload.split():
        if ':' not in entry:
            raise CdlParseError(
                'Invalid PININFO entry {!r}.'.format(entry),
                source=source,
                line_no=line_no,
            )
        pin_name, direction_code = entry.rsplit(':', 1)
        direction_code = direction_code.upper()
        if not pin_name or direction_code not in _PININFO_DIRECTIONS:
            raise CdlParseError(
                'Invalid PININFO entry {!r}.'.format(entry),
                source=source,
                line_no=line_no,
            )
        if pin_name in directions:
            raise CdlParseError(
                'Duplicate PININFO entry for pin {!r}.'.format(pin_name),
                source=source,
                line_no=line_no,
            )
        directions[pin_name] = _PININFO_DIRECTIONS[direction_code]
    return directions


def split_inline_annotation(line, source='<string>', line_no=None):
    """Split CDL code from an optional ``$ @BAG`` annotation.

    Ordinary inline ``$`` comments are removed from the returned code and are
    otherwise ignored.
    """
    dollar = _find_unquoted_dollar(line)
    if dollar < 0:
        return line.rstrip(), None

    code = line[:dollar].rstrip()
    comment = line[dollar:]
    match = _INLINE_RE.match(comment)
    if match is None:
        return code, None

    payload = comment[match.end():].strip()
    if not payload:
        raise CdlParseError(
            'Missing JSON object after $ @BAG.',
            source=source,
            line_no=line_no,
        )
    return code, parse_payload(payload, source=source, line_no=line_no)


def merge_metadata(target, update, source='<string>', line_no=None):
    """Merge metadata dictionaries while rejecting conflicting definitions."""
    for key, value in update.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            merge_metadata(
                target[key], value, source=source, line_no=line_no
            )
        elif key in target and target[key] != value:
            raise CdlParseError(
                'Conflicting BAG annotation value for {!r}.'.format(key),
                source=source,
                line_no=line_no,
            )
        else:
            target[key] = value
