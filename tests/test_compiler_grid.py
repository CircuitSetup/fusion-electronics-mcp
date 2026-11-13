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

    def test_active_grid_origin_and_explicit_off_grid_readback(self):
        asset = self.design["templates"]["board"]
        template = ET.parse(asset["path"])
        template.getroot().find("./drawing/grid").attrib.update(
            distance="50", unitdist="mil", unit="inch", originx="10", originy="20")
        path = self.root / "offset.brd"
        template.write(path, encoding="utf-8")
        asset.update(path=str(path), sha256=sha256(path.read_bytes()).hexdigest())
        self.design["parts"][1]["board"].update(x=15.3, y=21.0)
        self.design["parts"][2]["board"].update(x=24.1, y=21.17, off_grid=True)
        root = ET.fromstring(self.compile("offset").read_artifact("board"))
        for ref, expected in (("R1", (15.494, 20.828)), ("D1", (24.1, 21.17))):
            element = root.find(f".//element[@name='{ref}']")
            self.assertEqual(tuple(float(element.get(axis)) for axis in "xy"), expected)

    def test_copper_clearance_can_keep_a_legal_off_grid_request(self):
        from fusion_offline import part_placement as placement

        root = ET.fromstring((Path(__file__).parent / "fixtures" / "mini.brd").read_bytes())
        ET.SubElement(root.find("./drawing"), "grid", distance="10", unitdist="mm")
        signals = root.find(".//board/signals")
        signals.clear()
        signal = ET.SubElement(signals, "signal", name="OTHER")
        ET.SubElement(signal, "wire", x1="9.5", x2="9.5", y1="0", y2="11", width="0.3", layer="1")
        board = root.find(".//board")
        for wire in board.findall("./plain/wire"):
            for attr in ("y1", "y2"):
                if wire.get(attr) == "10":
                    wire.set(attr, "11")
        for element in list(board.find("elements")):
            if element.get("name") != "R1":
                board.find("elements").remove(element)
        result = placement.plan(root, "R1", (8.2, 9.7))
        self.assertEqual(result["at"], [8.2, 9.7])
        self.assertIn("routing clearance at R1", result["off_grid_reason"])

    def test_automatic_references_reserve_explicit_names(self):
        self.design["nets"] = []
        self.design["routing"]["selected_nets"] = []
        self.design["parts"][1]["ref"] = None
        copy = json.loads(json.dumps(self.design["parts"][1]))
        copy["ref"] = "R1"
        copy["board"].update(x=20, y=25)
        copy["units"][0].update(x=12, y=42)
        self.design["parts"].append(copy)
        candidate = self.compile("refs")
        for kind, tag in (("schematic", "part"), ("board", "element")):
            names = {node.get("name") for node in ET.fromstring(candidate.read_artifact(kind)).iter(tag)}
            self.assertTrue({"R1", "R2"} <= names, names)
