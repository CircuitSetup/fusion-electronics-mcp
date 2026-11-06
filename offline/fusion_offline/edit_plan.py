"""Pure, deterministic schematic edit plans. No bridge calls or CAD writes."""
from __future__ import annotations
import copy
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict
from . import design as D


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def xml_hash(xml):
    if xml is None: return None
    def node(e):
        return [e.tag, sorted(e.attrib.items()), (e.text or '').strip(), [node(c) for c in e]]
    return digest(node(D.read_xml(xml.encode() if isinstance(xml,str) else xml)))


def identity(snapshot):
    if not snapshot.get('document_id'): raise ValueError('Stable document identity unavailable; restart the updated add-in')
    return {k:snapshot.get(k) for k in ('document_id','project_id')}


def fingerprint(snapshot):
    return dict(identity(snapshot), sch=xml_hash(snapshot['sch_xml']), board=xml_hash(snapshot.get('board_xml')), sources=snapshot.get('sources',{}))


def name(s):
    if not isinstance(s,str) or not re.fullmatch(r'[A-Za-z0-9_$+!#@./%&=() -]+',s):
        raise ValueError(f'Invalid CAD name: {s!r}')
    return s


def number(x):
    if isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x): raise ValueError('Expected a finite number')
    return round(x,4)


def pin(sch, item):
    if set(item) != {'part','gate','pin'}: raise ValueError('Pins require part, gate and pin')
    for s in item.values(): name(s)
    matches=[p for p in sch.pins if (p.part,p.gate,p.pin)==(item['part'],item['gate'],item['pin'])]
    if len(matches)!=1: raise ValueError(f'Missing or ambiguous pin: {item}')
    return matches[0]


def net_of(sch,p):
    matches=[n.name for n in sch.nets.values() if any((r.part,r.gate,r.pin)==(p.part,p.gate,p.pin) for r in n.pins)]
    if len(matches)>1: raise ValueError('Pin belongs to multiple nets')
    return matches[0] if matches else None


FIELDS={
    'add_sheet': {'op','sheet','title'},
    'connect': {'op','net','pins'},
    'label_net': {'op','net','sheet'},
    'place_unit': {'op','part','gate','sheet','x','y','rotation'},
    'add_part': {'op','ref','library_path','library','deviceset','device','gate','sheet','x','y','rotation','evidence_path'},
}


