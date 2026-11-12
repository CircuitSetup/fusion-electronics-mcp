"""Canonical compiler identities across equivalent input representations."""

from hashlib import sha256
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from test_compiler_schema import minimal_design


class CompilerDeterminismTest(unittest.TestCase):
    def test_normalizes_portable_artifact_paths(self):
        from fusion_offline.compiler.paths import normalize_artifact_path

        self.assertEqual(normalize_artifact_path(r"routing-jobs\abc\routes.json"),
                         "routing-jobs/abc/routes.json")
        for path in ("", ".", "../board.brd", "a/../b", "/board.brd", r"C:\board.brd",
                     "C:board.brd", "//server/share/a"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                normalize_artifact_path(path)

    def test_rejects_windows_device_names_and_ambiguous_artifact_components(self):
        from fusion_offline.compiler.paths import normalize_artifact_path

        self.assertEqual(normalize_artifact_path("a//./board.brd"), "a/board.brd")
        for path in ("a./board.brd", "a /board.brd", "board.brd.", "board.brd ",
                     "a/NUL.json", "con /board.brd", "COM1.txt", "LPT\u00b2.json", "CONOUT$"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                normalize_artifact_path(path)

    def test_canonical_json_has_exact_utf8_bytes(self):
        from fusion_offline.compiler.canonical import canonical_digest, canonical_json_bytes

        value = {"z": [2, 1], "a": "µ"}
        expected = b'{"a":"\xc2\xb5","z":[2,1]}'
        self.assertEqual(canonical_json_bytes(value), expected)
        self.assertEqual(canonical_digest(value), sha256(expected).hexdigest())
        with self.assertRaises(ValueError):
            canonical_json_bytes({"bad": float("nan")})

    def test_mm_normalization_has_no_negative_zero(self):
        from fusion_offline.compiler.canonical import canonical_json_bytes, normalize_mm

        self.assertEqual(normalize_mm(1.23456), 1.2346)
        self.assertEqual(canonical_json_bytes(normalize_mm(-0.00001)), b"0.0")
        for invalid in (True, float("inf"), float("nan")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_mm(invalid)

    def test_equivalent_designs_have_identical_identity(self):
        from fusion_offline.compiler.canonical import canonical_json_bytes
        from fusion_offline.compiler.model import load_design_spec

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            design_path = root / "design.json"

            def load(data, **json_options):
                payload = json.dumps(data, **json_options).encode()
                design_path.write_bytes(payload)
                return load_design_spec(str(design_path), sha256(payload).hexdigest())

            data = minimal_design()
            data["parts"][0]["units"].append(
                {"gate": "A", "sheet": 1, "x": 7.123456, "y": -0.00001, "rotation": 0}
            )
            data["libraries"].append({**data["libraries"][0], "name": "OTHER"})
            board = data["board"]
            board["net_classes"] = [
                {"number": 1, "name": "second", "width": 0.123456, "drill": 0},
                {"number": 0, "name": "default", "width": 0, "drill": 0},
            ]
            for part in data["parts"][:2]:
                board["placements"].append({"ref": part["ref"], **part["board"]})
                part["board"] = None
            data["nets"][0]["wires"] = [
                {"x1": 4, "y1": 4, "x2": 3, "y2": 3, "layer": 1, "width": 0.2},
                {"x1": 3, "y1": 3, "x2": 2, "y2": 2, "layer": 1, "width": 0.2},
            ]
            first = load(data, indent=2)
            for rows in (data["parts"], data["parts"][0]["units"], data["nets"],
                         data["libraries"], board["net_classes"], board["placements"]):
                rows.reverse()
            for net in data["nets"]:
                net["pins"].reverse()
            relocated = root / "relocated"
            relocated.mkdir()
            for row in (*data["templates"].values(), *data["libraries"]):
                source = Path(row["path"])
                destination = relocated / source.name
                shutil.copyfile(source, destination)
                path = destination.as_posix() if destination.drive else str(destination)
                row["path"] = path
            second = load(data, sort_keys=True, separators=(",", ":"))

            first_bytes = canonical_json_bytes(first)
            self.assertEqual(first_bytes, canonical_json_bytes(second))
            self.assertEqual(first.digest, second.digest)
            self.assertEqual(first.digest, sha256(first_bytes).hexdigest())
            self.assertNotIn(b"relocated", canonical_json_bytes(second))
            self.assertNotIn(b'"path"', first_bytes)
            self.assertEqual([row["ref"] for row in second.parts], ["D1", "J1", "Q1", "R1"])
            self.assertEqual([row.name for row in second.libraries], ["OTHER", "PROJECT"])
            self.assertEqual([row["name"] for row in second.nets], ["CTRL", "GND", "LED_A", "LED_K", "VCC"])
            vcc = second.nets[-1]
            self.assertEqual([row["part"] for row in vcc["pins"]], ["J1", "R1"])
            self.assertEqual([row["x1"] for row in vcc["wires"]], [4.0, 3.0])
            units = second.parts[1]["units"]
            self.assertEqual([row["gate"] for row in units], ["A", "G$1"])
            self.assertEqual(units[0]["x"], 7.1235)
            self.assertEqual(second.board["outline"], ((0, 0), (40, 0), (40, 30), (0, 30)))
            self.assertEqual([row["number"] for row in second.board["net_classes"]], [0, 1])
            self.assertEqual([row["ref"] for row in second.board["placements"]], ["J1", "R1"])

            board["outline"].reverse()
            self.assertNotEqual(load(data).digest, second.digest)
