"""Replace only reviewed completion functions for one pinned pending native import.

This does not finish, save, close, import, or reload anything. The normal public
MCP finish call owns the transaction afterward. Default is read-only preflight.
"""
import ast
import hashlib
import json
import marshal
import os
from pathlib import Path
import runpy
import sys
import types

FUNCTIONS=('_verify_import_links','_pending_import_first_save','op_finish_eagle_pair_import')


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def definitions(path, expected_sha256, names):
    raw=Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected_sha256: raise ValueError('Reviewed source hash changed')
    tree=ast.parse(raw)
    nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
    if (len(nodes)!=len(names) or {node.name for node in nodes}!=set(names)
            or any(node.decorator_list or node.args.defaults or node.args.kw_defaults for node in nodes)):
        raise ValueError('Expected only the exact reviewed completion definitions')
    return ast.Module(body=nodes,type_ignores=[])


def patch(live,config,verify_originals):
    rid=config['request_id']; record=live._pair_import_records[rid]
    if (live.ADDIN_VERSION!='0.14.22' or live._events.session_id!=config['session_id']
            or record['public']['status']!='state_unknown' or record.get('save_requested')
            or record.get('completion_patch_attempted')
            or [key for key,value in live._pair_import_records.items() if value['public']['status'] in live._PAIR_PENDING]!=[rid]):
        raise ValueError('Expected only the pinned unsaved pending 0.14.22 request')
    for key in ('schematic_sha256','board_sha256'):
        if record['args'][key]!=config[key]: raise ValueError('Pending source pins changed')
    if any(record['public']['destination'].get(k)!=v for k,v in config['destination'].items()):
        raise ValueError('Pending destination changed')
    before_ids=live._import_preserved(record); live._import_source(record)
    if before_ids!=set(config['protected_document_ids']): raise ValueError('Protected documents changed')
    created=[d for d in live._docs() if live._doc_info(d)['document_id'] not in before_ids]
    if (len(created)!=3 or any(d.isSaved or d.dataFile is not None for d in created)
            or {live._kind_of(d):live._doc_info(d)['document_id'] for d in created}!=config['document_ids']):
        raise ValueError('Pending imported document identities changed')
    verify_originals(live,before_ids,config['original_exports'])
    owner=Path(live.__file__)
    old=live.op_finish_eagle_pair_import
    if (os.path.normcase(os.path.realpath(old.__code__.co_filename))!=os.path.normcase(str(owner.resolve()))
            or live.OPS['finish_eagle_pair_import'] is not old):
        raise ValueError('Completion dispatch is not the expected installed function')
    raw=owner.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=config['bridge_source_sha256']: raise ValueError('Installed source hash changed')
    previous=compile(raw,old.__code__.co_filename,'exec',dont_inherit=True,optimize=sys.flags.optimize)
    expected=[code for code in previous.co_consts if isinstance(code,types.CodeType) and code.co_name=='op_finish_eagle_pair_import']
    old_hash=hashlib.sha256(marshal.dumps(old.__code__)).hexdigest()
    # Marshal includes reference layout; equal code objects can serialize differently.
    if len(expected)!=1 or old.__code__!=expected[0]:
        raise ValueError('Running completion bytecode differs from installed reviewed source')
    replacement=definitions(config['patched_source_path'],config['patched_source_sha256'],FUNCTIONS)
    code=compile(replacement,str(Path(config['patched_source_path']).resolve()),'exec',dont_inherit=True)
    result={'status':'completion_patch_preflight_passed','request_id':rid,'session_id':config['session_id'],
            'functions':list(FUNCTIONS),'old_function_sha256':old_hash,'source_sha256':config['patched_source_sha256'],
            'document_ids':config['document_ids'],'saved':False,'reloaded':False}
    if config.get('apply') is not True: return result
    if live._jobs: raise ValueError('Bridge jobs are outstanding; no completion patch installed')
    with live._pair_import_lock:
        record['completion_patch_attempted']=True
        record['first_save_documents']=[config['document_ids'][k] for k in ('design','schematic','board')]
        exec(code,live.__dict__)
        live.OPS['finish_eagle_pair_import']=live.op_finish_eagle_pair_import
        live._import_update(record,completion_patch_sha256=config['patched_source_sha256'])
    live._import_preserved(record); live._import_source(record)
    verify_originals(live,before_ids,config['original_exports'])
    result['status']='completion_patch_installed'
    receipt=Path(live.INFO_DIR)/'import-recoveries'/(rid+'-'+config['session_id']+'-completion-patch.json')
    result['receipt_path']=str(receipt)
    live._management().U.atomic_json(receipt,result)
    return result


def run(config_path):
    config=json.loads(Path(config_path).read_text(encoding='utf-8'))
    if os.getpid()!=config['pid']: raise ValueError('Completion patch belongs to another native process')
    helper=Path(__file__).with_name('recover_rejected_import.py')
    if digest(helper)!=config['recovery_helper_sha256']: raise ValueError('Reviewed bridge verifier changed')
    recovery=runpy.run_path(str(helper))
    result=patch(recovery['live_bridge'](config),config,recovery['_original_exports'])
    print(json.dumps(result,indent=2))
    return result
