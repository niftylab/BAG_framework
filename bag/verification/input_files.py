"""Collect and stage explicit CDL inputs without copying unrelated source files."""

from pathlib import Path
import os
import re
import sys

INCLUDE = re.compile(r'^(\s*\.(include|inc|lib)\s+)("[^"]+"|\x27[^\x27]+\x27|\S+)(.*)$', re.I)


def includes(path):
    for line in Path(path).read_text(encoding='utf-8').splitlines(keepends=True):
        match = INCLUDE.match(line.rstrip('\r\n'))
        if match and not (match[2].lower() == 'lib' and
                          (not match[4].strip() or match[4].lstrip().startswith(('*',';','$','//')))):
            value = match[3].strip('"\x27')
            dependency = Path(os.path.expandvars(os.path.expanduser(value)))
            yield line, match, (Path(path).parent/dependency).resolve()


def cdl_files(source):
    """Return the referenced include closure, accepting cycles once per file."""
    visited = set()
    def visit(path):
        path = Path(path).resolve()
        if path in visited:
            return
        if not path.is_file():
            raise ValueError('source_netlist_path/include is not a file: {}'.format(path))
        visited.add(path)
        for _, _, dependency in includes(path):
            visit(dependency)
    visit(source)
    return sorted(visited)


def stage_cdl(source, destination, *, absolute_includes=True):
    """Stage a self-contained tree; rewrite paths for the extraction host cwd."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    files = cdl_files(source)
    common = Path(os.path.commonpath([str(p.parent) for p in files]))
    directory = destination.parent/'includes'
    locations = {p: directory/p.relative_to(common) for p in files}
    locations[source] = destination
    for path, target in locations.items():
        replacements = {}
        for line, match, dependency in includes(path):
            other = locations[dependency]
            value = other.as_posix() if absolute_includes else Path(os.path.relpath(other, target.parent)).as_posix()
            replacements[line] = match[1]+'"'+value+'"'+match[4]+('\n' if line.endswith('\n') else '')
        target.parent.mkdir(parents=True, exist_ok=True)
        content = ''.join(replacements.get(line, line) for line in path.read_text(encoding='utf-8').splitlines(keepends=True))
        target.write_text(content, encoding='utf-8')
    return destination


if __name__ == '__main__':
    stage_cdl(sys.argv[1], sys.argv[2])
