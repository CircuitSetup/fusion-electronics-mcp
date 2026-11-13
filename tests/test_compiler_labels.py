"""Native NAME and VALUE fields emitted by the paired compiler."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

from fusion_offline.compiler.compile import compile_candidate
from fusion_offline.compiler.validate import validate_candidate
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
        data["parts"][2]["board"].update(side="bottom", rotation=90, off_grid=True)
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
            self.assertTrue(all(node.get("font") == "proportional" for node in labels.values()))
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
            labels = {node.get("name"): node for node in element.findall("attribute")
                      if node.get("name") in ("NAME", "VALUE")}
            expected = ("26", "28") if element.get("name") == "D1" else ("25", "27")
            self.assertEqual({name: node.get("layer") for name, node in labels.items()},
                             dict(zip(("NAME", "VALUE"), expected)))
            self.assertEqual(labels["VALUE"].get("font"), "proportional")
            if element.get("name") != "D1":
                self.assertEqual(labels["NAME"].get("font"), "proportional")
        name = root.find(".//element[@name='D1']/attribute[@name='NAME']")
        self.assertEqual((float(name.get("x")), float(name.get("y")), name.get("rot")), (26, 20, "MR90"))
        self.assertEqual((name.get("size"), name.get("font"), name.get("ratio")), ("1.2", "vector", "8"))
        self.assertIsNone(name.get("value"))
        self.assertEqual(root.find(".//package[@name='LED_0805']/text").get("layer"), "25")
        self.assertEqual(next(row.attrs for row in parse_board(board).elements if row.name == "D1"),
                         {"MPN": "TEST-LED"})

    def test_board_relocates_two_occupied_grid_requests_and_labels(self):
        data = minimal_design()
        data["parts"][1]["board"].update(x=15.2, y=20.8)
        data["parts"][2]["board"].update(x=15.2, y=20.8, rotation=90)
        source = self.root / "design.json"
        raw = json.dumps(data).encode()
        source.write_bytes(raw)
        candidate = compile_candidate(source, sha256(raw).hexdigest(), self.root / "candidate")
        root = ET.fromstring(candidate.read_artifact("board"))
        from fusion_offline import footprints, placement_check
        positions = set()
        for ref in ("R1", "D1"):
            element = root.find(f".//element[@name='{ref}']")
            point = tuple(float(element.get(axis)) for axis in "xy")
            self.assertTrue(all(abs(value / 2.54 - round(value / 2.54)) < 1e-6 for value in point))
            positions.add(point)
        self.assertEqual(len(positions), 2)
        self.assertEqual({row["ref"] for row in footprints.silkscreen(root) if row["kind"] == "text"},
                         {"R1", "J1", "D1", "Q1"})
        self.assertFalse([row for row in placement_check.silk_on_pads(placement_check.silk_shapes(root),
                                                                      placement_check.pads(root))
                          if row["kind"] == "name"])

    def test_native_custom_attributes_match_the_repaired_21_mpn_rows(self):
        data = minimal_design()
        resistor = next(part for part in data["parts"] if part["ref"] == "R1")
        data["parts"], data["nets"] = [], []
        # Revision G .before-routing-tidy.brd omitted placement on all RA/RB/RC divider MPNs.
        mpn = "TNPV1206240KBEEA"
        expected = {}
        for index, phase in enumerate("ABC"):
            for number in range(1, 8):
                ref = f"R{phase}{number}"
                x, y = 4 + 5 * (number - 1), 5 + 7 * index
                part = deepcopy(resistor)
                part.update(ref=ref, attributes={"MPN": mpn})
                part["units"][0].update(x=x * 4, y=y * 4)
                part["board"].update(x=x, y=y, side="bottom" if phase == "C" else "top", off_grid=True)
                data["parts"].append(part)
                expected[ref] = (x, y, "28" if phase == "C" else "27")

        source = self.root / "design.json"
        raw = json.dumps(data).encode()
        source.write_bytes(raw)
        candidate = compile_candidate(source, sha256(raw).hexdigest(), self.root / "candidate")
        self.assertEqual(candidate.data["status"], "pass")
        root = ET.fromstring(candidate.read_artifact("board"))
        rows = {element.attrib["name"]: element.find("attribute[@name='MPN']")
                for element in root.findall(".//board/elements/element")}
        self.assertEqual(len(rows), 21)
        for ref, row in rows.items():
            x, y, layer = expected[ref]
            self.assertEqual((float(row.get("x", "nan")), float(row.get("y", "nan")), row.get("layer")),
                             (x, y, layer))
            self.assertEqual((row.get("size"), row.get("display"), row.get("value")), ("1.02", "off", mpn))
            row.attrib.clear()
            row.attrib.update(name="MPN", value=mpn)

        report = validate_candidate(candidate.read_artifact("schematic"), ET.tostring(root),
                                    json.loads(candidate.read_artifact("routing_intent")))
        self.assertEqual(report["status"], "fail")
        finding = next(row for row in report["findings"] if row["check"] == "element_attributes")
        self.assertTrue(finding["required"])
        self.assertEqual(finding["status"], "fail")
        self.assertTrue(all(f"{ref}.MPN" in finding["detail"] for ref in expected))
