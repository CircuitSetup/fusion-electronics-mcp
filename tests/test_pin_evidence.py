import copy
import unittest
from fusion_offline import pin_evidence as E

PART={'id':'test','deviceset':'TEST','device':'','package':{'name':'QFN','smds':[{'name':'1'},{'name':'2'},{'name':'3'}]},'attributes':{'MPN':'PART-1'},'symbol':{'pins':[{'name':'IN','unit':'A','pad':'1','direction':'in'},{'name':'IN','unit':'B','pad':'2 3','direction':'in'}]}}
def evidence():
    e={'version':1,'part_id':'test','mpn':'PART-1','package':'QFN','source':'datasheet.pdf','revision':'Rev A','sha256':'a'*64,'pages':[2],
       'pins':[{'pad':pad,'gate':gate,'pin':'IN','datasheet_name':'INPUT','direction':'in','intent':'signal','net':None,'domain':None,'alternate_function':None,'pages':[2],'uncertain':False} for pad,gate in [('1','A'),('2','B'),('3','B')]],'reviews':[]}
    e['reviews'].append({'status':'approved','reviewer':'Test reviewer','reviewed_at':'2026-11-05T00:00:00Z','content_sha256':E.content_hash(e)})
    return e
class EvidenceTest(unittest.TestCase):
    def test_valid_multi_pad_multi_unit(self):
        self.assertTrue(all(f['status']=='pass' for f in E.validate_pin_evidence(PART,evidence(),actual_sha256='a'*64)))

    def test_native_package_case_preserves_approved_evidence_and_hash_checks(self):
        e=evidence()
        e['package']='WSON-8-1EP_2x2mm_P0.5mm_EP0.9x1.6mm'
        e['reviews'][-1]['content_sha256']=E.content_hash(e)
        original=copy.deepcopy(e)
        part=copy.deepcopy(PART); part['package']['name']=e['package'].upper()
        self.assertTrue(all(f['status']=='pass' for f in E.validate_pin_evidence(part,e,'a'*64)))
        self.assertEqual(e,original)
        self.assertEqual(E.review_status(e),'approved')
        bad_hash=E.validate_pin_evidence(part,e,'b'*64)
        self.assertEqual(next(f['status'] for f in bad_hash if f['rule_id']=='datasheet_hash'),'fail')
        e['package']=e['package'].upper()
        self.assertEqual(E.review_status(e),'unreviewed')

    def test_active_native_package_case_does_not_hide_real_mismatch(self):
        from fusion_offline.design import SchematicDesign, SchPart, SchPin
        e=evidence(); e['package']='Wson_2x2mm'
        placed=SchPart('U1','LIB','TEST','','','TEST','WSON_2X2MM',False,{'MPN':'PART-1'})
        pins=[SchPin('U1','A','IN','1','in',0,0,1),SchPin('U1','B','IN','2 3','in',0,0,1)]
        schematic=SchematicDesign({'U1':placed},{},pins,1,{})
        self.assertEqual(E.report(E.compare_pin_evidence(schematic,{'test':PART},{'test':e}))['status'],'pass')
        placed.package='WSON_3X3MM'
        self.assertEqual(E.report(E.compare_pin_evidence(schematic,{'test':PART},{'test':e}))['status'],'fail')

    def test_native_pin_and_gate_case_preserves_all_physical_mappings(self):
        from fusion_offline.design import SchematicDesign, SchPart, SchPin
        part=copy.deepcopy(PART)
        for pad,name in zip(part['package']['smds'],('A1','B2','C3')): pad['name']=name
        part['symbol']['pins'][0]['pad']='A1'; part['symbol']['pins'][1]['pad']='B2 C3'
        e=evidence()
        for row,pad in zip(e['pins'],('a1','b2','c3')):
            row.update(pad=pad,gate=row['gate'].lower(),pin='in')
        e['reviews'][-1]['content_sha256']=E.content_hash(e)
        self.assertTrue(all(f['status']=='pass' for f in E.validate_pin_evidence(part,e,'a'*64)))
        placed=SchPart('U1','LIB','test','','','TEST','QFN',False,{'MPN':'PART-1'})
        pins=[SchPin('U1','A','IN','A1','in',0,0,1),SchPin('U1','B','IN','B2 C3','in',0,0,1)]
        schematic=SchematicDesign({'U1':placed},{},pins,1,{})
        self.assertEqual(E.report(E.compare_pin_evidence(schematic,{'test':part},{'test':e}))['status'],'pass')
        placed.connects={('A','IN','A1'),('B','IN','B2'),('B','IN','C3'),('UNUSED','EXTRA','D4')}
        self.assertEqual(E.report(E.compare_pin_evidence(schematic,{'test':part},{'test':e}))['status'],'fail')

    def test_part_id_and_mpn_remain_case_sensitive(self):
        for field in ('part_id','mpn'):
            e=evidence(); e[field]=e[field].swapcase()
            e['reviews'][-1]['content_sha256']=E.content_hash(e)
            findings=E.validate_pin_evidence(PART,e,'a'*64)
            self.assertEqual(next(f['status'] for f in findings if f['rule_id']==field),'fail')
    def test_swapped_missing_unknown_and_variant_do_not_pass(self):
        for mutation in ('swap','missing','ocr','package','mpn','hash','review'):
            e=evidence()
            if mutation=='swap': e['pins'][0]['gate']='B'
            if mutation=='missing': e['pins'].pop()
            if mutation=='ocr': e['pins'][0]['uncertain']=True
            if mutation=='package': e['package']='SOT'
            if mutation=='mpn': e['mpn']='PART-2'
            if mutation=='review': e['reviews']=[]
            actual='b'*64 if mutation=='hash' else 'a'*64
            with self.subTest(mutation=mutation): self.assertTrue(any(f['status'] in ('fail','unknown') for f in E.validate_pin_evidence(PART,e,actual_sha256=actual)))
    def test_unknown_source_is_not_pass_and_approval_invalidates(self):
        e=evidence()
        self.assertTrue(any(f['status']=='unknown' for f in E.validate_pin_evidence(PART,e)))
        e['pins'][0]['net']='VCC'
        self.assertEqual(E.review_status(e),'unreviewed')
    def test_duplicate_pad_is_rejected(self):
        e=evidence(); e['pins'].append(copy.deepcopy(e['pins'][0]))
        self.assertTrue(any(f['status']=='fail' for f in E.validate_pin_evidence(PART,e,actual_sha256='a'*64)))


    def test_existing_mp_attribute_is_supported(self):
        p=copy.deepcopy(PART); p['attributes']={'MP':'PART-1'}
        findings=E.validate_pin_evidence(p,evidence(),actual_sha256='a'*64)
        self.assertTrue(all(f['status']=='pass' for f in findings))


    def test_active_mapping_preserves_reviewed_unmapped_pad(self):
        from fusion_offline.design import SchematicDesign, SchPart, SchPin
        e = evidence()
        e['pins'].append(dict(e['pins'][0], pad='4', gate=None, pin=None, direction=None, intent='dnc'))
        e['reviews'][-1]['content_sha256'] = E.content_hash(e)
        placed = SchPart('U1', 'LIB', 'TEST', '', '', 'TEST', 'QFN', False, {'MPN': 'PART-1'})
        for pads, extra, want in [('3 2', None, 'pass'), ('2 3 4', None, 'fail'), ('3 2', '4', 'fail')]:
            with self.subTest(pads=pads, extra=extra):
                pins = [SchPin('U1', 'A', 'IN', '1', 'in', 0, 0, 1),
                        SchPin('U1', 'B', 'IN', pads, 'in', 0, 0, 1),
                        SchPin('U1', 'B', 'EXTRA', extra, 'out', 0, 0, 1)]
                schematic = SchematicDesign({'U1': placed}, {}, pins, 1, {})
                findings = E.compare_pin_evidence(schematic, {'test': PART}, {'test': e})
                self.assertEqual(E.report(findings)['status'], want)

