"""Fusion lifecycle helpers. All Fusion API calls must run on its main thread."""
from collections import deque
from datetime import datetime, timezone
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import threading
import time
import uuid

spec = importlib.util.spec_from_file_location('femcp_ulp_support', Path(__file__).with_name('ulp_support.py'))
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)
MAIN_ID = '7c1f5e2a-4b3d-4e8f-9a61-2d0c8b5e9f41'
HELPER_ID = '1497455a-4622-48fd-b225-48fbd1e6c830'
HELPER_NAME = 'FusionElectronicsMCPReload'
RELOAD_ATTR = '_fusion_electronics_mcp_reload_ticket'


def canonical(path): return os.path.normcase(os.path.realpath(path))


def request_id(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value): raise ValueError('Invalid reload request ID')
    return value


def script_at(app, folder):
    if not isinstance(folder, str) or not os.path.isabs(folder): raise ValueError('Use the exact absolute script folder')
    scripts = app.scripts
    matches = [s for i in range(scripts.count) if (s := scripts.item(i)) and canonical(s.folder) == canonical(folder)]
    if len(matches) != 1: raise ValueError('Expected exactly one script already known to Fusion at this folder')
    return matches[0]


def script_info(script):
    return {'id': script.id, 'name': script.name, 'folder': script.folder, 'version': script.version,
            'is_addin': script.isAddIn, 'running': script.isRunning, 'internal': script.isInternal}


def list_scripts(app, query='', limit=25, offset=0):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError('limit must be 1..100 and offset nonnegative')
    rows = [script_info(app.scripts.item(i)) for i in range(app.scripts.count)]
    rows = sorted((r for r in rows if query.casefold() in r['name'].casefold()), key=lambda r:(r['name'], r['folder']))
    return {'scripts': rows[offset:offset+limit], 'total': len(rows),
            'next_offset': offset+limit if offset+limit < len(rows) else None}


def source_hash(folder):
    root = Path(folder).resolve(); files = []; total = 0
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in ('__pycache__', '.git'))
        if any((Path(base) / d).is_symlink() or (Path(base) / d).is_junction() for d in dirs):
            raise ValueError('Review a source folder without linked dependencies')
        for name in sorted(names):
            if name.endswith(('.pyc', '.pyo')): continue
            path = Path(base) / name
            if path.is_symlink() or not path.resolve().is_relative_to(root): raise ValueError('Source escapes script folder')
            with path.open('rb') as f: raw = f.read(32 * 1024 * 1024 + 1)
            total += len(raw)
            if total > 32 * 1024 * 1024 or len(files) >= 2048: raise ValueError('Source folder exceeds 32 MiB or 2048 files')
            files.append({'path': path.relative_to(root).as_posix(), 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)})
    if not files: raise ValueError('No source files found')
    return U.digest(sorted(files, key=lambda row:row['path'])), files


def inspect_script(app, folder):
    result = script_info(script_at(app, folder))
    result['source_sha256'], result['files'] = source_hash(folder)
    result['coverage'] = 'Folder contents only; external imports/resources are not pinned. Trusted code only.'
    return result


def control_script(app, owner_folder, action, folder, expected_id, expected_source_sha256=None):
    script = script_at(app, folder)
    protected = (canonical(owner_folder), canonical(Path(owner_folder).parent / HELPER_NAME))
    if script.id in (MAIN_ID, HELPER_ID) or canonical(folder) in protected:
        raise ValueError('Use reload_mcp_addin for the MCP bridge; its reload helper cannot be controlled directly')
    if script.id != expected_id: raise ValueError('Script identity changed; inspect it again')
    if action not in ('run', 'stop'): raise ValueError('Action must be run or stop')
    if action == 'run' and source_hash(folder)[0] != expected_source_sha256:
        raise ValueError('Source differs from the explicitly reviewed folder hash')
    before = script.isRunning
    if (action == 'run' and not before) or (action == 'stop' and before):
        if not getattr(script, action)(): raise ValueError(f'Fusion refused to {action} the script')
    after = script.isRunning
    if action == 'stop' and after: raise ValueError('Fusion still reports the script running')
    return {'id': script.id, 'folder': script.folder, 'action': action, 'running_before': before,
            'running_after': after, 'status': 'requested' if action == 'run' and not before else 'unchanged' if before == after else 'stopped',
            'warning': 'Script effects are not sandboxed, verified or rolled back; a run request is not proof of completion.'}


