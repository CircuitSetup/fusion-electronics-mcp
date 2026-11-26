from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from test_compiler_schema import minimal_design


class NativeExportTest(unittest.TestCase):
    def test_out_of_sync_edits_publish_neither_archive(self):
        from fusion_offline.compiler.compile import compile_candidate
        from fusion_offline.compiler.export import export_edited_pair

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        raw = json.dumps(minimal_design()).encode()
        spec = root / "design.json"
        spec.write_bytes(raw)
        candidate = compile_candidate(spec, sha256(raw).hexdigest(), root / "compiled")
        sources = {}
        for kind, suffix in (("schematic", ".sch"), ("board", ".brd")):
            sources[kind] = root / f"design.f{suffix[1:]}"
            with ZipFile(sources[kind], "w") as archive:
                archive.writestr("Manifest.dat", b"template metadata")
                archive.writestr(f"{kind}/electron.BlobParts/ExtFile.test{suffix}", candidate.read_artifact(kind))

        destination = root / "edit"
        board = ET.fromstring(candidate.read_artifact("board"))
        contact = board.find(".//signals/signal/contactref")
        contact.set("pad", "WRONG")
        edited = destination.with_suffix(".brd")
        edited.write_bytes(ET.tostring(board, encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "contact mappings differ"):
            export_edited_pair(sources["schematic"], sources["board"], destination, edited_board=edited)
        self.assertFalse(destination.exists())

        written = export_edited_pair(sources["schematic"], sources["board"], destination)
        self.assertEqual({path.name for path in written.values()}, {"design.fsch", "design.fbrd"})
        self.assertTrue(all(path.is_file() for path in written.values()))
