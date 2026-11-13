"""Datasheet-backed parts, grouped pins and multiple library symbol units."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fusion_mcp import library as L, server as S
from fusion_offline import symbols
from test_server import load_addin

FP = '(footprint "TEST4" ' + ' '.join(
    f'(pad "{i}" smd rect (at {i} 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))'
    for i in range(1, 5)) + ')'


class DatasheetPartTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.fp = self.base / 'test.kicad_mod'
        self.fp.write_text(FP)
        patch = mock.patch.object(S, 'library', L.Library(str(self.base / 'parts')))
        patch.start()
        self.addCleanup(patch.stop)

    def create(self, **options):
        return S.create_library_part(str(self.fp), 'test-four', 'TEST4', 'U', 'TEST4',
                                     pin_names={'1': 'IN', '2': 'OUT', '3': 'VDD', '4': 'GND'}, **options)

    def test_grouped_units_and_provenance_round_trip(self):
        result = self.create(pin_groups={'Signal': ['IN', 'OUT'], 'Power': ['VDD', 'GND']},
                             pin_units={'A': ['IN', 'OUT'], 'P': ['VDD', 'GND']},
                             pin_sides={'IN': 'left', 'OUT': 'right'},
                             datasheet={'source': 'https://example.com/test.pdf', 'revision': 'Rev B', 'pages': [3, 7]})
        part = L.Library(str(self.base / 'parts')).get('test-four')
        self.assertEqual(part.data['metadata']['datasheet']['pages'], [3, 7])
        self.assertFalse(part.data['metadata']['verified'])
        import jsonschema
        schema = json.loads((Path(__file__).parents[1]/'library/schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(part.data, schema)
        self.assertEqual(result['units'], ['A', 'P'])
        script = L.build_script(part)
        load_addin().validate(script)
        self.assertIn("ADD 'TEST4_A' 'A'", script)
        self.assertIn("ADD 'TEST4_P' 'P'", script)
        self.assertIn("CONNECT 'A.IN' '1';", script)
        self.assertIn("CONNECT 'P.GND' '4';", script)
        self.assertIn("ATTRIBUTE DATASHEET_REVISION 'Rev B';", script)
        self.assertEqual(script.count(".dev'"), 1)
        with self.assertRaisesRegex(ValueError, 'multi-unit'):
            symbols.from_part_json(part.data)

    def test_group_spacing_matches_offline_geometry(self):
        result = self.create(pin_groups={'Signal': ['IN', 'OUT'], 'Power': ['VDD', 'GND']},
                             pin_sides={n: 'left' for n in ['IN', 'OUT', 'VDD', 'GND']})
        part = S.library.get('test-four')
        geo = symbols.from_part_json(part.data)
        self.assertAlmostEqual(geo.pins['OUT'].y - geo.pins['VDD'].y, 5.08)
        self.assertEqual(result['units'], ['G$1'])
        script = L.build_script(part)
        self.assertIn("PIN 'VDD' pas short R0 (-7.62 -2.54);", script)

    def test_reject_bad_layout_before_saving(self):
        for options in [
            {'pin_units': {'A': ['IN', 'OUT'], 'P': ['VDD']}},
            {'pin_groups': {'A': ['IN', 'OUT'], 'P': ['VDD', 'GND', 'IN']}},
            {'pin_units': {'a': ['IN', 'OUT'], 'A': ['VDD', 'GND']}},
            {'pin_units': {'A;RUN': ['IN', 'OUT', 'VDD', 'GND']}},
            {'pin_units': {'A': [], 'P': ['IN', 'OUT', 'VDD', 'GND']}},
            {'pin_sides': {'TYPO': 'left'}},
            {'pin_sides': {'IN': 'top'}},
            {'datasheet': {'source': 'x', 'revision': 'B', 'pages': [0]}},
            {'datasheet': {'source': 'x', 'revision': 'B', 'pages': [True]}},
            {'directions': {'IN': 'pas;RUN'}},
            {'directions': {'TYPO': 'in'}},
        ]:
            with self.subTest(options=options), self.assertRaises(S.ToolError):
                self.create(**options)
            self.assertFalse((self.base/'parts/test-four.json').exists())

    def test_local_datasheet_hash_and_missing_file(self):
        pdf = self.base/'part.pdf'
        pdf.write_bytes(b'%PDF-test')
        self.create(datasheet={'source': str(pdf), 'revision': 'B', 'pages': [1]})
        import hashlib
        self.assertEqual(S.library.get('test-four').data['metadata']['datasheet']['sha256'],
                         hashlib.sha256(pdf.read_bytes()).hexdigest())
        with self.assertRaises(S.ToolError):
            self.create(overwrite=True, datasheet={'source': str(self.base/'missing.pdf'), 'revision': 'B', 'pages': [1]})
        self.assertEqual(S.library.get('test-four').data['metadata']['datasheet']['revision'], 'B')

    def test_legacy_part_stays_single_gate(self):
        result = self.create()
        part = S.library.get('test-four')
        self.assertEqual(result['units'], ['G$1'])
        self.assertNotIn('datasheet', part.data['metadata'])
        self.assertIn("CONNECT 'G$1.GND' '4';", L.build_script(part))

    def test_partial_pin_names_keep_unused_pins_and_nc_in_the_library(self):
        result = S.create_library_part(str(self.fp), 'test-four', 'TEST4', 'U', 'TEST4',
                                       pin_names={'1': 'IN', '3': 'NC'}, directions={'NC': 'nc'})
        self.assertEqual(result['pins'], 4)
        script = L.build_script(S.library.get('test-four'))
        self.assertIn("PIN 'NC' nc short", script)
        for name, pad in [('IN', '1'), ('2', '2'), ('NC', '3'), ('4', '4')]:
            self.assertIn(f"CONNECT 'G$1.{name}' '{pad}';", script)

    def test_nc_pins_cannot_be_combined_into_one_connection(self):
        with self.assertRaisesRegex(S.ToolError, 'NC.*separate'):
            S.create_library_part(str(self.fp), 'test-four', 'TEST4', 'U', 'TEST4',
                                  pin_names={'3': 'NC', '4': 'NC'}, directions={'NC': 'nc'})

    def test_default_pin_name_cannot_merge_with_an_explicit_name(self):
        with self.assertRaisesRegex(S.ToolError, 'Default pin name.*conflicts'):
            S.create_library_part(str(self.fp), 'test-four', 'TEST4', 'U', 'TEST4', pin_names={'1': '2'})

    def test_insert_refuses_missing_physical_pin_before_native_write(self):
        self.create()
        S.library.get('test-four').data['symbol']['pins'].pop()
        with mock.patch.object(S, 'session') as session:
            with self.assertRaisesRegex(S.ToolError, 'Unmapped electrical pads.*4'):
                S.insert_library_part('test-four')
            session.activate.assert_not_called()
            session.run_script.assert_not_called()

    def test_block_layout_refuses_multi_unit_geometry(self):
        from fusion_mcp.sch_blocks import BlockSchematic
        from types import SimpleNamespace
        block = object.__new__(BlockSchematic)
        with mock.patch.object(block, 'pad_nets') as pad_nets:
            with self.assertRaisesRegex(ValueError, 'multi-unit'):
                block.layout({'U1': SimpleNamespace(gates=2)}, {})
            pad_nets.assert_not_called()

    def test_insert_checks_gate_qualified_connections(self):
        self.create(pin_units={'A': ['IN', 'OUT'], 'P': ['VDD', 'GND']})
        xml = '''<eagle><drawing><library><packages><package name="TEST4">
        <smd name="1"/><smd name="2"/><smd name="3"/><smd name="4"/>
        </package></packages><devicesets><deviceset name="TEST4"><gates>
        <gate name="A" symbol="TEST4_A"/><gate name="P" symbol="TEST4_P"/>
        </gates><devices><device name="" package="TEST4"><connects>
        <connect gate="A" pin="IN" pad="1"/><connect gate="A" pin="OUT" pad="2"/>
        <connect gate="P" pin="VDD" pad="3"/><connect gate="P" pin="GND" pad="4"/>
        </connects></device></devices></deviceset></devicesets></library></drawing></eagle>'''
        for text, ok in [(xml, True), (xml.replace('gate="P" pin="GND"', 'gate="A" pin="GND"'), False)]:
            with self.subTest(ok=ok), mock.patch.object(S, 'session') as session:
                session.export.side_effect = [b'<eagle><drawing><library/></drawing></eagle>', text.encode()]
                if ok:
                    self.assertTrue(S.insert_library_part('test-four')['ok'])
                else:
                    with self.assertRaisesRegex(S.ToolError, 'pin-pad'):
                        S.insert_library_part('test-four')


if __name__ == '__main__':
    unittest.main()
