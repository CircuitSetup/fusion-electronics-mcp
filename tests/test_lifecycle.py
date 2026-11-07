import asyncio
import contextlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from fusion_mcp import lifecycle as L
from fusion_mcp.addin.FusionElectronicsMCP import management as M
from fusion_mcp.bridge import BridgeOpError, BridgeUnavailable
from test_management import Event, Script, Scripts
from test_server import load_addin


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name); self.info = self.root / 'bridge.json'
        self.old = {'token': 'SECRET-OLD', 'pid': 123, 'addin_version': '0.14.14', 'event_session_id': 'old'}
        M.U.atomic_json(self.info, self.old)
        self.calls = []
        self.bridge = types.SimpleNamespace(info_path=str(self.info), transport=lambda: 'addin',
                                             lock=types.SimpleNamespace(hold=lambda _: contextlib.nullcontext()), call=self.call)
        self.mode = 'success'

    def call(self, op, args=None, timeout=None):
        self.calls.append(op)
        if op == 'reload_addin':
            rid = args['request_id']
            if self.mode == 'unsupported': raise BridgeOpError('unknown_op', 'old add-in')
            if self.mode == 'missing': raise BridgeUnavailable('lost reply')
            M.U.atomic_json(self.root / 'reloads' / (rid + '.json'),
                            dict(request_id=rid, status='failed' if self.mode == 'failed' else 'restarted',
                                 pid=123, old_token=self.old['token']))
            if self.mode != 'unchanged':
                M.U.atomic_json(self.info, self.old | {'token': 'SECRET-NEW', 'event_session_id': 'new'})
            if self.mode == 'lost_reply': raise BridgeUnavailable('lost reply')
            return {'request_id': rid}
        if op == 'ping':
            return {'pid': 999 if self.mode == 'wrong_pid' else 123, 'addin_version': '0.14.14'}
        raise AssertionError(op)

    def test_reload_verifies_new_instance_even_when_submission_reply_is_lost(self):
        for mode in ('success', 'lost_reply'):
            self.mode = mode; self.calls.clear()
            result = L.reload_addin(self.bridge, timeout=0)
            self.assertEqual(result['status'], 'verified'); self.assertEqual(result['event_session_id'], 'new')
            self.assertEqual(self.calls, ['reload_addin', 'ping'])
            self.assertNotIn('SECRET', json.dumps(result))
            self.assertEqual(L.reload_status(self.bridge, result['request_id'])['status'], 'verified')

    def test_uncertain_and_failed_runs_never_resubmit_or_claim_success(self):
        for mode in ('unchanged', 'wrong_pid', 'failed', 'missing'):
            M.U.atomic_json(self.info, self.old)
            self.mode = mode; self.calls.clear()
            result = L.reload_addin(self.bridge, timeout=0)
            self.assertEqual(result['status'], 'failed' if mode == 'failed' else 'unknown')
            self.assertEqual(self.calls.count('reload_addin'), 1)
            self.assertNotIn('SECRET', json.dumps(result))
        with self.assertRaises(ValueError): L.reload_status(self.bridge, '../bridge')
        self.mode = 'unsupported'
        with self.assertRaises(BridgeOpError): L.reload_addin(self.bridge)
        self.bridge.transport = lambda: 'builtin'; self.calls.clear()
        with self.assertRaises(ValueError): L.reload_addin(self.bridge)
        self.assertEqual(self.calls, [])

    def test_helper_rejects_pending_duplicate_and_expires_before_stop(self):
        owner = self.root / 'FusionElectronicsMCP'; owner.mkdir()
        folder = self.root / M.HELPER_NAME; folder.mkdir(); (folder / (M.HELPER_NAME + '.py')).touch()
        helper = Script(folder, identity=M.HELPER_ID)
        target = Script(owner, identity=M.MAIN_ID); target.isRunning = True
        app = types.SimpleNamespace(scripts=Scripts([helper, target]))
        M.U.atomic_json(self.info, self.old | {'pid': M.os.getpid()})
        M.queue_reload(app, owner, self.info, 'a'*32)
        helper.isRunning = False  # Native run can be queued before the script reports running.
        with self.assertRaises(ValueError): M.queue_reload(app, owner, self.info, 'b'*32)
        self.assertEqual(helper.runs, 1)
        request = L.read_json(self.root / 'reloads' / ('a'*32 + '.json'))
        with patch.object(M.time, 'time', return_value=request['expires'] + 1), self.assertRaises(ValueError):
            M.perform_reload(app, owner, self.info, request)
        self.assertEqual(target.stops, 0)

    def test_helper_queues_main_thread_event_cleans_up_and_records_failure(self):
        addin = load_addin()
        path = Path(addin.__file__).parent.parent / M.HELPER_NAME / (M.HELPER_NAME + '.py')
        spec = importlib.util.spec_from_file_location('reload_test', path)
        helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
        event = Event()
        app = types.SimpleNamespace(registerCustomEvent=lambda _: event, unregisterCustomEvent=MagicMock(), fireCustomEvent=MagicMock())
        helper.adsk.core.Application = types.SimpleNamespace(get=lambda: app)
        helper.adsk.autoTerminate = MagicMock(); helper.adsk.terminate = MagicMock()
        helper.INFO = self.info
        status = self.root / 'reloads' / ('c'*32 + '.json')
        M.U.atomic_json(self.root / 'reload-request.json', {'request_id': 'c'*32})
        M.U.atomic_json(status, {'status': 'queued'})
        with patch.object(helper.threading, 'Timer') as timer, patch.object(helper.M, 'perform_reload', side_effect=ValueError('failed stop')) as perform:
            helper.run(None)
            self.assertFalse(perform.called); timer.return_value.start.assert_called_once()
            timer.call_args.args[1]()
            app.fireCustomEvent.assert_called_once_with(helper.EVENT)
            event.fire(None)
            self.assertEqual(L.read_json(status)['status'], 'failed')
            self.assertEqual(event.handlers, []); helper.adsk.terminate.assert_called_once()
            helper.stop(None)  # Native termination may call stop a second time.
            app.unregisterCustomEvent.assert_called_once()

    def test_events_bypass_main_thread_but_still_require_authentication(self):
        addin = load_addin(); addin._token = 'secret'; addin._events = M.EventLog()
        addin._events.append('test')
        addin._app = types.SimpleNamespace(fireCustomEvent=MagicMock(side_effect=AssertionError('UI queue must not be touched')))
        for token in ('wrong', 'secret'):
            conn = MagicMock(); conn.recv.return_value = (json.dumps({'token': token, 'op': 'events', 'args': {}})+'\n').encode()
            addin._serve_conn(conn)
            response = json.loads(conn.sendall.call_args.args[0])
            self.assertEqual(response['ok'], token == 'secret')
            if token == 'secret': self.assertEqual(response['result']['events'][0]['event'], 'test')

    def test_addin_stop_retains_legacy_output(self):
        addin = load_addin(); addin.WORK_DIR = str(self.root / 'work')
        artifact = Path(addin.WORK_DIR) / 'legacy-output.txt'; artifact.parent.mkdir(); artifact.write_text('keep')
        addin._server = MagicMock(); addin._app = MagicMock(); addin.INFO_PATH = str(self.root / 'absent')
        addin.stop(None)
        self.assertEqual(artifact.read_text(), 'keep')

    def test_management_annotations_and_transport_guards(self):
        from fusion_mcp import bridge as B
        from fusion_mcp.server import mcp
        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        for name in ('list_fusion_scripts', 'inspect_fusion_script', 'get_fusion_events', 'get_mcp_reload_status'):
            self.assertTrue(tools[name].annotations.read_only_hint)
        for name in ('run_fusion_script', 'stop_fusion_script', 'reload_mcp_addin'):
            self.assertFalse(tools[name].annotations.read_only_hint)
            self.assertFalse(tools[name].annotations.idempotent_hint)
        self.assertTrue(tools['run_fusion_script'].annotations.open_world_hint)
        self.assertTrue(tools['stop_fusion_script'].annotations.open_world_hint)
        bridge = B.Bridge()
        conn = MagicMock(); conn.__enter__.return_value = conn
        conn.recv.return_value = b'{"ok":true,"result":{}}\n'
        for op in B.MANAGEMENT_OPS:
            with patch.object(bridge, 'transport', return_value='builtin'), self.assertRaises(BridgeOpError): bridge._call(op)
            with patch.object(bridge, 'transport', return_value='addin'), \
                 patch.object(bridge, '_info', return_value={'protocol': 1, 'pid': 123, 'port': 1, 'token': 'test'}), \
                 patch.object(B.socket, 'create_connection', return_value=conn), \
                 patch.object(B.dialogs, 'supported', side_effect=AssertionError('must leave dialogs alone')), \
                 patch.object(B.dialogs, 'foreground', side_effect=AssertionError('must leave focus alone')):
                self.assertEqual(bridge._call(op), {})

    def test_installer_includes_helper_and_preflights_existing_destinations(self):
        from fusion_mcp import cli
        dest = self.root / 'AddIns'
        with patch.object(cli, 'fusion_addins_dir', return_value=str(dest)), contextlib.redirect_stdout(__import__('io').StringIO()):
            self.assertEqual(cli.install_addin(), 0)
            self.assertTrue((dest / M.HELPER_NAME / (M.HELPER_NAME + '.manifest')).is_file())
            marker = dest / cli.ADDIN_NAME / 'keep.txt'; marker.write_text('old')
            self.assertEqual(cli.install_addin(), 1); self.assertTrue(marker.exists())
            self.assertEqual(cli.install_addin(force=True), 0); self.assertFalse(marker.exists())
            self.assertTrue((dest / cli.ADDIN_NAME / 'management.py').is_file())


if __name__ == '__main__': unittest.main()
