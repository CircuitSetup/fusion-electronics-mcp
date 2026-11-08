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

    def add(self, board=None):
        after = Snapshot(sch_xml=ET.tostring(self.root), board_xml=board)
        def write(editor, commands, verify, **kwargs):
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
        self.before = Snapshot(sch_xml=ET.tostring(old), board_xml=ET.tostring(board))
        new = copy.deepcopy(elements[0]); new.set('name', 'R9'); elements.append(new)
        return board

    def test_linked_add_preserves_existing_board(self):
        self.assertTrue(self.add(ET.tostring(self.linked_board()))['ok'])

    def test_existing_pcb_move_is_rejected(self):
        board = self.linked_board()
        board.find(".//element[@name='R1']").set('x', '99')
        with self.assertRaisesRegex(ToolError, 'existing board element'):
            self.add(ET.tostring(board))

    def test_add_palette_is_observed_without_pressing_done(self):
        from fusion_mcp import dialogs as D
        info = dict(title='ADD', texts=['Mirror', 'Rotate', 'ADD'], buttons=['Done'],
                    radios=[], checks=[], edits=[], others=1)
        with patch.object(D, 'read', return_value=info), patch.object(D, 'act') as act:
            result = D.handle(D.Window(1, 'ADD', 'QWindowIcon'), forms=[D.FORM_ADD_PALETTE])
        self.assertTrue(result.expected)
        self.assertEqual(act.call_args.args[1], [])
