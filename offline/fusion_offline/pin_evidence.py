"""Pin evidence comparisons; extraction never implies engineering approval."""
from .edit_plan import digest


def finding(rule, status, observed=None, expected=None, refs=None, evidence=None):
    return {'rule_id':rule,'severity':'error' if status in ('fail','unknown') else 'info','status':status,
            'refs':refs or [],'observed':observed,'expected':expected,'evidence':evidence or []}


def report(findings):
    statuses={f['status'] for f in findings}
    return {'status':'fail' if 'fail' in statuses else 'incomplete' if 'unknown' in statuses or not findings else 'pass','findings':findings}


def content_hash(evidence): return digest({k:v for k,v in evidence.items() if k!='reviews'})


def review_status(evidence):
    records=evidence.get('reviews',[])
    if not records: return 'unreviewed'
    last=records[-1]
    if not last.get('reviewer') or not last.get('reviewed_at') or last.get('content_sha256')!=content_hash(evidence): return 'unreviewed'
    return last.get('status','unreviewed')


def part_mapping(part):
    return {(p.get('unit','G$1'),p['name']): (set(str(p.get('pad','')).split()),p.get('direction','pas')) for p in part['symbol']['pins']}


def validate_pin_evidence(part, evidence, actual_sha256=None):
    e=evidence; refs=[part.get('id','')]; loc=[{'source':e.get('source'),'revision':e.get('revision'),'pages':e.get('pages'),'sha256':e.get('sha256')}]
    out=[]
    def add(rule,status,observed=None,expected=None): out.append(finding(rule,status,observed,expected,refs,loc))
    status=review_status(e)
    add('evidence_review','pass' if status=='approved' else 'fail' if status=='rejected' else 'unknown',status,'approved')
    for field,want in [('part_id',part.get('id')),('package',part.get('package',{}).get('name')),('mpn',(part.get('attributes',{}).get('MPN') or part.get('attributes',{}).get('MP')))]:
        got=e.get(field)
        add(field,'unknown' if not want or not got else 'pass' if want==got else 'fail',got,want)
    pages=e.get('pages')
    provenance=bool(e.get('source') and e.get('revision') and isinstance(pages,list) and pages and all(type(p)==int and p>0 for p in pages))
    add('source_reference','pass' if provenance else 'unknown')
    add('datasheet_hash','unknown' if not actual_sha256 else 'pass' if actual_sha256==e.get('sha256') else 'fail',actual_sha256,e.get('sha256'))
    rows=e.get('pins',[]); mapping=part_mapping(part)
    pads={str(p['name']) for kind in ('pads','smds') for p in part.get('package',{}).get(kind,[])}
    seen=set()
    for row in rows:
        pad=row.get('pad'); key=(row.get('gate'),row.get('pin')); actual=mapping.get(key)
        if pad in seen: add('duplicate_pad','fail',pad)
        seen.add(pad)
        intent=row.get('intent')
        complete=(row.get('datasheet_name') and intent in ('signal','supply','nc','dnc','reserved','exposed_pad','alternate')
                  and isinstance(row.get('pages'),list) and row['pages'] and all(type(p)==int and p>0 for p in row['pages'])
                  and not row.get('uncertain',True))
        add('pin_evidence','pass' if complete else 'unknown',row)
        if actual is None:
            add('pin_mapping','pass' if key==(None,None) and intent in ('nc','dnc','reserved') else 'fail',key,pad)
        else:
            add('pin_mapping','pass' if pad in actual[0] and row.get('direction')==actual[1] else 'fail',{'pad':pad,'gate':key[0],'pin':key[1]},sorted(actual[0]))
    add('package_pad_coverage','pass' if seen==pads else 'fail',sorted(seen,key=str),sorted(pads))
    declared={(r.get('gate'),r.get('pin'),r.get('pad')) for r in rows}
    expected={(g,p,pad) for (g,p),(pads,_) in mapping.items() for pad in pads}
    add('symbol_pin_coverage','pass' if expected<=declared else 'fail',sorted(expected-declared))
    return out


def compare_pin_evidence(schematic, parts, evidence, references=None):
    out=[]
    for part_id,part in parts.items():
        e=evidence.get(part_id)
        if not e:
            out.append(finding('missing_evidence','unknown',refs=[part_id])); continue
        matches=[p for p in schematic.parts.values() if p.deviceset==part['deviceset'] and p.device==part.get('device','') and (references is None or p.name in references.get(part_id,[]))]
        if not matches: out.append(finding('part_not_placed','unknown',refs=[part_id]))
        for placed in matches:
            out.append(finding('active_package','pass' if placed.package==e['package'] else 'fail',placed.package,e['package'],[placed.name]))
            out.append(finding('active_mpn','unknown' if not (placed.attributes.get('MPN') or placed.attributes.get('MP')) else 'pass' if (placed.attributes.get('MPN') or placed.attributes.get('MP'))==e['mpn'] else 'fail',(placed.attributes.get('MPN') or placed.attributes.get('MP')),e['mpn'],[placed.name]))
            for row in e['pins']:
                if row.get('pin') is None: continue
                p=schematic.pin(placed.name,row['pin'],row['gate']); refs=[placed.name,row['gate'],row['pin']]
                ok=p is not None and row['pad'] in (p.pad or '').split() and p.direction==row['direction']
                out.append(finding('active_pin_mapping','pass' if ok else 'fail',p.pad if p else None,row['pad'],refs,[{'pages':row['pages'],'source':e['source']}]))
                if p is None: continue
                net=schematic.net_of(p.part,p.pin,p.gate)
                if row['intent'] in ('nc','dnc','reserved'):
                    out.append(finding('no_connect','pass' if net is None else 'fail',net,None,refs))
                elif row.get('net'):
                    out.append(finding('expected_rail','pass' if net==row['net'] else 'fail',net,row['net'],refs))
    return out


def library_part(root,deviceset,device,part_id):
    """Read every gate/pad mapping from cached Fusion XML, including uninvoked units."""
    ds=next((d for d in root.iter('deviceset') if d.get('name')==deviceset),None)
    if ds is None: raise ValueError('Unknown deviceset')
    dv=next((d for d in ds.findall('./devices/device') if d.get('name','')==device),None)
    if dv is None: raise ValueError('Unknown device variant')
    package=next((p for p in root.iter('package') if p.get('name')==dv.get('package')),None)
    if package is None: raise ValueError('Package geometry unavailable')
    symbols={s.get('name'):s for s in root.iter('symbol')}; pins=[]
    for gate in ds.findall('./gates/gate'):
        sym=symbols[gate.get('symbol')]
        for pin in sym.findall('pin'):
            cs=[c for c in dv.findall('./connects/connect') if c.get('gate')==gate.get('name') and c.get('pin')==pin.get('name')]
            pins.append({'unit':gate.get('name'),'name':pin.get('name'),'pad':' '.join(c.get('pad','') for c in cs),'direction':pin.get('direction','io')})
    attrs={a.get('name'):a.get('value') for a in dv.findall('./technologies/technology/attribute')}
    return {'id':part_id,'deviceset':deviceset,'device':device,'attributes':attrs,'symbol':{'pins':pins},
            'package':{'name':dv.get('package'),'pads':[{'name':p.get('name')} for p in package.findall('pad')],
                       'smds':[{'name':p.get('name')} for p in package.findall('smd')]}}
