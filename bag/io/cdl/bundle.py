# -*- coding: utf-8 -*-

"""Build self-contained, library-qualified CDL source bundles."""

from collections import OrderedDict
import hashlib
import json
import os
import re
import shutil
import tempfile

from .cdl_writer import CdlWriter


_INCLUDE_RE = re.compile(
    r"""^\s*\.inc(?:lude)?\s+(?:"([^"]+)"|'([^']+)'|(\S+))""",
    re.IGNORECASE,
)
_SUBCKT_RE = re.compile(r'^\s*\.subckt\s+(\S+)', re.IGNORECASE)
_ENDS_RE = re.compile(r'^\s*\.ends(?:\s+\S+)?\s*$', re.IGNORECASE)


def _validate_component(value, description):
    if (
            not isinstance(value, str)
            or not value
            or os.path.basename(value) != value
            or any(character.isspace() for character in value)):
        raise ValueError(
            '{} must be a non-empty path-safe identifier: {!r}.'
            .format(description, value)
        )
    return value


def _posix_path(path):
    return path.replace(os.sep, '/')


def _sha256_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _strip_generated_includes(text):
    """Remove includes; the bundle root includes each dependency once."""
    return '\n'.join(
        line for line in text.splitlines()
        if _INCLUDE_RE.match(line) is None
    ).rstrip() + '\n'


def _normalize_subckt_block(lines):
    return '\n'.join(line.strip() for line in lines).lower()


def _deduplicate_subckts(ordered_keys, source_texts,
                         subckt_overrides=None):
    """Apply authoritative overrides, then deduplicate subcircuits."""
    seen = {}
    result = {}
    overrides = {}
    for name, text in (subckt_overrides or {}).items():
        lines = text.splitlines()
        if (
                not lines
                or _SUBCKT_RE.match(lines[0]) is None
                or _SUBCKT_RE.match(lines[0]).group(1).lower()
                != name.lower()
                or _ENDS_RE.match(lines[-1]) is None):
            raise ValueError(
                'Invalid authoritative .SUBCKT override for {}.'
                .format(name)
            )
        overrides[name.lower()] = lines

    for key in ordered_keys:
        lines = source_texts[key].splitlines()
        output = []
        index = 0
        while index < len(lines):
            match = _SUBCKT_RE.match(lines[index])
            if match is None:
                output.append(lines[index])
                index += 1
                continue

            name = match.group(1)
            block = [lines[index]]
            index += 1
            while index < len(lines):
                block.append(lines[index])
                index += 1
                if _ENDS_RE.match(block[-1]):
                    break
            else:
                raise ValueError(
                    'Unterminated .SUBCKT {} in {}/{}.'
                    .format(name, key[0], key[1])
                )

            canonical_name = name.lower()
            if canonical_name in overrides:
                block = overrides[canonical_name]
            normalized = _normalize_subckt_block(block)
            digest = _sha256_text(normalized)
            previous = seen.get(canonical_name)
            if previous is None:
                seen[canonical_name] = (digest, key)
                output.extend(block)
            elif previous[0] != digest:
                raise ValueError(
                    'Conflicting .SUBCKT {} definitions in {}/{} and {}/{}.'
                    .format(
                        name,
                        previous[1][0],
                        previous[1][1],
                        key[0],
                        key[1],
                    )
                )

        result[key] = '\n'.join(output).rstrip() + '\n'
    return result


def _iter_statements(text):
    current = None
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith('*'):
            continue
        if stripped.startswith('+'):
            if current is not None:
                current += ' ' + stripped[1:].strip()
            continue
        if current is not None:
            yield current
        current = stripped
    if current is not None:
        yield current


