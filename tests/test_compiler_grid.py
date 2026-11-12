"""Check opt-in 50 mil placement alignment through compiled artifacts."""
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

from fusion_offline.compiler.compile import compile_candidate
from fusion_offline.compiler.model import load_design_spec
from test_compiler_schema import minimal_design


class CompilerGridTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.design = minimal_design()

    def pin(self):
        source = self.root / "design.json"
        raw = json.dumps(self.design).encode()
        source.write_bytes(raw)
        return source, sha256(raw).hexdigest()

    def compile(self, name):
        return compile_candidate(*self.pin(), self.root / name)

    def test_opt_in_snaps_both_board_placement_sources_and_schematic(self):
        self.design["align_50mil"] = True
        schematic_asset = self.design["templates"]["schematic"]
        template = ET.parse(schematic_asset["path"])
        drawing = template.getroot().find("drawing")
        drawing.remove(drawing.find("grid"))
        source = self.root / "without-grid.sch"
        template.write(source, encoding="utf-8")
        schematic_asset.update(path=str(source), sha256=sha256(source.read_bytes()).hexdigest())
        first, second = self.design["parts"][:2]
        first["units"][0].update(x=5.21, y=28.0)
        self.design["board"]["placements"].append(dict(second["board"], ref=second["ref"]))
        second["board"] = None

        candidate = self.compile("aligned")
        self.assertEqual(candidate.data["status"], "pass")
        schematic, board = (ET.fromstring(candidate.read_artifact(kind))
                            for kind in ("schematic", "board"))
        for document in (schematic, board):
            grid = document.find("./drawing/grid")
            self.assertEqual((grid.get("distance"), grid.get("unitdist"), grid.get("unit")),
                             ("50", "mil", "mil"))
        unit = schematic.find(".//instance[@part='J1']")
        self.assertEqual((unit.get("x"), unit.get("y")), ("5.08", "27.94"))
        for ref, expected in (("J1", ("3.81", "10.16")), ("R1", ("15.24", "21.59"))):
            element = board.find(f".//element[@name='{ref}']")
            self.assertEqual((element.get("x"), element.get("y")), expected)

    def test_omitted_or_false_preserves_template_grids_and_placements(self):
        legacy = self.compile("legacy")
        self.design["align_50mil"] = False
        explicit = self.compile("false")
        for kind in ("schematic", "board"):
            raw = legacy.read_artifact(kind)
            self.assertEqual(raw, explicit.read_artifact(kind))
            self.assertEqual(ET.fromstring(raw).find("./drawing/grid").get("distance"), "0.1")

    def test_disallows_snapping_board_with_explicit_preroute(self):
        self.design["align_50mil"] = True
        self.design["nets"][0]["wires"] = [
            {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "layer": 1, "width": 0.3},
        ]
        with self.assertRaisesRegex(ValueError, "50 mil.*explicit prerout"):
            load_design_spec(*self.pin())

    def test_already_aligned_board_can_keep_explicit_preroute(self):
        self.design["align_50mil"] = True
        for index, part in enumerate(self.design["parts"], 1):
            part["board"].update(x=index * 5.08, y=10.16)
        self.design["nets"][0]["vias"] = [{"x": 1, "y": 2, "extent": "1-16", "drill": 0.3}]
        spec = load_design_spec(*self.pin())
        self.assertEqual(spec.nets[-1]["vias"][0]["x"], 1)
