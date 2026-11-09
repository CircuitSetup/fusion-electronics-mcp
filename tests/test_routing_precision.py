import copy
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from fusion_mcp import commands as C, server as S
from fusion_mcp.session import Snapshot
from fusion_offline import router as R
import test_offline


class RoutingPrecisionTest(unittest.TestCase):
    def test_command_numbers_preserve_eight_decimal_coordinates(self):
        for value, expected in [(29.05011875, '29.05011875'), (-29.05011875, '-29.05011875'),
                                (235.5175, '235.5175'), (123456.12345678, '123456.12345678'),
                                (0.00000006, '0.00000006'), (-0.000000001, '0'), (2.0, '2')]:
            with self.subTest(value=value):
                self.assertEqual(C.n(value), expected)

    def test_smd_and_through_pad_centers_keep_native_precision(self):
        for kind, rotation, expected, layers in [
                ('smd', 'R0', (14.605, 29.05011875), (1,)),
                ('smd', 'MR0', (14.605, 29.05011875), (16,)),
                ('smd', 'R90', (27.105, 41.55011875), (1,)),
                ('pad', 'R0', (14.605, 29.05011875), (1, 2, 15, 16)),
                ('pad', 'R180', (14.605, 54.05011875), (1, 2, 15, 16))]:
            attributes = 'layer="1" dx="0.3" dy="1"' if kind == 'smd' else 'drill="0.3"'
            root = ET.fromstring(f'''<eagle><drawing><board>
                <designrules><param name="layerSetup" value="(1*2*15*16)"/></designrules>
                <libraries><library name="L"><packages><package name="P">
                <{kind} name="21" x="0" y="-12.5" {attributes}/>
                </package></packages></library></libraries>
                <elements><element name="U1" library="L" package="P" x="14.605"
                y="41.55011875" rot="{rotation}"/></elements>
                <signals><signal name="GPIO13"><contactref element="U1" pad="21"/></signal></signals>
                </board></drawing></eagle>''')
            before = ET.tostring(root)
            with self.subTest(kind=kind, rotation=rotation):
                x, y, actual_layers, net = R.pad_target(root, 'U1', '21')
                self.assertAlmostEqual(x, expected[0], places=12)
                self.assertAlmostEqual(y, expected[1], places=12)
                self.assertEqual((actual_layers, net), (layers, 'GPIO13'))
                self.assertEqual(ET.tostring(root), before)

    def test_route_remaining_preserves_pad_centers_through_command_output(self):
        root = ET.fromstring(test_offline.RouterTest.BOARD)
        root.find('.//element[@name="A"]').set('y', '10.07011875')
        root.find('.//element[@name="B"]').set('y', '10.02011875')
        ET.SubElement(root.find('.//signal[@name="S"]'), 'wire', layer='19', width='0',
                      x1='5.03', y1='10.07011875', x2='35.11', y2='10.02011875')
        plans = []

        def render(plan, *args, **kwargs):
            plans.append(copy.deepcopy(plan))
            return {'path': 'precision-plan.png'}

        with patch.object(S, '_planning_snapshot', return_value=(Snapshot(board_xml=ET.tostring(root)), None, {})), \
                patch.object(S.session, 'context', return_value={}), \
                patch('fusion_offline.render.render', side_effect=render):
            result = S.route_remaining.__wrapped__(nets=['S'], allow_vias=False)
        self.assertEqual(result['planned'], 1)
        self.assertFalse(result['written'])
        wires = [w for w in plans[0].findall('.//signal[@name="S"]/wire') if w.get('layer') != '19']
        endpoints = {(float(w.get(x)), float(w.get(y))) for w in wires
                     for x, y in (('x1', 'y1'), ('x2', 'y2'))}
        self.assertIn((5.03, 10.07011875), endpoints)
        self.assertIn((35.11, 10.02011875), endpoints)
        commands = ' '.join(C.add_trace('S', int(w.get('layer')), float(w.get('width')),
            [(float(w.get('x1')), float(w.get('y1'))), (float(w.get('x2')), float(w.get('y2')))]) for w in wires)
        self.assertIn('(5.03 10.07011875)', commands)
        self.assertIn('(35.11 10.02011875)', commands)


if __name__ == '__main__':
    unittest.main()