def _subckt_references(text):
    references = set()
    for statement in _iter_statements(text):
        if not statement or statement[0].upper() != 'X':
            continue
        statement = statement.split('$', 1)[0].strip()
        tokens = statement.split()
        if len(tokens) < 2:
            continue
        parameter_index = len(tokens)
        for index, token in enumerate(tokens[1:], 1):
            if '=' in token or token.upper() == 'PARAMS:':
                parameter_index = index
                break
        target_index = parameter_index - 1
        if target_index >= 1:
            references.add(tokens[target_index].lower())
    return references


def _subckt_definitions(text):
    return set(
        match.group(1).lower()
        for line in text.splitlines()
        for match in [_SUBCKT_RE.match(line)]
        if match is not None
    )


class CdlBundleBuilder(object):
    """Build one top-cell LVCDL bundle from generated implementation files."""

    def __init__(self, source_resolver, extension='.sp',
                 external_subckts=None, subckt_overrides=None):
        self._source_resolver = source_resolver
        self._extension = extension
        self._external_subckts = set(
            name.lower() for name in (external_subckts or [])
        )
        self._subckt_overrides = dict(subckt_overrides or {})

    def build(self, lib_name, cell_name, bundle_root):
        """Create a dependency-complete bundle and return its ``top.sp``."""
        top_key = (
            _validate_component(lib_name, 'Top library name'),
            _validate_component(cell_name, 'Top cell name'),
        )
        bundle_root = os.path.realpath(os.path.abspath(str(bundle_root)))
        os.makedirs(bundle_root, exist_ok=True)
        target_dir = os.path.join(bundle_root, top_key[0], top_key[1])
        self._validate_target(bundle_root, target_dir)

        source_paths = {}
        dependencies = {}
        ordered_keys = []
        states = {}

        def visit(key):
            state = states.get(key)
            if state == 'done':
                return
            if state == 'active':
                raise ValueError(
                    'Cyclic CDL dependency detected at {}/{}.'
                    .format(key[0], key[1])
                )
            states[key] = 'active'
            source_path = os.path.realpath(os.path.abspath(
                self._source_resolver(key[0], key[1])
            ))
            if not os.path.isfile(source_path):
                raise ValueError(
                    'CDL implementation {}/{} has not been generated: {}'
                    .format(key[0], key[1], source_path)
                )
            source_paths[key] = source_path
            cell_dependencies = self._load_dependencies(source_path, key)
            dependencies[key] = cell_dependencies
            for dependency in cell_dependencies:
                visit(dependency)
            states[key] = 'done'
            ordered_keys.append(key)

        visit(top_key)

        staging_dir = tempfile.mkdtemp(
            prefix='.lvcdl-bundle-',
            dir=bundle_root,
        )
        try:
            final_top = self._write_bundle(
                staging_dir,
                top_key,
                ordered_keys,
                source_paths,
                dependencies,
            )
            if os.path.isdir(target_dir):
                shutil.rmtree(target_dir)
            os.makedirs(os.path.dirname(target_dir), exist_ok=True)
            os.replace(staging_dir, target_dir)
            staging_dir = None
            return os.path.join(target_dir, os.path.basename(final_top))
        finally:
            if staging_dir is not None and os.path.isdir(staging_dir):
                shutil.rmtree(staging_dir)

    @staticmethod
    def _validate_target(bundle_root, target_dir):
        try:
            inside_root = (
                os.path.commonpath([bundle_root, target_dir]) == bundle_root
            )
        except ValueError:
            inside_root = False
        if not inside_root or target_dir == bundle_root:
            raise ValueError(
                'LVCDL bundle target escapes bundle root: {}'
                .format(target_dir)
            )

    def _load_dependencies(self, source_path, key):
        dependency_path = CdlWriter.get_dependency_path(source_path)
        if os.path.isfile(dependency_path):
            with open(dependency_path, 'r', encoding='utf-8') as stream:
                data = json.load(stream)
            if (
                    data.get('lib_name') != key[0]
                    or data.get('cell_name') != key[1]):
                raise ValueError(
                    'CDL dependency metadata does not match {}/{}: {}'
                    .format(key[0], key[1], dependency_path)
                )
            result = []
            seen = set()
            for dependency in data.get('dependencies', []):
                dependency_key = (
                    _validate_component(
                        dependency.get('lib_name'),
                        'Dependency library name',
                    ),
                    _validate_component(
                        dependency.get('cell_name'),
                        'Dependency cell name',
                    ),
                )
                if dependency_key not in seen:
                    seen.add(dependency_key)
                    result.append(dependency_key)
            return result

        result = []
        seen = set()
        with open(source_path, 'r', encoding='utf-8') as stream:
            for line in stream:
                match = _INCLUDE_RE.match(line)
                if match is None:
                    continue
                include_path = next(
                    value for value in match.groups() if value is not None
                )
                include_name = os.path.basename(include_path)
                cell_name, extension = os.path.splitext(include_name)
                if extension.lower() != self._extension.lower():
                    raise ValueError(
                        'Legacy CDL include requires dependency metadata: {}'
                        .format(include_path)
                    )
                dependency_key = (key[0], cell_name)
                if dependency_key not in seen:
                    seen.add(dependency_key)
                    result.append(dependency_key)
        return result

    def _write_bundle(self, staging_dir, top_key, ordered_keys,
                      source_paths, dependencies):
        destination_paths = OrderedDict()
        source_texts = {}
        for key in ordered_keys:
            relative_path = os.path.join(
                'libs', key[0], key[1] + self._extension
            )
            destination_paths[key] = relative_path
            with open(
                    source_paths[key],
                    'r',
                    encoding='utf-8',
                    errors='replace') as stream:
                source_texts[key] = _strip_generated_includes(stream.read())

        source_texts = _deduplicate_subckts(
            ordered_keys,
            source_texts,
            subckt_overrides=self._subckt_overrides,
        )
        definitions = set()
        references = set()
        for key in ordered_keys:
            definitions.update(_subckt_definitions(source_texts[key]))
            references.update(_subckt_references(source_texts[key]))
        missing_subckts = sorted(
            references - definitions - self._external_subckts
        )
        if missing_subckts:
            raise ValueError(
                'LVCDL bundle source is incomplete; missing .SUBCKT '
                'definitions: {}.'
                .format(', '.join(missing_subckts))
            )

        cells = []
        for key in ordered_keys:
            relative_path = destination_paths[key]
            destination = os.path.join(staging_dir, relative_path)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, 'w', encoding='utf-8') as stream:
                stream.write(source_texts[key])
            cells.append({
                'lib_name': key[0],
                'cell_name': key[1],
                'path': _posix_path(relative_path),
                'dependencies': [
                    {
                        'lib_name': dependency[0],
                        'cell_name': dependency[1],
                    }
                    for dependency in dependencies[key]
                ],
                'sha256': _sha256_text(source_texts[key]),
            })

        top_path = os.path.join(staging_dir, 'top.sp')
        top_lines = ['* BAG LVCDL library-qualified source bundle']
        top_lines.extend(
            '.include "{}"'.format(_posix_path(destination_paths[key]))
            for key in ordered_keys
        )
        with open(top_path, 'w', encoding='utf-8') as stream:
            stream.write('\n'.join(top_lines) + '\n')

        manifest = {
            'schema': 'bag.lvcdl.bundle.v1',
            'top': {
                'lib_name': top_key[0],
                'cell_name': top_key[1],
                'path': _posix_path(destination_paths[top_key]),
            },
            'cells': cells,
            'preflight': {
                'defined_subckts': sorted(definitions),
                'referenced_subckts': sorted(references),
                'external_subckts': sorted(self._external_subckts),
                'missing_subckts': missing_subckts,
            },
        }
        with open(
                os.path.join(staging_dir, 'manifest.json'),
                'w',
                encoding='utf-8') as stream:
            json.dump(manifest, stream, indent=2, sort_keys=True)
            stream.write('\n')
        return top_path


__all__ = ['CdlBundleBuilder']
