"""Fixed native-console recovery for a confirmed rejected paired-EAGLE import.

Run only inside Fusion with a reviewed, pinned recovery JSON. This does not reload
the add-in, save documents, dismiss dialogs, execute CAD commands, or retry import.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _original_exports(live, before_ids, expected):
    docs=[doc for doc in live._docs() if live._doc_info(doc)['document_id'] in before_ids]
    families={kind:[d for d in docs if live._kind_of(d)==kind] for kind in ('design','schematic','board')}
    if any(len(families[k])!=1 for k in families):
        raise ValueError('Cannot identify the single protected original Electronics family')
    products={kind:live._document_product(families[kind][0],kind) for kind in families}
    design,sch,brd=(products[k] for k in ('design','schematic','board'))
    for parent in (design,sch.parentDesign,brd.parentDesign):
        if parent is None or not live._same_document(parent.parentDocument,families['design'][0]):
            raise ValueError('Protected original family parent identity changed')
    exports={kind:live._export_product(products[kind],kind)['path'] for kind in ('schematic','board')}
    if {kind:_hash(path) for kind,path in exports.items()}!=expected:
        raise ValueError('Protected original export hash differs from the approved recovery baseline')
    # Electronics adapters can compare unequal; verify linked exports and saved parent identity instead.
    for label,product,kind in (('design.schematic',design.schematic,'schematic'),('design.board',design.board,'board'),
                               ('schematic.linkedBoard',sch.linkedBoard,'board'),('board.linkedSchematic',brd.linkedSchematic,'schematic')):
        if (product is None or product.parentDesign is None
                or not live._same_document(product.parentDesign.parentDocument,families['design'][0])):
            raise ValueError('Protected original family linked parent identity changed')
        exports[label]=live._export_product(product,kind)['path']
        if _hash(exports[label])!=expected[kind]:
            raise ValueError('Protected original family linked export hash changed')
    return exports


def _empty_parent_link(design,name):
    try: return getattr(design,name)
    except RuntimeError as ex:
        if str(ex)=='2 : InternalValidationError : '+name+'Link': return None
        raise


def reconcile(live, config):
    rid=live._pair_support().request_id(config['request_id'])
    if live._events.session_id!=config['session_id']:
        raise ValueError('Recovery bridge session changed')
    record=live._pair_import_records[rid]
    if record['public']['status']=='cancelled_native_rejection': return record['public']['recovery']
    if (record['public']['status'] not in ('state_unknown','awaiting_file_selection')
            or not record['public'].get('dispatch_acknowledged') or record.get('save_requested')):
        raise ValueError('Only the unsaved, acknowledged rejected import can be recovered')
    if record.get('recovery_attempted'): raise ValueError('Recovery was already attempted; inspect before any retry')
    pending=[key for key,value in live._pair_import_records.items() if value['public']['status'] in live._PAIR_PENDING]
    if pending!=[rid]: raise ValueError('Recovery requires exactly this one quarantined request')
    message=config.get('rejection_message','')
    if (config.get('rejection_acknowledged') is not True
            or Path(record['public']['staged']['schematic']).name not in message
            or not message.endswith('has errors and cannot be opened. Close the empty design file without saving and continue working.')):
        raise ValueError('Require the exact acknowledged native file-rejection evidence')
    for key in ('schematic_sha256','board_sha256'):
        if record['args'][key]!=config[key]: raise ValueError('Rejected source hash binding changed')
    snapshot=live._events.read(config['session_id'],0,200)
    if (snapshot.get('reset') or snapshot.get('dropped')
            or snapshot.get('next_after_seq',0)<snapshot.get('latest_seq',0)):
        raise ValueError('Complete native rejection event history is unavailable; no documents closed')
    events=snapshot['events']
    starts=[e['seq'] for e in events if e['event']=='commandStarting' and e.get('command_id')=='ImportSCHAndBRDCmd']
    if (len(starts)!=1 or not any(e['event']=='commandTerminated' and e.get('command_id')=='ImportSCHAndBRDCmd'
                                 and e['seq']>starts[0] for e in events)):
        raise ValueError('Native importer termination is not verified; cancel its owned picker first')
    if record.get('dispatch_job') in live._jobs: raise ValueError('Import callback is still outstanding')
    live._import_source(record)
    before_ids=live._import_preserved(record)
    _original_exports(live,before_ids,config['original_exports'])
    created=[d for d in live._docs() if live._doc_info(d)['document_id'] not in before_ids]
    if sorted(live._kind_of(d) for d in created)!=['design','schematic']:
        raise ValueError('Expected only the rejected import empty parent and schematic')
    if any(d.name!='Untitled' or d.isSaved or d.dataFile is not None for d in created):
        raise ValueError('Recovery targets must be only the new unsaved Untitled documents')
    parent=next(d for d in created if live._kind_of(d)=='design')
    child=next(d for d in created if live._kind_of(d)=='schematic')
    design=live._document_product(parent,'design'); sch=live._document_product(child,'schematic')
    parent_sch=_empty_parent_link(design,'schematic')
    if (not live._same_document(design.parentDocument,parent) or _empty_parent_link(design,'board') is not None
            or (sch.parentDesign is not None and not live._same_document(sch.parentDesign.parentDocument,parent))
            or sch.linkedBoard is not None):
        raise ValueError('Rejected import has an original or foreign family link')
    exported=live._export_product(sch,'schematic')['path']
    if parent_sch is not None:
        if (parent_sch.parentDesign is None or not live._same_document(parent_sch.parentDesign.parentDocument,parent)
                or _hash(live._export_product(parent_sch,'schematic')['path'])!=_hash(exported)):
            raise ValueError('Rejected import parent has a foreign schematic link')
    root=ET.parse(exported).getroot(); schematic=root.find('./drawing/schematic')
    if schematic is None: raise ValueError('Cannot verify an empty schematic export')
    containers=['./plain','./parts','./modules','./sheets/sheet/plain','./sheets/sheet/instances',
                './sheets/sheet/busses','./sheets/sheet/nets','./sheets/sheet/moduleinsts']
    if any(list(node) for path in containers for node in schematic.findall(path)):
        raise ValueError('Rejected schematic is not empty; no documents closed')
    if any((node.text or '').strip() for node in schematic.findall('.//description')):
        raise ValueError('Rejected schematic has nonempty description content')
    targets=[live._doc_info(child),live._doc_info(parent)]
    if config.get('apply') is not True:
        return {'status':'recovery_preflight_passed','request_id':rid,
                'empty_import_documents':targets,'protected_document_count':len(before_ids)}
    record['recovery_attempted']=True
    live._import_update(record,recovery_attempted=True,recovery_targets=targets)
    for doc in (child,parent):
        if any(live._same_document(doc,current) for current in live._docs()):
            try: doc.close(False)
            except Exception:
                if any(live._same_document(doc,current) for current in live._docs()): raise
            if any(live._same_document(doc,current) for current in live._docs()):
                raise ValueError('A recovery target remained open; no automatic close retry')
        live._import_preserved(record)
    if {live._doc_info(d)['document_id'] for d in live._docs()}!=before_ids:
        raise ValueError('Unexpected documents remain after rejected-import closure')
    exports=_original_exports(live,before_ids,config['original_exports'])
    receipt=Path(live.INFO_DIR)/'import-recoveries'/(rid+'-'+config['session_id']+'.json')
    result={'request_id':rid,'session_id':config['session_id'],'status':'cancelled_native_rejection',
            'closed_without_save':targets,'originals_preserved':True,'original_exports':exports,
            'rejection_message':message,'receipt_path':str(receipt)}
    live._management().U.atomic_json(receipt,result)
    live._import_update(record,status='cancelled_native_rejection',recovery=result,
                        next_action='Rejected empty family closed without saving. Use a new request ID and corrected source hashes; do not resume this rejected source.')
    return result


def live_bridge(config):
    """Find only the current authenticated native bridge pinned by the reviewed manifest."""
    info_dir=Path(os.environ['APPDATA'])/'fusion-electronics-mcp'
    info=json.loads((info_dir/'bridge.json').read_text(encoding='utf-8'))
    if info['pid']!=os.getpid() or info['event_session_id']!=config['session_id']:
        raise ValueError('Recovery is not running in its pinned native Fusion process/session')
    owner=Path(os.environ['APPDATA'])/'Autodesk/Autodesk Fusion 360/API/AddIns/FusionElectronicsMCP/FusionElectronicsMCP.py'
    if _hash(owner)!=config['bridge_source_sha256']: raise ValueError('Installed bridge source changed')
    candidates=[module for module in list(sys.modules.values()) if getattr(module,'__file__',None)
                and os.path.normcase(os.path.realpath(module.__file__))==os.path.normcase(str(owner.resolve()))
                and getattr(module,'_token',None)==info['token']]
    if len(candidates)!=1: raise ValueError('Cannot uniquely identify the current live installed bridge')
    return candidates[0]


def run(config_path):
    """Find only the current authenticated installed bridge, then apply the fixed recovery."""
    config=json.loads(Path(config_path).read_text(encoding='utf-8'))
    result=reconcile(live_bridge(config),config)
    print(json.dumps(result,indent=2))
    return result
