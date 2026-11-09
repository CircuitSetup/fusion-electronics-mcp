import copy
import unittest
import xml.etree.ElementTree as ET
from fusion_offline import requirements as R


BOARD = '''<eagle><drawing><board><plain>
<wire x1="0" y1="0" x2="30" y2="0" layer="20" width="0"/>
<wire x1="30" y1="0" x2="30" y2="20" layer="20" width="0"/>
<wire x1="30" y1="20" x2="0" y2="20" layer="20" width="0"/>
<wire x1="0" y1="20" x2="0" y2="0" layer="20" width="0"/></plain>
<designrules><param name="layerSetup" value="(1*2*15*16)"/></designrules>
<libraries><library name="L"><packages><package name="QFN">
<smd name="VIN" x="0" y="0" dx="0.6" dy="0.6" layer="1"/>
<smd name="EP" x="2" y="0" dx="1" dy="1" layer="1"/>
</package><package name="0805"><smd name="1" x="0" y="0" dx="0.6" dy="0.6" layer="1"/>
<smd name="2" x="0" y="1.5" dx="0.6" dy="0.6" layer="1"/></package></packages></library></libraries>
<elements><element name="U1" library="L" package="QFN" x="10" y="10"><attribute name="MPN" value="TEST-BUCK"/></element>
<element name="C1" library="L" package="0805" x="8" y="10"><attribute name="MPN" value="TEST-CAP"/></element></elements>
<signals><signal name="VIN"><contactref element="U1" pad="VIN"/><contactref element="C1" pad="1"/>
<wire x1="8" y1="10" x2="10" y2="10" layer="1" width="0.5"/></signal>
<signal name="GND"><contactref element="U1" pad="EP"/><contactref element="C1" pad="2"/>
<via x="12" y="10" drill="0.3" diameter="0.6" extent="1-16"/></signal></signals></board></drawing></eagle>'''


def fixture():
    evidence = {'ds': dict(mpn='TEST-BUCK', package='QFN', revision='A', sha256='a'*64,
                           source_url='https://vendor.example/buck.pdf', page_count=4,
                           inspected_pages={'1': {'source_sha256': 'a'*64}}),
                'cap': dict(mpn='TEST-CAP', package='0805', revision='B', sha256='b'*64,
                            source_url='https://vendor.example/cap.pdf', page_count=1,
                            inspected_pages={'1': {'source_sha256': 'b'*64}})}
    spec = dict(version=1, sources={'ds': {'sha256': 'a'*64}, 'cap': {'sha256': 'b'*64}},
                devices={'U1': {'mpn': 'TEST-BUCK', 'package': 'QFN'}, 'C1': {'mpn': 'TEST-CAP', 'package': '0805'}},
                operating_conditions={'vin_v': 5, 'temperature_c': 25}, requirements=[], components={})
    return ET.fromstring(BOARD), spec, evidence


def req(kind, **kw):
    return dict(id=kw.pop('id', kind), type=kind, device='U1', origin='manufacturer',
                source='ds', pages=[1], statement='Synthetic test requirement, not a real datasheet.',
                conditions={'vin_v': [4, 6], 'temperature_c': [0, 85]}, **kw)