def simulate(xml, operations, sources=None):
    root=D.read_xml(xml.encode() if isinstance(xml,str) else xml)
    sources=sources or {}
    for op in operations:
        kind=op.get('op')
        if kind not in FIELDS or set(op)!=FIELDS[kind]: raise ValueError(f'Invalid fields for operation {kind!r}')
        sch=D.parse_schematic_design(root); base=root.find('./drawing/schematic'); sheets=base.find('sheets')
        sheet=op.get('sheet')
        if sheet is not None and (type(sheet)!=int or not 1<=sheet<=sch.sheets+(kind=='add_sheet')): raise ValueError('Invalid sheet')
        if kind=='add_sheet':
            if sheet!=sch.sheets+1: raise ValueError('New sheet must be the next sheet')
            new=ET.SubElement(sheets,'sheet'); ET.SubElement(new,'description').text=name(op['title'])
            for tag in ('plain','instances','busses','nets'): ET.SubElement(new,tag)
        elif kind=='add_part':
            ref=name(op['ref'])
            if ref.casefold() in {r.casefold() for r in sch.parts}: raise ValueError('Duplicate reference')
            source=sources.get(op['library_path'])
            if not source or 'content' not in source: raise ValueError('Cached library XML unavailable')
            if not sources.get(op['evidence_path']): raise ValueError('Reviewed pin evidence required')
            libroot=D.read_xml(source['content'].encode()); libs=list(libroot.iter('library'))
            if len(libs)!=1: raise ValueError('Expected one library')
            lib=copy.deepcopy(libs[0]); lib.set('name',name(op['library']))
            embedded=next((l for l in base.find('libraries') if l.get('name')==op['library']),None)
            if embedded is None: base.find('libraries').append(lib)
            elif xml_hash(ET.tostring(embedded))!=xml_hash(ET.tostring(lib)): raise ValueError('Embedded library differs from cached library')
            ds=next((d for d in lib.iter('deviceset') if d.get('name')==op['deviceset']),None)
            if ds is None: raise ValueError('Unknown deviceset')
            dev=next((d for d in ds.findall('./devices/device') if d.get('name','')==op['device']),None)
            gates=ds.findall('./gates/gate')
            if dev is None or not gates or gates[0].get('name')!=op['gate']: raise ValueError('Unknown variant or initial gate')
            ET.SubElement(base.find('parts'),'part',name=ref,library=op['library'],deviceset=name(op['deviceset']),device=op['device'],value='')
            _place(sheets,op,ref,op['gate'])
        elif kind=='place_unit':
            ref=name(op['part']); gate=name(op['gate'])
            if ref not in sch.parts: raise ValueError('Unknown part')
            if any(i.part==ref and i.gate==gate for i in sch.instances): raise ValueError('Unit already invoked; moving existing units requires a separate reviewed operation')
            part=sch.parts[ref]
            lib=next(l for l in base.find('libraries') if l.get('name')==part.library)
            ds=next(d for d in lib.iter('deviceset') if d.get('name')==part.deviceset)
            if not any(g.get('name')==gate for g in ds.findall('./gates/gate')): raise ValueError('Unknown gate')
            _place(sheets,op,ref,gate)
        elif kind=='connect':
            net=name(op['net'])
            if not isinstance(op['pins'],list) or not op['pins']: raise ValueError('Explicit pins required')
            if len({tuple(sorted(p.items())) for p in op['pins']})!=len(op['pins']): raise ValueError('Duplicate pins')
            for target in op['pins']:
                p=pin(sch,target); current=net_of(sch,p)
                if current and current!=net: raise ValueError('Implicit net merge refused')
                if current==net: continue
                ns=sheets[p.sheet-1].find('nets')
                n=next((n for n in ns if n.get('name')==net),None)
                if n is None: n=ET.SubElement(ns,'net',name=net,**{'class':'0'})
                seg=ET.SubElement(n,'segment'); ET.SubElement(seg,'pinref',**target)
                ex,ey=p.x+p.outward[0]*5.08,p.y+p.outward[1]*5.08
                ET.SubElement(seg,'wire',x1=str(p.x),y1=str(p.y),x2=str(ex),y2=str(ey),width='0',layer='91')
                ET.SubElement(seg,'label',x=str(ex),y=str(ey),size='1.27',layer='95')
        elif kind=='label_net':
            name(op['net']); found=False
            for n in sheets[sheet-1].findall('./nets/net'):
                if n.get('name')!=op['net']: continue
                for seg in n.findall('segment'):
                    w=seg.find('wire')
                    if w is not None and seg.find('label') is None:
                        ET.SubElement(seg,'label',x=w.get('x2'),y=w.get('y2'),size='1.27',layer='95'); found=True
            if not found: raise ValueError('No unlabeled wired segment')
    return ET.tostring(root,encoding='unicode')


def _place(sheets,op,ref,gate):
    x,y,angle=(number(op[k]) for k in ('x','y','rotation'))
    if angle%90: raise ValueError('Unit rotation must be a multiple of 90 degrees')
    ET.SubElement(sheets[op['sheet']-1].find('instances'),'instance',part=ref,gate=name(gate),x=str(x),y=str(y),rot=f'R{angle%360:g}')


def electrical(xml):
    """Order-independent read-back contract; ignores exporter-generated IDs and text placement."""
    sch=D.parse_schematic_design(D.read_xml(xml.encode() if isinstance(xml,str) else xml))
    return {'parts':{r:[p.library,p.deviceset,p.device,p.package,p.value,p.attributes] for r,p in sch.parts.items()},
            'units': sorted((i.part,i.gate,i.sheet,i.x,i.y,i.angle,i.mirror) for i in sch.instances),
            'pins': sorted((p.part,p.gate,p.pin,p.pad,p.direction) for p in sch.pins),
            'nets':{n:sorted((p.part,p.gate,p.pin) for p in net.pins) for n,net in sch.nets.items()},
            'sheets':sch.sheets, 'descriptions':sch.descriptions,
            'labels':{n:sum(s.labels for s in net.segments) for n,net in sch.nets.items()}}


