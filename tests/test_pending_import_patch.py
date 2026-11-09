import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest import mock

from fusion_mcp.addin.FusionElectronicsMCP import ulp_support


class PendingPatchTest(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('pending_import_patch',Path(__file__).resolve().parents[1]/'tools/patch_pending_import_finish.py')
        self.patch=importlib.util.module_from_spec(spec); spec.loader.exec_module(self.patch)
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.root=Path(tmp.name)
        self.old=self.root/'old.py'; self.old.write_text('def op_finish_eagle_pair_import(args):\n    return "old"\n')
        self.new=self.root/'new.py'
        self.new.write_text('raise AssertionError("module code must never run")\n'+''.join(
            'def '+name+'(args):\n    return "new"\n' for name in self.patch.FUNCTIONS))
        self.live=live=types.ModuleType('native'); live.__file__=str(self.old)
        exec(compile(self.old.read_text(),str(self.old),'exec'),live.__dict__)
        live.ADDIN_VERSION='0.14.22'; live._events=types.SimpleNamespace(session_id='session')
        live.OPS={'finish_eagle_pair_import':live.op_finish_eagle_pair_import,'other':object()}
        self.other=live.OPS['other']; live._PAIR_PENDING=('state_unknown',)
        live._jobs={}; live._pair_import_lock=threading.RLock(); live.INFO_DIR=str(self.root/'info')
        live._management=lambda:types.SimpleNamespace(U=ulp_support)
        originals=[types.SimpleNamespace(id='original'+str(i),kind='other',isSaved=True,dataFile=object()) for i in range(4)]
        self.created=[types.SimpleNamespace(id=k,kind=k,isSaved=False,dataFile=None) for k in ('design','schematic','board')]
        live._docs=lambda:originals+self.created; live._doc_info=lambda d:{'document_id':d.id}; live._kind_of=lambda d:d.kind
        live._import_preserved=lambda record:{d.id for d in originals}; live._import_source=lambda record:None
        live._import_update=lambda record,**values:record['public'].update(values)
        live._pair_import_records={'a'*32:{'public':{'status':'state_unknown','destination':{'name':'New'}},
                                           'args':{'schematic_sha256':'1'*64,'board_sha256':'2'*64}}}
        self.config={'request_id':'a'*32,'session_id':'session','schematic_sha256':'1'*64,'board_sha256':'2'*64,
                     'destination':{'name':'New'},'document_ids':{d.kind:d.id for d in self.created},
                     'protected_document_ids':[d.id for d in originals],'original_exports':{},
                     'bridge_source_sha256':self.patch.digest(self.old),'patched_source_path':str(self.new),
                     'patched_source_sha256':self.patch.digest(self.new)}
        self.original=live.op_finish_eagle_pair_import

    def install(self,**changes): return self.patch.patch(self.live,dict(self.config,**changes),lambda *args:None)

    def test_preflight_does_not_change_dispatch_and_apply_replaces_only_reviewed_functions(self):
        self.assertEqual(self.install()['status'],'completion_patch_preflight_passed')
        self.assertIs(self.live.OPS['finish_eagle_pair_import'],self.original)
        result=self.install(apply=True)
        self.assertEqual(result['status'],'completion_patch_installed')
        self.assertEqual(self.live.OPS['finish_eagle_pair_import']({}),'new')
        self.assertIs(self.live.OPS['other'],self.other)
        self.assertEqual(json.loads(Path(result['receipt_path']).read_text())['status'],result['status'])
        with self.assertRaises(ValueError): self.install(apply=True)

    def test_source_and_running_bytecode_changes_refuse(self):
        with self.assertRaisesRegex(ValueError,'source hash'): self.install(patched_source_sha256='0'*64)
        exec(compile('def op_finish_eagle_pair_import(args):\n    return "different"\n',str(self.old),'exec'),self.live.__dict__)
        self.live.OPS['finish_eagle_pair_import']=self.live.op_finish_eagle_pair_import
        with self.assertRaisesRegex(ValueError,'bytecode'): self.install()

    def test_equivalent_code_ignores_marshal_reference_layout(self):
        with mock.patch.object(self.patch.marshal,'dumps',side_effect=[b'runtime-layout',b'compiled-layout']):
            self.assertEqual(self.install()['status'],'completion_patch_preflight_passed')

    def test_old_full_source_compilation_retains_future_flags_without_module_execution(self):
        self.old.write_text('from __future__ import annotations\ndef op_finish_eagle_pair_import(args):\n    return "old"\nraise AssertionError("old module must never run")\n')
        module=compile(self.old.read_text(),str(self.old),'exec',dont_inherit=True)
        code=next(c for c in module.co_consts if isinstance(c,types.CodeType) and c.co_name=='op_finish_eagle_pair_import')
        self.live.op_finish_eagle_pair_import=types.FunctionType(code,self.live.__dict__)
        self.live.OPS['finish_eagle_pair_import']=self.live.op_finish_eagle_pair_import
        self.config['bridge_source_sha256']=self.patch.digest(self.old)
        self.assertEqual(self.install()['status'],'completion_patch_preflight_passed')

    def test_changed_ticket_or_document_bindings_refuse_without_dispatch_change(self):
        for change in ({'schematic_sha256':'0'*64},{'destination':{'name':'Other'}},
                       {'document_ids':{'design':'foreign'}},{'protected_document_ids':['missing']}):
            with self.subTest(change=change),self.assertRaises(ValueError): self.install(**change)
        self.created[0].isSaved=True
        with self.assertRaises(ValueError): self.install()
        self.assertIs(self.live.OPS['finish_eagle_pair_import'],self.original)

    def test_duplicate_or_decorated_definition_refuses(self):
        self.new.write_text('def op_finish_eagle_pair_import(args): pass\ndef op_finish_eagle_pair_import(args): pass\n')
        with self.assertRaises(ValueError): self.install(patched_source_sha256=self.patch.digest(self.new))
        self.new.write_text('@unknown\ndef op_finish_eagle_pair_import(args): pass\n')
        with self.assertRaises(ValueError): self.patch.definitions(self.new,self.patch.digest(self.new),('op_finish_eagle_pair_import',))