class RequirementsTest(unittest.TestCase):
    def test_foreign_pour_on_disjoint_layer_preserves_clearance_pass(self):
        root, spec, evidence = fixture()
        spec['requirements'] = [req('routing', net='VIN', layers=[1], clearance_mm=.2)]
        before = R.check(root, spec, evidence)
        self.assertEqual(before['status'], 'pass')
        ET.SubElement(root.find(".//signal[@name='GND']"), 'polygon', layer='2')
        after = R.check(root, spec, evidence)
        self.assertEqual(after['status'], 'pass')
        self.assertEqual(R.regressions(before, after), [])

    def test_pour_clearance_remains_unverified_for_relevant_or_unknown_layers(self):
        for net, layer in [('GND', '1'), ('VIN', '2'), ('GND', None),
                           ('GND', 'unknown'), ('GND', '3')]:
            with self.subTest(net=net, layer=layer):
                root, spec, evidence = fixture()
                spec['requirements'] = [req('routing', net='VIN', clearance_mm=.2)]
                polygon = ET.SubElement(root.find(f".//signal[@name='{net}']"), 'polygon')
                if layer is not None:
                    polygon.set('layer', layer)
                result = R.check(root, spec, evidence)
                self.assertEqual(result['status'], 'unverified')
                self.assertIn('actual poured copper geometry unavailable', result['findings'][-1]['observed'])

    def test_through_copper_makes_internal_pour_clearance_unverified(self):
        for kind in ('via', 'pad'):
            with self.subTest(kind=kind):
                root, spec, evidence = fixture()
                spec['requirements'] = [req('routing', net='VIN', clearance_mm=.2)]
                if kind == 'via':
                    ET.SubElement(root.find(".//signal[@name='VIN']"), 'via',
                                  x='9', y='10', drill='.3', diameter='.6', extent='1-16')
                else:
                    package = root.find(".//package[@name='QFN']")
                    package.remove(package.find("smd[@name='VIN']"))
                    ET.SubElement(package, 'pad', name='VIN', x='0', y='0', drill='.3', diameter='.6')
                self.assertEqual(R.check(root, spec, evidence)['status'], 'pass')
                ET.SubElement(root.find(".//signal[@name='GND']"), 'polygon', layer='2')
                self.assertEqual(R.check(root, spec, evidence)['status'], 'unverified')

    def test_solid_pour_requires_solid_fill_and_direct_connection(self):
        root, spec, evidence = fixture()
        polygon = ET.SubElement(root.find(".//signal[@name='GND']"), 'polygon',
                                layer='1', isolate='.25', pour='solid', thermals='no')
        spec['requirements'] = [req('pour', net='GND', layer=1, clearance_mm=.2, solid=True)]
        self.assertEqual(R.check(root, spec, evidence)['status'], 'pass')
        polygon.set('pour', 'hatch')
        self.assertEqual(R.check(root, spec, evidence)['status'], 'fail')
        polygon.set('pour', 'solid'); polygon.set('thermals', 'yes')
        self.assertEqual(R.check(root, spec, evidence)['status'], 'fail')
        spec['requirements'] = [req('pour_layout', net='GND', required_pads=['U1.EP'])]
        self.assertEqual(R.check(root, spec, evidence)['status'], 'unverified')

    def test_pin_distance_and_actual_path(self):
        root, spec, evidence = fixture()
        spec['requirements'] = [req('distance', pads=['U1.VIN', 'C1.1'], net='VIN', max_mm=2.5, same_side=True),
                                req('path_length', pads=['U1.VIN', 'C1.1'], net='VIN', max_mm=2.5)]
        self.assertEqual(R.check(root, spec, evidence)['status'], 'pass')
        root.find('.//wire[@layer="1"]').set('x2', '9')
        result = R.check(root, spec, evidence)
        self.assertEqual(result['findings'][-1]['status'], 'unverified')
        root.find('.//element[@name="C1"]').set('x', '2')
        self.assertEqual(R.check(root, spec, evidence)['status'], 'fail')

    def test_wrong_identity_stale_and_uninspected_are_not_pass(self):
        for mutation in ('package', 'mpn', 'hash', 'page', 'conditions'):
            root, spec, evidence = fixture()
            spec['requirements'] = [req('distance', pads=['U1.VIN', 'C1.1'], net='VIN', max_mm=3)]
            if mutation in ('package', 'mpn'):
                spec['devices']['U1'][mutation] = 'WRONG'
            elif mutation == 'hash':
                evidence['ds']['sha256'] = 'c'*64
            elif mutation == 'page':
                evidence['ds']['inspected_pages'] = {}
            else:
                spec['operating_conditions']['vin_v'] = 12
            with self.subTest(mutation=mutation):
                self.assertNotEqual(R.check(root, spec, evidence)['status'], 'pass')

    def test_selection_bounds_and_effective_capacitance(self):
        root, spec, evidence = fixture()
        spec['components']['C1'] = {'parameters': {
            'effective_capacitance_f': {'min': 5e-6, 'max': 10e-6, 'source': 'cap', 'pages': [1],
                                      'conditions': {'vin_v': [0, 5], 'temperature_c': [0, 85]}},
            'esr_ohm': {'min': .01, 'max': .1, 'source': 'cap', 'pages': [1],
                        'conditions': {'vin_v': [0, 5], 'temperature_c': [0, 85]}}}}
        spec['requirements'] = [req('selection', component='C1', metric='effective_capacitance_f', min=4e-6, max=22e-6),
                                req('selection', id='esr', component='C1', metric='esr_ohm', min=.005, max=.2)]
        self.assertEqual(R.check(root, spec, evidence)['status'], 'pass')
        spec['components']['C1']['parameters']['effective_capacitance_f']['min'] = 2e-6
        self.assertEqual(R.check(root, spec, evidence)['status'], 'fail')
        del spec['components']['C1']['parameters']['effective_capacitance_f']
        self.assertEqual(R.check(root, spec, evidence)['status'], 'unverified')

    def test_layer_width_via_thermal_and_unsupported_analysis(self):
        root, spec, evidence = fixture()
        spec['requirements'] = [req('routing', net='VIN', layers=[1, 2], min_width_mm=.4, clearance_mm=.2, priority=10),
                                req('thermal_vias', pad='U1.EP', net='GND', radius_mm=1, min_count=1, min_drill_mm=.3)]
        self.assertEqual(R.check(root, spec, evidence)['status'], 'pass')
        root.find('.//wire[@layer="1"]').set('layer', '15')
        self.assertEqual(R.check(root, spec, evidence)['status'], 'fail')
        spec['requirements'] = [req('return_path', net='VIN', reference_net='GND', reference_layer=2)]
        self.assertEqual(R.check(root, spec, evidence)['status'], 'unverified')

    def test_assumptions_stay_labelled_and_nonfinite_is_rejected(self):
        root, spec, evidence = fixture()
        r = req('distance', pads=['U1.VIN', 'C1.1'], net='VIN', max_mm=3)
        r.update(origin='assumption', rationale='Chosen bound for qualitative close placement')
        spec['requirements'] = [r]
        result = R.check(root, spec, evidence)
        self.assertEqual(result['findings'][-1]['origin'], 'assumption')
        r['max_mm'] = float('nan')
        with self.assertRaises(ValueError):
            R.validate(spec)

    def test_missing_requirements_explicit(self):
        root, _, _ = fixture()
        self.assertEqual(R.check(root, None, {})['status'], 'not_supplied')

    def test_all_supported_selection_metrics_use_worst_case(self):
        for metric in ['resistance_ohm', 'voltage_rating_v', 'power_rating_w', 'saturation_current_a',
                       'rms_current_a', 'vds_rating_v', 'vgs_rating_v', 'id_rating_a', 'rds_on_ohm']:
            root, spec, evidence = fixture()
            spec['requirements'] = [req('selection', component='C1', metric=metric, min=1, max=10)]
            fact = {'min': 2, 'max': 9, 'source': 'cap', 'pages': [1], 'conditions': {'vin_v': [4,6], 'temperature_c': [0,85]}}
            spec['components'] = {'C1': {'parameters': {metric: fact}}}
            with self.subTest(metric=metric):
                self.assertEqual(R.check(root, spec, evidence)['status'], 'pass')
                fact['max'] = 11
                self.assertEqual(R.check(root, spec, evidence)['status'], 'fail')

    def test_qualitative_manufacturer_bound_and_unknown_field_rejected(self):
        _, spec, _ = fixture()
        spec['requirements'] = [req('distance', pads=['U1.VIN','C1.1'], qualitative=True, max_mm=3)]
        with self.assertRaisesRegex(ValueError, 'assumption'):
            R.validate(spec)
        spec['requirements'][0].update(qualitative=False, max_mm_typo=2)
        with self.assertRaisesRegex(ValueError, 'field'):
            R.validate(spec)

    def test_clearance_uses_actual_width_and_arcs_are_unverified(self):
        root, spec, evidence = fixture()
        spec['requirements'] = [req('routing', net='VIN', layers=[1], min_width_mm=.25, clearance_mm=.2)]
        root.find('.//wire[@layer="1"]').set('width', '3')
        self.assertEqual(R.check(root, spec, evidence)['status'], 'fail')
        root.find('.//wire[@layer="1"]').set('width', '.25')
        root.find('.//wire[@layer="1"]').set('curve', '90')
        self.assertEqual(R.check(root, spec, evidence)['status'], 'unverified')

    def test_loop_and_keepout_check_readback_geometry(self):
        root, spec, evidence = fixture()
        spec['requirements'] = [req('loop', segments=[{'pads':['U1.VIN','C1.1'],'net':'VIN'},
                                                     {'pads':['C1.1','U1.VIN'],'net':'VIN'}], max_mm=3)]
        self.assertEqual(R.check(root, spec, evidence)['status'], 'fail')
        spec['requirements'] = [req('keepout', region_mm=[9,9,11,11], layers=[1], allowed_nets=[])]
        self.assertEqual(R.check(root, spec, evidence)['status'], 'fail')
        spec['requirements'][0]['region_mm'] = [20, 15, 22, 17]
        self.assertEqual(R.check(root, spec, evidence)['status'], 'pass')

    def test_offline_acceptance_files_and_violations(self):
        import json
        from pathlib import Path
        folder = Path(__file__).parent / 'fixtures' / 'datasheet-workflow'
        root = ET.parse(folder / 'regulator.brd').getroot()
        spec = json.loads((folder / 'requirements.json').read_text())
        evidence = json.loads((folder / 'synthetic-evidence.json').read_text())
        result = R.check(root, spec, evidence)
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual([f['id'] for f in result['findings'] if f['status'] != 'pass'], ['reference-return'])
        root.find('.//element[@name="C1"]').set('x','1')
        report = R.check(root, spec, evidence)
        self.assertEqual(report['status'], 'fail')
        self.assertTrue(all(f.get('pages') for f in report['findings'] if f.get('type')))
