import hashlib
import json
import socket
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import load_addin
from test_document_lifecycle import family


LIB = '<libraries><library name="L"><devicesets><deviceset name="D"><devices><device name="" package="P"><connects><connect gate="G" pin="1" pad="1 2"/></connects></device></devices></deviceset></devicesets></library></libraries>'
SCH = '<eagle><drawing><schematic>' + LIB + '<parts><part name="U1" library="L" deviceset="D" device=""/></parts><sheets><sheet><nets><net name="GND"><segment><pinref part="U1" gate="G" pin="1"/></segment></net></nets></sheet></sheets></schematic></drawing></eagle>'
BRD = '<eagle><drawing><board><elements><element name="U1"/></elements><signals><signal name="GND"><contactref element="U1" pad="1"/><contactref element="U1" pad="2"/></signal></signals></board></drawing></eagle>'


class DataFiles:
    """Public SDK shape: DataFiles has count/item, but no itemByName."""
    def __init__(self, names=()):
        self.names = list(names)
        self.reads = []
    @property
    def count(self): return len(self.names)
    def item(self, index):
        self.reads.append(index)
        return types.SimpleNamespace(name=self.names[index])


class PairImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sch = self.root / 'Source.sch'; self.sch.write_text(SCH)
        self.brd = self.root / 'Source.brd'; self.brd.write_text(BRD)
        self.args = dict(request_id='a'*32, schematic_path=str(self.sch), board_path=str(self.brd),
                         schematic_sha256=hashlib.sha256(self.sch.read_bytes()).hexdigest(),
                         board_sha256=hashlib.sha256(self.brd.read_bytes()).hexdigest(),
                         project_id='project', folder='', name='New product')
        self.a = load_addin(); self.a.WORK_DIR = str(self.root / 'work')
        self.a._kind_of = lambda d: d.kind
        self.a._settle = lambda n=10: None
        self.originals = list(family()); self.docs = self.originals[:]
        for d in self.docs:
            d.isSaved = True; d.isModified = False
        self.a._docs = lambda: self.docs[:]
        self.a._doc_info = lambda d: dict(document_id=str(id(d)), name=d.name, kind=d.kind,
                                          saved=d.isSaved, modified=d.isModified,
                                          data_file_id=getattr(d.dataFile, 'id', None))
        self.folder = types.SimpleNamespace(id='folder-id', dataFolders=types.SimpleNamespace(itemByName=lambda n: None),
                                            dataFiles=DataFiles())
        self.project = types.SimpleNamespace(id='project', rootFolder=self.folder)
        self.starts = []
        command = types.SimpleNamespace(execute=lambda: self.starts.append('start'),
                                         controlDefinition=types.SimpleNamespace(isEnabled=True))
        self.a._app = types.SimpleNamespace(data=types.SimpleNamespace(activeProject=self.project),
                                            fireCustomEvent=lambda *args: None)
        self.a._ui = types.SimpleNamespace(commandDefinitions=types.SimpleNamespace(itemById=lambda n: command))
        self.a._events = types.SimpleNamespace(session_id='session')
        self.timers = []
        def timer(delay, callback):
            return types.SimpleNamespace(start=lambda: self.timers.append(callback))
        self.timer_patch = patch.object(self.a.threading, 'Timer', timer); self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)

    def start(self): return self.a.op_start_eagle_pair_import(self.args)

    def test_sources_are_pinned_and_staged_without_modification(self):
        result = self.start()
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['expected'], {'physical_parts':1, 'schematic_parts':1, 'sheets':1, 'contacts':2})
        self.assertEqual(Path(result['staged']['schematic']).read_text(), SCH)
        self.assertEqual(Path(result['staged']['board']).read_text(), BRD)
        self.assertEqual(self.sch.read_text(), SCH)
        self.assertEqual(self.starts, [])
        self.assertEqual(self.docs, self.originals)

    def test_bad_pair_hash_or_mapping_is_rejected_before_launch(self):
        for changes in ({'board_path':str(self.root/'Wrong.brd')}, {'schematic_sha256':'0'*64}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.a.op_start_eagle_pair_import(dict(self.args, **changes))
        self.brd.write_text(BRD.replace('pad="2"','pad="3"'))
        self.args['board_sha256'] = hashlib.sha256(self.brd.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'contact'):
            self.start()
        self.assertEqual(self.timers, [])

    def test_unsaved_modified_wrong_project_and_missing_folder_refuse(self):
        for attr in ('isSaved','isModified'):
            setattr(self.docs[0], attr, attr == 'isModified')
            with self.assertRaises(ValueError): self.start()
            setattr(self.docs[0], attr, attr == 'isSaved')
        for changes in ({'project_id':'other'}, {'folder':'missing'}, {'folder':'../escape'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.a.op_start_eagle_pair_import(dict(self.args, **changes))
        self.assertEqual(self.timers, [])

    def test_destination_collision_uses_public_bounded_datafiles_api(self):
        for existing in ('New product','new PRODUCT.fprj'):
            with self.subTest(existing=existing):
                self.folder.dataFiles=DataFiles(['Other',existing])
                with self.assertRaisesRegex(ValueError,'already exists'): self.start()
                self.assertEqual(self.timers,[])
        self.folder.dataFiles=DataFiles(['Other']*1001)
        with self.assertRaisesRegex(ValueError,'1000'): self.start()
        self.assertEqual(self.folder.dataFiles.reads,[])
        self.assertEqual(self.timers,[])

    def test_failed_destination_guard_does_not_record_or_launch_request(self):
        self.folder.dataFiles=DataFiles(['New product'])
        with self.assertRaisesRegex(ValueError,'already exists'): self.start()
        with self.assertRaisesRegex(ValueError,'Unknown import request'):
            self.a.op_eagle_pair_import_status({'request_id':self.args['request_id']})
        self.folder.dataFiles=DataFiles(['Other'])
        self.assertEqual(self.start()['status'],'queued')
        self.assertEqual(self.starts,[])

    def test_request_id_is_idempotent_and_cannot_be_rebound(self):
        first = self.start()
        self.assertEqual(self.start(), first)
        self.assertEqual(len(self.timers), 0)
        with self.assertRaises(ValueError): self.a.op_start_eagle_pair_import(dict(self.args, name='Other'))
        with self.assertRaises(ValueError): self.a.op_start_eagle_pair_import(dict(self.args, request_id='b'*32))

    def test_launch_is_pending_not_success_and_rechecks_protected_documents(self):
        self.start()
        self.a._launch_eagle_pair_import(self.args['request_id'])
        status = self.a.op_eagle_pair_import_status({'request_id':self.args['request_id']})
        self.assertEqual(status['status'], 'awaiting_file_selection')
        self.assertEqual(self.starts, ['start'])
        result = self.a.op_finish_eagle_pair_import({'request_id':self.args['request_id']})
        self.assertEqual(result['status'], 'state_unknown')
        self.assertNotIn('saved', result)

    def test_changed_protected_document_prevents_command_start(self):
        self.start(); self.originals[0].isModified = True
        self.a._launch_eagle_pair_import(self.args['request_id'])
        self.assertEqual(self.starts, [])
        self.assertEqual(self.a.op_eagle_pair_import_status({'request_id':self.args['request_id']})['status'], 'not_started')

    def test_uncertain_import_keeps_new_requests_and_mutations_blocked(self):
        self.start(); self.a._launch_eagle_pair_import(self.args['request_id'])
        self.a.op_finish_eagle_pair_import({'request_id':self.args['request_id']})
        with self.assertRaisesRegex(ValueError,'pending'):
            self.a.op_start_eagle_pair_import(dict(self.args,request_id='b'*32))
        job={'op':'run','args':{},'state':'queued','done':self.a.threading.Event()}
        self.a._jobs['job']=job
        self.a._Handler().notify(types.SimpleNamespace(additionalInfo='job'))
        self.assertEqual(job['result']['error']['code'],'import_pending')

    def test_cached_status_and_retries_bypass_modal_ui_and_other_reads_refuse(self):
        self.start(); self.a._launch_eagle_pair_import(self.args['request_id'])
        self.a._token='secret'
        def request(op,args):
            client,server=socket.socketpair()
            worker=self.a.threading.Thread(target=self.a._serve_conn,args=(server,)); worker.start()
            with client:
                client.settimeout(2)
                client.sendall((json.dumps({'token':'secret','op':op,'args':args,'timeout':0.1})+'\n').encode())
                result=json.loads(client.recv(65536))
            worker.join(2)
            return result
        self.assertTrue(request('eagle_pair_import_status',{'request_id':self.args['request_id']})['ok'])
        self.assertTrue(request('start_eagle_pair_import',self.args)['ok'])
        self.assertEqual(request('context',{})['error']['code'],'import_pending')

    def add_import(self, linked=True):
        design, sch, brd = family()
        for d in (design, sch, brd):
            d.isSaved=False; d.isModified=True
        if not linked: sch.schematic.parentDesign = self.originals[0].ecadDesign
        self.docs.extend((design, sch, brd))
        self.a._export_product = lambda prod, kind: {'path':str(self.sch if kind=='schematic' else self.brd)}
        self.saves = []
        def save(name, folder, *args):
            self.saves.append((name,folder))
            for d in (design,sch,brd):
                d.isSaved=True; d.isModified=False
                d.dataFile=types.SimpleNamespace(id=d.kind+'-new-id',name=name,parentFolder=folder,parentProject=self.project)
            return True
        design.saveAs=save
        return design,sch,brd

    def test_finish_requires_new_linked_family_and_saves_only_that_family(self):
        self.start(); self.a._launch_eagle_pair_import(self.args['request_id']); self.add_import()
        result=self.a.op_finish_eagle_pair_import({'request_id':self.args['request_id']})
        self.assertEqual(result['status'],'saved_linked')
        self.assertEqual(self.saves,[('New product',self.folder)])
        self.assertEqual(self.a.op_finish_eagle_pair_import({'request_id':self.args['request_id']}),result)
        self.assertEqual(len(self.saves),1)
        self.assertTrue(all(d.isSaved and not d.isModified for d in self.originals))

    def test_wrong_link_or_readback_mismatch_never_saves(self):
        for linked in (False,True):
            with self.subTest(linked=linked):
                self.a._pair_import_records={}; self.docs=self.originals[:]
                self.start(); self.a._launch_eagle_pair_import(self.args['request_id']); self.add_import(linked)
                if linked: self.a._export_product=lambda prod,kind: {'path':str(self.brd)}
                result=self.a.op_finish_eagle_pair_import({'request_id':self.args['request_id']})
                self.assertEqual(result['status'],'state_unknown')
                self.assertEqual(self.saves,[])

    def test_uncertain_save_is_not_submitted_twice(self):
        self.start(); self.a._launch_eagle_pair_import(self.args['request_id'])
        design,sch,brd=self.add_import()
        design.saveAs=lambda *args: self.saves.append(args) or False
        for _ in range(2):
            self.assertEqual(self.a.op_finish_eagle_pair_import({'request_id':self.args['request_id']})['status'],'state_unknown')
        self.assertEqual(len(self.saves),1)

    def test_staged_tampering_prevents_launch(self):
        result=self.start()
        Path(result['staged']['board']).write_text(BRD.replace('GND','VCC'))
        self.a._launch_eagle_pair_import(self.args['request_id'])
        self.assertEqual(self.starts,[])

    def test_dispatch_uses_bridge_job_and_acknowledgement_not_return_value(self):
        self.start()
        fired=[]
        def fire(event_id,job_id):
            fired.append(job_id)
            self.a._Handler().notify(types.SimpleNamespace(additionalInfo=job_id))
            return False
        self.a._app.fireCustomEvent=fire
        self.a._queue_eagle_pair_dispatch(self.args['request_id'])
        status=self.a.op_eagle_pair_import_status({'request_id':self.args['request_id']})
        self.assertEqual(status['status'],'awaiting_file_selection')
        self.assertTrue(status['dispatch_acknowledged'])
        self.assertIs(status['dispatch_return'],False)
        self.assertEqual(self.starts,['start'])
        self.a._queue_eagle_pair_dispatch(self.args['request_id'])
        self.assertEqual(len(fired),1)

    def test_unacknowledged_dispatch_is_cancelled_before_known_not_started_retry(self):
        first=self.start()
        self.a._app.fireCustomEvent=lambda *args: None
        self.a._queue_eagle_pair_dispatch(self.args['request_id'])
        record=self.a._pair_import_records[self.args['request_id']]
        stale_job=record['dispatch_job']
        self.assertEqual(self.a.op_eagle_pair_import_status({'request_id':self.args['request_id']})['status'],'queued')
        record['dispatch_deadline']=0
        self.assertEqual(self.a.op_eagle_pair_import_status({'request_id':self.args['request_id']})['status'],'not_started')
        self.a._Handler().notify(types.SimpleNamespace(additionalInfo=stale_job))
        self.assertEqual(self.starts,[])
        self.a._events.session_id='new-session'
        old_info=self.a._doc_info
        self.a._doc_info=lambda d: dict(old_info(d),document_id='new-'+str(id(d)))
        resumed=self.start()
        self.assertEqual(resumed['status'],'queued')
        self.assertEqual(resumed['session_id'],'new-session')
        self.assertEqual(resumed['staged'],first['staged'])
        self.assertEqual(self.starts,[])

    def test_read_only_probe_uses_same_dispatch_and_never_launches_import(self):
        def fire(event_id,job_id):
            self.a._Handler().notify(types.SimpleNamespace(additionalInfo=job_id))
            return None
        self.a._app.fireCustomEvent=fire
        self.a._queue_eagle_pair_dispatch()
        self.assertEqual(self.a._pair_dispatch_probe['status'],'acknowledged')
        self.assertIsNone(self.a._pair_dispatch_probe['dispatch_return'])
        self.assertEqual(self.starts,[])

    def test_controlled_stop_retains_only_known_not_started_tickets(self):
        self.start()
        self.a._pair_import_records[self.args['request_id']]['public']['status']='not_started'
        self.a._pair_import_records['b'*32]={'public':{'status':'state_unknown'}}
        self.a._events=None
        self.a._server=types.SimpleNamespace(close=lambda: None)
        self.a.INFO_PATH=str(self.root/'absent-bridge.json')
        self.a.stop(None)
        retained=getattr(self.a.adsk.core,self.a._PAIR_RETAINED_ATTR)
        self.assertEqual(list(retained),[self.args['request_id']])
        self.assertEqual(self.starts,[])

    def test_known_not_started_retry_refuses_another_pending_import(self):
        self.start()
        self.a._pair_import_records[self.args['request_id']]['public']['status']='not_started'
        self.a._pair_import_records['b'*32]={'public':{'status':'queued'}}
        with self.assertRaisesRegex(ValueError,'pending'): self.start()
        self.assertEqual(self.a._pair_import_records[self.args['request_id']]['public']['status'],'not_started')


if __name__ == '__main__': unittest.main()
