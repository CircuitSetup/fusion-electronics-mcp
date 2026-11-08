import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fusion_mcp.bridge import Bridge, BridgeOpError
from test_server import load_addin


class Collection:
    def __init__(self, items):
        self.items = items
        self.count = len(items)
        self.item_calls = 0
        self.array_calls = 0

    def item(self, i):
        self.item_calls += 1
        return self.items[i]

    def asArray(self):
        self.array_calls += 1
        return self.items[:]

    def itemByName(self, name):
        return next((x for x in self.items if x.name == name), None)


class ProjectLibrariesTest(unittest.TestCase):
    def setUp(self):
        self.a = load_addin()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.a.INFO_DIR = self.temp.name
        self.a.FOLDERS_PATH = str(Path(self.temp.name) / 'folders.json')
        self.root = types.SimpleNamespace(name='Root', isRoot=True, dataFolders=Collection([]))
        self.project = types.SimpleNamespace(id='project-id', name='Project', rootFolder=self.root)
        self.files = Collection([self.file('Design', 'fprj'), self.file('Other', 'step'),
                                 self.file('Library', 'flbr')])
        self.root.dataFiles = self.files
        self.a._app = types.SimpleNamespace(data=types.SimpleNamespace(activeProject=self.project),
                                             activeDocument=None)
        self.a._settle = lambda n=10: None
        self.a._kind_of = lambda d: d.kind
        self.a.op_context = lambda args: {'active_document': self.a._doc_info(self.a._app.activeDocument)}

    def file(self, name, ext):
        return types.SimpleNamespace(id='id-' + name, name=name, fileExtension=ext, versionNumber=1,
                                     parentFolder=self.root, parentProject=self.project)

    def test_listing_pages_all_files_and_returns_direct_open_ids(self):
        first = self.a.op_list_designs({'limit': 2})
        self.assertEqual(first['next_offset'], 2)
        self.assertFalse(first['complete'])
        self.assertEqual(first['libraries'], [])
        self.assertEqual(first['designs'][0]['data_file_id'], 'id-Design')
        second = self.a.op_list_designs({'offset': first['next_offset'], 'limit': 2})
        self.assertTrue(second['complete'])
        self.assertIsNone(second['next_offset'])
        self.assertEqual(second['libraries'][0]['data_file_id'], 'id-Library')
        self.assertEqual(second['project_id'], 'project-id')
        self.assertEqual(self.files.item_calls, 0)
        self.assertEqual(self.files.array_calls, 2)

    def test_slow_listing_returns_progress_instead_of_discarding_it(self):
        with patch.object(self.a.time, 'monotonic', side_effect=[0, 9]):
            page = self.a.op_list_designs({'limit': 100})
        self.assertEqual(page['next_offset'], 1)
        self.assertFalse(page['complete'])
        self.assertEqual(len(page['designs']), 1)

    def test_empty_and_exhausted_pages_are_complete(self):
        for files, offset in (([], 0), (self.files.items, 99)):
            self.root.dataFiles = Collection(files)
            page = self.a.op_list_designs({'offset': offset})
            self.assertTrue(page['complete'])
            self.assertIsNone(page['next_offset'])
            self.assertEqual(page['designs'], [])
            self.assertEqual(page['libraries'], [])

    def test_folder_lookup_is_direct_and_does_not_create_folders(self):
        child = types.SimpleNamespace(name='Parts', dataFiles=self.files, dataFolders=Collection([]))
        self.root.dataFolders = Collection([child])
        page = self.a.op_list_designs({'folder': 'Parts', 'limit': 1})
        self.assertEqual(page['designs'][0]['folder'], 'Parts')
        self.assertEqual(self.root.dataFolders.item_calls, 0)
        with self.assertRaises(self.a.BridgeError) as cm:
            self.a.op_list_designs({'folder': 'Missing'})
        self.assertEqual(cm.exception.code, 'not_found')

    def test_invalid_pagination_and_other_project_are_rejected(self):
        for args in ({'limit': 0}, {'limit': 101}, {'offset': -1}, {'offset': True}, {'limit': 1.5},
                     {'project': 'Other'}):
            with self.subTest(args=args), self.assertRaises(self.a.BridgeError):
                self.a.op_list_designs(args)

    def prepare_creation(self, saved=True):
        self.assertIn('new_library', self.a.OPS)
        old = types.SimpleNamespace(name='Keep', kind='board', isModified=True)
        new = types.SimpleNamespace(name='Untitled', kind='library', isSaved=False,
                                    isModified=False, isActive=True, dataFile=None)
        docs = [old]
        self.a._docs = lambda: docs[:]
        self.a._app.activeDocument = old
        self.executed = []
        def execute():
            self.executed.append(True)
            docs.append(new)
            self.a._app.activeDocument = new
        command = types.SimpleNamespace(execute=execute, controlDefinition=types.SimpleNamespace(isEnabled=True))
        self.a._ui = types.SimpleNamespace(commandDefinitions=types.SimpleNamespace(
            itemById=lambda cid: command if cid == 'NewElectronLbrDocumentCommand' else None))
        def save(name, folder, description, tag):
            self.assertIs(folder, self.root)
            new.name = name
            new.isSaved = saved
            if saved:
                new.dataFile = self.file(name, 'flbr')
            return saved
        new.saveAs = save
        return new, old

    def test_create_and_save_library_remembers_identity_without_touching_existing_design(self):
        new, old = self.prepare_creation()
        result = self.a.OPS['new_library']({'name': 'Parts'})
        self.assertEqual(result['active_document']['kind'], 'library')
        self.assertEqual(result['active_document']['data_file_id'], 'id-Parts')
        self.assertTrue(new.isSaved)
        self.assertTrue(old.isModified)
        self.assertEqual(self.a._folders()['project-id|flbr|Parts']['file_id'], 'id-Parts')
        self.assertEqual(self.files.array_calls, 0)

    def test_create_unsaved_library_and_reject_folder_without_name(self):
        new, old = self.prepare_creation()
        with self.assertRaises(self.a.BridgeError):
            self.a.OPS['new_library']({'folder': 'Parts'})
        self.assertEqual(self.executed, [])
        self.a.OPS['new_library']({})
        self.assertFalse(new.isSaved)
        self.assertTrue(old.isModified)

    def test_invalid_names_do_not_create_documents(self):
        self.prepare_creation()
        for name in ('', '  ', 'Parts/Other', 'Parts\\Other', 'Parts\nOther', 123):
            with self.subTest(name=name), self.assertRaises(self.a.BridgeError):
                self.a.OPS['new_library']({'name': name})
        self.assertEqual(self.executed, [])

    def test_missing_or_disabled_command_does_not_create_documents(self):
        self.prepare_creation()
        for command in (None, types.SimpleNamespace(controlDefinition=types.SimpleNamespace(isEnabled=False))):
            self.a._ui.commandDefinitions.itemById = lambda cid: command
            with self.assertRaises(self.a.BridgeError) as cm:
                self.a.OPS['new_library']({'name': 'Parts'})
            self.assertEqual(cm.exception.code, 'unsupported')
        self.assertEqual(self.executed, [])

    def test_save_exception_reports_uncertainty_without_retrying(self):
        new, _ = self.prepare_creation()
        new.saveAs = lambda *args: (_ for _ in ()).throw(RuntimeError('cloud'))
        with self.assertRaises(self.a.BridgeError) as cm:
            self.a.OPS['new_library']({'name': 'Parts'})
        self.assertEqual(cm.exception.code, 'state_unknown')
        self.assertEqual(len(self.executed), 1)

    def test_failed_save_keeps_new_library_and_reports_failure(self):
        new, old = self.prepare_creation(saved=False)
        with self.assertRaises(self.a.BridgeError) as cm:
            self.a.OPS['new_library']({'name': 'Parts'})
        self.assertEqual(cm.exception.code, 'save_failed')
        self.assertIs(self.a._app.activeDocument, new)
        self.assertTrue(old.isModified)

    def test_wrong_new_document_is_never_saved(self):
        new, _ = self.prepare_creation()
        new.kind = 'other'
        new.saveAs = lambda *args: self.fail('saved a non-library')
        with self.assertRaises(self.a.BridgeError) as cm:
            self.a.OPS['new_library']({'name': 'Parts'})
        self.assertEqual(cm.exception.code, 'state_unknown')

    def test_claimed_save_must_match_project_name_kind_and_folder(self):
        for field, value in (('name', 'Other'), ('fileExtension', 'fprj'),
                             ('parentProject', types.SimpleNamespace(id='other')),
                             ('parentFolder', types.SimpleNamespace(isRoot=False, name='Other', parentFolder=self.root))):
            with self.subTest(field=field):
                new, _ = self.prepare_creation()
                save = new.saveAs
                def wrong_save(*args):
                    ok = save(*args)
                    setattr(new.dataFile, field, value)
                    return ok
                new.saveAs = wrong_save
                with self.assertRaises(self.a.BridgeError) as cm:
                    self.a.OPS['new_library']({'name': 'Parts'})
                self.assertEqual(cm.exception.code, 'state_unknown')