def build_plan(snapshot, operations, evidence):
    if not isinstance(operations,list) or not 1<=len(operations)<=4096: raise ValueError('Expected 1..4096 typed operations')
    from .pin_evidence import library_part, validate_pin_evidence
    for op in operations:
        if op.get('op') != 'add_part': continue
        sources=snapshot.get('sources',{})
        try:
            evidence_json=json.loads(sources[op['evidence_path']]['content'])
            library_root=D.read_xml(sources[op['library_path']]['content'].encode())
            part=library_part(library_root,op['deviceset'],op['device'],evidence_json['part_id'])
            findings=validate_pin_evidence(part,evidence_json,sources.get('datasheet:'+op['evidence_path'],{}).get('sha256'))
        except (KeyError,TypeError) as ex: raise ValueError('Complete cached library and reviewed pin evidence required') from ex
        if any(f['status'] != 'pass' for f in findings): raise ValueError('Part evidence is incomplete, stale or mismatched: '+json.dumps(findings))
    after=simulate(snapshot['sch_xml'],operations,snapshot.get('sources'))
    findings=[]
    if evidence:
        from .circuit_blocks import validate_block, expand_block
        from .project_checks import check_project
        if set(evidence)!={'block_path','rules_path','instances','port_connections'}: raise ValueError('Unknown plan evidence fields')
        sources=snapshot['sources']; block=json.loads(sources[evidence['block_path']]['content']); rules=json.loads(sources[evidence['rules_path']]['content'])
        sch=D.parse_schematic_design(D.read_xml(snapshot['sch_xml'].encode()))
        expanded=expand_block(block,evidence['instances'],sch.parts,evidence['port_connections'],sch.nets)
        if expanded!=operations: raise ValueError('Block operations do not match reviewed template expansion')
        findings+=validate_block(block,{},rules)
        projected=D.parse_schematic_design(D.read_xml(after.encode()))
        findings+=check_project(projected,None,rules)
        for op in operations:
            if op['op']!='add_part': continue
            root=D.read_xml(sources[op['library_path']]['content'].encode())
            ds=next(d for d in root.iter('deviceset') if d.get('name')==op['deviceset'])
            expected_gates={g.get('name') for g in ds.findall('./gates/gate')}
            actual_gates={i.gate for i in projected.instances if i.part==op['ref']}
            if expected_gates!=actual_gates: raise ValueError('Block omits one or more units')
    plan={'version':1,'before':fingerprint(snapshot),'operations':copy.deepcopy(operations),
          'evidence':copy.deepcopy(evidence),'expected':electrical(after),'findings':findings}
    plan['sha256']=digest(plan)
    return plan


def validate_plan(plan,current_snapshot):
    findings=[]
    body={k:v for k,v in plan.items() if k!='sha256'}
    if digest(body)!=plan.get('sha256'): findings.append({'status':'fail','rule_id':'plan_hash'})
    if plan.get('before')!=fingerprint(current_snapshot): findings.append({'status':'fail','rule_id':'stale_plan'})
    return findings


def preview_html(snapshot,plan):
    import html
    from .symbols import unit_geometry
    from .sch_layout import Drawing
    from .sch_render import render_svg
    xml=simulate(snapshot['sch_xml'],plan['operations'],snapshot.get('sources'))
    root=D.read_xml(xml.encode()); sch=D.parse_schematic_design(root)
    pages=[]
    for sheet in range(1,sch.sheets+1):
        d=Drawing(); geo={}; values={}
        for i in sch.instances:
            if i.sheet!=sheet: continue
            p=sch.parts[i.part]; key=f'{i.part}.{i.gate}'
            lib=next(l for l in root.iter('library') if l.get('name')==p.library)
            g=unit_geometry(lib).get((p.deviceset+p.device,i.gate))
            if g is None: raise ValueError(f'Geometry unavailable for {key}')
            geo[key]=g; d.parts[key]=(i.x,i.y,i.angle,i.mirror); values[key]=p.value
            a,b,c,e=g.body(); corners=[D.transform(x,y,i.x,i.y,i.angle,i.mirror) for x,y in ((a,b),(c,b),(c,e),(a,e))]
            d.boxes.append((min(x for x,y in corners),min(y for x,y in corners),max(x for x,y in corners),max(y for x,y in corners),key))
        for n in root.findall(f'./drawing/schematic/sheets/sheet[{sheet}]/nets/net'):
            for w in n.iter('wire'): d.wires.append((n.get('name'),[(float(w.get('x1')),float(w.get('y1'))),(float(w.get('x2')),float(w.get('y2')))]))
            for label in n.iter('label'): d.labels.append((n.get('name'),float(label.get('x')),float(label.get('y')),1))
        pages.append(render_svg(d,geo,values,{},title=f'Proposed sheet {sheet}'))
    diff={'operations':plan['operations'],'before':electrical(snapshot['sch_xml']),'after':plan['expected'],'findings':plan['findings']}
    return '<!doctype html><meta charset="utf-8"><title>Schematic edit preview</title><h1>Schematic edit preview</h1><p>Plan SHA256: '+plan['sha256']+'</p>'+''.join(pages)+'<pre>'+html.escape(json.dumps(diff,indent=2))+'</pre>'
