import asyncio
from contextlib import closing
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fusion_mcp import server as S
from mcp.server.mcpserver.exceptions import ToolError


# Columns used from Fusion's schema 114; rows are synthetic, not user data.
SCHEMA = """
CREATE TABLE Libraries (id INTEGER PRIMARY KEY, name TEXT, description TEXT,
    libtype TEXT, path TEXT, urn TEXT, version INTEGER, isused INTEGER, downloaded INTEGER);
CREATE TABLE DeviceSets (id INTEGER PRIMARY KEY, name TEXT, description TEXT, library INTEGER);
CREATE TABLE Packages (id INTEGER PRIMARY KEY, name TEXT, description TEXT);
CREATE TABLE Devices (id INTEGER PRIMARY KEY, variantname TEXT, deviceset INTEGER, package INTEGER);
CREATE TABLE Technologies (id INTEGER PRIMARY KEY, technology TEXT, fullname TEXT, device INTEGER);
CREATE TABLE DeviceAttributes (id INTEGER PRIMARY KEY, technology INTEGER, name TEXT, value TEXT);
INSERT INTO Libraries VALUES
    (1,'Logic','Digital parts','ltOnlineId','/libs/logic.lbr','urn:logic',4,1,1),
    (2,'Connectors','USB connectors','ltWipId','/libs/connectors.lbr','urn:conn',2,0,1),
    (3,'Remote USB','USB Type C connectors','ltOnlineId',NULL,'urn:remote',1,0,0),
    (4,'Logic','Custom logic','ltLocalPath','/custom/logic.lbr','',-1,1,1);
INSERT INTO DeviceSets VALUES
    (1,'74*850','<b>8-bit</b> &amp; comparator',1),
    (2,'USB_C','USB type C connector',2),
    (3,'GND','Ground symbol',1),
    (4,'74AS850N','Custom comparator',4);
INSERT INTO Packages VALUES (1,'DIP16','Through hole'),(2,'SMD_16','Surface mount');
INSERT INTO Devices VALUES (1,'N',1,1),(2,'_16P',2,2),(3,'',3,NULL),(4,'',4,1);
INSERT INTO Technologies VALUES
    (1,'AS','74AS850N',1),(2,'ALS','74ALS850N',1),
    (3,'','USB_C_16P',2),(4,'','74AS850N',4);
INSERT INTO DeviceAttributes VALUES
    (1,1,'MF','Acme'),(2,1,'MPN','ABC123'),(3,3,'JLCPCB','C12345'),
    (4,3,'NOTE','10% tolerance_1 München');
"""


class FusionLibrarySearchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'libraries.db'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(SCHEMA)

    def search(self, query, **kwargs):
        return S.search_fusion_libraries(query, database_path=str(self.db), **kwargs)

    def test_searches_description_package_attributes_and_technology_fullname(self):
        result = self.search('8-bit acme DIP16 abc123')
        self.assertEqual(result['total'], 1)
        part = result['parts'][0]
        self.assertEqual(part['device'], '74AS850N')
        self.assertEqual(part['deviceset'], '74*850')
        self.assertEqual(part['variant'], 'N')
        self.assertEqual(part['technology'], 'AS')
        self.assertEqual(part['library_name'], 'Logic')
        self.assertEqual(part['description'], '8-bit & comparator')
        self.assertEqual(part['attributes']['MPN'], 'ABC123')

    def test_inactive_parts_and_unindexed_library_metadata_are_visible(self):
        result = self.search('usb')
        self.assertEqual(result['total'], 1)
        self.assertFalse(result['parts'][0]['active'])
        self.assertTrue(result['parts'][0]['downloaded'])
        self.assertEqual(result['coverage']['libraries'], 4)
        self.assertEqual(result['coverage']['libraries_with_devices'], 3)
        self.assertEqual(result['coverage']['libraries_without_devices'], 1)
        self.assertEqual(result['unindexed_libraries'][0]['library_name'], 'Remote USB')
        self.assertFalse(result['unindexed_libraries'][0]['downloaded'])
        self.assertTrue(result['warnings'])

    def test_duplicate_library_names_are_not_collapsed(self):
        parts = self.search('74AS850N')['parts']
        self.assertEqual(len(parts), 2)
        self.assertEqual({p['library_id'] for p in parts}, {1, 4})
        self.assertEqual({p['library_path'] for p in parts}, {'/libs/logic.lbr', '/custom/logic.lbr'})
        self.assertTrue(all(p['ambiguous_library_name'] for p in parts))

    def test_pagination_is_stable_and_exact_names_rank_first(self):
        first = self.search('74AS850N', limit=1)
        second = self.search('74AS850N', limit=1, offset=1)
        self.assertEqual(first['total'], 2)
        self.assertEqual(first['next_offset'], 1)
        self.assertIsNone(second['next_offset'])
        self.assertNotEqual(first['parts'][0]['library_id'], second['parts'][0]['library_id'])
        self.assertEqual(self.search('74AS850N', offset=50)['parts'], [])

    def test_literal_search_and_unicode_and_exact_library_filter(self):
        self.assertEqual(self.search('10% TOLERANCE_1 MÜNCHEN')['total'], 1)
        self.assertEqual(self.search("' OR 1=1 --")['total'], 0)
        self.assertEqual(self.search('%missing')['total'], 0)
        self.assertEqual(self.search('usb', library_name='connectors')['total'], 1)
        self.assertEqual(self.search('usb', library_name='con')['total'], 0)

    def test_symbols_without_packages_or_technologies_are_searchable(self):
        part = self.search('ground')['parts'][0]
        self.assertEqual(part['device'], 'GND')
        self.assertIsNone(part['package'])
        self.assertEqual(part['attributes'], {})

    def test_validates_query_and_pagination(self):
        for query, kwargs in [('', {}), ('   ', {}), ('x', {'limit': 0}),
                              ('x', {'limit': 101}), ('x', {'offset': -1})]:
            with self.subTest(query=query, kwargs=kwargs), self.assertRaises(ToolError):
                self.search(query, **kwargs)

    def test_missing_or_unsupported_database_is_an_error_not_no_matches(self):
        self.db.unlink()
        with self.assertRaisesRegex(ToolError, 'Library Manager|index|database'):
            self.search('usb')
        self.assertFalse(self.db.exists())
        self.db.write_bytes(b'not sqlite')
        with self.assertRaisesRegex(ToolError, 'index|database'):
            self.search('usb')

    def test_incompatible_schema_reports_an_actionable_error(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('DROP TABLE Technologies')
        with self.assertRaisesRegex(ToolError, 'schema changed'):
            self.search('usb')

    def test_database_connection_rejects_writes(self):
        def try_write(conn, *args):
            conn.execute('DELETE FROM Libraries')
        with patch('fusion_mcp.fusion_libraries._search', side_effect=try_write):
            with self.assertRaisesRegex(ToolError, 'readonly'):
                self.search('usb')
        self.assertEqual(self.search('usb')['coverage']['libraries'], 4)

    def test_complete_words_rank_above_incidental_substring_matches(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript("""
                INSERT INTO DeviceSets VALUES (5,'USB_A','USB connector type A',2);
                INSERT INTO Devices VALUES (5,'',5,2);
                INSERT INTO DeviceSets VALUES (6,'A_RES_510K','510k resistor',1);
                INSERT INTO DeviceSets VALUES (7,'Z_RES_10K','10k resistor',1);
                INSERT INTO Devices VALUES (6,'',6,2),(7,'',7,2);
            """)
        self.assertEqual(self.search('USB C')['parts'][0]['device'], 'USB_C_16P')
        self.assertEqual(self.search('10k resistor')['parts'][0]['device'], 'Z_RES_10K')

    def test_reads_current_wal_and_never_changes_library_records(self):
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute("UPDATE DeviceAttributes SET value='FRESH123' WHERE id=2")
            writer.commit()
            before = list(writer.iterdump())
            self.assertEqual(self.search('fresh123')['total'], 1)
            self.assertEqual(list(writer.iterdump()), before)

    def test_discovery_uses_single_account_and_rejects_ambiguity(self):
        root = Path(self.temp.name) / 'Autodesk/Autodesk Fusion 360/Electron'
        first = root / 'account-a/lbr/libraries.db'
        first.parent.mkdir(parents=True)
        first.write_bytes(self.db.read_bytes())
        with patch.dict(os.environ, {'LOCALAPPDATA': self.temp.name}), patch('sys.platform', 'win32'):
            self.assertEqual(S.search_fusion_libraries('usb')['total'], 1)
            second = root / 'account-b/lbr/libraries.db'
            second.parent.mkdir(parents=True)
            second.write_bytes(self.db.read_bytes())
            with self.assertRaisesRegex(ToolError, 'multiple|Multiple'):
                S.search_fusion_libraries('usb')

    def test_missing_index_discovery_and_mac_location(self):
        with patch('pathlib.Path.home', return_value=Path(self.temp.name)), patch('sys.platform', 'darwin'):
            with self.assertRaisesRegex(ToolError, 'Library Manager'):
                S.search_fusion_libraries('usb')
            db = Path(self.temp.name) / 'Library/Application Support/Autodesk/Autodesk Fusion 360/Electron/user/lbr/libraries.db'
            db.parent.mkdir(parents=True)
            db.write_bytes(self.db.read_bytes())
            self.assertEqual(S.search_fusion_libraries('usb')['total'], 1)

    def test_registered_as_read_only_tool(self):
        tool = next(t for t in asyncio.run(S.mcp.list_tools()) if t.name == 'search_fusion_libraries')
        self.assertTrue(tool.annotations.read_only_hint)


if __name__ == '__main__':
    unittest.main()
