"""A successful schematic connection must reach the exact linked PCB pads too."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from fusion_mcp import server as S
from fusion_mcp.session import Snapshot, WriteFailed
from mcp.server.mcpserver.exceptions import ToolError


def snapshot(pins=(), pads=(), extra=False):
    root = ET.parse(Path(__file__).parent / 'fixtures' / 'mini.sch').getroot()
    nets = root.find('.//schematic/sheets/sheet/nets')
    nets.clear()
    segment = ET.SubElement(ET.SubElement(nets, 'net', name='LED_K', **{'class': '0'}), 'segment')
    for part, pin in pins:
        ET.SubElement(segment, 'pinref', part=part, gate='G$1', pin=pin)
    if extra:
        segment = ET.SubElement(ET.SubElement(nets, 'net', name='OTHER', **{'class': '0'}), 'segment')
        ET.SubElement(segment, 'pinref', part='R2', gate='G$1', pin='P$1')
    board = ET.parse(Path(__file__).parent / 'fixtures' / 'mini.brd').getroot()
    signals = board.find('.//board/signals'); signals.clear()
    signal = ET.SubElement(signals, 'signal', name='LED_K')
    for ref, pad in pads:
        ET.SubElement(signal, 'contactref', element=ref, pad=pad)
    return Snapshot(sch_xml=ET.tostring(root), board_xml=ET.tostring(board))


class ConnectPinsTest(unittest.TestCase):
    def connect(self, before, after, name='LED_K', allow_merge=False):
        def write(editor, commands, verify, **kwargs):
            ok, detail = verify(before, after)
            if not ok: raise WriteFailed(detail)
            return after, detail
        session = SimpleNamespace(snapshot=lambda *args, **kwargs: before, verified_write=write)
        with patch.object(S, 'session', session), patch.object(S, 'get_net', return_value={}):
            return S.connect_pins(name, ['R1.1', 'R1.2'], allow_merge=allow_merge)

    def test_missing_forward_annotation_cannot_report_success(self):
        before = snapshot()
        after = snapshot([('R1', 'P$1'), ('R1', 'P$2')], [('R1', '1')])
        with self.assertRaisesRegex(ToolError, 'board|PCB'):
            self.connect(before, after)

    def test_disappearing_unrelated_pin_cannot_report_success(self):
        before = snapshot(extra=True)
        after = snapshot([('R1', 'P$1'), ('R1', 'P$2')], [('R1', '1'), ('R1', '2')])
        with self.assertRaisesRegex(ToolError, 'other pins'):
            self.connect(before, after)

    def test_lowercase_name_is_normalized_to_fusion_net_name(self):
        after = snapshot([('R1', 'P$1'), ('R1', 'P$2')], [('R1', '1'), ('R1', '2')])
        self.assertTrue(self.connect(snapshot(), after, 'led_k')['ok'])

    def test_matching_schematic_and_board_are_accepted(self):
        after = snapshot([('R1', 'P$1'), ('R1', 'P$2')], [('R1', '1'), ('R1', '2')])
        self.assertTrue(self.connect(snapshot(), after)['ok'])

    def test_already_connected_still_checks_linked_board(self):
        snap = snapshot([('R1', 'P$1'), ('R1', 'P$2')], [('R1', '1')])
        with self.assertRaisesRegex(ToolError, 'PCB'):
            self.connect(snap, snap)

    def test_linked_board_disappearing_is_not_a_pass(self):
        after = snapshot([('R1', 'P$1'), ('R1', 'P$2')])
        after.board_xml = None
        with self.assertRaisesRegex(ToolError, 'PCB disappeared'):
            self.connect(snapshot(), after)

    def test_standalone_schematic_remains_supported(self):
        before = snapshot(); before.board_xml = None
        after = snapshot([('R1', 'P$1'), ('R1', 'P$2')]); after.board_xml = None
        self.assertTrue(self.connect(before, after)['ok'])

    def test_unrelated_pad_added_on_board_is_rejected(self):
        after = snapshot([('R1', 'P$1'), ('R1', 'P$2')], [('R1', '1'), ('R1', '2'), ('R2', '1')])
        with self.assertRaisesRegex(ToolError, 'PCB'):
            self.connect(snapshot(), after)

    def test_authorized_merge_moves_existing_net_pads_together(self):
        before = snapshot([('R1', 'P$1'), ('R2', 'P$1')], [('R1', '1'), ('R2', '1')])
        sch = ET.fromstring(before.sch_xml)
        sch.find('.//net').set('name', 'OLD_NET')
        before.sch_xml = ET.tostring(sch)
        board = ET.fromstring(before.board_xml)
        board.find('.//signal').set('name', 'OLD_NET')
        before.board_xml = ET.tostring(board)
        after = snapshot([('R1', 'P$1'), ('R1', 'P$2'), ('R2', 'P$1')],
                         [('R1', '1'), ('R1', '2'), ('R2', '1')])
        self.assertTrue(self.connect(before, after, allow_merge=True)['ok'])
