"""Trust-boundary tests for the canonical compiler model."""

from dataclasses import FrozenInstanceError
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from test_compiler_schema import minimal_design


class CompilerModelTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.spec_path = self.root / "design.json"

    def load(self, data):
        from fusion_offline.compiler.model import load_design_spec

        payload = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.spec_path.write_bytes(payload)
        return load_design_spec(str(self.spec_path), sha256(payload).hexdigest())

    def test_loads_immutable_records_with_pinned_xml(self):
        from fusion_offline.compiler.model import DesignSpec

        spec = self.load(minimal_design())
        self.assertIsInstance(spec, DesignSpec)
        self.assertEqual(spec.name, "led-switch")
        self.assertEqual(spec.templates["schematic"].data[:5], b"<?xml")
        self.assertEqual(spec.libraries[0].name, "PROJECT")
        with self.assertRaises(FrozenInstanceError):
            spec.name = "changed"
        with self.assertRaises(TypeError):
            spec.parts[0]["units"][0]["x"] = 0
        self.assertIsInstance(spec.board["outline"][0], tuple)

    def test_rejects_unknown_fields_and_nonfinite_values(self):
        data = minimal_design()
        data["parts"][0]["units"][0]["other"] = 1
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.load(data)
        del data["parts"][0]["units"][0]["other"]
        for invalid in (float("nan"), float("inf"), True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                data["parts"][0]["units"][0]["x"] = invalid
                self.load(data)

    def test_rejects_positive_dimensions_that_round_to_zero(self):
        data = minimal_design()
        data["board"]["holes"].append({"x": 3, "y": 5, "drill": 0.00001})
        with self.assertRaisesRegex(ValueError, "drill.*rounds to zero"):
            self.load(data)

    def test_rejects_text_outside_the_xml_character_set(self):
        data = minimal_design()
        for value in ("\ud800", "\x00", "\x0b", "\ufffe", "\uffff"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "value.*XML"):
                data["parts"][0]["value"] = value
                self.load(data)

    def test_preserves_valid_unicode_and_xml_whitespace(self):
        data = minimal_design()
        value = "\t\n\r\u00b5\U0001f680"
        data["parts"][0]["value"] = value
        spec = self.load(data)
        self.assertEqual(next(part for part in spec.parts if part["ref"] == "J1")["value"], value)
        self.assertEqual(len(spec.digest), 64)

    def test_rejects_positive_via_diameter_that_rounds_to_auto(self):
        data = minimal_design()
        data["nets"][0]["vias"] = [
            {"x": 3, "y": 5, "drill": 0.25, "extent": "1-16", "diameter": 0.00001}
        ]
        with self.assertRaisesRegex(ValueError, "diameter.*rounds to zero"):
            self.load(data)

    def test_rejects_duplicate_json_keys(self):
        with self.assertRaisesRegex(ValueError, "duplicate.*version"):
            self.load(b'{"version":1,"version":1}')

    def test_rejects_unsafe_xml_even_when_the_hash_matches(self):
        data = minimal_design()
        xml_path = self.root / "template.sch"
        for payload in (
            b'<!DOCTYPE eagle [<!ENTITY x "example">]><eagle>&x;</eagle>',
            b'<!DOCTYPE eagle [<!ENTITY x SYSTEM "file:///secret">]><eagle>&x;</eagle>',
            b'<!DOCTYPE eagle SYSTEM "https://example.invalid/external"><eagle/>',
            '<?xml version="1.0" encoding="UTF-16"?><eagle/>'.encode("utf-16-le"),
            b'<?xml version="1.0" encoding="ISO-8859-1"?><eagle/>',
        ):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "XML"):
                xml_path.write_bytes(payload)
                data["templates"]["schematic"] = {
                    "path": str(xml_path), "sha256": sha256(payload).hexdigest(),
                }
                self.load(data)

    def test_pinned_read_checks_size_and_exact_bytes(self):
        from fusion_offline.compiler.paths import read_pinned_file

        payload = b"0123456789"
        pin = sha256(payload).hexdigest()
        self.spec_path.write_bytes(payload)
        self.assertEqual(read_pinned_file(str(self.spec_path), pin.upper(), 10), payload)
        with self.assertRaisesRegex(ValueError, "size"):
            read_pinned_file(str(self.spec_path), pin, 9)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            read_pinned_file(str(self.spec_path), "0" * 64, 10)

    def test_rejects_unc_before_accessing_the_path(self):
        from fusion_offline.compiler.paths import read_pinned_file

        for path in ("//example.invalid/share/spec.json", r"\\example.invalid\share\spec.json"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "local"):
                read_pinned_file(path, "0" * 64, 1)

    def test_rejects_symlink_file_and_ancestor(self):
        from fusion_offline.compiler.paths import read_pinned_file

        self.spec_path.write_bytes(b"{}")
        pin = sha256(b"{}").hexdigest()
        direct = self.root / "linked.json"
        folder = self.root / "linked-directory"
        try:
            direct.symlink_to(self.spec_path)
            folder.symlink_to(self.root, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        for path in (direct, folder / self.spec_path.name):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "symlink"):
                read_pinned_file(str(path), pin, 2)

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_rejects_windows_junction_ancestor(self):
        from subprocess import run

        from fusion_offline.compiler.paths import read_pinned_file

        self.spec_path.write_bytes(b"{}")
        target = self.root / "junction"
        run(["cmd", "/d", "/c", "mklink", "/J", str(target), str(self.root)],
            check=True, capture_output=True)
        self.addCleanup(target.rmdir)
        with self.assertRaisesRegex(ValueError, "symlink|junction"):
            read_pinned_file(str(target / self.spec_path.name), sha256(b"{}").hexdigest(), 2)

    def test_atomic_file_and_directory_publish_only_completed_data(self):
        from fusion_offline.compiler.paths import atomic_directory, atomic_write

        atomic_write(self.spec_path, b"original")
        atomic_write(self.spec_path, b"replacement")
        self.assertEqual(self.spec_path.read_bytes(), b"replacement")
        target = self.root / "candidate"
        target.mkdir()
        with self.assertRaisesRegex(RuntimeError, "stop"):
            with atomic_directory(target) as staging:
                self.assertEqual(staging.parent, self.root)
                atomic_write(staging / "artifact.json", b"partial")
                raise RuntimeError("stop")
        self.assertEqual(list(target.iterdir()), [])
        with atomic_directory(target) as staging:
            atomic_write(staging / "artifact.json", b"complete")
            self.assertFalse((target / "artifact.json").exists())
        with self.assertRaisesRegex(ValueError, "nonempty"):
            with atomic_directory(target):
                self.fail("Nonempty target was accepted")
        self.assertEqual((target / "artifact.json").read_bytes(), b"complete")
        self.assertEqual(set(self.root.iterdir()), {self.spec_path, target})
