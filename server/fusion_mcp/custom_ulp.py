"""Registered ULP reads and immutable, verified command proposals."""
import html
import hashlib
import json
import os
from pathlib import Path
import re
from decimal import Decimal, InvalidOperation
import xml.etree.ElementTree as ET
import uuid

from fusion_offline import edit_plan as P
from . import data_dir
from .addin.FusionElectronicsMCP import ulp_support as U


def plan_dir(): return Path(data_dir('ulp-plans'))


def registered(directory=None):
    directory = Path(directory) if directory else U.registry_dir()
    return [U.load(directory, p.stem)[0] for p in sorted(directory.glob('*.json'))]


def discover(query='', limit=25, offset=0, root=None):
    """List locally cached Fusion ULP Manager sources, without running or approving them."""
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError('limit must be 1..100 and offset nonnegative')
    root = Path(root) if root else Path(os.environ.get('LOCALAPPDATA', '')) / 'Autodesk/Autodesk Fusion 360/Electron'
    paths = sorted(p for p in root.glob('*/AssetCache/ulpfilesCache/*.ulp')
                   if all(word in p.name.casefold() for word in query.casefold().split()))
    rows = []
    for path in paths[offset:offset + limit]:
        raw = path.read_bytes()
        rows.append({'name': path.stem, 'source_path': str(path.resolve()), 'sha256': hashlib.sha256(raw).hexdigest(),
                     'bytes': len(raw)})
    return {'results': rows, 'total': len(paths), 'next_offset': offset + limit if offset + limit < len(paths) else None,
            'coverage': 'Local Fusion ULP Manager cache only; uncached cloud ULPs must first be downloaded in Fusion.'}


def _identity(context, allow_library=False):
    doc = context.get('active_document') or {}
    if context.get('active_editor') not in (('board', 'schematic', 'library') if allow_library else ('board', 'schematic')) or not doc.get('document_id'):
        raise ValueError('Activate a board or schematic in the updated add-in')
    return {key: doc.get(key) for key in ('document_id', 'project_id', 'data_file_id', 'version', 'name', 'kind')}


def snapshot(session, allow_library=False):
    if session.bridge.transport() != 'addin': raise ValueError('Registered ULPs require the add-in for stable document identity')
    context = session.context(); doc = _identity(context, allow_library)
    if doc['kind'] == 'library':
        xml = session.export('library').decode('utf-8')
        if _identity(session.context(), True) != doc: raise ValueError('Active document changed during export')
        return {'document': doc, 'board': None, 'schematic': None, 'library': xml}
    snap = session.snapshot()
    if _identity(session.context()) != doc: raise ValueError('Active document changed during export')
    return {'document': doc, 'board': snap.board_xml.decode('utf-8') if snap.board_xml else None,
            'schematic': snap.sch_xml.decode('utf-8') if snap.sch_xml else None}


_NUMERIC = {'x', 'y', 'x1', 'x2', 'y1', 'y2', 'dx', 'dy', 'width', 'size', 'drill', 'diameter', 'curve', 'ratio', 'distance'}


def xml_hash(xml):
    if xml is None: return None
    root = ET.fromstring(xml)
    for node in root.iter():
        for key, value in list(node.attrib.items()):
            if key in _NUMERIC:
                try:
                    number = Decimal(value)
                    if not number.is_finite(): raise ValueError('Nonfinite XML coordinate')
                    node.set(key, str(number.normalize()) if number else '0')
                except InvalidOperation: pass  # units such as "6mil" remain exact
    return P.xml_hash(ET.tostring(root, encoding='unicode'))


def fingerprint(snap):
    return {'document': snap['document'], **{k: xml_hash(snap.get(k)) for k in ('board', 'schematic', 'library')}}


