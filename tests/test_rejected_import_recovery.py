import hashlib
import importlib.util
import json
from pathlib import Path
import types
import unittest

import test_eagle_pair_import as F
from test_document_lifecycle import family, NativeProduct
from fusion_mcp.addin.FusionElectronicsMCP.management import EventLog


SCRIPT=Path(__file__).resolve().parents[1]/'tools/recover_rejected_import.py'


class RejectedRecoveryTest(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('rejected_import_recovery',SCRIPT)
        self.module=importlib.util.module_from_spec(spec); spec.loader.exec_module(self.module)
        self.fx=F.PairImportTest('test_sources_are_pinned_and_staged_without_modification')
        self.fx.setUp(); self.addCleanup(self.fx.doCleanups)
        self.a=self.fx.a
        self.a.INFO_DIR=str(self.fx.root/'info')
        self.fx.start(); self.a._launch_eagle_pair_import(self.fx.args['request_id'])
        self.a._pair_import_records[self.fx.args['request_id']]['public']['dispatch_acknowledged']=True
        self.a._events=EventLog(); self.a._events.session_id='session'
        self.a._events.append('commandStarting',command_id='ImportSCHAndBRDCmd')
        self.a._events.append('commandTerminated',command_id='ImportSCHAndBRDCmd')
        self.parent,self.sch,_=family()
        self.parent.ecadDesign.board=None; self.sch.schematic.linkedBoard=None
        for d in (self.parent,self.sch): d.name='Untitled'; d.isSaved=False; d.isModified=True
        self.fx.docs.extend((self.parent,self.sch))
        self.a._docs=lambda:[d for d in self.fx.docs if not d.closed]
        original_product=self.a._document_product
        self.product_docs={id(original_product(d,d.kind)):d for d in self.fx.docs}
        self.a._document_product=lambda d,k:NativeProduct(original_product(d,k),self.product_docs)
        self.empty=self.fx.root/'empty.sch'
        self.empty.write_text('<eagle><drawing><schematic><parts/><sheets><sheet><plain/><instances/><nets/><busses/></sheet></sheets></schematic></drawing></eagle>')
        def export(product,kind):
            product=getattr(product,'wrapped',product)
            return {'path':str(self.empty if product is self.sch.schematic else self.fx.sch if kind=='schematic' else self.fx.brd)}
        self.a._export_product=export
        self.config={'request_id':self.fx.args['request_id'],'session_id':'session','apply':True,
                     'schematic_sha256':self.fx.args['schematic_sha256'],'board_sha256':self.fx.args['board_sha256'],
                     'rejection_acknowledged':True,'rejection_message':'Source.sch has errors and cannot be opened. Close the empty design file without saving and continue working.',
                     'original_exports':{'schematic':hashlib.sha256(self.fx.sch.read_bytes()).hexdigest(),
                                         'board':hashlib.sha256(self.fx.brd.read_bytes()).hexdigest()},
                     }

    def test_only_empty_new_family_closes_and_ticket_becomes_terminal(self):
        result=self.module.reconcile(self.a,self.config)
        self.assertEqual(result['status'],'cancelled_native_rejection')
        self.assertTrue(self.parent.closed and self.sch.closed)
        self.assertTrue(all(not d.closed for d in self.fx.originals))
        self.assertFalse(self.a._pair_import_pending())
        self.assertEqual(self.module.reconcile(self.a,self.config),result)
        self.assertEqual(json.loads(Path(result['receipt_path']).read_text())['status'],'cancelled_native_rejection')

    def test_nonempty_schematic_refuses_before_any_close(self):
        self.empty.write_text(F.SCH)
        with self.assertRaisesRegex(ValueError,'empty'): self.module.reconcile(self.a,self.config)
        self.assertFalse(self.parent.closed or self.sch.closed)
        self.assertTrue(self.a._pair_import_pending())

    def test_finish_preserves_cancelled_native_rejection_receipt(self):
        result=self.module.reconcile(self.a,self.config)
        status=self.a.op_eagle_pair_import_status({'request_id':self.fx.args['request_id']})
        self.assertEqual(self.a.op_finish_eagle_pair_import({'request_id':self.fx.args['request_id']}),status)
        self.assertFalse(self.a._pair_import_pending())
        self.assertEqual(json.loads(Path(result['receipt_path']).read_text()),result)

    def test_default_is_read_only_preflight(self):
        result=self.module.reconcile(self.a,dict(self.config,apply=False))
        self.assertEqual(result['status'],'recovery_preflight_passed')
        self.assertFalse(self.parent.closed or self.sch.closed)
        self.assertNotIn('recovery_attempted',self.a._pair_import_records[self.fx.args['request_id']])

    def test_native_empty_parent_missing_links_and_unlinked_child(self):
        product=self.a._document_product
        self.a._document_product=lambda d,k:NativeProduct(product(d,k).wrapped,self.product_docs,d is self.parent)
        self.sch.schematic.parentDesign=None
        result=self.module.reconcile(self.a,dict(self.config,apply=False))
        self.assertEqual(result['status'],'recovery_preflight_passed')
        self.assertFalse(self.parent.closed or self.sch.closed)

    def test_foreign_original_parent_document_refuses_before_close(self):
        self.product_docs[id(self.fx.originals[0].ecadDesign)]=self.parent
        with self.assertRaisesRegex(ValueError,'original.*parent'):
            self.module.reconcile(self.a,self.config)
        self.assertFalse(self.parent.closed or self.sch.closed)

    def test_linked_export_mismatch_refuses_even_with_same_parent(self):
        export=self.a._export_product
        calls=[]
        def changed_link(product,kind):
            calls.append(kind)
            return {'path':str(self.empty)} if len(calls)==3 else export(product,kind)
        self.a._export_product=changed_link
        with self.assertRaisesRegex(ValueError,'linked export hash'):
            self.module.reconcile(self.a,self.config)
        self.assertFalse(self.parent.closed or self.sch.closed)

    def test_unexpected_native_link_error_is_not_missing_link(self):
        class Unexpected:
            @property
            def schematic(self): raise RuntimeError('different adapter failure')
        with self.assertRaisesRegex(RuntimeError,'different adapter'):
            self.module._empty_parent_link(Unexpected(),'schematic')

    def test_wrong_session_unacknowledged_rejection_or_changed_original_refuses(self):
        for changes in ({'session_id':'other'},{'rejection_acknowledged':False},{'schematic_sha256':'0'*64}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                self.module.reconcile(self.a,dict(self.config,**changes))
        self.fx.originals[0].isModified=True
        with self.assertRaises(ValueError): self.module.reconcile(self.a,self.config)
        self.assertFalse(self.parent.closed or self.sch.closed)

    def test_original_copper_or_cross_family_link_changes_refuse(self):
        self.config['original_exports']['board']='0'*64
        with self.assertRaisesRegex(ValueError,'hash'):self.module.reconcile(self.a,self.config)
        self.config['original_exports']['board']=hashlib.sha256(self.fx.brd.read_bytes()).hexdigest()
        self.parent.ecadDesign.board=self.fx.originals[2].board
        with self.assertRaisesRegex(ValueError,'original|foreign'):self.module.reconcile(self.a,self.config)
        self.assertFalse(self.parent.closed or self.sch.closed)

    def test_partial_close_never_blindly_retries(self):
        self.parent.close=lambda save:False
        with self.assertRaises(ValueError):self.module.reconcile(self.a,self.config)
        self.assertTrue(self.sch.closed);self.assertFalse(self.parent.closed)
        with self.assertRaisesRegex(ValueError,'already attempted'):self.module.reconcile(self.a,self.config)
        self.assertTrue(all(not d.closed for d in self.fx.originals))

    def test_truncated_event_history_refuses_before_any_close(self):
        for _ in range(200):self.a._events.append('commandStarting',command_id='Other')
        with self.assertRaisesRegex(ValueError,'history'):self.module.reconcile(self.a,self.config)
        self.assertFalse(self.parent.closed or self.sch.closed)


if __name__=='__main__':unittest.main()
