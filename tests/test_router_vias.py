import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from fusion_offline import router as R, route_all as RA
from fusion_mcp import server as S
import test_offline


class RouterViaTest(unittest.TestCase):
    def route(self, net_class_drill=None, **kwargs):
        root = ET.fromstring(test_offline.RouterTest.BOARD.replace('y1="2" x2="20" y2="18"', 'y1="-1" x2="20" y2="21"'))
        rules = root.find('.//designrules')
        for name, value in [('msDrill', '0.35mm'), ('rlMinViaOuter', '8mil'), ('mlViaStopLimit', '32mil')]:
            ET.SubElement(rules, 'param', name=name, value=value)
        if net_class_drill is not None:
            classes = ET.SubElement(root.find('.//board'), 'classes')
            ET.SubElement(classes, 'class', number='1', name='power', drill=str(net_class_drill))
            root.find(".//signal[@name='S']").set('class', '1')
        route = R.route(root, 'S', (5.03, 10.07), (1,), (35.11, 10.02), (1,), step=.254, **kwargs)
        self.assertFalse(route.problems)
        return root, route

    def test_board_rules_size_planned_and_written_vias(self):
        root, route = self.route()
        self.assertEqual(len(route.vias), 2)
        self.assertEqual(route.via_drill, .35)
        self.assertAlmostEqual(route.via_diameter, .7564)
        self.assertTrue(route.via_tented)
        RA._commit(root, RA.Conn('test', 'S', (), (), (), ()), route)
        for via in root.findall('.//via'):
            self.assertEqual(float(via.get('drill')), .35)
            self.assertAlmostEqual(float(via.get('diameter')), .7564)
        with patch.object(S, '_only_layers', return_value=('DISPLAY NONE 18;', 'DISPLAY ALL;')), \
                patch.object(S, '_native_layer', side_effect=lambda l: 304 if l == 'bottom' else 1):
            commands = ' '.join(S._route_vias('S', route))
        self.assertIn('CHANGE DRILL 0.35;', commands)
        self.assertIn("VIA 'S' 0.7564", commands)
        self.assertEqual(commands.count('CHANGE STOP OFF (C>'), 2)
        self.assertTrue(commands.endswith('DISPLAY ALL;'))

    def test_explicit_undersized_vias_are_rejected(self):
        for kwargs in [dict(via_drill=.3), dict(via_d=.6), dict(via_d=float('nan'))]:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, 'via'):
                self.route(**kwargs)

    def test_net_class_drill_minimum_is_respected(self):
        _, route = self.route(net_class_drill=.6)
        self.assertEqual(route.via_drill, .6)
        self.assertAlmostEqual(route.via_diameter, 1.0064)
        with self.assertRaisesRegex(ValueError, 'via drill'):
            self.route(net_class_drill=.6, via_drill=.35)
