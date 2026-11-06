import copy
import tempfile
import unittest
from pathlib import Path
from fusion_offline import edit_plan as P
from fusion_mcp import edit_plan as Live
from fusion_mcp.session import Snapshot
from contextlib import nullcontext

XML = (Path(__file__).parent / 'fixtures/mini.sch').read_text()
def snapshot():
    return {'document_id': 'session:one', 'project_id': 'project:one', 'sch_xml': XML, 'board_xml': None, 'sources': {}}
OPS = [{'op': 'connect', 'net': 'INPUT', 'pins': [{'part':'U1','gate':'G$1','pin':'VIN'}]}]

class FakeSession:
    def __init__(self):
        self.bridge = self
        self.lock = self
        self.xml = XML
        self.calls = []
        self.doc = 'session:one'
        self.fail = False
    def hold(self, *a): return nullcontext()
    def context(self): return {'active_document': {'document_id': self.doc, 'project_id': 'project:one', 'kind':'schematic'}}
    def snapshot(self): return Snapshot(sch_xml=self.xml.encode())
    def call(self, op, args, **kw):
        self.calls.append((op,args))
        if self.fail: raise TimeoutError('uncertain')
        return {}

class PlanTest(unittest.TestCase):
    def test_hash_and_stale_inputs(self):
        a = P.build_plan(snapshot(), OPS, {})
        self.assertEqual(a['sha256'], P.build_plan(snapshot(), OPS, {})['sha256'])
        self.assertEqual(P.validate_plan(a, snapshot()), [])
        for key, value in [('document_id','session:two'),('sch_xml',XML.replace('x="40"','x="41"')),('sources',{'x':'changed'})]:
            s=snapshot(); s[key]=value
            self.assertTrue(P.validate_plan(a,s))
        a['operations'][0]['net']='ALTERED'
        self.assertTrue(P.validate_plan(a,snapshot()))
    def test_no_merges_scripts_or_duplicate_parts(self):
        for ops in ([{'op':'script','commands':'DELETE;'}],
                    [{'op':'connect','net':'BAD','pins':[{'part':'R1','gate':'G$1','pin':'P$2'}]}],
                    [{'op':'add_part','ref':'R1'}]):
            with self.subTest(ops=ops), self.assertRaises(ValueError): P.build_plan(snapshot(),ops,{})
    def test_semantic_hash_ignores_formatting_but_keeps_electrical_changes(self):
        self.assertEqual(P.xml_hash(XML),P.xml_hash(XML.replace('\n','  ')))
        self.assertNotEqual(P.xml_hash(XML),P.xml_hash(XML.replace('pad="1"','pad="9"')))
    def test_preview_is_read_only_and_path_hash_bound(self):
        s=FakeSession()
        with tempfile.TemporaryDirectory() as d:
            out=Live.preview(s,OPS,'session:one',Path(d))
            self.assertEqual(s.calls,[])
            self.assertIn('U1',Path(out['preview_path']).read_text())
            p=Path(out['plan_path']); data=p.read_text(); p.write_text(data.replace('INPUT','EVIL'))
            with self.assertRaises(ValueError): Live.apply(s,str(p),out['sha256'],Path(d))
            self.assertEqual(s.calls,[])
            with self.assertRaises(ValueError): Live.apply(s,str(Path(d).parent/'outside.json'),out['sha256'],Path(d))
    def test_timeout_reports_unknown_and_never_undos_or_retries(self):
        s=FakeSession()
        with tempfile.TemporaryDirectory() as d:
            out=Live.preview(s,OPS,'session:one',Path(d)); s.fail=True
            result=Live.apply(s,out['plan_path'],out['sha256'],Path(d))
            self.assertEqual(result['status'],'state_unknown')
            self.assertEqual(len(s.calls),1)
            self.assertEqual(result['completed_operations'],[])
    def test_wrong_document_sends_no_write(self):
        s=FakeSession()
        with tempfile.TemporaryDirectory() as d:
            out=Live.preview(s,OPS,'session:one',Path(d)); s.doc='session:two'
            with self.assertRaises(ValueError): Live.apply(s,out['plan_path'],out['sha256'],Path(d))
            self.assertEqual(s.calls,[])


    def test_success_and_failed_readback(self):
        import xml.etree.ElementTree as ET
        for mode in ('success','export_failure','mismatch','unexpected_dialog'):
            session=FakeSession()
            def run(op,args,**kw):
                session.calls.append((op,args))
                if mode=='export_failure':
                    session.snapshot=lambda: (_ for _ in ()).throw(OSError('export unavailable'))
                elif mode in ('success','unexpected_dialog'):
                    root=ET.fromstring(XML); nets=root.find('./drawing/schematic/sheets/sheet/nets')
                    net=ET.SubElement(nets,'net',name='INPUT',**{'class':'0'}); seg=ET.SubElement(net,'segment')
                    ET.SubElement(seg,'pinref',part='U1',gate='G$1',pin='VIN')
                    ET.SubElement(seg,'label',x='52.7',y='0',size='1.27',layer='95')
                    session.xml=ET.tostring(root,encoding='unicode')
                return {'dialogs':[{'expected':False,'title':'Unexpected'}]} if mode=='unexpected_dialog' else {}
            session.call=run
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                preview=Live.preview(session,OPS,'session:one',Path(d))
                result=Live.apply(session,preview['plan_path'],preview['sha256'],Path(d))
                self.assertEqual(result['status'],'applied' if mode=='success' else 'state_unknown')
                self.assertEqual(session.calls[0][1]['expected_document_id'],'session:one')
                self.assertEqual(len(session.calls),1)

    def test_addin_identity_and_wrong_tab_guard(self):
        from test_server import load_addin
        from types import SimpleNamespace
        addin=load_addin(); d=SimpleNamespace(name='same',isModified=False,isSaved=False,isActive=True)
        one=addin._document_id(d)
        self.assertEqual(one,addin._document_id(d))
        addin._app=SimpleNamespace(activeDocument=d)
        with self.assertRaises(addin.BridgeError): addin.op_run({'commands':'GRID MM;','expected_document_id':'other'})

    def test_nonfinite_positions_and_unknown_fields_rejected(self):
        for bad in (float('nan'),float('inf'),True):
            with self.assertRaises(ValueError): P.number(bad)
        for op in [dict(OPS[0],commands='DELETE;'),{'op':'add_sheet','sheet':True,'title':'x'}]:
            with self.assertRaises(ValueError): P.build_plan(snapshot(),[op],{})

    def test_source_and_metadata_changes_invalidate_plan(self):
        s=snapshot(); s['sources']={'x':{'sha256':'a','content':'one'}}
        p=P.build_plan(s,OPS,{})
        current=copy.deepcopy(s); current['sources']['x']['content']='two'
        self.assertTrue(P.validate_plan(p,current))
        current=copy.deepcopy(s); current['sch_xml']=XML.replace('value="10k"','value="20k"')
        self.assertTrue(P.validate_plan(p,current))

if __name__=='__main__': unittest.main()
