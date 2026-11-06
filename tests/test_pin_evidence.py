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

if __name__=='__main__': unittest.main()
