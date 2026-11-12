"""Contract tests for deterministic EAGLE schematic emission."""
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

from fusion_offline.compiler.libraries import resolve_libraries
from fusion_offline.compiler.model import load_design_spec
from fusion_offline.design import parse_schematic_design
from fusion_offline.edit_plan import electrical, preview_html
from fusion_offline.sch_render import WIRE
from test_compiler_schema import minimal_design


class CompilerSchematicTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data = minimal_design()

    def pin(self, asset, data):
        path = self.root / Path(asset["path"]).name
        path.write_bytes(data)
        asset.update(path=str(path), sha256=sha256(data).hexdigest())

    def build(self):
        from fusion_offline.compiler.schematic import build_schematic

        path = self.root / "design.json"
        raw = json.dumps(self.data).encode()
        path.write_bytes(raw)
        spec = load_design_spec(str(path), sha256(raw).hexdigest())
        template = spec.templates["schematic"].data
        resolved = resolve_libraries(spec)
        xml = build_schematic(spec, template, resolved)
        self.assertEqual(build_schematic(spec, template, resolved), xml)
        return template, xml

    def test_label_stubs_reject_foreign_pins_and_wires(self):
        resistor = next(part for part in self.data["parts"] if part["ref"] == "R1")
        resistor["units"][0].update(x=15, y=20)
        other = deepcopy(resistor)
        other.update(ref="R2")
        other["board"].update(x=25)
        self.data["parts"] = [resistor, other]
        self.data["nets"] = [
            {"name": name, "class": "default", "pins": [{"part": ref, "gate": "G$1", "pin": pin}],
             "schematic_style": "labels", "port": "inter-block", "route": False}
            for name, ref, pin in (("A", "R1", "P$2"), ("B", "R2", "P$1"))
        ]
        self.data["routing"].update(selected_nets=[], protected_nets=[], policies={})
        # At 15 mm the first stub hits a pin; at 18 mm only the stubs overlap.
        for spacing in (15, 18):
            with self.subTest(spacing=spacing):
                other["units"][0].update(x=15 + spacing, y=20)
                with self.assertRaisesRegex(ValueError, "Blocked pin escape"):
                    self.build()
        other["units"][0].update(x=40, y=20)
        _, xml = self.build()
        self.assertEqual(len(ET.fromstring(xml).findall(".//segment/label")), 2)

    def test_minimal_template_electrical_and_render_signatures(self):
        template, xml = self.build()
        self.assertEqual(xml.splitlines()[:2], template.splitlines()[:2])
        before, after = ET.fromstring(template), ET.fromstring(xml)
        self.assertEqual(before.attrib, after.attrib)
        for tag in ("settings", "grid", "layers"):
            self.assertEqual(ET.tostring(before.find(f"./drawing/{tag}")),
                             ET.tostring(after.find(f"./drawing/{tag}")))
        self.assertIsNone(after.find("./drawing/schematic/modules"))
        signature = electrical(xml)
        self.assertEqual(signature["descriptions"], ["LED switch"])
        self.assertEqual(signature["nets"], {
            net["name"]: sorted((pin["part"], pin["gate"], pin["pin"]) for pin in net["pins"])
            for net in self.data["nets"]
        })
        self.assertEqual(signature["labels"], {"CTRL": 2, "GND": 0, "LED_A": 0, "LED_K": 0, "VCC": 0})
        for part in self.data["parts"]:
            self.assertEqual(signature["parts"][part["ref"]][-2:], [part["value"], part["attributes"]])
        library = after.find("./drawing/schematic/libraries/library")
        self.assertEqual(library.get("name"), "PROJECT")
        self.assertEqual({node.get("name") for node in library.findall("./devicesets/deviceset")},
                         {part["deviceset"] for part in self.data["parts"]})
        self.assertEqual(signature["wires"][1], [])
        self.assertTrue(signature["wires"][0])

        html = preview_html({"sch_xml": xml.decode()}, {
            "operations": [], "expected": signature, "findings": [], "sha256": "test",
        })
        svg = ET.fromstring(re.search(r"<svg .*?</svg>", html, re.S)[0])
        nodes = Counter((node.tag.split("}")[-1], node.get("stroke")) for node in svg)
        self.assertEqual(nodes["polyline", WIRE], 17)
        svg_text = Counter(node.text for node in svg if node.tag.endswith("}text"))
        self.assertEqual(svg_text['CTRL'], 2)
        self.assertTrue({'Proposed sheet 1', 'D1.G$1', 'J1.G$1', 'Q1.G$1', 'R1.G$1'} <= svg_text.keys())

    def test_multi_unit_labels_are_separate_on_requested_sheets(self):
        asset = self.data["libraries"][0]
        lib = ET.parse(asset["path"]).getroot()
        header = lib.find(".//deviceset[@name='HEADER3']")
        ET.SubElement(header.find("gates"), "gate", name="B", symbol="HEADER3", x="0", y="0")
        connect = header.find(".//connect[@pin='3']")
        connect.set("gate", "B")
        self.pin(asset, ET.tostring(lib))
        self.data["schematic"]["sheets"].insert(0, {"number": 2, "description": "Control"})
        part = next(part for part in self.data["parts"] if part["ref"] == "J1")
        part["units"].append({"gate": "B", "sheet": 2, "x": 20, "y": 20, "rotation": 90})
        part["value"] = "Power \t\n\r & control"
        part["attributes"]["MPN"] = "V\t\n\r & 1"
        net = next(net for net in self.data["nets"] if net["name"] == "CTRL")
        net.update(schematic_style="labels", port="inter-sheet", **{"class": "control"})
        net["pins"][0]["gate"] = "B"
        self.data["board"]["net_classes"] = [
            {"name": "control", "number": 4, "width": 0.3, "drill": 0.2,
             "clearances": {"default": 0.2}},
        ]
        _, xml = self.build()
        root = ET.fromstring(xml)
        sch = parse_schematic_design(root)
        self.assertEqual(sch.descriptions, ["LED switch", "Control"])
        self.assertEqual([(i.gate, i.sheet, i.angle) for i in sch.instances if i.part == "J1"],
                         [("G$1", 1, 0), ("B", 2, 90)])
        self.assertEqual(sch.parts["J1"].value, part["value"])
        self.assertEqual(sch.parts["J1"].attributes, part["attributes"])
        self.assertEqual(sch.nets["CTRL"].net_class, "control")
        self.assertEqual([(s.sheet, s.labels, s.wires, s.qualified_pins) for s in sch.nets["CTRL"].segments],
                         [(1, 1, 1, [("Q1", "G$1", "B")]), (2, 1, 1, [("J1", "B", "3")])])

    def test_labels_on_the_same_sheet_have_one_segment_per_pin(self):
        _, xml = self.build()
        segments = ET.fromstring(xml).findall(".//net[@name='CTRL']/segment")
        self.assertEqual(len(segments), 2)
        for segment in segments:
            self.assertEqual(Counter(node.tag for node in segment),
                             {"pinref": 1, "wire": 1, "label": 1})

    def test_rejects_populated_and_hierarchical_templates(self):
        asset = self.data["templates"]["schematic"]
        original = Path(asset["path"]).read_bytes()
        for path in ("./parts", "./sheets/sheet/nets", "."):
            with self.subTest(path=path):
                root = ET.fromstring(original)
                target = root.find(f"./drawing/schematic/{path}")
                node = ET.SubElement(target, {"./parts": "part", "./sheets/sheet/nets": "net", ".": "modules"}[path])
                if path == ".":
                    ET.SubElement(node, "module", name="child")
                self.pin(asset, ET.tostring(root))
                with self.assertRaisesRegex(ValueError, "empty|flat"):
                    self.build()

    def test_empty_modules_and_unused_libraries_are_replaced(self):
        asset = self.data["templates"]["schematic"]
        root = ET.parse(asset["path"]).getroot()
        document = root.find("./drawing/schematic")
        ET.SubElement(document, "modules")
        ET.SubElement(document.find("libraries"), "library", name="UNUSED")
        self.pin(asset, ET.tostring(root))

        _, xml = self.build()
        document = ET.fromstring(xml).find("./drawing/schematic")
        self.assertIsNone(document.find("modules"))
        self.assertEqual([node.get("name") for node in document.findall("./libraries/library")],
                         ["PROJECT"])

    def test_rejects_sparse_sheet_numbers(self):
        self.data["schematic"]["sheets"].append({"number": 3, "description": "Missing sheet"})
        with self.assertRaisesRegex(ValueError, "contiguous"):
            self.build()
