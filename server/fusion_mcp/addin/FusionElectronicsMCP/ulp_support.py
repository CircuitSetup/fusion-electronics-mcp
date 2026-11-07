"""Shared registry and source checks; stdlib only so the add-in can load this file."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def registry_dir():
    return Path(os.environ.get('APPDATA', os.path.expanduser('~'))) / 'fusion-electronics-mcp' / 'registered-ulps'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def valid_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name):
        raise ValueError('ULP name must be 1..64 lowercase letters, digits, underscores or hyphens, starting with a letter')
    return name


def read_json(path, limit=2 * 1024 * 1024):
    with Path(path).open('rb') as f:
        raw = f.read(limit + 1)
    if len(raw) > limit: raise ValueError('JSON exceeds size limit')
    return json.loads(raw)


def atomic_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.ulp-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, indent=2, sort_keys=True, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


_TOKENS = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_][A-Za-z_0-9]*|\d+|[^\s]')
_FORBIDDEN = {'system', 'netget', 'netpost', 'fileread', 'fileglob', 'cfgget', 'cfgset',
              'setgroup', 'clrgroup', 'setvariant'}
_OUTPUT = ['output', '(', 'MCP_OUTPUT', ',', '"wt"', ')']


def check_source(raw):
    """Conservative checks for the proposal dialect, not a sandbox for untrusted code."""
    if len(raw) > 512 * 1024: raise ValueError('ULP source exceeds 512 KiB')
    source = raw.decode('utf-8-sig')
    tokens = [m for m in _TOKENS.finditer(source) if not m[0].startswith(('//', '/*'))]
    words = [m[0] for m in tokens]
    output_span = None
    for i, word in enumerate(words):
        if word in _FORBIDDEN or word.lower().startswith('dlg'):
            raise ValueError(f'{word} is not supported in read/proposal ULPs')
        if word == '#' and words[i+1:i+2] not in (['usage'], ['require']):
            raise ValueError('Includes and macros are not supported; register a self-contained ULP')
        if word == '\\': raise ValueError('Line continuations outside strings are not supported')
        if word == 'exit' and words[i+1:i+5] not in (['(', '0', ')', ';'], ['(', '1', ')', ';'],
                                                      ['(', 'EXIT_SUCCESS', ')', ';'], ['(', 'EXIT_FAILURE', ')', ';']):
            raise ValueError('ULPs must return commands as JSON, not execute them through exit()')
        if word == 'output':
            if words[i:i+6] != _OUTPUT or output_span is not None:
                raise ValueError('Use exactly one output(MCP_OUTPUT, "wt") block')
            output_span = (tokens[i+2].start(), tokens[i+2].end())
        if word == 'MCP_OUTPUT' and (i < 2 or words[i-2:i+4] != _OUTPUT):
            raise ValueError('MCP_OUTPUT may only occur in the output block')
    if output_span is None: raise ValueError('Use output(MCP_OUTPUT, "wt") for the JSON result')
    return source, output_span


def render_source(raw, output):
    source, (start, end) = check_source(raw)
    path = str(output).replace('\\', '/')
    return source[:start] + json.dumps(path, ensure_ascii=False) + source[end:]


def validate_entry(entry):
    if not isinstance(entry, dict) or set(entry) != {'schema', 'name', 'mode', 'editors', 'argument_count', 'sha256', 'source_path'}:
        raise ValueError('Invalid registry entry')
    valid_name(entry['name'])
    if entry['schema'] != 1 or entry['mode'] not in ('read', 'edit', 'legacy'):
        raise ValueError('Mode must be read, edit or legacy')
    if (not isinstance(entry['editors'], list) or not entry['editors']
            or any(x not in (('board', 'schematic', 'library') if entry['mode'] == 'legacy' else ('board', 'schematic')) for x in entry['editors'])):
        raise ValueError('Editors must be board/schematic (or library for legacy ULPs)')
    if type(entry['argument_count']) is not int or not 0 <= entry['argument_count'] <= 16:
        raise ValueError('argument_count must be 0..16')
    if not isinstance(entry['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', entry['sha256']):
        raise ValueError('Invalid source SHA-256')
    if not isinstance(entry['source_path'], str): raise ValueError('Invalid source path')
    return entry


def register(directory, name, source_path, mode, editors, argument_count, reviewed_sha256):
    directory = Path(directory); path = Path(source_path).resolve()
    if path.suffix.lower() != '.ulp': raise ValueError('Source must be a .ulp file')
    with path.open('rb') as f: raw = f.read(512 * 1024 + 1)
    sha = hashlib.sha256(raw).hexdigest()
    if sha != reviewed_sha256: raise ValueError('Source differs from the reviewed SHA-256')
    entry = validate_entry({'schema': 1, 'name': name, 'mode': mode, 'editors': editors,
                            'argument_count': argument_count, 'sha256': sha, 'source_path': str(path)})
    check_registered_source(raw, mode)
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / (sha + '.ulp')
    if source.exists():
        if source.read_bytes() != raw: raise ValueError('Registered source copy was altered')
    else:
        with source.open('xb') as f: f.write(raw)
    atomic_json(directory / (name + '.json'), entry)
    return entry


def load(directory, name):
    directory = Path(directory)
    entry = validate_entry(read_json(directory / (valid_name(name) + '.json')))
    if entry['name'] != name: raise ValueError('Registry name mismatch')
    with (directory / (entry['sha256'] + '.ulp')).open('rb') as f: raw = f.read(512 * 1024 + 1)
    if hashlib.sha256(raw).hexdigest() != entry['sha256']: raise ValueError('Registered source copy was altered')
    check_registered_source(raw, entry['mode'])
    return entry, raw


def check_registered_source(raw, mode):
    if mode != 'legacy': return check_source(raw)
    if len(raw) > 512 * 1024: raise ValueError('ULP source exceeds 512 KiB')
    tokens = [m[0] for m in _TOKENS.finditer(raw.decode('utf-8-sig')) if not m[0].startswith(('//', '/*'))]
    if any(tokens[i:i+2] == ['#', 'include'] for i in range(len(tokens))):
        raise ValueError('Legacy includes require a reviewed self-contained copy so dependencies cannot change after approval')


def arguments(entry, values):
    if not isinstance(values, list) or len(values) != entry['argument_count']:
        raise ValueError(f"Expected {entry['argument_count']} arguments")
    if any(not isinstance(x, str) or not x or len(x) > 1024 or any(c in x for c in "'\";\\\r\n")
           or any(ord(c) < 32 for c in x) for x in values):
        raise ValueError('Arguments must be nonempty strings without command quoting/control characters')
    return values
