"""Portable candidate review files and persisted routing diagnostics."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from test_compiler_schema import minimal_design

from fusion_offline.compiler.compile import compile_candidate
from fusion_offline.compiler.manifest import load_candidate_manifest
from fusion_offline.compiler import routing_job
from fusion_offline.compiler.render import _schematic_sheets, render_candidate, render_candidate_png


class CandidateRenderTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.design = minimal_design()
        self.design["routing"]["protected_nets"] = ["GND"]
        self.design["schematic"]["sheets"].append({"number": 2, "description": "Second <sheet>"})
        self.design["board"]["keepouts"] = [{"vertices": [[34, 2], [37, 2], [37, 5]], "layers": [1]}]
        self.design["board"]["pours"] = [
            {"net": "GND", "layer": 16, "width": 0.2, "vertices": [[3, 3], [8, 3], [8, 7]]},
        ]
        vcc = next(net for net in self.design["nets"] if net["name"] == "VCC")
        vcc["wires"] = [{"x1": 4, "y1": 10, "x2": 12, "y2": 8, "width": 0.25, "layer": 1}]
        vcc["vias"] = [{"x": 12, "y": 8, "drill": 0.3, "extent": "1-16"}]

    def compile(self, name):
        source = self.root / "design.json"
        raw = json.dumps(self.design).encode()
        source.write_bytes(raw)
        return compile_candidate(source, sha256(raw).hexdigest(), self.root / name)

    def test_publishes_identical_path_free_review_artifacts_in_two_directories(self):
        with patch.object(routing_job, "route_all", side_effect=AssertionError("unexpected routing")):
            first, second = self.compile("first"), self.compile("second")
        self.assertEqual(first.sha256, second.sha256)
        files = {row["path"]: key for key, row in first.data["artifacts"].items()}
        required = {"index.html", "board.svg", "schematic-sheet-1.svg", "schematic-sheet-2.svg",
                    "routing-diagnostics.json", "validation.json", "candidate-summary.json"}
        self.assertLessEqual(required, files.keys())
        for filename in required:
            with self.subTest(filename=filename):
                raw = first.read_artifact(files[filename])
                self.assertEqual(raw, second.read_artifact(files[filename]))
                self.assertNotIn(str(self.root).encode(), raw)
                self.assertNotIn(b"timestamp", raw)
        html = first.read_artifact(files["index.html"]).decode()
        for link in re.findall(r'(?:href|src)="([^"]+)"', html):
            self.assertIn(link, files.keys() | {"candidate-manifest.json"})
        self.assertEqual(files["index.html"], "preview")
        self.assertIn("Second &lt;sheet&gt;", html)
        self.assertIn(first.data["candidate_id"], html)
        self.assertIn(first.data["inputs"]["spec_sha256"], html)
        self.assertIn("Native Fusion ERC/DRC has not yet run", html)
        self.assertEqual(json.loads(first.read_artifact("routing_diagnostics")), {})
        summary = json.loads(first.read_artifact(files["candidate-summary.json"]))
        self.assertEqual(summary["import_eligible"], first.data["status"] == "pass")
        self.assertEqual(summary["vias_by_net"]["VCC"], 1)
        svg = ET.fromstring(first.read_artifact("board_svg"))
        groups = {node.get("id"): node for node in svg.iter() if node.get("id")}
        for group in ("outline", "parts", "pads", "routes", "vias", "pours", "keepouts", "layer-legend"):
            self.assertTrue(len(groups[group]), group)
        self.assertIn("fixed", {node.get("class") for node in groups["parts"]})
        self.assertIn("protected", {node.get("class") for node in groups["pours"]})
        self.assertEqual(load_candidate_manifest(first.path, first.sha256).data, first.data)

    def test_promoted_review_uses_exact_persisted_routing_bytes(self):
        base = self.compile("base")
        job = routing_job.plan_routing_job(base.path, base.sha256, ["GND", "VCC"])
        with patch.object(routing_job, "route_all", side_effect=AssertionError("unexpected routing")):
            promoted = routing_job.promote_routing_job(
                base.path, base.sha256, job.path, job.sha256, self.root / "promoted",
            )
        self.assertEqual(promoted.read_artifact("routing_diagnostics"), job.read_artifact("diagnostics"))
        self.assertEqual(promoted.read_artifact("board_svg"), job.read_artifact("preview"))
        svg = ET.fromstring(promoted.read_artifact("board_svg"))
        overlay = next(node for node in svg.iter() if node.get("id") == "routing-diagnostics")
        self.assertEqual(sum(node.get("class") == "failed-endpoint" for node in overlay.iter()), 2)
        summary = json.loads(promoted.read_artifact("summary"))
        diagnostics = json.loads(job.read_artifact("diagnostics"))
        self.assertEqual(summary["routing_counts"]["failed"], 1)
        self.assertEqual(summary["routing_budget_consumed"]["expanded_nodes"],
                         sum(row["expanded_nodes"] for row in diagnostics.values()))


    def test_budget_limited_job_displays_persisted_blockers(self):
        self.design["routing"]["budget"].update(max_nodes_per_attempt=1, max_nodes_per_connection=1)
        base = self.compile("base")
        job = routing_job.plan_routing_job(base.path, base.sha256, ["VCC"])
        diagnostics = json.loads(job.read_artifact("diagnostics"))
        row, = diagnostics.values()
        self.assertTrue(row["candidate_blockers"])
        svg = ET.fromstring(job.read_artifact("preview"))
        overlay = next(node for node in svg.iter() if node.get("id") == "routing-diagnostics")
        self.assertEqual(sum(node.get("class") == "candidate-blocker" for node in overlay.iter()),
                         len(row["candidate_blockers"]))

    def test_unsupported_slot_blocks_import_and_has_no_circular_substitute(self):
        source = self.design["libraries"][0]
        library = ET.parse(source["path"]).getroot()
        library.find(".//package[@name='HEADER3']/pad").set("shape", "slot")
        raw = ET.tostring(library, encoding="utf-8")
        path = self.root / "slot.lbr"
        path.write_bytes(raw)
        source.update(path=path.as_posix(), sha256=sha256(raw).hexdigest())
        candidate = self.compile("slot")
        summary = json.loads(candidate.read_artifact("summary"))
        self.assertFalse(summary["import_eligible"])
        self.assertIn("keepouts", {row["check"] for row in summary["required_unverified_findings"]})
        board = candidate.read_artifact("board_svg")
        self.assertIn(b"J1.1: unsupported pad geometry", board)
        self.assertNotIn(b'data-pad="J1.1"', board)

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "optional PNG backend is unavailable")
    def test_optional_png_is_outside_candidate_identity(self):
        candidate = self.compile("candidate")
        before = {path.name: path.read_bytes() for path in candidate.path.parent.iterdir()}
        with patch.object(routing_job, "route_all", side_effect=AssertionError("unexpected routing")):
            with self.assertRaisesRegex(ValueError, "outside the candidate"):
                render_candidate_png(candidate.path, candidate.sha256, candidate.path.parent / "board.png")
            path = render_candidate_png(candidate.path, candidate.sha256, self.root / "board.png")
            with self.assertRaises(FileExistsError):
                render_candidate_png(candidate.path, candidate.sha256, path)
            self.assertEqual(render_candidate(candidate.path, candidate.sha256).data, candidate.data)
        self.assertTrue(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual({path.name: path.read_bytes() for path in candidate.path.parent.iterdir()}, before)

    def test_schematic_uses_saved_junctions_at_interior_taps(self):
        schematic = b"""<eagle><drawing><schematic><sheets><sheet><nets>
          <net name="A"><segment><wire x1="0" y1="0" x2="5" y2="0"/></segment></net>
          <net name="B"><segment><wire x1="0" y1="0" x2="0" y2="5"/>
              <wire x1="0" y1="0" x2="-5" y2="0"/></segment></net>
          <net name="C"><segment><wire x1="0" y1="8" x2="8" y2="8"/>
              <wire x1="4" y1="8" x2="4" y2="12"/><junction x="4" y="8"/></segment></net>
        </nets></sheet></sheets></schematic></drawing></eagle>"""
        _, _, raw = next(_schematic_sheets(schematic))
        svg = ET.fromstring(raw)
        circles = svg.findall("{*}circle")
        self.assertEqual(len(circles), 1)
        tap = svg.findall("{*}polyline")[-1].attrib["points"].split()[0]
        self.assertEqual(f'{circles[0].get("cx")},{circles[0].get("cy")}', tap)

    def test_schematic_viewbox_contains_a_long_saved_label(self):
        net = "POWER_CONTROL_INTERCONNECT_" * 6
        schematic = f"""<eagle><drawing><schematic><sheets><sheet><nets>
          <net name="{net}"><segment><wire x1="0" y1="0" x2="5" y2="0"/>
            <label x="5" y="0"/></segment></net>
        </nets></sheet></sheets></schematic></drawing></eagle>""".encode()
        _, _, raw = next(_schematic_sheets(schematic))
        svg = ET.fromstring(raw)
        _, _, width, _ = map(float, svg.attrib["viewBox"].split())
        x, tip, flag = map(float, re.match(r"M([\d.]+),[\d.]+ l([\d.]+),-[\d.]+ h([\d.]+)",
                                         svg.find("{*}path").attrib["d"]).groups())
        self.assertGreater(width, x + tip + flag)

    def test_symbol_major_arc_preserves_the_saved_sweep(self):
        from fusion_offline.sch_layout import Drawing
        from fusion_offline.sch_render import render_svg
        from fusion_offline.symbols import Geo

        drawing = Drawing(parts={"U1": (0, 0, 0, False)}, boxes=[(-3, -3, 3, 3, "U1")])
        symbol = Geo("U1", {}, {}, [("wire", 0, 0, 2, 0, 0.25, 270)])
        svg = ET.fromstring(render_svg(drawing, {"U1": symbol}, {}, {}))
        arc = svg.find("{*}path").attrib["d"]
        self.assertRegex(arc, r"A[\d.]+,[\d.]+ 0 1 0 ")