class LibraryTransportTest(unittest.TestCase):
    def test_server_registers_creation_and_passes_pagination(self):
        from fusion_mcp import server as s
        with patch.object(s.session.bridge, 'call', return_value={}) as call:
            s.new_library('Parts', 'Libraries')
            call.assert_called_once_with('new_library', {'name': 'Parts', 'folder': 'Libraries'}, timeout=120)
        with patch.object(s.session.bridge, 'call', return_value={}) as call:
            s.list_designs('Libraries', limit=2, offset=3)
            call.assert_called_once_with('list_designs', {'folder': 'Libraries', 'limit': 2, 'offset': 3}, timeout=120)

    def test_creation_requires_addin_before_dispatch(self):
        bridge = Bridge(watch_dialogs=False)
        with patch.object(bridge, 'transport', return_value='builtin'), patch.object(
                bridge, '_info', side_effect=AssertionError('contacted Fusion')):
            with self.assertRaises(BridgeOpError) as cm:
                bridge._call('new_library', {'name': 'Parts'})
        self.assertEqual(cm.exception.code, 'needs_addin')

    def test_old_addin_cannot_silently_ignore_pagination(self):
        bridge = Bridge(watch_dialogs=False)
        info = {'protocol': 1, 'addin_version': '0.14.17'}
        with patch.object(bridge, 'transport', return_value='addin'), patch.object(bridge, '_info', return_value=info):
            with self.assertRaises(BridgeOpError) as cm:
                bridge._call('list_designs', {'limit': 2, 'offset': 0})
        self.assertEqual(cm.exception.code, 'needs_addin')


if __name__ == '__main__':
    unittest.main()
