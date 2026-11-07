import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import types
import unittest
from unittest.mock import patch

from fusion_mcp.addin.FusionElectronicsMCP import ulp_support as U
from test_server import load_addin


class RegisteredUlpBridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, APPDATA=self.tmp.name); env.start(); self.addCleanup(env.stop)
        self.addin = load_addin(); self.addin.WORK_DIR = str(self.root / 'work')
        self.doc = types.SimpleNamespace(name='PCB', isModified=False)
        self.addin._app = types.SimpleNamespace(activeDocument=self.doc, executeTextCommand=self.execute)
        self.addin._product_kind = lambda: 'board'
        self.calls = []
        self.payload = {'schema': 1, 'mode': 'read', 'editor': 'board', 'complete': True, 'data': {'count': 2}}
        self.entry = self.register('read', b'output(MCP_OUTPUT, "wt") { printf("test"); }')
        self.args = {'name': 'test', 'arguments': [], 'expected_document_id': self.addin._document_id(self.doc),
                     'registration_sha256': U.digest(self.entry)}

    def register(self, mode, source):
        path = self.root / 'existing.ulp'; path.write_bytes(source)
        return U.register(U.registry_dir(), 'test', path, mode, ['board'], 0, hashlib.sha256(source).hexdigest())

    def execute(self, command):
        self.calls.append(command)
        source = Path(re.findall("'([^']*)'", command)[0])
        self.assertEqual(source.name, 'existing.ulp')
        if self.entry['mode'] != 'legacy':
            output = re.search(r'output\(("[^"]+")', source.read_text()).group(1)
            Path(json.loads(output)).write_text(json.dumps(self.payload))
        else:
            (source.parent / 'export.txt').write_text('retain this')
        return ''

    def test_read_validates_envelope_and_removes_staged_files(self):
        result = self.addin.op_run_registered_ulp(self.args)
        self.assertEqual(result['data']['data'], {'count': 2})
        self.assertEqual(list((self.root / 'work').iterdir()), [])
        for data in ({}, dict(self.payload, editor='schematic'), dict(self.payload, complete=False),
                     dict(self.payload, commands='MOVE R1 (0 0)')):
            self.payload = data
            with self.assertRaises(self.addin.BridgeError): self.addin.op_run_registered_ulp(self.args)

    def test_wrong_hash_document_and_extra_inputs_do_not_execute(self):
        for extra in ({'registration_sha256': '0'*64}, {'expected_document_id': 'other'},
                      {'path': 'evil.ulp'}, {'arguments': ['x']}, {'legacy_source_sha256': self.entry['sha256']}):
            with self.subTest(extra=extra), self.assertRaises(self.addin.BridgeError):
                self.addin.op_run_registered_ulp(self.args | extra)
        (U.registry_dir() / (self.entry['sha256'] + '.ulp')).write_bytes(b'exit("WRITE;");')
        with self.assertRaises(self.addin.BridgeError): self.addin.op_run_registered_ulp(self.args)
        self.assertEqual(self.calls, [])

    def test_changed_document_is_an_error(self):
        execute = self.execute
        def changed(command):
            execute(command); self.doc.isModified = True
        self.addin._app.executeTextCommand = changed
        with self.assertRaises(self.addin.BridgeError): self.addin.op_run_registered_ulp(self.args)

    def test_legacy_requires_hash_and_retains_generated_artifacts(self):
        self.entry = self.register('legacy', b'exit(0);')
        self.args['registration_sha256'] = U.digest(self.entry)
        with self.assertRaises(self.addin.BridgeError): self.addin.op_run_registered_ulp(self.args)
        self.assertEqual(self.calls, [])
        self.args['legacy_source_sha256'] = self.entry['sha256']
        result = self.addin.op_run_registered_ulp(self.args)
        self.assertTrue(result['legacy'])
        self.assertEqual((Path(result['artifact_directory']) / 'export.txt').read_text(), 'retain this')

    def test_legacy_dependencies_are_not_silently_unpinned(self):
        with self.assertRaisesRegex(ValueError, 'self-contained'):
            self.register('legacy', b'#include "mutable.ulp"\nexit(0);')

    def test_custom_tools_expose_expected_effect_annotations(self):
        import asyncio
        from fusion_mcp.server import mcp
        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        for name in ('discover_fusion_ulps', 'list_registered_ulps', 'run_registered_ulp', 'preview_ulp_edit'):
            self.assertTrue(tools[name].annotations.read_only_hint)
        for name in ('register_ulp', 'unregister_ulp', 'apply_ulp_edit', 'run_legacy_ulp'):
            self.assertFalse(tools[name].annotations.read_only_hint)
        self.assertTrue(tools['run_legacy_ulp'].annotations.open_world_hint)
        self.assertFalse(tools['run_legacy_ulp'].annotations.idempotent_hint)

    def test_legacy_leaves_dialogs_and_focus_to_the_user(self):
        from fusion_mcp import bridge as B
        # A synthetic socket reply exercises the normal transport without touching Fusion.
        from unittest.mock import MagicMock
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.return_value = b'{"ok":true,"result":{}}\n'
        bridge = B.Bridge(watch_dialogs=True, keep_focus=True)
        with patch.object(bridge, 'transport', return_value='addin'), \
             patch.object(bridge, '_info', return_value={'protocol': B.PROTOCOL, 'pid': 123, 'port': 1, 'token': 'test'}), \
             patch.object(B.socket, 'create_connection', return_value=connection), \
             patch.object(B.dialogs, 'supported', side_effect=AssertionError('legacy must not start a dialog watchdog')), \
             patch.object(B.dialogs, 'foreground', side_effect=AssertionError('legacy must not manage focus')):
            self.assertEqual(bridge._call('run_registered_ulp', {'legacy_source_sha256': 'a'*64}), {})

    def test_registered_runner_refuses_builtin_transport(self):
        from fusion_mcp import bridge as B
        bridge = B.Bridge()
        with patch.object(bridge, 'transport', return_value='builtin'):
            with self.assertRaises(B.BridgeOpError): bridge._call('run_registered_ulp', {})


if __name__ == '__main__': unittest.main()
