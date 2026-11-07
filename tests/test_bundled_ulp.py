import json
from pathlib import Path
import re
import tempfile
import types
import unittest
from unittest.mock import patch

from test_server import load_addin


class BundledUlpTest(unittest.TestCase):
    def setUp(self):
        self.addin = load_addin()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addin.WORK_DIR = self.tmp.name
        self.doc = types.SimpleNamespace(name="PCB", isModified=False)
        self.calls = []
        self.payload = {"schema": 1, "probe": "probe", "editor": "board", "complete": True}
        self.addin._app = types.SimpleNamespace(activeDocument=self.doc, executeTextCommand=self.execute)
        self.addin._product_kind = lambda: "board"

    def execute(self, command):
        self.calls.append(command)
        paths = re.findall("'([^']*)'", command)
        self.assertEqual(len(paths), 2)
        self.assertTrue(Path(paths[0]).is_file())
        self.assertEqual(Path(paths[0]).name, "probe.ulp")
        self.assertTrue(Path(paths[1]).is_relative_to(self.tmp.name))
        self.assertFalse(Path(paths[1]).exists())
        if self.payload is not None:
            Path(paths[1]).write_text(json.dumps(self.payload), encoding="utf-8")
        return ""

    def run_probe(self, **args):
        self.assertTrue(hasattr(self.addin, "op_run_bundled_ulp"), "bundled runner is missing")
        return self.addin.op_run_bundled_ulp(args)

    def test_returns_completed_result_and_cleans_private_output(self):
        result = self.run_probe(name="probe", design="PCB")
        self.assertEqual(result["data"], self.payload)
        self.assertEqual(result["editor"], "board")
        self.assertEqual(result["document"], "PCB")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_rejects_names_paths_code_and_extra_arguments_before_execution(self):
        for args in ({"name": "../probe"}, {"name": "probe.ulp"}, {"name": "x'; RUN evil"},
                     {"name": []}, {"path": "x.ulp"}, {"script": "exit(0);"},
                     {"output": "elsewhere.json"}, {"args": ["elsewhere.json"]}):
            with self.subTest(args=args), self.assertRaises(self.addin.BridgeError):
                self.run_probe(**args)
        self.assertEqual(self.calls, [])

    def test_refuses_wrong_design_or_editor(self):
        with self.assertRaises(self.addin.BridgeError):
            self.run_probe(design="Different PCB")
        self.addin._product_kind = lambda: "library"
        with self.assertRaises(self.addin.BridgeError):
            self.run_probe()
        self.assertEqual(self.calls, [])

    def test_missing_incomplete_or_wrong_context_output_is_an_error(self):
        for payload in (None, {}, [], {**self.payload, "complete": False},
                        {**self.payload, "editor": "schematic"}):
            self.payload = payload
            with self.subTest(payload=payload), self.assertRaises(self.addin.BridgeError):
                self.run_probe()
            self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_document_switch_is_not_reported_as_success(self):
        execute = self.execute
        def switch(command):
            execute(command)
            self.addin._app.activeDocument = types.SimpleNamespace(name="Other", isModified=False)
        self.addin._app.executeTextCommand = switch
        with self.assertRaises(self.addin.BridgeError):
            self.run_probe()
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_builtin_runner_finds_bundled_asset_without_cancelling_active_tool(self):
        from fusion_mcp.builtin import build_script, READ_ONLY_OPS
        scope = {}
        exec(build_script("run_bundled_ulp", {}, read_only="run_bundled_ulp" in READ_ONLY_OPS), scope)
        scope["WORK_DIR"] = self.tmp.name
        scope["_product_kind"] = lambda: "board"
        self.addin._app.userInterface = object()  # has no terminateActiveCommand: a read must not call it
        self.addin.adsk.core.Application = types.SimpleNamespace(get=lambda: self.addin._app)
        with patch("builtins.print") as output:
            scope["run"]("test")
        response = json.loads(output.call_args.args[0].split("@@FEMCP@@", 1)[1])
        self.assertEqual(response["result"]["data"], self.payload)

    def test_modified_flag_change_is_not_reported_as_success(self):
        execute = self.execute
        def modify(command):
            execute(command)
            self.doc.isModified = True
        self.addin._app.executeTextCommand = modify
        with self.assertRaises(self.addin.BridgeError):
            self.run_probe()
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_malformed_or_oversized_output_is_rejected_and_cleaned(self):
        for content in (b'{broken', b'\xff', b' ' * (2 * 1024 * 1024 + 1)):
            def invalid(command):
                self.execute(command)
                output = re.findall("'([^']*)'", command)[1]
                Path(output).write_bytes(content)
            self.addin._app.executeTextCommand = invalid
            with self.subTest(size=len(content)), self.assertRaises(self.addin.BridgeError):
                self.run_probe()
            self.assertEqual(list(Path(self.tmp.name).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