class EventLog:
    def __init__(self, capacity=500):
        self.session_id = uuid.uuid4().hex; self.sequence = 0
        self.events = deque(maxlen=capacity); self.lock = threading.Lock()
        self.handlers = []; self.supported = []; self.unavailable = []

    def append(self, kind, **fields):
        with self.lock:
            self.sequence += 1
            self.events.append({'seq': self.sequence, 'time': datetime.now(timezone.utc).isoformat(), 'event': kind, **fields})

    def start(self, app, core):
        endpoints = [(app, name, 'DocumentEventHandler') for name in
                     ('documentActivated', 'documentOpened', 'documentClosing', 'documentClosed', 'documentSaved')]
        endpoints += [(app.userInterface, name, 'ApplicationCommandEventHandler') for name in ('commandStarting', 'commandTerminated')]
        for owner, name, base in endpoints:
            try:
                event = getattr(owner, name)
                def notify(handler, args, kind=name):
                    try:
                        fields = {}
                        if kind.startswith('command'):
                            fields['command_id'] = str(args.commandId)[:256]
                            if kind == 'commandTerminated': fields['reason'] = str(args.terminationReason)[:256]
                        elif kind != 'documentClosed':
                            doc = args.document
                            fields['document'] = {'name': str(doc.name)[:512], 'type': str(doc.objectType)[:128]}
                        self.append(kind, **fields)
                    except Exception as ex:
                        self.append(kind, detail_error=str(ex)[:256])
                handler = type('LifecycleHandler', (getattr(core, base),), {'notify': notify})()
                if event.add(handler) is False: raise ValueError('Event rejected handler')
                self.handlers.append((event, handler)); self.supported.append(name)
            except Exception:
                self.unavailable.append(name)

    def close(self):
        for event, handler in self.handlers:
            try: event.remove(handler)
            except Exception: pass
        self.handlers.clear()

    def read(self, session_id=None, after_seq=0, limit=100):
        if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('after_seq must be nonnegative and limit must be 1..200')
        if after_seq and not session_id: raise ValueError('A nonzero cursor requires its session_id')
        with self.lock:
            reset = session_id is not None and session_id != self.session_id
            if reset: after_seq = 0
            if after_seq > self.sequence: raise ValueError('Cursor is ahead of this event log')
            oldest = self.events[0]['seq'] if self.events else self.sequence+1
            rows = [dict(row) for row in self.events if row['seq'] > after_seq][:limit]
            return {'session_id': self.session_id, 'events': rows, 'reset': reset, 'dropped': after_seq < oldest-1,
                    'oldest_seq': oldest, 'latest_seq': self.sequence,
                    'next_after_seq': rows[-1]['seq'] if rows else after_seq,
                    'supported_events': self.supported[:], 'unavailable_events': self.unavailable[:]}


def queue_reload(app, owner_folder, info_path, rid):
    request_id(rid)
    helper_folder = Path(owner_folder).parent / HELPER_NAME
    if not (helper_folder / (HELPER_NAME + '.py')).is_file(): raise ValueError('Install the bundled reload helper first')
    helper = app.scripts.itemByPath(str(helper_folder)) or app.scripts.addExisting(str(helper_folder))
    if helper is None or canonical(helper.folder) != canonical(helper_folder) or helper.id != HELPER_ID:
        raise ValueError('Cannot resolve bundled reload helper identity')
    if helper.isRunning: raise ValueError('A reload helper is already running; inspect its status, do not retry')
    info = U.read_json(info_path)
    if info['pid'] != os.getpid(): raise ValueError('Bridge belongs to another Fusion process')
    pointer = Path(info_path).parent / 'reload-request.json'
    if pointer.exists():
        previous_id = request_id(U.read_json(pointer)['request_id'])
        previous = U.read_json(pointer.parent / 'reloads' / (previous_id + '.json'))
        if previous['status'] in ('queued', 'supervising') and previous['expires'] >= time.time():
            raise ValueError('A reload request is pending; inspect its status, do not retry')
    record = {'request_id': rid, 'status': 'queued', 'expires': time.time()+30, 'pid': os.getpid(),
              'old_token': info['token'], 'target_folder': str(Path(owner_folder).resolve())}
    status = Path(info_path).parent / 'reloads' / (rid + '.json')
    if status.exists(): raise ValueError('Reload request was already submitted')
    U.atomic_json(status, record)
    U.atomic_json(pointer, {'request_id':rid})
    if not helper.run():
        record['status'] = 'failed'; record['error'] = 'Fusion refused to start reload helper'; U.atomic_json(status, record)
        raise ValueError(record['error'])
    return {'request_id': rid, 'status': 'queued'}


def claim_reload(core, owner_folder):
    ticket = getattr(core, RELOAD_ATTR, None)
    if not ticket or canonical(ticket['target_folder']) != canonical(owner_folder): return None
    delattr(core, RELOAD_ATTR)
    if ticket['pid'] != os.getpid() or time.time() > ticket['expires']: return None
    ticket['claimed'] = True
    return ticket


def clear_reload(core, ticket):
    if ticket is not None and getattr(core, RELOAD_ATTR, None) is ticket:
        delattr(core, RELOAD_ATTR)


def perform_reload(app, owner_folder, info_path, request, core):
    if time.time() > request['expires']: raise ValueError('Reload request expired; nothing stopped')
    if request['pid'] != os.getpid() or canonical(request['target_folder']) != canonical(owner_folder):
        raise ValueError('Reload target does not match this helper')
    info = U.read_json(info_path)
    if info['pid'] != request['pid'] or info['token'] != request['old_token']:
        raise ValueError('Bridge instance changed; nothing stopped')
    target = script_at(app, str(owner_folder))
    if target.id != MAIN_ID or not target.isRunning: raise ValueError('Expected running MCP add-in not found')
    if not target.stop() or target.isRunning: raise ValueError('MCP add-in did not stop; restart was not attempted')
    ticket = {key: request[key] for key in ('request_id', 'pid', 'expires', 'target_folder')}
    ticket['claimed'] = False
    setattr(core, RELOAD_ATTR, ticket)
    try:
        if not target.run(): raise ValueError('MCP add-in stopped but Fusion refused to restart it')
    except Exception:
        clear_reload(core, ticket)
        raise
    return ticket