class UninvokedGateTest(unittest.TestCase):
    def test_dnc_pad_mapped_by_uninvoked_gate_fails(self):
        from pathlib import Path
        import xml.etree.ElementTree as ET
        from fusion_offline.design import parse_schematic_design
        root = ET.parse(Path(__file__).parent / 'fixtures' / 'mini.sch').getroot()
        ds = root.find(".//deviceset[@name='LDO']")
        connects = ds.find('./devices/device/connects')
        connects.remove(connects.find("connect[@pin='NC']"))
        ET.SubElement(ds.find('gates'), 'gate', name='UNUSED', symbol='REG')
        placed = root.find(".//parts/part[@name='U1']")
        ET.SubElement(placed, 'attribute', name='MPN', value='PART-1')
        pins = [{'pad': c.get('pad'), 'gate': c.get('gate'), 'pin': c.get('pin'),
                 'direction': {'VIN': 'pwr', 'VOUT': 'out', 'EN': 'in'}[c.get('pin')],
                 'intent': 'signal', 'pages': [1]} for c in connects]
        pins.append({'pad': '4', 'gate': None, 'pin': None, 'intent': 'dnc'})
        part = {'deviceset': 'LDO', 'device': ''}
        e = {'package': 'SOT23', 'mpn': 'PART-1', 'pins': pins, 'source': 'synthetic'}
        self.assertEqual(E.report(E.compare_pin_evidence(parse_schematic_design(root), {'x': part}, {'x': e}))['status'], 'pass')
        ET.SubElement(connects, 'connect', gate='UNUSED', pin='VIN', pad='4')
        self.assertEqual(E.report(E.compare_pin_evidence(parse_schematic_design(root), {'x': part}, {'x': e}))['status'], 'fail')

if __name__=='__main__': unittest.main()
