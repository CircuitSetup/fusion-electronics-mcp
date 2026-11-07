from pathlib import Path
import tempfile
import types
import unittest


class Event:
    def __init__(self): self.handlers = []
    def add(self, handler): self.handlers.append(handler); return True
    def remove(self, handler): self.handlers.remove(handler); return True
    def fire(self, args):
        for handler in self.handlers[:]: handler.notify(args)


class Script:
    def __init__(self, folder, name='test', identity='test-id'):
        self.folder = str(folder); self.name = name; self.id = identity
        self.version = '1'; self.isRunning = False; self.isAddIn = True; self.isInternal = False
        self.runs = 0; self.stops = 0
    def run(self): self.runs += 1; self.isRunning = True; return True
    def stop(self): self.stops += 1; self.isRunning = False; return True


class Scripts:
    def __init__(self, values): self.values = values
    @property
    def count(self): return len(self.values)
    def item(self, i): return self.values[i]
    def itemByPath(self, path): return next((s for s in self.values if Path(s.folder).resolve() == Path(path).resolve()), None)
    def addExisting(self, path):
        script = Script(path); self.values.append(script); return script


class ManagementTest(unittest.TestCase):
    def setUp(self):
        from fusion_mcp.addin.FusionElectronicsMCP import management as M
        self.M = M
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.owner = self.root / 'FusionElectronicsMCP'; self.owner.mkdir()
        folder = self.root / 'test'; folder.mkdir(); (folder / 'test.py').write_text('print("hello")')
        self.script = Script(folder)
        self.app = types.SimpleNamespace(scripts=Scripts([self.script]))

    def test_inspect_and_run_require_exact_reviewed_folder_hash(self):
        M = self.M
        info = M.inspect_script(self.app, self.script.folder)
        self.assertEqual(info['id'], self.script.id)
        self.assertEqual(len(info['source_sha256']), 64)
        with self.assertRaises(ValueError): M.control_script(self.app, self.owner, 'run', self.script.folder, 'wrong', info['source_sha256'])
        with self.assertRaises(ValueError): M.control_script(self.app, self.owner, 'run', self.script.folder, self.script.id, '0'*64)
        self.assertEqual(self.script.runs, 0)
        result = M.control_script(self.app, self.owner, 'run', self.script.folder, self.script.id, info['source_sha256'])
        self.assertTrue(result['running_after']); self.assertEqual(self.script.runs, 1)
        M.control_script(self.app, self.owner, 'run', self.script.folder, self.script.id, info['source_sha256'])
        self.assertEqual(self.script.runs, 1)
        M.control_script(self.app, self.owner, 'stop', self.script.folder, self.script.id)
        self.assertFalse(self.script.isRunning)
        (Path(self.script.folder) / 'dependency.py').write_text('changed')
        with self.assertRaises(ValueError): M.control_script(self.app, self.owner, 'run', self.script.folder, self.script.id, info['source_sha256'])

    def test_protect_bridge_and_helper_and_do_not_add_unknown_scripts(self):
        for folder, identity in ((self.owner, 'other'), (self.root / 'elsewhere', self.M.MAIN_ID),
                                 (self.owner.parent / 'FusionElectronicsMCPReload', 'other')):
            script = Script(folder, identity=identity); self.app.scripts.values.append(script)
            with self.assertRaises(ValueError): self.M.control_script(self.app, self.owner, 'stop', str(folder), identity)
            self.assertEqual(script.stops, 0)
        with self.assertRaises(ValueError): self.M.inspect_script(self.app, str(self.root / 'unknown'))
        self.assertEqual(len(self.app.scripts.values), 4)

    def test_list_filters_pages_and_rejects_invalid_limits(self):
        self.app.scripts.values.append(Script(self.root / 'another', name='Another'))
        result = self.M.list_scripts(self.app, limit=1)
        self.assertEqual(result['scripts'][0]['name'], 'Another')
        self.assertEqual(result['next_offset'], 1)
        self.assertEqual(self.M.list_scripts(self.app, query='TEST')['total'], 1)
        with self.assertRaises(ValueError): self.M.list_scripts(self.app, limit=101)
        with self.assertRaises(ValueError): self.M.list_scripts(self.app, offset=-1)

    def test_event_cursor_reset_overflow_and_detach(self):
        log = self.M.EventLog(capacity=3)
        app = types.SimpleNamespace(userInterface=types.SimpleNamespace(commandStarting=Event(), commandTerminated=Event()),
                                    documentActivated=Event(), documentSaved=Event())
        core = types.SimpleNamespace(DocumentEventHandler=object, ApplicationCommandEventHandler=object)
        log.start(app, core)
        app.documentActivated.fire(types.SimpleNamespace(document=types.SimpleNamespace(name='PCB', objectType='board')))
        app.userInterface.commandStarting.fire(types.SimpleNamespace(commandId='test-command'))
        data = log.read(limit=1); self.assertEqual(len(data['events']), 1)
        for _ in range(4): app.documentSaved.fire(types.SimpleNamespace(document=types.SimpleNamespace(name='PCB', objectType='board')))
        data = log.read(data['session_id'], data['next_after_seq'])
        self.assertTrue(data['dropped']); self.assertEqual(len(data['events']), 3)
        reset = log.read('old-session', 9999); self.assertTrue(reset['reset'])
        with self.assertRaises(ValueError): log.read(after_seq=1)
        self.assertIn('documentOpened', data['unavailable_events'])
        log.close(); self.assertEqual(app.documentActivated.handlers, [])
        self.assertEqual(app.userInterface.commandStarting.handlers, [])

    def test_reload_helper_checks_deadline_and_old_instance_before_stopping(self):
        M = self.M; target = Script(self.owner, identity=M.MAIN_ID); target.isRunning = True
        self.app.scripts.values.append(target)
        info = self.root / 'bridge.json'; M.U.atomic_json(info, {'pid': M.os.getpid(), 'token': 'old'})
        request = {'request_id': 'a'*32, 'expires': M.time.time()+30, 'pid': M.os.getpid(),
                   'old_token': 'old', 'target_folder': str(self.owner)}
        with self.assertRaises(ValueError): M.perform_reload(self.app, self.owner, info, request | {'expires': 0})
        with self.assertRaises(ValueError): M.perform_reload(self.app, self.owner, info, request | {'old_token': 'other'})
        with self.assertRaises(ValueError): M.perform_reload(self.app, self.owner, info, request | {'target_folder': str(self.root)})
        self.assertEqual(target.stops, 0)
        result = M.perform_reload(self.app, self.owner, info, request)
        self.assertEqual(result['status'], 'restarted'); self.assertEqual((target.stops, target.runs), (1, 1))

    def test_reload_stop_failure_never_runs_target(self):
        M = self.M; target = Script(self.owner, identity=M.MAIN_ID); target.isRunning = True
        target.stop = lambda: False; self.app.scripts.values.append(target)
        info = self.root / 'bridge.json'; M.U.atomic_json(info, {'pid': M.os.getpid(), 'token': 'old'})
        request = {'expires': M.time.time()+30, 'pid': M.os.getpid(), 'old_token':'old', 'target_folder':str(self.owner)}
        with self.assertRaises(ValueError): M.perform_reload(self.app, self.owner, info, request)
        self.assertEqual(target.runs, 0)


if __name__ == '__main__': unittest.main()