def _run(session, name, arguments, expected_document_id, mode, directory):
    entry, _ = U.load(directory, name)
    if entry['mode'] != mode: raise ValueError(f"Use the {entry['mode']} workflow for this registered ULP")
    U.arguments(entry, arguments)
    before = snapshot(session)
    if before['document']['document_id'] != expected_document_id: raise ValueError('Wrong document')
    if before['document']['kind'] not in entry['editors']: raise ValueError('ULP does not support the active editor')
    result = session.bridge.call('run_registered_ulp', {'name': name, 'arguments': arguments,
        'registration_sha256': U.digest(entry), 'expected_document_id': expected_document_id}, timeout=120)
    after = snapshot(session)
    if fingerprint(before) != fingerprint(after):
        raise ValueError('ULP changed the document during read/preview; stop and inspect the design. No automatic undo attempted.')
    if U.load(directory, name)[0] != entry: raise ValueError('Registration changed during execution')
    return entry, before, result['data']


def read(session, name, arguments, expected_document_id, directory=None):
    with session.bridge.lock.hold('read registered ULP'):
        entry, before, data = _run(session, name, arguments, expected_document_id, 'read', directory or U.registry_dir())
    return {'name': name, 'sha256': entry['sha256'], 'document': before['document'], 'data': data['data']}


def validate_commands(commands):
    if not isinstance(commands, str) or not commands.strip() or len(commands) > 100000:
        raise ValueError('Expected a nonempty command string, at most 100000 characters')
    allowed = {'MOVE', 'ROTATE', 'VALUE', 'NAME', 'ATTRIBUTE', 'CHANGE', 'MIRROR', 'SMASH'}
    rows, buf, quoted = [], [], False
    for c in commands:
        if c in '\\"' or (ord(c) < 32 and c not in '\r\n\t'):
            raise ValueError('Unsupported command quoting/control character')
        if c == "'": quoted = not quoted
        if c in '\r\n;' and not quoted:
            if ''.join(buf).strip(): rows.append(''.join(buf).strip())
            buf = []
        else:
            if c in '\r\n;' and quoted: raise ValueError('Command literals cannot contain line breaks or semicolons')
            buf.append(c)
    if quoted: raise ValueError('Unclosed command quote')
    if ''.join(buf).strip(): rows.append(''.join(buf).strip())
    if not rows or len(rows) > 500: raise ValueError('Expected 1..500 commands')
    for row in rows:
        if row.split()[0] not in allowed: raise ValueError('Unsupported editing command: ' + row.split()[0])
    # Preserve whitespace inside quoted values; Session.with_grid collapses it.
    return 'GRID MM 0.0001; ' + ' '.join(row + ';' for row in rows) + ' GRID LAST;'


def expected_snapshot(before, changes):
    if not isinstance(changes, list) or not 1 <= len(changes) <= 500:
        raise ValueError('Editing ULP must declare 1..500 attribute changes')
    roots = {key: ET.fromstring(before[key]) if before[key] else None for key in ('board', 'schematic')}
    seen, review = set(), []
    for change in changes:
        if not isinstance(change, dict) or set(change) != {'document', 'xpath', 'attributes'}:
            raise ValueError('Each change requires document, xpath and attributes')
        kind, xpath, attributes = change['document'], change['xpath'], change['attributes']
        if not isinstance(kind, str) or kind not in roots or roots[kind] is None: raise ValueError('Missing target document')
        if not isinstance(xpath, str) or not xpath.startswith(f'./drawing/{kind}/') or '..' in xpath[2:]:
            raise ValueError('Use an explicit XPath inside the target board/schematic')
        try: matches = roots[kind].findall(xpath)
        except (SyntaxError, KeyError, TypeError): raise ValueError('Invalid XPath') from None
        if len(matches) != 1: raise ValueError('XPath must match exactly one existing object')
        node = matches[0]
        if node.tag not in {'element', 'instance', 'part', 'attribute', 'wire', 'via', 'text', 'label', 'net', 'signal'}:
            raise ValueError('Unsupported attribute-edit target')
        if not isinstance(attributes, dict) or not attributes: raise ValueError('Expected nonempty attributes')
        old = {}
        for key, value in attributes.items():
            if not isinstance(key, str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]*', key) or not isinstance(value, (str, type(None))):
                raise ValueError('Attribute values must be strings or null for removal')
            if (kind, id(node), key) in seen: raise ValueError('Duplicate attribute change')
            seen.add((kind, id(node), key)); old[key] = node.get(key)
            if key in _NUMERIC and value is not None:
                try:
                    if not Decimal(value).is_finite(): raise ValueError('Nonfinite attribute')
                except InvalidOperation: raise ValueError('Numeric attributes require finite numbers') from None
            if value is None: node.attrib.pop(key, None)
            else: node.set(key, value)
        review.append({**change, 'before': old})
    expected = {'document': before['document'], **{k: ET.tostring(v, encoding='unicode') if v is not None else None for k, v in roots.items()}}
    if fingerprint(before) == fingerprint(expected): raise ValueError('Proposal declares no design change')
    return expected, review


