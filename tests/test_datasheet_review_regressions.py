import unittest
import xml.etree.ElementTree as ET
from fusion_offline import requirements as R, router as RT, route_all as RA
from test_design_requirements import fixture, req


class ReviewRegressions(unittest.TestCase):
    def test_routing_clearance_includes_via_copper(self):
        root, spec, evidence = fixture()
        for net in ('VIN', 'GND'):
            ET.SubElement(root.find(f'.//signal[@name="{net}"]'), 'via', x='20', y='10',
                          diameter='.6', drill='.3', extent='1-16')
        spec['requirements'] = [req('routing', net='VIN', layers=[1,2,15,16], clearance_mm=.2)]
        self.assertEqual(R.check(root, spec, evidence)['status'], 'fail')
        sig = root.find('.//signal[@name="VIN"]')
        sig.remove(sig.find('wire'))
        self.assertEqual(R.check(root, spec, evidence)['status'], 'fail')

    def test_tap_must_reach_goal_layer_not_coincident_disconnected_trace(self):
        root, _, _ = fixture()
        ET.SubElement(root.find('.//signal[@name="VIN"]'), 'wire', x1='8', y1='10', x2='10', y2='10',
                      width='.5', layer='2')
        conn = RA.Conn('test', 'VIN', (8,8), (2,), (10,10), (1,))
        excluded = RA._not_joined(root, conn)
        route = RT.route(root, conn.net, conn.start, conn.start_layers, conn.goal, conn.goal_layers,
                         vias=False, join_existing=True, tap_exclude=excluded, step=.25)
        self.assertTrue(route.problems)

    def test_effective_capacitance_requires_all_applicable_conditions(self):
        root, spec, evidence = fixture()
        spec['requirements'] = [req('selection', component='C1', metric='effective_capacitance_f', min=4e-6)]
        spec['components']['C1'] = {'parameters': {'effective_capacitance_f': {
            'min':5e-6, 'max':10e-6, 'source':'cap', 'pages':[1], 'conditions':{'temperature_c':[0,85]}}}}
        self.assertEqual(R.check(root, spec, evidence)['status'], 'unverified')

    def test_supporting_pad_requires_exact_device_binding(self):
        root, spec, evidence = fixture()
        del spec['devices']['C1']
        spec['requirements'] = [req('distance', pads=['U1.VIN','C1.1'],net='VIN',max_mm=3)]
        self.assertEqual(R.check(root, spec, evidence)['status'], 'unverified')

    def test_qualitative_routing_is_unverified(self):
        root, spec, evidence = fixture()
        spec['requirements'] = [req('routing', net='VIN', qualitative=True)]
        self.assertEqual(R.check(root, spec, evidence)['status'], 'unverified')


if __name__ == '__main__': unittest.main()
