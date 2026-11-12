"""Native NAME and VALUE fields emitted by the paired compiler."""
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

from fusion_offline.compiler.compile import compile_candidate
from fusion_offline.eagle import parse_board
from fusion_offline.sch_labels import instances
from test_compiler_schema import minimal_design


class CompilerLabelsTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def compile(self):
        data = minimal_design()
        data["parts"][2]["board"].update(side="bottom", rotation=90)
        asset = data["libraries"][0]
        lib = ET.parse(asset["path"]).getroot()
        package = lib.find(".//package[@name='LED_0805']")
        ET.SubElement(package, "text", x="-1", y="2", size="1.2", layer="25",
                      font="vector", ratio="8").text = ">NAME"
        library = self.root / "labels.lbr"
        raw = ET.tostring(lib)
        library.write_bytes(raw)
        asset.update(path=str(library), sha256=sha256(raw).hexdigest())
        source = self.root / "design.json"
        raw = json.dumps(data).encode()
        source.write_bytes(raw)
        candidate = compile_candidate(source, sha256(raw).hexdigest(), self.root / "candidate")
        return candidate.read_artifact("schematic"), candidate.read_artifact("board")

    def test_schematic_uses_native_label_fields_and_preserves_existing_origin(self):
        schematic, _ = self.compile()
        root = ET.fromstring(schematic)
        for unit in root.findall(".//sheet/instances/instance"):
            self.assertEqual(unit.get("smashed"), "yes")
            labels = {node.get("name"): node for node in unit.findall("attribute")}
            self.assertEqual({name: node.get("layer") for name, node in labels.items()},
                             {"NAME": "95", "VALUE": "96"})
            self.assertTrue(all(node.get("value") is None for node in labels.values()))
        header = next(row for row in instances(root) if row["part"] == "J1")
        name, value = (header["labels"][field] for field in ("NAME", "VALUE"))
        self.assertEqual((name["x"], name["y"], name["size"]), (2.54, 33.02, 1.27))
        self.assertLess(value["y"] + value["size"], header["body"][1])
        self.assertEqual(root.find(".//part[@name='J1']").get("value"), "Power and control")
        self.assertIsNone(root.find(".//symbol[@name='HEADER3']/text[@layer='96']"))

    def test_board_uses_the_placed_side_and_keeps_library_and_bom_attributes(self):
        _, board = self.compile()
        root = ET.fromstring(board)
        for element in root.findall(".//board/elements/element"):
            self.assertEqual(element.get("smashed"), "yes")
            labels = {node.get("name"): node for node in element.findall("attribute") if node.get("x")}
            expected = ("26", "28") if element.get("name") == "D1" else ("25", "27")
            self.assertEqual({name: node.get("layer") for name, node in labels.items()},
                             dict(zip(("NAME", "VALUE"), expected)))
        name = root.find(".//element[@name='D1']/attribute[@name='NAME']")
        self.assertEqual((float(name.get("x")), float(name.get("y")), name.get("rot")), (26, 20, "MR90"))
        self.assertEqual((name.get("size"), name.get("font"), name.get("ratio")), ("1.2", "vector", "8"))
        self.assertIsNone(name.get("value"))
        self.assertEqual(root.find(".//package[@name='LED_0805']/text").get("layer"), "25")
        self.assertEqual(next(row.attrs for row in parse_board(board).elements if row.name == "D1"),
                         {"MPN": "TEST-LED"})
