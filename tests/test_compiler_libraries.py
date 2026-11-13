"""Exact pinned library selection and compiler pad mapping."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from fusion_offline.edit_plan import geometry_hash
from fusion_offline.compiler.model import load_design_spec
from test_compiler_schema import minimal_design


class CompilerLibrariesTest(unittest.TestCase):
    def setUp(self):
        self.data = minimal_design()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name).resolve()
        self.source = Path(self.data["libraries"][0]["path"])
        self.xml = ET.fromstring(self.source.read_bytes())
        self.library = self.xml.find("./drawing/library")
        self.led = self.library.find("./devicesets/deviceset[@name='LED']")
        self.device = self.led.find("./devices/device")
        self.package = self.library.find("./packages/package[@name='LED_0805']")

    def resolve(self):
        self.assertIsNotNone(importlib.util.find_spec("fusion_offline.compiler.libraries"),
                             "The pinned library resolver is missing")
        from fusion_offline.compiler.libraries import resolve_libraries

        asset = self.data["libraries"][0]
        lib_path = self.directory / "fixture.lbr"
        payload = ET.tostring(self.xml, encoding="utf-8")
        lib_path.write_bytes(payload)
        asset.update(path=str(lib_path), sha256=hashlib.sha256(payload).hexdigest())
        spec_path = self.directory / "design.json"
        raw = json.dumps(self.data).encode()
        spec_path.write_bytes(raw)
        result = resolve_libraries(load_design_spec(str(spec_path), hashlib.sha256(raw).hexdigest()))
        self.assertEqual(lib_path.read_bytes(), payload)
        return result

    def test_minimizes_exact_selected_definitions_and_keeps_geometry(self):
        ET.SubElement(self.library.find("symbols"), "symbol", name="UNUSED")
        ET.SubElement(self.library.find("packages"), "package", name="UNUSED")
        ET.SubElement(self.library.find("devicesets"), "deviceset", name="UNUSED")
        ET.SubElement(self.led.find("devices"), "device", name="OTHER", package="UNUSED")
        technologies = self.device.find("technologies")
        unused = ET.SubElement(technologies, "technology", name="OTHER")
        ET.SubElement(unused, "attribute", name="MPN", value="wrong")
        ET.SubElement(technologies.find("technology"), "attribute", name="MPN", value="selected")
        source_symbol = self.library.find("./symbols/symbol[@name='LED']")
        result = self.resolve()
        library = result.libraries["PROJECT"]
        self.assertEqual(library.get("name"), "PROJECT")
        for path in ("./packages/package", "./symbols/symbol", "./devicesets/deviceset"):
            self.assertEqual(len(library.findall(path)), 4)
        led = library.find("./devicesets/deviceset[@name='LED']")
        self.assertEqual([d.get("name") for d in led.findall("./devices/device")], [""])
        self.assertEqual([t.get("name") for t in led.findall("./devices/device/technologies/technology")], [""])
        self.assertEqual(geometry_hash(library.find("./packages/package[@name='LED_0805']")),
                         geometry_hash(self.package))
        self.assertEqual(result.package_hashes["PROJECT", "LED_0805"], geometry_hash(self.package))
        self.assertEqual(result.source_hashes["PROJECT"], self.data["libraries"][0]["sha256"])
        part = result.parts["D1"]
        self.assertEqual((part.library, part.deviceset, part.device, part.technology, part.package),
                         ("PROJECT", "LED", "", "", "LED_0805"))
        self.assertEqual(part.attributes, {"MPN": "selected"})
        self.assertEqual(geometry_hash(part.gates["G$1"].symbol), geometry_hash(source_symbol))
        self.assertEqual(part.gates["G$1"].pins["A"].get("name"), "A")
        self.assertEqual(part.pads["G$1", "A"], ("A",))

    def test_order_does_not_change_embedded_library(self):
        first = ET.tostring(self.resolve().libraries["PROJECT"])
        self.data["parts"].reverse()
        self.data["nets"].reverse()
        for net in self.data["nets"]:
            net["pins"].reverse()
        self.assertEqual(ET.tostring(self.resolve().libraries["PROJECT"]), first)

    def test_exact_names_required(self):
        led = next(part for part in self.data["parts"] if part["ref"] == "D1")
        for field, name in (("deviceset", "led"), ("device", "OTHER"), ("technology", "OTHER")):
            with self.subTest(field=field):
                original = led[field]
                led[field] = name
                with self.assertRaisesRegex(ValueError, field):
                    self.resolve()
                led[field] = original
        self.library.set("name", "project")
        with self.assertRaisesRegex(ValueError, "library"):
            self.resolve()

    def test_requires_physical_package_and_intended_pads(self):
        for name in ("", "led_0805"):
            self.device.set("package", name)
            with self.assertRaisesRegex(ValueError, "package"):
                self.resolve()
        self.device.set("package", "LED_0805")
        connect = self.device.find("./connects/connect[@pin='A']")
        connect.set("pad", "a")
        with self.assertRaisesRegex(ValueError, "pad"):
            self.resolve()
        self.device.find("connects").remove(connect)
        with self.assertRaisesRegex(ValueError, "pad"):
            self.resolve()

    def test_rejects_casefold_ambiguous_definitions(self):
        other = ET.SubElement(self.library.find("devicesets"), "deviceset", name="led")
        with self.assertRaisesRegex(ValueError, "duplicate.*deviceset"):
            self.resolve()
        self.library.find("devicesets").remove(other)
        ET.SubElement(self.package, "pad", name="a", drill="1", x="0", y="0")
        with self.assertRaisesRegex(ValueError, "duplicate.*pad"):
            self.resolve()

    def test_rejects_pin_with_wrong_case(self):
        net = next(net for net in self.data["nets"] if net["name"] == "LED_A")
        next(pin for pin in net["pins"] if pin["part"] == "D1")["pin"] = "a"
        with self.assertRaisesRegex(ValueError, "pin"):
            self.resolve()

    def test_one_pin_may_supply_two_distinct_routed_contacts(self):
        net = next(net for net in self.data["nets"] if net["name"] == "LED_A")
        net["pins"] = [pin for pin in net["pins"] if pin["part"] == "D1"]
        with self.assertRaisesRegex(ValueError, "distinct.*pad"):
            self.resolve()
        ET.SubElement(self.package, "smd", name="A2", x="0", y="0", dx="1", dy="1", layer="1")
        self.device.find("./connects/connect[@pin='A']").set("pad", "A A2")
        self.assertEqual(self.resolve().parts["D1"].pads["G$1", "A"], ("A", "A2"))

    def test_preserves_unverified_slot_geometry(self):
        pad = self.library.find("./packages/package[@name='HEADER3']/pad")
        pad.set("shape", "slot")
        pad.set("slotLength", "2.3")
        result = self.resolve()
        self.assertEqual(result.libraries["PROJECT"].find("./packages/package[@name='HEADER3']/pad").attrib,
                         pad.attrib)
        self.assertEqual(result.findings[0]["status"], "unverified")
        self.assertEqual(result.findings[0]["pad"], "1")


    def test_multiple_selected_technologies_share_one_device(self):
        led = next(part for part in self.data["parts"] if part["ref"] == "D1")
        technologies = self.device.find("technologies")
        tech = ET.SubElement(technologies, "technology", name="Red")
        ET.SubElement(tech, "attribute", name="MPN", value="red")
        ET.SubElement(technologies, "technology", name="Unused")
        self.data["parts"].append({**led, "ref": "D2", "technology": "Red"})
        result = self.resolve()
        devices = result.libraries["PROJECT"].findall("./devicesets/deviceset[@name='LED']/devices/device")
        self.assertEqual(len(devices), 1)
        self.assertEqual([t.get("name") for t in devices[0].findall("./technologies/technology")], ["", "Red"])
        self.assertEqual(result.parts["D2"].attributes, {"MPN": "red"})

    def test_schematic_only_device_may_omit_package(self):
        led = next(part for part in self.data["parts"] if part["ref"] == "D1")
        led["board"] = None
        self.device.attrib.pop("package")
        self.device.remove(self.device.find("connects"))
        for net in self.data["nets"]:
            if any(pin["part"] == "D1" for pin in net["pins"]):
                net["route"] = False
        part = self.resolve().parts["D1"]
        self.assertIsNone(part.package)
        self.assertFalse(part.physical)
        self.assertEqual(part.pads, {})

    def test_separate_board_placement_requires_physical_package(self):
        led = next(part for part in self.data["parts"] if part["ref"] == "D1")
        self.data["board"]["placements"] = [{**led["board"], "ref": "D1"}]
        led["board"] = None
        self.device.attrib.pop("package")
        with self.assertRaisesRegex(ValueError, "package"):
            self.resolve()

    def test_maps_unplaced_gates_and_preserves_gate_qualified_pin_names(self):
        ET.SubElement(self.led.find("gates"), "gate", name="Spare", symbol="LED", x="0", y="0")
        ET.SubElement(self.device.find("connects"), "connect", gate="Spare", pin="A", pad="K")
        part = self.resolve().parts["D1"]
        self.assertEqual(part.gates["Spare"].pins["A"].get("name"), "A")
        self.assertEqual(part.pads["Spare", "A"], ("K",))

    def test_candidate_retains_unwired_pin_without_creating_a_board_net(self):
        from fusion_offline.compiler.compile import compile_candidate

        ET.SubElement(self.package, "smd", name="3", x="0", y="0", dx="0.4", dy="0.4", layer="1")
        symbol = self.library.find("./symbols/symbol[@name='LED']")
        ET.SubElement(symbol, "pin", name="NC", x="0", y="2.54", length="short", direction="nc")
        ET.SubElement(self.device.find("connects"), "connect", gate="G$1", pin="NC", pad="3")
        self.resolve()  # Write the pinned fixture library and spec.
        source = self.directory / "design.json"
        candidate = compile_candidate(source, hashlib.sha256(source.read_bytes()).hexdigest(),
                                      self.directory / "candidate")
        schematic = ET.fromstring(candidate.read_artifact("schematic"))
        board = ET.fromstring(candidate.read_artifact("board"))
        self.assertIsNotNone(schematic.find(".//symbol[@name='LED']/pin[@name='NC']"))
        self.assertIsNotNone(schematic.find(".//connect[@pin='NC'][@pad='3']"))
        self.assertIsNone(schematic.find(".//pinref[@part='D1'][@pin='NC']"))
        self.assertIsNotNone(board.find(".//package[@name='LED_0805']/smd[@name='3']"))
        self.assertIsNone(board.find(".//contactref[@element='D1'][@pad='3']"))

    def test_rejects_shared_pad_across_different_nets(self):
        self.device.find("./connects/connect[@pin='A']").set("pad", "K")
        with self.assertRaisesRegex(ValueError, "pad.*net"):
            self.resolve()

    def test_packaged_device_requires_board_placement(self):
        led = next(part for part in self.data["parts"] if part["ref"] == "D1")
        led["board"] = None
        with self.assertRaisesRegex(ValueError, "packaged device.*placement"):
            self.resolve()
