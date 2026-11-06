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
