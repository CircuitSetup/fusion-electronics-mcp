"""Repeated ADD may ask to refresh an already embedded library."""
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from fusion_mcp import server as S
from fusion_mcp.session import Snapshot, WriteFailed
from mcp.server.mcpserver.exceptions import ToolError


class AddPartTest(unittest.TestCase):
    def setUp(self):
        fixtures = Path(__file__).parent / 'fixtures'
        self.before = Snapshot(sch_xml=(fixtures / 'mini.sch').read_bytes(), board_xml=None)
        self.root = ET.fromstring(self.before.sch_xml)
        parts = self.root.find('.//parts')
        new = copy.deepcopy(parts.find("part[@name='R1']"))
        new.set('name', 'R9'); parts.append(new)
        ET.SubElement(self.root.find('.//instances'), 'instance', part='R9', gate='G$1', x='0', y='0')

    def add(self, board=None):
        after = Snapshot(sch_xml=ET.tostring(self.root), board_xml=board)
        def write(editor, commands, verify, **kwargs):
            if editor == 'board':
                from fusion_offline import part_placement as P
                root = ET.fromstring(after.board_xml)
                plan = P.place_existing(root, 'R9', P.default_origin(root))
                P.apply(root, plan)
                placed = Snapshot(sch_xml=after.sch_xml, board_xml=ET.tostring(root))
                ok, detail = verify(after, placed)
                if not ok: raise WriteFailed(detail)
                return placed, detail
            self.answers = kwargs.get('answers', [])
            ok, detail = verify(self.before, after)
            if not ok: raise WriteFailed(detail)
            return after, detail
        session = SimpleNamespace(snapshot=lambda *a, **kw: self.before, verified_write=write)
        with patch.object(S, 'session', session), patch.object(S, 'get_part', return_value={}):
            return S.add_part('RES_0402_10K', 'TESTLIB', 'R9', 0, 0)

    def test_same_definition_refresh_can_add_part(self):
        self.assertTrue(self.add()['ok'])
        self.assertIn('Yes', [answer for _, answer in self.answers])

    def test_changed_existing_pin_definition_is_rejected(self):
        self.root.find('.//symbols/symbol/pin').set('x', '123')
        with self.assertRaisesRegex(ToolError, 'existing'):
            self.add()

    def test_changed_existing_part_value_is_rejected(self):
        self.root.find(".//parts/part[@name='R1']").set('value', '999')
        with self.assertRaisesRegex(ToolError, 'existing'):
            self.add()

    def test_missing_linked_board_is_rejected(self):
        self.before.board_xml = (Path(__file__).parent / 'fixtures/mini.brd').read_bytes()
        with self.assertRaisesRegex(ToolError, 'board'):
            self.add()

    def linked_board(self):
        board = ET.parse(Path(__file__).parent / 'fixtures/mini.brd').getroot()
        parts = self.root.find('.//parts')
        for part in list(parts):
            if part.get('name') not in ('R1', 'R9'): parts.remove(part)
        for nets in self.root.findall('.//sheet/nets'): nets.clear()
        self.root.find('.//library').append(copy.deepcopy(board.find('.//library/packages')))
        elements = board.find('.//elements')
        for el in list(elements):
            if el.get('name') != 'R1': elements.remove(el)
        board.find('.//signals').clear()
        old = copy.deepcopy(self.root)
        old.find('.//parts').remove(old.find(".//part[@name='R9']"))
        old.find('.//instances').remove(old.find(".//instance[@part='R9']"))
        self.before = Snapshot(sch_xml=ET.tostring(old), board_xml=ET.tostring(board))
        new = copy.deepcopy(elements[0]); new.set('name', 'R9'); elements.append(new)
        return board

    def test_linked_add_preserves_existing_board(self):
        self.assertTrue(self.add(ET.tostring(self.linked_board()))['ok'])

    def test_linked_add_plans_an_occupied_placement_and_missing_name(self):
        board = self.linked_board()
        after_sch = ET.tostring(self.root)
        board.find('.//elements').remove(board.find(".//element[@name='R9']"))
        ET.SubElement(board.find('./drawing'), 'grid', distance='50', unitdist='mil')
        self.before.board_xml = ET.tostring(board)
        before_sch = self.before.sch_xml
        snapshots = []

        def write(editor, commands, verify, **kwargs):
            before = snapshots[-1] if snapshots else self.before
            if editor == 'schematic':
                self.assertIn("'R9'", commands)
                element = copy.deepcopy(board.find(".//element[@name='R1']"))
                element.set('name', 'R9')
                board.find('.//elements').append(element)
            else:
                self.assertIn('CHANGE FONT PROPORTIONAL;', commands)
                P.apply(board, recorded[-1])
            after = Snapshot(sch_xml=after_sch, board_xml=ET.tostring(board))
            ok, detail = verify(before, after)
            self.assertTrue(ok, detail)
            snapshots.append(after)
            return after, detail

        from fusion_offline import part_placement as P
        recorded = []
        planner = P.place_existing

        def capture(*args, **kwargs):
            result = planner(*args, **kwargs)
            recorded.append(result)
            return result

        session = SimpleNamespace(snapshot=lambda *a, **kw: snapshots[-1] if snapshots else self.before,
                                  verified_write=write)
        with patch.object(S, 'session', session), patch.object(S, 'get_part', return_value={}), patch.object(P, 'place_existing', capture):
            result = S.add_part('RES_0402_10K', 'TESTLIB', 'R9', 0, 0, board_x_mm=5, board_y_mm=5)
        self.assertTrue(result['ok'])
        self.assertEqual(self.before.sch_xml, before_sch)
        self.assertEqual(len(snapshots), 2)
        self.assertNotEqual(result['placement']['at'], [5, 5])
        self.assertEqual(result['placement']['label']['layer'], '25')

    def test_existing_pcb_move_is_rejected(self):
        board = self.linked_board()
        board.find(".//element[@name='R1']").set('x', '99')
        with self.assertRaisesRegex(ToolError, 'existing board element'):
            self.add(ET.tostring(board))

    def test_batch_reserves_snapped_positions_and_explicit_names(self):
        from fusion_offline import part_placement as P

        board = self.linked_board()
        board.find('.//elements').remove(board.find(".//element[@name='R9']"))
        ET.SubElement(board.find('./drawing'), 'grid', distance='50', unitdist='mil')
        schematic = ET.fromstring(self.before.sch_xml)
        for instance in list(schematic.findall(".//instances/instance")):
            if instance.get("part") != "R1":
                schematic.find(".//instances").remove(instance)
        ref = None
        plan = None

        def snapshot(*args, **kwargs):
            return Snapshot(sch_xml=ET.tostring(schematic), board_xml=ET.tostring(board))

        def write(editor, commands, verify, **kwargs):
            nonlocal ref, plan
            before = snapshot()
            if editor == 'schematic':
                import re
                ref = re.search(r"@TESTLIB' '([^']+)'", commands)[1]
                part = copy.deepcopy(schematic.find(".//part[@name='R1']"))
                part.set('name', ref)
                schematic.find('.//parts').append(part)
                ET.SubElement(schematic.find('.//instances'), 'instance', part=ref, gate='G$1', x='0', y='0')
                element = copy.deepcopy(board.find(".//element[@name='R1']"))
                element.set('name', ref)
                board.find('.//elements').append(element)
                plan = P.place_existing(copy.deepcopy(board), ref, (5, 5))
            else:
                P.apply(board, plan)
            after = snapshot()
            ok, detail = verify(before, after)
            self.assertTrue(ok, detail)
            return after, detail

        session = SimpleNamespace(snapshot=snapshot, verified_write=write)
        row = dict(device='RES_0402_10K', library_name='TESTLIB', board_x_mm=5, board_y_mm=5)
        with patch.object(S, 'session', session), patch.object(S, 'get_part', side_effect=lambda ref: {'ref': ref}):
            result = S.add_parts([row, dict(row, ref='R2')])
            retried = S.add_part(**row, ref='R2')
        self.assertTrue(result['ok'], result)
        self.assertEqual([item['part']['ref'] for item in result['parts']], ['R3', 'R2'])
        self.assertEqual(len({tuple(item['placement']['at']) for item in result['parts']}), 2)
        self.assertEqual(retried['ref'], 'R2')
        self.assertIn('already added', retried['detail'])

    def test_add_palette_is_observed_without_pressing_done(self):
        from fusion_mcp import dialogs as D
        info = dict(title='ADD', texts=['Mirror', 'Rotate', 'ADD'], buttons=['Done'],
                    radios=[], checks=[], edits=[], others=1)
        with patch.object(D, 'read', return_value=info), patch.object(D, 'act') as act:
            result = D.handle(D.Window(1, 'ADD', 'QWindowIcon'), forms=[D.FORM_ADD_PALETTE])
        self.assertTrue(result.expected)
        self.assertEqual(act.call_args.args[1], [])
