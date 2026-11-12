"""Command-line contracts for the pinned offline compiler."""
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
import unittest
from unittest.mock import patch

from fusion_mcp.cli import main
from test_compiler_schema import minimal_design


class CompilerCLITest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.design = minimal_design()

    def invoke(self, *args, code=0):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                result = main(list(args))
            except SystemExit as error:
                self.fail(f"unexpected argparse exit {error.code}: {stderr.getvalue()}")
        self.assertEqual(result, code, stdout.getvalue() + stderr.getvalue())
        data = json.loads(stdout.getvalue())
        self.assertIsInstance(data, dict)
        if code:
            self.assertTrue(stderr.getvalue().strip())
        else:
            self.assertEqual(stderr.getvalue(), "")
        return data

    def spec_args(self, pin=None):
        source = self.root / "design.json"
        raw = json.dumps(self.design).encode()
        source.write_bytes(raw)
        return ("--spec", str(source), "--spec-sha256", pin or sha256(raw).hexdigest())

    def compile(self, *, code=0):
        return self.invoke("compile-candidate", *self.spec_args(),
                           "--output-directory", str(self.root / "candidate"), code=code)

    @staticmethod
    def manifest_args(payload):
        return ("--manifest", payload["manifest_path"],
                "--manifest-sha256", payload["manifest_sha256"])

    def test_compile_render_and_validate_saved_candidate(self):
        base = self.compile()
        self.assertEqual(base["status"], "pass")
        manifest = Path(base["manifest_path"])
        self.assertEqual(sha256(manifest.read_bytes()).hexdigest(), base["manifest_sha256"])
        args = self.manifest_args(base)
        render = self.invoke("render-candidate", *args)
        self.assertEqual(Path(render["preview_path"]), manifest.parent / "index.html")
        self.assertTrue(Path(render["preview_path"]).is_file())
        validation = self.invoke("validate-candidate", *args)
        self.assertEqual(validation["report"], json.loads((manifest.parent / "validation.json").read_bytes()))

    def test_invalid_or_missing_pin_returns_json_error(self):
        for args in (self.spec_args("0" * 64), self.spec_args()[:2]):
            with self.subTest(args=args):
                result = self.invoke("compile-candidate", *args, "--output-directory",
                                     str(self.root / "candidate"), code=2)
                self.assertEqual(result["status"], "invalid_input")
                self.assertFalse((self.root / "candidate").exists())

    def test_rejects_changed_artifact_on_render_and_validate(self):
        base = self.compile()
        (Path(base["manifest_path"]).parent / "candidate.brd").write_bytes(b"changed")
        for command in ("render-candidate", "validate-candidate"):
            with self.subTest(command=command):
                result = self.invoke(command, *self.manifest_args(base), code=2)
                self.assertEqual(result["status"], "invalid_input")

    def test_preserves_unrelated_output(self):
        output = self.root / "candidate"
        output.mkdir()
        marker = output / "unrelated"
        marker.write_bytes(b"keep me")
        self.assertEqual(self.compile(code=2)["status"], "invalid_input")
        self.assertEqual(list(output.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b"keep me")

    def test_validation_failure_has_distinct_exit_code(self):
        self.design["routing"]["require_fully_routed"] = True
        base = self.compile(code=3)
        self.assertEqual(base["status"], "fail")
        result = self.invoke("validate-candidate", *self.manifest_args(base), code=3)
        self.assertEqual(result["report"]["airwires"], 5)

    def test_route_and_promote_exact_persisted_board(self):
        base = self.compile()
        args = self.manifest_args(base)
        job = self.invoke("route-candidate", *args, "--net", "VCC")
        job_path = Path(job["manifest_path"])
        self.assertEqual(sha256(job_path.read_bytes()).hexdigest(), job["manifest_sha256"])
        output = ("--output-directory", str(self.root / "promoted"))
        promote = ("route-candidate", *args, *output, "--job-manifest", str(job_path))
        self.invoke(*promote, code=2)
        self.invoke(*promote, "--job-manifest-sha256", "0" * 64, code=2)
        with patch("fusion_offline.compiler.routing_job.route_all",
                   side_effect=AssertionError("promotion called the router")):
            candidate = self.invoke(*promote, "--job-manifest-sha256", job["manifest_sha256"])
        self.assertEqual((Path(candidate["manifest_path"]).parent / "candidate.brd").read_bytes(),
                         (job_path.parent / "candidate-board.brd").read_bytes())

    def test_budget_limited_routing_publishes_partial_outcome(self):
        self.design["routing"]["budget"]["max_nodes_per_attempt"] = 1
        self.design["routing"]["budget"]["max_nodes_per_connection"] = 1
        self.design["routing"]["require_fully_routed"] = True
        base = self.compile(code=3)
        args = self.manifest_args(base)
        job = self.invoke("route-candidate", *args, "--net", "VCC", code=4)
        self.assertEqual(job["status"], "partial")
        self.assertIn("node_limit", {row["termination"] for row in job["diagnostics"].values()})
        candidate = self.invoke("route-candidate", *args, "--job-manifest", job["manifest_path"],
                                "--job-manifest-sha256", job["manifest_sha256"],
                                "--output-directory", str(self.root / "promoted"), code=4)
        validated = self.invoke("validate-candidate", *self.manifest_args(candidate), code=4)
        self.assertEqual(validated["report"]["status"], "fail")

    def test_console_entrypoint_ignores_unrelated_live_library(self):
        package = Path(__file__).resolve().parents[1] / "pyproject.toml"
        entrypoint = tomllib.loads(package.read_text(encoding="utf-8"))["project"]["scripts"][
            "fusion-electronics-mcp"
        ]
        module, function = entrypoint.split(":")
        library = self.root / "live-library"
        library.mkdir()
        (library / "broken.json").write_text("not JSON", encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-c", f"from {module} import {function}; raise SystemExit({function}())",
             "compile-candidate", *self.spec_args(), "--output-directory", str(self.root / "candidate")],
            env={**os.environ, "FUSION_MCP_LIBRARY": str(library)},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "pass")

    def test_optional_full_routing_keeps_validation_pass_with_partial_routing(self):
        self.design["routing"]["budget"]["max_nodes_per_attempt"] = 1
        base = self.compile()
        args = self.manifest_args(base)
        job = self.invoke("route-candidate", *args, "--net", "VCC", code=4)
        candidate = self.invoke("route-candidate", *args, "--job-manifest", job["manifest_path"],
                                "--job-manifest-sha256", job["manifest_sha256"],
                                "--output-directory", str(self.root / "promoted"))
        results = [
            candidate,
            self.invoke("render-candidate", *self.manifest_args(candidate)),
            self.invoke("validate-candidate", *self.manifest_args(candidate)),
        ]
        for result in results:
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["routing_status"], "partial")
            self.assertEqual(result["diagnostics"], job["diagnostics"])
        self.assertEqual(results[-1]["report"]["status"], "pass")
    def test_unexpected_error_has_distinct_exit_code(self):
        with patch("fusion_offline.compiler.compile.compile_candidate",
                   side_effect=RuntimeError("unexpected failure")):
            self.assertEqual(self.compile(code=5)["status"], "internal_error")
