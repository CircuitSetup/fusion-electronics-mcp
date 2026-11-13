"""All new batch writes pass through immutable, stale-checked edit plans."""
from pathlib import Path
import hashlib
import json
from fusion_offline import edit_plan as P
from . import data_dir, commands as C


def plan_dir(): return Path(data_dir('edit-plans'))


def sources_for(operations,evidence=None):
    paths={o[k] for o in operations for k in ('library_path','evidence_path') if k in o}
    paths.update((evidence or {}).get(k) for k in ('block_path','rules_path') if (evidence or {}).get(k))
    out={}
    for p in sorted(paths):
        path=Path(p)
        if not path.is_absolute() or not path.is_file(): raise ValueError(f'Local source unavailable: {p}')
        raw=path.read_bytes()
        out[p]={'sha256':hashlib.sha256(raw).hexdigest(),'content':raw.decode('utf-8-sig')}
    for o in operations:
        if 'evidence_path' not in o: continue
        p=o['evidence_path']; evidence=json.loads(out[p]['content']); source=Path(evidence['source'])
        if not source.is_absolute(): source=Path(p).parent/source
        if source.is_file(): out['datasheet:'+p]={'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
    return out


def snapshot(session,operations,evidence=None):
    before=session.context().get('active_document') or {}
    if before.get('kind')!='schematic': raise ValueError('Activate the target schematic before preview/apply')
    snap=session.snapshot()
    after=session.context().get('active_document') or {}
    if before.get('document_id')!=after.get('document_id'): raise ValueError('Active document changed during snapshot')
    return dict(P.identity(before), sch_xml=snap.sch_xml.decode(),board_xml=snap.board_xml.decode() if snap.board_xml else None,sources=sources_for(operations,evidence))


def preview(session,operations,expected_document_id,directory=None,evidence=None):
    directory=Path(directory) if directory else plan_dir()
    with session.bridge.lock.hold('preview schematic edits'):
        snap=snapshot(session,operations,evidence)
        if snap['document_id']!=expected_document_id: raise ValueError('Wrong document')
        plan=P.build_plan(snap,operations,evidence or {})
        page=P.preview_html(snap,plan)
        if P.fingerprint(snapshot(session,operations,evidence))!=P.fingerprint(snap): raise ValueError('Design or sources changed during preview')
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/(plan['sha256']+'.json'); view=directory/(plan['sha256']+'.html')
    content=json.dumps(plan,sort_keys=True,indent=2)
    if path.exists() and path.read_text(encoding='utf-8')!=content: raise ValueError('Stored plan has been altered')
    if not path.exists():
        with path.open('x',encoding='utf-8') as f: f.write(content)
    view.write_text(page,encoding='utf-8')
    return {'status':'preview','sha256':plan['sha256'],'plan_path':str(path),'preview_path':str(view),'findings':plan['findings'],
            'instruction':'Review this exact preview/hash before authorizing apply. No CAD edits were made.'}


def compile_operation(xml,op):
    if op['op'] in ('connect','label_net'): op=dict(op,net=op['net'].upper())
    kind=op['op']; sch=P.D.parse_schematic_design(P.D.read_xml(xml.encode()))
    if kind=='add_sheet': return C.new_sheet(op['sheet'],op['title'],None,None)
    if kind=='add_part': return C.add_part(op['deviceset']+op['device'],op['library'],op['ref'],op['x'],op['y'],op['rotation'],op['sheet'])
    if kind=='place_unit': return C.invoke_unit(op['part'],op['gate'],op['x'],op['y'],op['rotation'],op['sheet'])
    if kind=='connect':
        from fusion_offline import sch_wiring as W
        pins=[P.pin(sch,p) for p in op['pins']]
        return C.schematic_wiring(op['net'],W.plan(P.D.read_xml(xml.encode()),op['net'],pins,op.get('style','wired')))
    if kind=='label_net':
        commands=[]
        for seg in sch.nets[op['net']].segments:
            if seg.sheet==op['sheet'] and not seg.labels and seg.first_wire:
                x,y,ex,ey=seg.first_wire
                commands.append(f'{C.sheet_prefix(op["sheet"])} LABEL {C.pt((x+ex)/2,(y+ey)/2)} {C.pt(ex,ey)};')
        return ' '.join(commands)
    raise ValueError('Unknown operation')


def apply(session,plan_path,expected_sha256,directory=None):
    directory=(Path(directory) if directory else plan_dir()).resolve()
    path=Path(plan_path).resolve()
    if path.parent!=directory or path.name!=expected_sha256+'.json': raise ValueError('Plan path/hash is not in the managed plan directory')
    plan=json.loads(path.read_text(encoding='utf-8'))
    if plan.get('sha256')!=expected_sha256: raise ValueError('Unexpected plan hash')
    completed=[]
    with session.bridge.lock.hold('apply schematic plan'):
        snap=snapshot(session,plan['operations'],plan['evidence'])
        if P.validate_plan(plan,snap): raise ValueError('Stale or altered plan; create and review a new preview')
        rebuilt=P.build_plan(snap,plan['operations'],plan['evidence'])
        if P.digest(rebuilt)!=P.digest(plan) or any(f.get('status') in ('fail','unknown') for f in plan['findings']): raise ValueError('Plan blocked or inconsistent')
        xml=snap['sch_xml']; last=snap
        for index,op in enumerate(plan['operations']):
            commands=compile_operation(xml,op)
            expected=P.simulate(xml,[op],snap['sources'])
            try:
                current=snapshot(session,plan['operations'],plan['evidence'])
                if P.fingerprint(current)!=P.fingerprint(last): raise ValueError('Design or sources changed between operations')
                if commands:
                    result=session.bridge.call('run',{'commands':session.with_grid(commands) if hasattr(session,'with_grid') else commands,
                         'editor':'schematic','expected_document_id':snap['document_id']},timeout=120)
                    if any(not d.get('expected') for d in (result or {}).get('dialogs',[])): raise ValueError('Unexpected Fusion dialog; inspect the design')
                after=snapshot(session,plan['operations'],plan['evidence'])
                if P.identity(after)!=P.identity(snap) or P.electrical(after['sch_xml'])!=P.electrical(expected): raise ValueError('Read-back did not match the reviewed operation')
                if after['sources']!=snap['sources']: raise ValueError('Source changed during apply')
                verify_board(last,after,expected)
                if op['op'] == 'add_part' and after['board_xml'] is not None:
                    from fusion_offline import part_placement as placement
                    from .server import _place_added_part
                    from .session import Snapshot

                    board = P.D.read_xml(after['board_xml'].encode())
                    if op['ref'] in P.D.parse_board_design(board).element_names:
                        proposed = placement.place_existing(board, op['ref'], placement.default_origin(board))
                        _place_added_part(session, Snapshot(board_xml=after['board_xml'].encode(),
                                                            sch_xml=after['sch_xml'].encode()), proposed)
                        session.activate('schematic')
                        after = snapshot(session,plan['operations'],plan['evidence'])
                        if P.identity(after)!=P.identity(snap) or P.electrical(after['sch_xml'])!=P.electrical(expected):
                            raise ValueError('PCB placement changed the reviewed schematic operation')
                xml=after['sch_xml']; last=after; completed.append(index)
            except Exception as ex:
                # No blind undo: multi-sheet commands and timeouts do not have a proven single undo boundary.
                return {'status':'state_unknown','completed_operations':completed,'uncertain_operation':index,'error':str(ex),
                        'recovery':'Stop editing. Inspect the target design and re-export before manual recovery; do not retry this plan.'}
    return {'status':'applied','completed_operations':completed,'sha256':expected_sha256,'saved':False}



def verify_board(before,after,expected_sch):
    """Preserve existing physical layout and verify every new pad-to-net annotation."""
    if before['board_xml'] is None:
        if after['board_xml'] is not None: raise ValueError('Unexpected linked board')
        return
    if after['board_xml'] is None: raise ValueError('Linked board disappeared')
    old=P.D.read_xml(before['board_xml'].encode()); new=P.D.read_xml(after['board_xml'].encode())
    for path in ('./drawing/board/elements/element','./drawing/board/plain/*','./drawing/board/designrules'):
        for item in old.findall(path):
            same=[x for x in new.findall(path) if x.tag==item.tag and x.get('name')==item.get('name')]
            if not any(P.xml_hash(P.ET.tostring(x))==P.xml_hash(P.ET.tostring(item)) for x in same):
                raise ValueError('An existing board element, outline or rule changed')
    sch=P.D.parse_schematic_design(P.D.read_xml(expected_sch.encode()))
    board=P.D.parse_board_design(new)
    expected_defs=P.library_definitions(P.D.read_xml(expected_sch.encode()))
    for el in new.findall('./drawing/board/elements/element'):
        lib=next((l for l in new.findall('./drawing/board/libraries/library') if l.get('name')==el.get('library')),None)
        pkg=next((p for p in lib.iter('package') if p.get('name')==el.get('package')),None) if lib is not None else None
        if el.get('name') not in expected_defs or P.geometry_hash(pkg)!=expected_defs[el.get('name')]['package']:
            raise ValueError('Board footprint geometry does not match the reviewed schematic library')
    physical={r for r,p in sch.parts.items() if p.package}
    if set(board.element_names)!=physical: raise ValueError('Physical package count does not match schematic')
    for n in sch.nets.values():
        for r in n.pins:
            p=sch.pin(r.part,r.pin,r.gate)
            if p is None: raise ValueError('Missing schematic pin during board verification')
            for pad in (p.pad or '').split():
                if (p.part,pad) not in (board.signals[n.name].contacts if n.name in board.signals else []):
                    raise ValueError('Board pad-to-net annotation mismatch')
    for n in old.findall('./drawing/board/signals/signal'):
        target=next((x for x in new.findall('./drawing/board/signals/signal') if x.get('name')==n.get('name')),None)
        if target is None: raise ValueError('Existing board net disappeared')
        for item in n:
            if not any(P.xml_hash(P.ET.tostring(x))==P.xml_hash(P.ET.tostring(item)) for x in target):
                raise ValueError('Existing board connectivity or routing changed')
