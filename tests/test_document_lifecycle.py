import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import load_addin


class Document:
    def __init__(self, name, kind, modified=False):
        self.name = name
        self.kind = kind
        self.isModified = modified
        self.isActive = False
        self.closed = False
        self.dataFile = None

    def activate(self):
        self.isActive = True

    def close(self, save):
        self.closed = True
        return True


class Product(types.SimpleNamespace):
    @property
    def parentDocument(self):
        raise RuntimeError("InternalValidationError: adapter")


class IncompleteDesign(Product):
    def __getattribute__(self, name):
        if name in ("schematic", "board") and name not in self.__dict__:
            raise RuntimeError(f"InternalValidationError: {name}Link")
        return super().__getattribute__(name)


def family():
    design = Document("Design A", "design")
    schematic = Document("Logic", "schematic", modified=True)
    board = Document("Layout", "board")
    design.ecadDesign = Product(name=design.name)
    schematic.schematic = Product(parentDesign=design.ecadDesign)
    board.board = Product(parentDesign=design.ecadDesign)
    schematic.schematic.linkedBoard = board.board
    board.board.linkedSchematic = schematic.schematic
    design.ecadDesign.schematic = schematic.schematic
    design.ecadDesign.board = board.board
    return design, schematic, board


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.addin = load_addin()
        self.addin._kind_of = lambda d: d.kind
        self.addin._settle = lambda n=10: None
        self.addin.op_context = lambda args: {}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addin.INFO_DIR = temp.name
        self.addin.FOLDERS_PATH = str(Path(temp.name) / "folders.json")

    def test_close_checks_entire_linked_family_and_preserves_unrelated_tab(self):
        design, schematic, board = family()
        unrelated = Document("Design A", "board")
        docs = [design, schematic, board, unrelated]
        self.addin._docs = lambda: [d for d in docs if not d.closed]
        with self.assertRaises(self.addin.BridgeError) as cm:
            self.addin.op_close_design({"name": "Layout"})
        self.assertEqual(cm.exception.code, "unsaved")
        self.assertEqual(self.addin._docs(), docs)
        self.addin.op_close_design({"name": "Layout", "discard_changes": True})
        self.assertEqual(self.addin._docs(), [unrelated])

    def test_close_preserves_changes_made_while_settling_previous_tab(self):
        design, schematic, board = family()
        schematic.isModified = False
        self.addin._docs = lambda: [doc for doc in (design, schematic, board) if not doc.closed]
        self.addin._settle = lambda n: setattr(schematic, "isModified", True)
        with self.assertRaises(self.addin.BridgeError) as cm:
            self.addin.op_close_design({"name": "Design A"})
        self.assertEqual(cm.exception.code, "unsaved")
        self.assertIn("Logic", str(cm.exception))
        self.assertTrue(board.closed)
        self.assertFalse(schematic.closed)
        self.assertFalse(design.closed)

    def test_closed_created_family_cannot_break_next_family_lookup(self):
        class CreatedParent(Document):
            @property
            def isValid(self):
                return not self.closed

            @property
            def dataFile(self):
                if self.closed:
                    raise RuntimeError("invalid document wrapper")
                return None

            @dataFile.setter
            def dataFile(self, value):
                pass

        original = family()
        created_design, schematic, board = family()
        created = CreatedParent("Disposable", "design")
        created.ecadDesign = created_design.ecadDesign
        created.ecadDesign.name = created.name
        docs = [*original, created, schematic, board]
        self.addin._docs = lambda: [doc for doc in docs if not doc.closed]
        self.addin._created_families = [(created, schematic, board)]
        self.addin.op_close_design({"name": "Disposable", "discard_changes": True})
        self.assertEqual(self.addin._docs(), list(original))
        self.assertEqual(len(list(self.addin._family_keys(self.addin._docs()))), 3)

    def test_activate_prefers_actual_family(self):
        design, schematic, board = family()
        unrelated = Document("Design A", "board")
        self.addin._docs = lambda: [unrelated, design, schematic, board]
        self.addin._app = types.SimpleNamespace(activeDocument=design)
        def activate_board():
            board.isActive = True
            self.addin._app.activeDocument = board
        board.activate = activate_board
        self.addin._product_kind = lambda: "board" if board.isActive or unrelated.isActive else "design"
        self.addin.op_activate({"kind": "board"})
        self.assertTrue(board.isActive)
        self.assertFalse(unrelated.isActive)

    def test_activate_resolves_generic_fusion_wrappers_without_parent_document(self):
        design, schematic, board = family()
        products = {"schematic": schematic.schematic, "board": board.board}
        products["schematic"].linkedBoard = None
        del schematic.schematic
        del board.board
        def cast(doc):
            return types.SimpleNamespace(**{doc.kind: products[doc.kind]})

        self.addin._docs = lambda: [design, schematic, board]
        self.addin._app = types.SimpleNamespace(activeDocument=schematic)
        def activate_board():
            board.isActive = True
            self.addin._app.activeDocument = board
        board.activate = activate_board
        self.addin._product_kind = lambda: "board" if board.isActive else "schematic"
        electron = self.addin.adsk.electron
        with patch.object(electron, "SchematicDocument", types.SimpleNamespace(cast=cast), create=True), \
                patch.object(electron, "BoardDocument", types.SimpleNamespace(cast=cast), create=True):
            self.addin.op_activate({"kind": "board"})
        self.assertTrue(board.isActive)

    def test_saved_document_references_resolve_family_with_unequal_products(self):
        design, schematic, board = family()
        stale_board = Document(board.name, "board")
        board.dataFile = types.SimpleNamespace(id="urn:board", versionId="urn:board?version=2")
        stale_board.dataFile = types.SimpleNamespace(id="urn:board", versionId="urn:board?version=1")
        schematic.schematic.parentDesign = Product(name="Design A")
        schematic.schematic.linkedBoard = Product(name=board.name)
        board.board.parentDesign = Product(name="Design A", different_adapter=True)
        refs = [types.SimpleNamespace(referencedDocument=schematic, dataFile=None),
                types.SimpleNamespace(referencedDocument=None, dataFile=board.dataFile)]
        design.documentReferences = types.SimpleNamespace(count=2, item=refs.__getitem__)
        self.addin._docs = lambda: [design, schematic, stale_board, board]
        self.addin._app = types.SimpleNamespace(activeDocument=schematic)
        self.addin._product_kind = lambda: self.addin._app.activeDocument.kind
        board.activate = lambda: setattr(self.addin._app, "activeDocument", board)

        self.addin.op_activate({"kind": "board"})
        self.assertIs(self.addin._app.activeDocument, board)

        refs.append(types.SimpleNamespace(referencedDocument=None, dataFile=stale_board.dataFile))
        design.documentReferences.count = len(refs)
        self.addin._app.activeDocument = schematic
        with self.assertRaises(self.addin.BridgeError) as cm:
            self.addin.op_activate({"kind": "board"})
        self.assertEqual(cm.exception.code, "ambiguous")
        self.assertIs(self.addin._app.activeDocument, schematic)

    def test_document_wrapper_identity_requires_same_cloud_revision(self):
        left = Document("Design A", "design")
        right = Document("Design A", "design")
        left.dataFile = types.SimpleNamespace(id="urn:parent", versionId="urn:parent?version=2")
        right.dataFile = types.SimpleNamespace(id="urn:parent", versionId="urn:parent?version=1")
        self.assertFalse(self.addin._same_document(left, right))
        right.dataFile.versionId = left.dataFile.versionId
        self.assertTrue(self.addin._same_document(left, right))

    def test_activate_verifies_target_document_even_if_editor_matches(self):
        target = Document("Target", "board")
        other = Document("Other", "board")
        self.addin._app = types.SimpleNamespace(activeDocument=other)
        self.addin._docs = lambda: [target, other]
        self.addin._product_kind = lambda: "board"
        with self.assertRaises(self.addin.BridgeError) as cm:
            self.addin.op_activate({"kind": "board", "name": "Target"})
        self.assertEqual(cm.exception.code, "activate_failed")

    def test_already_saved_document_skips_save_and_wait(self):
        doc = Document("Saved", "design")
        doc.isSaved = True
        doc.dataFile = types.SimpleNamespace(versionNumber=3)
        doc.save = lambda desc: self.fail("called save on an unmodified saved document")
        self.addin._app = types.SimpleNamespace(activeDocument=doc)
        result = self.addin.op_save({})
        self.assertTrue(result["ok"])
        self.assertEqual((result["version_before"], result["version_after"]), (3, 3))

    def test_known_library_reopens_by_file_id(self):
        root = types.SimpleNamespace(isRoot=True)
        project = types.SimpleNamespace(id="project", name="Proj", rootFolder=root)
        file = types.SimpleNamespace(id="urn:library", parentProject=project, parentFolder=root,
                                     name="Parts", fileExtension="flbr")
        doc = Document("Parts", "library")
        doc.dataFile = file
        open_docs = [doc]
        opened = []
        lookups = []
        self.addin._docs = lambda: open_docs
        def find_file(file_id):
            lookups.append(file_id)
            return file
        self.addin._app = types.SimpleNamespace(
            data=types.SimpleNamespace(activeProject=project, findFileById=find_file),
            documents=types.SimpleNamespace(open=opened.append))
        self.addin._project_designs = lambda **kwargs: self.fail("enumerated project for a known library")
        args = {"name": "Parts", "kind": "library", "folder": ""}
        self.assertTrue(self.addin.op_open_design(args)["already_open"])
        open_docs.clear()
        self.addin.op_open_design(args)
        self.assertEqual(lookups, ["urn:library"])
        self.assertEqual(opened, [file])

    def test_explicit_library_id_must_match_project_and_folder(self):
        root = types.SimpleNamespace(isRoot=True)
        project = types.SimpleNamespace(id="project", name="Proj", rootFolder=root)
        other = types.SimpleNamespace(id="other", name="Proj")
        file = types.SimpleNamespace(id="urn:library", parentProject=other, parentFolder=root,
                                     name="Parts", fileExtension="flbr")
        opened = []
        self.addin._docs = lambda: []
        self.addin._app = types.SimpleNamespace(
            data=types.SimpleNamespace(activeProject=project, findFileById=lambda file_id: file),
            documents=types.SimpleNamespace(open=opened.append))
        self.addin._project_designs = lambda **kwargs: self.fail("enumerated project for an explicit library ID")
        args = {"name": "Parts", "kind": "library", "data_file_id": "urn:library", "folder": ""}
        with self.assertRaises(self.addin.BridgeError):
            self.addin.op_open_design(args)
        file.parentProject = project
        with self.assertRaises(self.addin.BridgeError):
            self.addin.op_open_design(dict(args, folder="Wrong folder"))
        self.assertEqual(opened, [])
        self.addin.op_open_design(args)
        self.assertEqual(opened, [file])

    def test_explicit_id_rejects_open_library_in_another_project(self):
        root = types.SimpleNamespace(isRoot=True)
        project = types.SimpleNamespace(id="project", name="Proj", rootFolder=root)
        other = types.SimpleNamespace(id="other", name="Proj")
        doc = Document("Parts", "library")
        doc.dataFile = types.SimpleNamespace(id="urn:library", parentProject=other, parentFolder=root,
                                             name="Parts", fileExtension="flbr")
        self.addin._docs = lambda: [doc]
        self.addin._app = types.SimpleNamespace(data=types.SimpleNamespace(activeProject=project,
                                                                          findFileById=lambda _: doc.dataFile))
        args = {"name": "Parts", "kind": "library", "data_file_id": "urn:library", "folder": ""}
        with self.assertRaises(self.addin.BridgeError):
            self.addin.op_open_design(args)
        self.assertFalse(doc.isActive)

    def test_push_uses_visible_fusion_3d_pcb_control(self):
        _, _, board = family()
        pcb = Document("3D PCB", "other")
        app = types.SimpleNamespace(activeDocument=board)
        docs = [board]
        self.addin._app = app
        self.addin._docs = lambda: docs[:]
        self.addin._product_kind = lambda: "board"
        self.addin._pcb3d_product = lambda doc: object() if doc is pcb else None
        self.addin.op_pcb3d_bodies = lambda args: {"document": pcb.name}

        def push():
            docs.append(pcb)
            app.activeDocument = pcb

        definition = types.SimpleNamespace(id="Switch3dPcbDocCmd", name="3D PCB", execute=push,
                                           controlDefinition=types.SimpleNamespace(isEnabled=True))
        control = types.SimpleNamespace(isVisible=True, commandDefinition=definition)
        panel = types.SimpleNamespace(isVisible=True, controls=types.SimpleNamespace(count=1, item=lambda i: control))
        self.addin._ui = types.SimpleNamespace(activeWorkspace=types.SimpleNamespace(
            toolbarPanels=types.SimpleNamespace(count=1, item=lambda i: panel)))
        self.assertEqual(self.addin.op_push_3d({}), {"document": "3D PCB"})
        self.assertEqual(self.addin._pcb3d_link(), (board, pcb))

    def test_saved_board_reference_recovers_only_its_exact_3d_pcb(self):
        _, _, board = family()
        pcb = Document("3D PCB", "other")
        unrelated = Document("Mechanical", "other")
        old_pcb = Document("3D PCB", "other")
        pcb.dataFile = types.SimpleNamespace(id="urn:3d", versionId="urn:3d?version=2")
        old_pcb.dataFile = types.SimpleNamespace(id="urn:3d", versionId="urn:3d?version=1")
        board.dataFile = types.SimpleNamespace(childReferences=types.SimpleNamespace(
            count=1, item=lambda i: pcb.dataFile))
        self.addin._docs = lambda: [board, unrelated, old_pcb, pcb]
        self.addin._app = types.SimpleNamespace(activeDocument=board)
        self.addin._pcb3d_product = lambda doc: object()
        self.assertEqual(self.addin._pcb3d_link(), (board, pcb))
        self.addin._app.activeDocument = unrelated
        self.assertEqual(self.addin._pcb3d_link(), (None, None))

    def test_pull_uses_only_enabled_fixed_command_for_associated_pcb(self):
        _, _, board = family()
        pcb = Document("3D PCB", "other")
        unrelated = Document("Mechanical", "other")
        app = types.SimpleNamespace(activeDocument=board)
        self.addin._app = app
        self.addin._docs = lambda: [board, pcb, unrelated]
        self.addin._pcb3d_links = {"board": (board, pcb)}
        pcb.activate = lambda: setattr(app, "activeDocument", pcb)
        self.addin.op_pcb3d_bodies = lambda args: {"document": app.activeDocument.name}
        executed = []
        control = types.SimpleNamespace(isEnabled=True)
        command = types.SimpleNamespace(name="Pull", controlDefinition=control,
                                        execute=lambda: executed.append(app.activeDocument))
        def lookup(command_id):
            self.assertEqual(command_id, "PullPCB3DCmd")
            return command
        self.addin._ui = types.SimpleNamespace(
            activeWorkspace=types.SimpleNamespace(toolbarPanels=types.SimpleNamespace(count=0)),
            commandDefinitions=types.SimpleNamespace(itemById=lookup))
        self.assertEqual(self.addin.op_push_3d({"command": "unrelated"}), {"document": "3D PCB"})
        self.assertEqual(executed, [pcb])
        control.isEnabled = False
        with self.assertRaises(self.addin.BridgeError) as cm:
            self.addin.op_push_3d({})
        self.assertEqual(cm.exception.code, "unsupported")
        self.assertEqual(executed, [pcb])

    def test_push_never_pulls_into_unrelated_mechanical_document(self):
        _, _, board = family()
        other = Document("Other", "other")
        other.products = types.SimpleNamespace(count=1, item=lambda i: product)
        product = types.SimpleNamespace(rootComponent=types.SimpleNamespace(
            allOccurrences=types.SimpleNamespace(count=1)))
        docs = [board, other]
        self.addin._docs = lambda: docs
        self.addin._product_kind = lambda: "board"
        self.addin._app = types.SimpleNamespace(activeDocument=board, documents=types.SimpleNamespace(
            count=2, item=docs.__getitem__), userInterface=types.SimpleNamespace(
            commandDefinitions=types.SimpleNamespace(
                itemById=lambda cid: self.fail("looked up Pull for an unrelated mechanical document"))))
        self.addin._ui = types.SimpleNamespace(activeWorkspace=types.SimpleNamespace(
            toolbarPanels=types.SimpleNamespace(count=0)))
        fusion = types.SimpleNamespace(Design=types.SimpleNamespace(cast=lambda p: p))
        with patch.dict(sys.modules, {"adsk.fusion": fusion}), patch.object(self.addin.adsk, "fusion", fusion, create=True):
            with self.assertRaises(self.addin.BridgeError) as cm:
                self.addin.op_push_3d({})
        self.assertEqual(cm.exception.code, "unsupported")
        self.assertFalse(other.isActive)

    def test_new_design_creates_children_from_parent_workspace(self):
        design, schematic, board = family()
        app = types.SimpleNamespace(activeDocument=None)
        self.addin._app = app
        docs = []
        self.addin._docs = lambda: docs[:]
        design.ecadDesign = IncompleteDesign(name=design.name)
        schematic.schematic.parentDesign = design.ecadDesign
        board.board.parentDesign = design.ecadDesign
        design.activate = lambda: setattr(app, "activeDocument", design)
        def create_design():
            docs.append(design)
            design.activate()
        calls = []

        def collection(items):
            return types.SimpleNamespace(count=len(items), item=items.__getitem__)

        def child_control(product, label, child_doc):
            def execute():
                self.assertIs(app.activeDocument, design)
                calls.append(label)
                setattr(design.ecadDesign, product, getattr(child_doc, product))
                docs.append(child_doc)
                app.activeDocument = child_doc
            cd = types.SimpleNamespace(id=product, name=label, execute=execute,
                                       controlDefinition=types.SimpleNamespace(isEnabled=True))
            return types.SimpleNamespace(isVisible=True, commandDefinition=cd)

        panel = types.SimpleNamespace(isVisible=True, controls=collection([
            child_control("schematic", "New Schematic", schematic),
            child_control("board", "New PCB", board),
        ]))
        self.addin._ui = types.SimpleNamespace(
            commandDefinitions=types.SimpleNamespace(itemById=lambda _: types.SimpleNamespace(execute=create_design)),
            activeWorkspace=types.SimpleNamespace(toolbarPanels=collection([panel])),
        )
        self.addin.op_new_design({})
        self.assertEqual(calls, ["New Schematic", "New PCB"])

    def test_failed_creation_keeps_empty_parent_available_for_cleanup(self):
        design = Document("Disposable", "design")
        design.ecadDesign = IncompleteDesign(name=design.name)
        app = types.SimpleNamespace(activeDocument=None)
        self.addin._app = app
        self.addin._docs = lambda: [design] if app.activeDocument is not None and not design.closed else []
        create = types.SimpleNamespace(execute=lambda: setattr(app, "activeDocument", design))
        self.addin._ui = types.SimpleNamespace(
            commandDefinitions=types.SimpleNamespace(itemById=lambda _: create),
            activeWorkspace=types.SimpleNamespace(toolbarPanels=types.SimpleNamespace(count=0)),
        )
        with self.assertRaises(self.addin.BridgeError) as cm:
            self.addin.op_new_design({})
        self.assertEqual(cm.exception.code, "unsupported")
        self.assertEqual(self.addin._docs(), [design])
        self.addin.op_close_design({"name": "Disposable", "discard_changes": True})
        self.assertTrue(design.closed)

    def test_new_design_requires_reciprocal_links_before_save(self):
        design, schematic, board = family()
        self.addin._app = types.SimpleNamespace(activeDocument=None)
        self.addin._docs = lambda: [] if self.addin._app.activeDocument is None else [design, schematic, board]
        def create():
            self.addin._app.activeDocument = design
        self.addin._ui = types.SimpleNamespace(commandDefinitions=types.SimpleNamespace(
            itemById=lambda name: types.SimpleNamespace(execute=create)))
        board.board.linkedSchematic = None
        with self.assertRaises(self.addin.BridgeError) as cm:
            self.addin.op_new_design({})
        self.assertEqual(cm.exception.code, "unsupported")
        self.assertEqual(self.addin._created_families, [[design]])


if __name__ == "__main__":
    unittest.main()
