"""Local evidence I/O and append-only review records."""
from pathlib import Path
import datetime
import hashlib
import json
from fusion_offline import pin_evidence as E
from fusion_offline import design as D


def load(path):
    path=Path(path).resolve(); e=json.loads(path.read_text(encoding='utf-8-sig'))
    source=Path(e['source'])
    if not source.is_absolute(): source=path.parent/source
    actual=hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else None
    return e,actual


def record_review(path,expected_content_sha256,status,reviewer):
    if status not in ('approved','rejected') or not reviewer.strip(): raise ValueError('Explicit approval/rejection and named reviewer required')
    path=Path(path).resolve(); e,actual=load(path)
    if E.content_hash(e)!=expected_content_sha256: raise ValueError('Evidence changed since review')
    if not actual or actual!=e.get('sha256'): raise ValueError('Datasheet unavailable or changed')
    e.setdefault('reviews',[]).append({'status':status,'reviewer':reviewer,'reviewed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'content_sha256':expected_content_sha256})
    # Preserve the exact pre-review bytes beside the record before replacing it.
    backup=path.with_name(path.name+'.'+hashlib.sha256(path.read_bytes()).hexdigest()+'.bak')
    if not backup.exists(): backup.write_bytes(path.read_bytes())
    temp=path.with_suffix('.review.tmp'); temp.write_text(json.dumps(e,indent=2),encoding='utf-8'); temp.replace(path)
    return {'status':status,'content_sha256':expected_content_sha256,'backup':str(backup)}


def check(session,library,part_ids=None):
    from .edit_plan import snapshot
    with session.bridge.lock.hold('read-only datasheet comparison'):
        before=snapshot(session,[]); sch=D.parse_schematic_design(D.read_xml(before['sch_xml'].encode()))
        out=[]; evidence={}; selected={}
        for pid in part_ids if part_ids is not None else library.parts:
            p=library.get(pid).data; selected[pid]=p
            path=Path(library.directory).parent/'pin-evidence'/f'{pid}.json'
            if path.is_file():
                e,actual=load(path); evidence[pid]=e; out+=E.validate_pin_evidence(p,e,actual)
            else: out.append(E.finding('missing_evidence','unknown',refs=[pid]))
        out+=E.compare_pin_evidence(sch,selected,evidence)
        after=snapshot(session,[])
        if before!=after: raise ValueError('Design changed during read-only evidence check')
        return E.report(out)