def preview(session, name, arguments, expected_document_id, directory=None, plans=None):
    directory = directory or U.registry_dir(); plans = Path(plans) if plans else plan_dir()
    with session.bridge.lock.hold('preview registered ULP'):
        entry, before, data = _run(session, name, arguments, expected_document_id, 'edit', directory)
        commands = validate_commands(data['commands'])
        expected, changes = expected_snapshot(before, data['changes'])
        plan = {'schema': 1, 'registration': entry, 'arguments': arguments, 'before': before,
                'expected': expected, 'commands': commands, 'changes': changes}
        sha = U.digest(plan); plan['sha256'] = sha
        if len(json.dumps(plan, indent=2, sort_keys=True).encode('utf-8')) > 32 * 1024 * 1024:
            raise ValueError('Plan exceeds 32 MiB; reduce the design/proposal size')
        plans.mkdir(parents=True, exist_ok=True)
        path = plans / (sha + '.json'); view = plans / (sha + '.html')
        if path.exists() and U.read_json(path, 32 * 1024 * 1024) != plan: raise ValueError('Stored plan was altered')
        if not path.exists(): U.atomic_json(path, plan)
        view.write_text('<!doctype html><meta charset="utf-8"><title>ULP command preview</title>'
                        '<h1>ULP command preview</h1><p>' + html.escape(name + ' — ' + before['document']['name'])
                        + '</p><p>SHA-256: ' + sha + '</p><h2>Commands</h2><pre>' + html.escape(commands)
                        + '</pre><h2>Expected changes (all other XML must remain unchanged)</h2><pre>'
                        + html.escape(json.dumps(changes, indent=2)) + '</pre>', encoding='utf-8')
    return {'status': 'preview', 'sha256': sha, 'plan_path': str(path), 'preview_path': str(view),
            'commands': commands, 'changes': changes, 'instruction': 'Review the exact commands, changes and hash before authorizing apply.'}


def apply(session, plan_path, expected_sha256, directory=None, plans=None):
    directory = directory or U.registry_dir(); plans = (Path(plans) if plans else plan_dir()).resolve()
    path = Path(plan_path).resolve()
    if not isinstance(expected_sha256, str) or not re.fullmatch('[a-f0-9]{64}', expected_sha256): raise ValueError('Invalid plan hash')
    if path.parent != plans or path.name != expected_sha256 + '.json': raise ValueError('Plan path/hash is outside the managed directory')
    plan = U.read_json(path, 32 * 1024 * 1024)
    if plan.get('sha256') != expected_sha256 or U.digest({k:v for k,v in plan.items() if k != 'sha256'}) != expected_sha256:
        raise ValueError('Plan was altered')
    attempt = plans / (expected_sha256 + '.attempt.json')
    with session.bridge.lock.hold('apply ULP plan'):
        if attempt.exists(): raise ValueError('This plan was already attempted; inspect the design and create a new preview')
        entry, _ = U.load(directory, plan['registration']['name'])
        if entry != plan['registration']: raise ValueError('Registration changed; create a new preview')
        if entry['mode'] != 'edit' or plan.get('schema') != 1: raise ValueError('Invalid edit plan')
        U.arguments(entry, plan['arguments'])
        commands = plan['commands']
        prefix, suffix = 'GRID MM 0.0001; ', ' GRID LAST;'
        if (not isinstance(commands, str) or not commands.startswith(prefix) or not commands.endswith(suffix)
                or validate_commands(commands[len(prefix):-len(suffix)]) != commands):
            raise ValueError('Invalid plan commands')
        before = snapshot(session)
        if fingerprint(before) != fingerprint(plan['before']): raise ValueError('Stale plan or wrong document')
        expected, _ = expected_snapshot(before, [{k:v for k,v in c.items() if k != 'before'} for c in plan['changes']])
        if fingerprint(expected) != fingerprint(plan['expected']): raise ValueError('Invalid expected result')
        with attempt.open('x', encoding='utf-8') as f: json.dump({'status': 'started'}, f)
        after = None
        try:
            result = session.bridge.call('run', {'commands': plan['commands'], 'editor': before['document']['kind'],
                'expected_document_id': before['document']['document_id']}, timeout=120)
            if any(not d.get('expected') for d in (result or {}).get('dialogs', [])): raise ValueError('Unexpected Fusion dialog')
            after = snapshot(session)
            if fingerprint(after) != fingerprint(expected): raise ValueError('Read-back differs from the reviewed changes')
            outcome = {'status': 'applied', 'sha256': expected_sha256, 'saved': False}
        except Exception as ex:
            # Never undo/retry a timeout or an unproven multi-command undo boundary.
            outcome = {'status': 'state_unknown', 'sha256': expected_sha256, 'error': str(ex),
                       'recovery': 'Stop editing; inspect/re-export the target design before recovery. This plan cannot be retried.'}
        U.atomic_json(attempt, {**outcome, 'after': after})
        outcome['report_path'] = str(attempt)
    return outcome


