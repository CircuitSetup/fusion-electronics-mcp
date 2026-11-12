"""Submit pinned staged EAGLE paths to the pending import's Windows picker once."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from . import data_dir
from .addin.FusionElectronicsMCP import eagle_import, ulp_support


def _session(info):
    version = info.get('addin_version', '')
    if (not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version)
            or tuple(map(int, version.split('.'))) < (0, 14, 21)
            or not info.get('event_session_id') or not isinstance(info.get('token'), str)
            or not info['token'] or type(info.get('pid')) is not int or info['pid'] <= 0):
        raise ValueError('A pinned add-in 0.14.21+ session is required')
    return dict(session_id=info['event_session_id'], pid=info['pid'],
                token_sha256=hashlib.sha256(info['token'].encode()).hexdigest())


def begin(bridge, args, *, journal_dir=None, expected_binding=None) -> dict:
    """Journal original source pins and event cursor before the idempotent native start."""
    keys = {'request_id', 'schematic_path', 'board_path', 'schematic_sha256',
            'board_sha256', 'project_id', 'folder', 'name'}
    if not isinstance(args, dict) or args.keys() != keys or not all(isinstance(v, str) for v in args.values()):
        raise ValueError('Use the exact explicit paired-import start arguments')
    args = dict(args)
    rid = eagle_import.request_id(args['request_id'])
    for key in ('schematic_sha256', 'board_sha256'):
        if not re.fullmatch('[a-fA-F0-9]{64}', args[key]):
            raise ValueError('Pin both original source SHA-256 hashes')
    with bridge.lock.hold('begin_eagle_pair_import'):
        info = bridge._info()
        binding = _session(info)
        if expected_binding is not None and binding != expected_binding:
            raise ValueError('Add-in session changed before paired import start')
        path = Path(journal_dir or data_dir('import-requests')) / (rid + '.json')
        pin_cursor = not path.exists()
        if path.exists():
            journal = json.loads(path.read_text(encoding='utf-8'))
            if journal.get('args') != args:
                raise ValueError('Request ID is already bound to different arguments or add-in session')
            if journal.get('binding') != binding:
                previous = journal.get('binding', {})
                ticket = bridge.call('eagle_pair_import_status', {'request_id': rid}, timeout=10)
                if (previous.get('session_id') == binding['session_id'] or previous.get('pid') != binding['pid']
                        or ticket.get('request_id') != rid or ticket.get('session_id') != previous.get('session_id')
                        or ticket.get('status') != 'not_started'
                        or ticket.get('save_requested') or 'destination' not in journal
                        or ticket.get('destination') != journal['destination']):
                    raise ValueError('Only the original known-not-started ticket can cross an add-in reload')
                staged = ticket.get('staged', {})
                if not all(isinstance(staged.get(kind), str) for kind in ('schematic', 'board')):
                    raise ValueError('Retained ticket is missing the original staged pair')
                _, signature = eagle_import.read_pair(dict(args, schematic_path=staged['schematic'],
                                                          board_path=staged['board']))
                if eagle_import.summary(signature) != ticket.get('expected'):
                    raise ValueError('Retained staged pair differs from the original source pins')
                pin_cursor = True
        else:
            journal = dict(args=args)
        if pin_cursor:
            events = bridge.call('events', dict(session_id=binding['session_id'], limit=1), timeout=10)
            if (events.get('session_id') != binding['session_id'] or events.get('reset')
                    or type(events.get('latest_seq')) is not int or events['latest_seq'] < 0
                    or _session(bridge._info()) != binding):
                raise ValueError('Cannot pin the current add-in event cursor')
            journal.update(binding=binding, after_seq=events['latest_seq'])
            ulp_support.atomic_json(path, journal)
        if _session(bridge._info()) != binding:
            raise ValueError('Add-in session changed before start')
        ticket = bridge.call('start_eagle_pair_import', args, timeout=30)
        destination = ticket.get('destination', {})
        if (ticket.get('request_id') != rid or ticket.get('session_id') != binding['session_id']
                or _session(bridge._info()) != binding
                or destination.get('project_id') != args['project_id'] or destination.get('name') != args['name']
                or not destination.get('folder_id')):
            raise ValueError('Start result differs from the pinned request or destination')
        if 'destination' in journal and journal['destination'] != destination:
            raise ValueError('Start destination changed for this request')
        journal['destination'] = copy.deepcopy(destination)
        ulp_support.atomic_json(path, journal)
        return ticket


def _active_import(bridge, journal):
    cursor = journal['after_seq']
    events = bridge.call('events', dict(session_id=journal['binding']['session_id'],
                                       after_seq=cursor, limit=200), timeout=10)
    if (events.get('session_id') != journal['binding']['session_id'] or events.get('reset')
            or events.get('dropped') or events.get('truncated')
            or events.get('next_after_seq') != events.get('latest_seq')
            or not {'commandStarting', 'commandTerminated'} <= set(events.get('supported_events', []))):
        raise ValueError('Import command event history is incomplete')
    rows = events.get('events', [])
    starts = [row for row in rows if row.get('event') == 'commandStarting'
              and row.get('command_id') == 'ImportSCHAndBRDCmd']
    if (len(starts) != 1 or starts[0].get('seq', 0) <= cursor
            or any(row.get('event') == 'commandTerminated' and row.get('command_id') == 'ImportSCHAndBRDCmd'
                   and row.get('seq', 0) > starts[0]['seq'] for row in rows)):
        raise ValueError('Expected exactly one still-active paired import since this request began')


def select_file(bridge, request_id, kind, expected_session_id, schematic_sha256,
                board_sha256, project_id, folder_id, name, *, native=None, receipt_dir=None) -> dict:
    eagle_import.request_id(request_id)
    if kind not in ('schematic', 'board'):
        raise ValueError('kind must be schematic or board')
    if not all(isinstance(v, str) and v for v in (expected_session_id, project_id, folder_id, name)):
        raise ValueError('Pin the session and explicit project/folder IDs and product name')
    with bridge.lock.hold('select_eagle_pair_import_file'):
        info = bridge._info()
        journal_path = Path(data_dir('import-requests')) / (request_id + '.json')
        if not journal_path.exists():
            raise ValueError('Untracked import ticket; selection requires its original server start journal')
        journal = json.loads(journal_path.read_text(encoding='utf-8'))
        hashes = dict(schematic_sha256=schematic_sha256, board_sha256=board_sha256)
        if (journal.get('binding') != _session(info) or journal.get('args', {}).get('request_id') != request_id
                or any(not isinstance(v, str) or v.lower() != journal.get('args', {}).get(k, '').lower()
                       for k, v in hashes.items()) or 'destination' not in journal):
            raise ValueError('Selection source pins or session differ from the original start journal')
        ticket = bridge.call('eagle_pair_import_status', {'request_id': request_id}, timeout=10)
        if (ticket.get('request_id') != request_id or ticket.get('session_id') != expected_session_id
                or info.get('event_session_id') != expected_session_id or not info.get('token')
                or not isinstance(info.get('pid'), int) or info['pid'] <= 0
                or ticket.get('status') != 'awaiting_file_selection'
                or ticket.get('dispatch_acknowledged') is not True or ticket.get('save_requested')):
            raise ValueError('Import is not an acknowledged pending picker in the pinned add-in session')
        destination = dict(project_id=project_id, folder_id=folder_id, name=name)
        if (ticket.get('destination') != journal['destination']
                or any(ticket.get('destination', {}).get(k) != v for k, v in destination.items())):
            raise ValueError('Import destination does not match')
        ticket_keys = ('request_id', 'session_id', 'status', 'dispatch_acknowledged',
                       'save_requested', 'destination', 'staged', 'expected')
        pinned_ticket = copy.deepcopy({key: ticket.get(key) for key in ticket_keys})
        staged = ticket.get('staged', {})
        paths = [Path(staged.get(k, '')) for k in ('schematic', 'board')]
        stage_root = (Path(tempfile.gettempdir()) / 'fusion-electronics-mcp').resolve()
        for path in paths:
            if (not path.is_absolute() or path.is_symlink() or path.parent.is_symlink()
                    or path.parent.resolve().parent != stage_root
                    or not path.parent.name.startswith('eagle-pair-')):
                raise ValueError('Ticket files must be in the private staged EAGLE pair directory')
        _, signature = eagle_import.read_pair(dict(hashes, schematic_path=str(paths[0]), board_path=str(paths[1])))
        if eagle_import.summary(signature) != ticket.get('expected'):
            raise ValueError('Staged pair does not match the ticket summary')
        binding = dict(request_id=request_id, session_id=expected_session_id, pid=info['pid'],
                       token_sha256=hashlib.sha256(info['token'].encode()).hexdigest(),
                       destination=destination, **{k: v.lower() for k, v in hashes.items()})
        directory = Path(receipt_dir or data_dir('import-pickers'))
        directory.mkdir(parents=True, exist_ok=True)
        receipt = directory / (request_id + '.json')
        record = json.loads(receipt.read_text(encoding='utf-8')) if receipt.exists() else dict(binding=binding)
        if record.get('binding') != binding:
            raise ValueError('Picker receipt belongs to another session, source or destination')
        if kind in record:
            raise ValueError('Selection attempt consumed; inspect native import state instead of retrying')
        if kind == 'board' and record.get('schematic', {}).get('status') != 'posted':
            raise ValueError('Board selection requires an acknowledged schematic post')
        native = native or WindowsPicker()
        identity = native.find(info['pid'], '.sch' if kind == 'schematic' else '.brd')

        def persist():
            # The bridge lock serializes writers; fsync precedes the irreversible message post.
            with receipt.open('w', encoding='utf-8') as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())

        if identity is None:
            if kind == 'board':
                record['schematic_picker_closed'] = True
                persist()
            return dict(status='pending', request_id=request_id, kind=kind)
        identity = list(identity)
        if (kind == 'board' and identity == record['schematic']['identity']
                and not record.get('schematic_picker_closed')):
            return dict(status='pending', request_id=request_id, kind=kind,
                        reason='Previous schematic picker has not visibly changed or closed')
        path = str(paths[0 if kind == 'schematic' else 1])
        _active_import(bridge, journal)
        native.set_path(identity, path)
        current_ticket = bridge.call('eagle_pair_import_status', {'request_id': request_id}, timeout=10)
        if {key: current_ticket.get(key) for key in ticket_keys} != pinned_ticket:
            raise ValueError('Import ticket changed before submission')
        if bridge._info() != info or native.find(info['pid'], '.sch' if kind == 'schematic' else '.brd') != identity:
            raise ValueError('Add-in or picker changed before submission')
        _active_import(bridge, journal)
        record[kind] = dict(identity=identity, path=path, status='submission_attempted')
        persist()
        native.post(identity)
        record[kind]['status'] = 'posted'
        persist()
        return dict(status='submitted', request_id=request_id, kind=kind,
                    next_action='Inspect native import status; selection is not import completion')


class WindowsPicker:
    """Small Win32 adapter; all control text crosses processes through WM_GETTEXT."""
    def __init__(self):
        if sys.platform != 'win32':
            raise ValueError('Native import file selection requires Windows')
        import ctypes as C
        from ctypes import wintypes as W
        self.C, self.W = C, W
        self.u = u = C.WinDLL('user32', use_last_error=True)
        self.callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        for name, args, result in [
            ('GetWindowThreadProcessId', [W.HWND, C.POINTER(W.DWORD)], W.DWORD),
            ('GetClassNameW', [W.HWND, W.LPWSTR, C.c_int], C.c_int),
            ('GetDlgCtrlID', [W.HWND], C.c_int),
            ('GetWindow', [W.HWND, W.UINT], W.HWND),
            ('IsWindowVisible', [W.HWND], W.BOOL),
            ('IsWindowEnabled', [W.HWND], W.BOOL),
            ('EnumWindows', [self.callback, W.LPARAM], W.BOOL),
            ('EnumChildWindows', [W.HWND, self.callback, W.LPARAM], W.BOOL),
            ('SendMessageTimeoutW', [W.HWND, W.UINT, W.WPARAM, W.LPARAM, W.UINT, W.UINT,
                                     C.POINTER(C.c_size_t)], W.LPARAM),
            ('PostMessageW', [W.HWND, W.UINT, W.WPARAM, W.LPARAM], W.BOOL),
        ]:
            function = getattr(u, name)
            function.argtypes, function.restype = args, result

    def message(self, hwnd, code, wparam=0, lparam=0):
        result = self.C.c_size_t()
        if not self.u.SendMessageTimeoutW(hwnd, code, wparam, lparam, 2, 2000, self.C.byref(result)):
            raise OSError('Native picker message timed out or failed')
        return result.value

    def text(self, hwnd):
        buf = self.C.create_unicode_buffer(4096)
        self.message(hwnd, 0xD, len(buf), self.C.cast(buf, self.C.c_void_p).value)
        return buf.value

    def cls(self, hwnd):
        buf = self.C.create_unicode_buffer(256)
        if not self.u.GetClassNameW(hwnd, buf, len(buf)):
            raise OSError('Cannot read native window class')
        return buf.value

    def pid(self, hwnd):
        value = self.W.DWORD()
        if not self.u.GetWindowThreadProcessId(hwnd, self.C.byref(value)):
            raise OSError('Cannot identify native window process')
        return value.value

    def enumerate(self, parent=None):
        handles = []
        callback = self.callback(lambda hwnd, _: handles.append(int(hwnd)) or True)
        if parent is None:
            if not self.u.EnumWindows(callback, 0):
                raise OSError('Cannot enumerate native windows')
        else:
            # EnumChildWindows does not define a meaningful return value.
            self.u.EnumChildWindows(parent, callback, 0)
        return handles

    def find(self, pid, extension):
        visible = [h for h in self.enumerate() if self.pid(h) == pid and self.u.IsWindowVisible(h)]
        if any(self.text(h) == 'Error' and self.u.GetWindow(h, 4) for h in visible):
            raise ValueError('Fusion has a visible owned Error window; inspect it before selecting a file')
        dialogs = [h for h in visible if self.pid(h) == pid
                   and self.u.IsWindowVisible(h) and self.cls(h) == '#32770' and self.text(h) == 'Open']
        if not dialogs:
            return None
        if len(dialogs) != 1:
            raise ValueError('Expected exactly one Fusion Open picker')
        hwnd = dialogs[0]
        owner = self.u.GetWindow(hwnd, 4)
        seen = {hwnd}
        if not owner or not self.u.IsWindowEnabled(hwnd):
            raise ValueError('Picker must be owned and enabled')
        while owner:
            if owner in seen or self.pid(owner) != pid:
                raise ValueError('Picker owner chain does not belong to Fusion')
            seen.add(owner)
            owner = self.u.GetWindow(owner, 4)
        controls = [h for h in self.enumerate(hwnd) if self.u.IsWindowVisible(h) and self.u.IsWindowEnabled(h)]

        def one(number, cls):
            matches = [h for h in controls if self.u.GetDlgCtrlID(h) == number and self.cls(h) == cls]
            if len(matches) != 1 or self.pid(matches[0]) != pid:
                raise ValueError('Expected exactly one enabled native picker control')
            return matches[0]

        edit, button, combo = one(1148, 'Edit'), one(1, 'Button'), one(1136, 'ComboBox')
        if self.text(button) != '&Open' or extension not in self.text(combo).lower():
            raise ValueError('Picker button or file type does not match the requested selection')
        return [hwnd, edit, button]

    def set_path(self, identity, path):
        buf = self.C.create_unicode_buffer(path)
        if not self.message(identity[1], 0xC, 0, self.C.cast(buf, self.C.c_void_p).value):
            raise OSError('Picker rejected staged filename')
        if self.text(identity[1]) != path:
            raise ValueError('Picker filename readback differs')

    def post(self, identity):
        if not self.u.PostMessageW(identity[2], 0xF5, 0, 0):
            raise OSError('Picker submission failed; attempt remains consumed')
