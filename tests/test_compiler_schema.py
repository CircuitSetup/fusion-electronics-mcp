"""Shape and fixture checks for the version-1 offline compiler contract."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from fusion_mcp.addin.FusionElectronicsMCP import eagle_import

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "compiler" / "minimal"
PATH_PREFIX = "/compiler-fixtures/minimal/"


def minimal_design() -> dict:
    """Materialize the example's absolute paths in this checkout."""
    data = json.loads((FIXTURE / "design.json").read_bytes())
    for asset in (*data["templates"].values(), *data["libraries"]):
        assert asset["path"].startswith(PATH_PREFIX)
        asset["path"] = str(FIXTURE / asset["path"].removeprefix(PATH_PREFIX))
    return data


class CompilerSchemaTest(unittest.TestCase):
    def setUp(self):
        self.data = minimal_design()

    def parse(self, data):
        self.assertIsNotNone(
            importlib.util.find_spec("fusion_offline.compiler"),
            "The offline compiler model is missing",
        )
        from fusion_offline.compiler.model import parse_design_spec

        return parse_design_spec(data)

    def reject_change(self, path, value, field):
        data = copy.deepcopy(self.data)
        target = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with self.assertRaisesRegex(ValueError, field):
            self.parse(data)

    def test_minimal_design_passes(self):
        self.parse(self.data)

    def test_requires_version_one_and_mm(self):
        for version in (0, 2, True, "1", 1.0):
            with self.subTest(version=version):
                self.reject_change(("version",), version, "version")
        self.reject_change(("units",), "in", "units")

    def test_every_asset_requires_absolute_path_and_full_hex_pin(self):
        for path in (("templates", "schematic"), ("templates", "board"), ("libraries", 0)):
            for value in ("../source.xml", "C:source.xml", "C:/source.xml\x00"):
                with self.subTest(path=path, value=value):
                    self.reject_change((*path, "path"), value, "path")
            for value in ("a" * 63, "g" * 64, 42):
                with self.subTest(path=path, value=value):
                    self.reject_change((*path, "sha256"), value, "sha256")

    def test_identifiers_reject_blank_or_unsupported_characters(self):
        fields = [
            ("libraries", 0, "name"),
            ("parts", 0, "ref"),
            ("parts", 0, "library"),
            ("parts", 0, "deviceset"),
            ("parts", 0, "units", 0, "gate"),
            ("nets", 0, "name"),
            ("nets", 0, "pins", 0, "pin"),
        ]
        for path in fields:
            for value in ("", "a\nb", "A<1"):
                with self.subTest(path=path, value=value):
                    self.reject_change(path, value, path[-1])
        self.reject_change(("parts", 0, "device"), "A<1", "device")
        self.reject_change(("parts", 0, "technology"), "A<1", "technology")
        for sheet in (0, True, "1", 2):
            with self.subTest(sheet=sheet):
                self.reject_change(("parts", 0, "units", 0, "sheet"), sheet, "sheet")

    def test_empty_device_and_technology_select_exact_defaults(self):
        self.assertEqual(self.data["parts"][0]["device"], "")
        self.assertEqual(self.data["parts"][0]["technology"], "")
        self.parse(self.data)

    def test_rejects_duplicate_names_units_and_pins(self):
        for path, rows, label in (
            (("libraries",), self.data["libraries"], "library"),
            (("parts",), self.data["parts"], "ref"),
            (("schematic", "sheets"), self.data["schematic"]["sheets"], "sheet"),
            (("nets",), self.data["nets"], "net"),
            (("parts", 0, "units"), self.data["parts"][0]["units"], "gate"),
            (("nets", 0, "pins"), self.data["nets"][0]["pins"], "pin"),
        ):
            with self.subTest(path=path):
                self.reject_change(path, [*rows, rows[0]], f"duplicate.*{label}")
        duplicate = {**self.data["parts"][0], "ref": self.data["parts"][0]["ref"].lower()}
        self.reject_change(("parts",), [*self.data["parts"], duplicate], "duplicate.*ref")
        pin = self.data["nets"][0]["pins"][0]
        self.reject_change(("nets", 1, "pins"), [*self.data["nets"][1]["pins"], pin], "duplicate.*pin")

    def test_rotations_follow_eagle_unit_and_board_domains(self):
        self.data["parts"][0]["units"][0]["rotation"] = 270
        self.data["parts"][0]["board"]["rotation"] = 12.5
        self.parse(self.data)
        self.reject_change(("parts", 0, "units", 0, "rotation"), 45, "rotation")
        for value in (-1, 360, True, "MR90"):
            with self.subTest(value=value):
                self.reject_change(("parts", 0, "board", "rotation"), value, "rotation")

    def test_numbers_are_finite_and_never_booleans(self):
        for path in (("parts", 0, "units", 0, "x"), ("parts", 0, "board", "y"),
                     ("board", "outline", 0, 0)):
            for value in (True, float("nan"), float("inf"), float("-inf"), 10 ** 400):
                with self.subTest(path=path, value=value):
                    self.reject_change(path, value, "finite")

    def test_explicit_stack_and_outside_placement_permission(self):
        self.data["board"]["copper_layers"] = [1, 2, 303, 304]
        self.data["parts"][0]["board"]["allow_outside"] = True
        self.parse(self.data)
        self.reject_change(("board", "copper_layers"), [1, 1, 304], "duplicate.*layer")
        self.reject_change(("board", "copper_layers"), [304, 1], "copper_layers")
        self.reject_change(("parts", 0, "board", "allow_outside"), 1, "allow_outside")

    def test_pre_route_uses_declared_export_layer_numbering(self):
        net = self.data["nets"][0]
        net["wires"] = [{"x1": 1, "y1": 2, "x2": 3, "y2": 4, "layer": 304, "width": 0.3}]
        net["vias"] = [{"x": 3, "y": 4, "extent": "1-304", "drill": 0.3}]
        self.parse(self.data)
        self.reject_change(("nets", 0, "vias", 0, "extent"), "304-1", "extent")

    def test_budget_values_must_be_positive_and_bounded(self):
        for field, above_limit in (
            ("max_nodes_per_attempt", 1_000_001),
            ("max_nodes_per_connection", 2_000_001),
            ("max_attempts_per_connection", 9),
            ("max_seconds_per_attempt", 61),
            ("max_seconds_per_job", 1801),
            ("max_vias_per_connection", 9),
            ("ripup_rounds", 9),
        ):
            for value in (0, -1, True, above_limit):
                with self.subTest(field=field, value=value):
                    self.reject_change(("routing", "budget"), {field: value}, field)
        self.reject_change(("routing", "budget"), {"max_nodes_per_attempt": 1.5}, "max_nodes_per_attempt")

    def test_routed_net_requires_a_declared_physical_pin(self):
        for part in self.data["parts"]:
            part["board"] = None
        with self.assertRaisesRegex(ValueError, "physical"):
            self.parse(self.data)

    def test_one_physical_pin_defers_pad_count_to_library_resolution(self):
        self.data["nets"][0]["pins"] = self.data["nets"][0]["pins"][:1]
        self.parse(self.data)

    def test_pins_reference_declared_parts_and_placed_gates(self):
        self.reject_change(("nets", 0, "pins", 0, "part"), "MISSING", "part")
        self.reject_change(("nets", 0, "pins", 0, "gate"), "MISSING", "gate")
        self.reject_change(("parts", 0, "library"), "MISSING", "library")

    def test_rejects_unknown_fields_at_each_object_boundary(self):
        for path in (
            (), ("templates",), ("templates", "schematic"), ("libraries", 0),
            ("parts", 0), ("parts", 0, "units", 0), ("parts", 0, "board"),
            ("schematic",), ("schematic", "sheets", 0), ("board",),
            ("nets", 0), ("nets", 0, "pins", 0), ("routing",), ("routing", "budget"),
        ):
            with self.subTest(path=path):
                self.reject_change((*path, "typo"), True, "unknown.*typo")

    def test_reviewed_template_pair_is_pinned_empty_and_flat(self):
        templates = self.data["templates"]
        args = {f"{kind}_{key}": value for kind, asset in templates.items() for key, value in asset.items()}
        raw, signature = eagle_import.read_pair(args)
        self.assertEqual(signature, {"parts": [], "elements": [], "sheets": 1, "contacts": []})
        schematic, board = map(ET.fromstring, raw)
        for root in (schematic, board):
            for section in ("settings", "grid", "layers"):
                self.assertIsNotNone(root.find(f"./drawing/{section}"))
        self.assertEqual(board.find(".//designrules/param[@name='layerSetup']").get("value"), "(1*16)")
        for root in (schematic, board):
            for tag in ("net", "signal", "instance", "via", "module", "moduleinst"):
                self.assertIsNone(root.find(f".//{tag}"), tag)

    def test_fixture_library_pins_match_expected_summary(self):
        asset = self.data["libraries"][0]
        raw = Path(asset["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), asset["sha256"])
        root = ET.fromstring(raw)
        parts = {part["ref"]: part for part in self.data["parts"]}
        contacts = []
        for net in self.data["nets"]:
            for pin in net["pins"]:
                part = parts[pin["part"]]
                device = root.find(
                    f".//deviceset[@name='{part['deviceset']}']/devices/device[@name='{part['device']}']"
                )
                connect = device.find(f"./connects/connect[@gate='{pin['gate']}'][@pin='{pin['pin']}']")
                for pad in connect.get("pad").split():
                    contacts.append([net["name"], pin["part"], pad])
        summary = json.loads((FIXTURE / "expected-summary.json").read_bytes())
        self.assertEqual(summary["parts"], sorted(parts))
        self.assertEqual(summary["elements"], sorted(parts))
        self.assertEqual(summary["sheets"], 1)
        self.assertEqual(summary["contacts"], sorted(contacts))