def xml_diff(before, after):
    def nodes(xml):
        result = {}
        if xml is None: return result
        def walk(node, path):
            result[path] = (node.attrib, (node.text or '').strip())
            counts = {}
            for child in node:
                counts[child.tag] = counts.get(child.tag, 0) + 1
                walk(child, path + '/' + child.tag + '[' + str(counts[child.tag]) + ']')
        walk(ET.fromstring(xml), '/eagle')
        return result
    old, new = nodes(before), nodes(after)
    added, removed = sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys())
    changes = [{'path': key, 'before': old[key][0], 'after': new[key][0]} for key in sorted(old.keys() & new.keys()) if old[key][0] != new[key][0]]
    return {'changed': xml_hash(before) != xml_hash(after), 'added_nodes': len(added), 'removed_nodes': len(removed),
            'attributes_changed': len(changes), 'text_changes': sum(old[k][1] != new[k][1] for k in old.keys() & new.keys()),
            'attribute_samples': changes[:100], 'added_samples': added[:100], 'removed_samples': removed[:100]}


def legacy(session, name, arguments, expected_document_id, expected_source_sha256, directory=None, runs=None):
    directory = directory or U.registry_dir(); runs = Path(runs) if runs else Path(data_dir('ulp-runs'))
    with session.bridge.lock.hold('run legacy ULP'):
        entry, _ = U.load(directory, name)
        if entry['mode'] != 'legacy' or entry['sha256'] != expected_source_sha256:
            raise ValueError('Legacy execution requires its explicitly reviewed source SHA-256')
        U.arguments(entry, arguments)
        before = snapshot(session, allow_library=True)
        if before['document']['document_id'] != expected_document_id: raise ValueError('Wrong document')
        if before['document']['kind'] not in entry['editors']: raise ValueError('Wrong editor')
        path = runs / (uuid.uuid4().hex + '.json')
        report = {'status': 'started', 'registration': entry, 'arguments': arguments, 'before': before}
        U.atomic_json(path, report)
        try:
            result = session.bridge.call('run_registered_ulp', {'name': name, 'arguments': arguments,
                'registration_sha256': U.digest(entry), 'expected_document_id': expected_document_id,
                'legacy_source_sha256': expected_source_sha256}, timeout=120)
            after = snapshot(session, allow_library=True)
            report['after'] = after
            report['diff'] = {kind: xml_diff(before.get(kind), after.get(kind)) for kind in ('board', 'schematic', 'library')}
            if before['document'] != after['document']: raise ValueError('Document identity or saved version changed')
            report.update(status='completed_unverified', bridge_result=result)
        except Exception as ex:
            report.update(status='state_unknown', error=str(ex))
        report['warning'] = 'No guaranteed preview, completion or rollback. External file/network/process effects are not captured. Inspect Fusion before another run.'
        U.atomic_json(path, report)
    return {k:v for k,v in report.items() if k not in ('before', 'after', 'registration', 'arguments')} | {'report_path': str(path)}
