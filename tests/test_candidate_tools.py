"""Offline compiler contracts through the registered MCP tools."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

from fusion_mcp import server
from fusion_offline.compiler import routing_job
from fusion_offline.compiler.canonical import canonical_json_bytes
from test_compiler_schema import minimal_design
from test_server import EXPECTED_TOOLS


class CandidateToolsTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.workspace = self.root / "workspace"
        self.design = minimal_design()
        self.enterContext(patch("fusion_mcp.candidate_compiler.data_dir", return_value=str(self.workspace)))
        self.enterContext(patch.object(server.session, "bridge", object()))
        runner = self.enterContext(asyncio.Runner())
        runner.get_loop()
        self.run_mcp = runner.run
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network call")))

    def spec(self):
        path = self.root / "design.json"
        raw = json.dumps(self.design).encode()
        path.write_bytes(raw)
        return str(path), sha256(raw).hexdigest()

    def compile(self):
        return server.compile_eagle_candidate(*self.spec(), "base")

    @staticmethod
    def pin(result):
        return result["manifest_path"], result["manifest_sha256"]

    def test_discovery_annotations_and_no_workspace_creation(self):
        tools = {tool.name: tool for tool in self.run_mcp(server.mcp.list_tools())}
        self.assertTrue(EXPECTED_TOOLS <= tools.keys())
        for name, required in {
            "get_offline_compiler_workflow": (),
            "compile_eagle_candidate": ("spec_sha256", "output_directory"),
            "plan_eagle_candidate_routing": ("manifest_sha256",),
            "promote_eagle_candidate_routing": ("manifest_sha256", "job_manifest_sha256", "output_directory"),
            "render_eagle_candidate": ("manifest_sha256",),
            "validate_eagle_candidate": ("manifest_sha256",),
        }.items():
            with self.subTest(tool=name):
                tool = tools[name]
                self.assertTrue(set(required) <= set(tool.input_schema.get("required", ())))
                self.assertEqual(tool.annotations.read_only_hint,
                                 name.startswith(("get_", "render_", "validate_")))
        workflow = server.get_offline_compiler_workflow()
        self.assertEqual(workflow["schema_version"], 1)
        self.assertTrue(Path(workflow["schema_path"]).is_file())
        self.assertEqual(Path(workflow["workspace"]), self.workspace)
        self.assertFalse(workflow["fusion_writes"])
        self.assertFalse(self.workspace.exists())

    def test_registered_compile_dispatch_requires_a_pin(self):
        path, pin = self.spec()
        args = {"spec_path": path, "output_directory": "dispatch"}
        with self.assertRaisesRegex(ToolError, "spec_sha256"):
            self.run_mcp(server.mcp.call_tool("compile_eagle_candidate", args))
        self.assertFalse(self.workspace.exists())
        with self.assertRaisesRegex(ToolError, "SHA-256"):
            self.run_mcp(server.mcp.call_tool("compile_eagle_candidate", dict(args, spec_sha256="0" * 64)))
        response = self.run_mcp(server.mcp.call_tool("compile_eagle_candidate", dict(args, spec_sha256=pin)))
        self.assertFalse(response.is_error)
        self.assertEqual(response.structured_content["status"], "pass")
        self.assertEqual(Path(response.structured_content["manifest_path"]).parent, self.workspace / "dispatch")



    def test_rejects_outside_or_nonportable_output(self):
        for target in ("../escaped", str(self.root / "outside"), "//host/share/out", "NUL"):
            with self.subTest(target=target):
                with self.assertRaises(ToolError):
                    server.compile_eagle_candidate(*self.spec(), target)
        self.assertFalse(self.workspace.exists())

    def test_exact_pin_and_atomic_publication(self):
        source, pin = self.spec()
        with self.assertRaisesRegex(ToolError, "SHA-256"):
            server.compile_eagle_candidate(source, "0" * 64, "base")
        self.assertFalse((self.workspace / "base").exists())
        with patch("fusion_offline.compiler.manifest.atomic_write", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(OSError, "write failed"):
                server.compile_eagle_candidate(source, pin, "base")
        self.assertEqual(list(self.workspace.iterdir()), [])

        base = server.compile_eagle_candidate(source, pin, str(self.workspace / "base"))
        self.assertEqual(base["status"], "pass")
        for receipt in (base, server.render_eagle_candidate(*self.pin(base))):
            self.assertEqual(sha256(Path(receipt["manifest_path"]).read_bytes()).hexdigest(),
                             receipt["manifest_sha256"])
            for artifact in receipt["artifacts"].values():
                self.assertEqual(sha256(Path(artifact["path"]).read_bytes()).hexdigest(), artifact["sha256"])
        Path(base["artifacts"]["board"]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ToolError, "SHA-256"):
            server.render_eagle_candidate(*self.pin(base))

    def test_budget_failure_stays_partial_and_promotion_reuses_exact_board(self):
        self.design["routing"]["require_fully_routed"] = True
        self.design["routing"]["budget"].update(max_nodes_per_attempt=1, max_nodes_per_connection=1)
        base = self.compile()
        self.assertEqual(base["status"], "fail")
        base_pin = self.pin(base)
        with patch.object(routing_job, "route_all", wraps=routing_job.route_all) as route:
            job = server.plan_eagle_candidate_routing(*base_pin, ["VCC"])
            self.assertEqual(job["status"], "partial")
            self.assertIn("node_limit", {row["termination"] for row in job["diagnostics"].values()})
            self.assertEqual(job, server.plan_eagle_candidate_routing(*base_pin, ["VCC"]))
            route.assert_called_once()
            self.assertEqual(route.call_args.kwargs["budget"].max_nodes_per_connection, 1)

        job_path, job_pin = job["job_manifest_path"], job["job_manifest_sha256"]
        self.assertEqual(sha256(Path(job_path).read_bytes()).hexdigest(), job_pin)
        with self.assertRaisesRegex(ToolError, "SHA-256"):
            server.promote_eagle_candidate_routing(*base_pin, job_path, "0" * 64, "promoted")
        with patch.object(routing_job, "route_all", side_effect=AssertionError("route rerun")):
            promoted = server.promote_eagle_candidate_routing(*base_pin, job_path, job_pin, "promoted")
            validation = server.validate_eagle_candidate(*self.pin(promoted))
        self.assertEqual(promoted["artifacts"]["board"]["sha256"], job["artifacts"]["candidate_board"]["sha256"])
        self.assertEqual(promoted["job_manifest_sha256"], job_pin)
        self.assertEqual(validation["status"], "fail")
        self.assertEqual(validation["report"]["status"], "fail")
        self.assertEqual(validation["routing_status"], "partial")

    def test_optional_partial_routing_keeps_passing_validation(self):
        self.design["routing"]["budget"].update(max_nodes_per_attempt=1, max_nodes_per_connection=1)
        base = self.pin(self.compile())
        job = server.plan_eagle_candidate_routing(*base, ["VCC"])
        promoted = server.promote_eagle_candidate_routing(*base, *self.pin(job), "promoted")
        self.assertEqual(promoted["status"], "pass")
        self.assertEqual(promoted["routing_status"], "partial")

    def test_output_cannot_enter_any_published_candidate_or_job(self):
        base = self.compile()
        job = server.plan_eagle_candidate_routing(*self.pin(base), ["VCC"])
        for receipt in (base, job):
            parent = Path(receipt["manifest_path"]).parent
            with self.subTest(parent=parent):
                target = parent / "nested"
                with self.assertRaisesRegex(ToolError, "published"):
                    server.compile_eagle_candidate(*self.spec(), str(target))
                self.assertFalse(target.exists())
        other = server.compile_eagle_candidate(*self.spec(), "other")
        with self.assertRaisesRegex(ToolError, "published"):
            server.plan_eagle_candidate_routing(*self.pin(other), ["VCC"], "base")
        self.assertFalse((self.workspace / "base" / "routing-jobs").exists())

    def test_rejects_exactly_pinned_xml_with_internal_entities(self):
        for key in ("schematic", "board"):
            with self.subTest(key=key):
                base = server.compile_eagle_candidate(*self.spec(), key)
                artifact = Path(base["artifacts"][key]["path"])
                manifest = Path(base["manifest_path"])
                xml = artifact.read_bytes()
                doctype = b'<!DOCTYPE eagle SYSTEM "eagle.dtd">'
                self.assertIn(doctype, xml)
                xml = xml.replace(doctype, b'<!DOCTYPE eagle [<!ENTITY marker "expanded">]>')
                xml = xml.replace(b"</eagle>", b"&marker;</eagle>")
                artifact.write_bytes(xml)
                data = json.loads(manifest.read_bytes())
                data["artifacts"][key]["sha256"] = sha256(xml).hexdigest()
                raw = canonical_json_bytes(data)
                manifest.write_bytes(raw)
                with self.assertRaisesRegex(ToolError, "XML entity declarations"):
                    server.render_eagle_candidate(str(manifest), sha256(raw).hexdigest())
