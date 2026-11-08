from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from fusion_mcp.session import Snapshot
from live.e2e_led_driver import preview_state


class PreviewEvidenceTest(unittest.TestCase):
    def test_only_translated_rule_prose_is_ignored(self):
        fixtures = Path(__file__).parent / 'fixtures'
        before = Snapshot(board_xml=(fixtures / 'mini.brd').read_bytes(),
                          sch_xml=(fixtures / 'mini.sch').read_bytes())
        board = ET.fromstring(before.board_xml)
        rules = board.find('.//designrules')
        ET.SubElement(rules, 'description', language='de').text = 'Translated rule prose'
        after = Snapshot(board_xml=ET.tostring(board), sch_xml=before.sch_xml)
        self.assertEqual(preview_state(before), preview_state(after))
        for element, field in [(rules.find('param'), 'value'), (board.find('.//element'), 'x')]:
            old = element.get(field)
            element.set(field, '99')
            after.board_xml = ET.tostring(board)
            self.assertNotEqual(preview_state(before), preview_state(after))
            element.set(field, old)
