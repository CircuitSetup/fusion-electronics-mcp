"""Declarative requirements over exported connectivity; no electrical defaults."""
from .pin_evidence import finding
from .edit_plan import pin, net_of, number


def check_project(schematic, board, rules):
    if rules.get('version')!=1 or not isinstance(rules.get('rules'),list): raise ValueError('Unsupported project rules')
    out=[]; ids=set()
    for r in rules['rules']:
        rid=r['id']
        if rid in ids: raise ValueError('Duplicate rule ID')
        ids.add(rid)
        status='unknown'; observed=None; expected=r.get('expected'); refs=[]
        if r.get('review_status')=='approved' and r.get('evidence'):
            kind=r['type']
            if kind=='part_count':
                selector=r['selector']
                if set(selector)-{'prefix','deviceset','mpn'}: raise ValueError('Unknown part selector')
                refs=[p.name for p in schematic.parts.values() if all(
                    p.name.startswith(v) if k=='prefix' else p.deviceset==v if k=='deviceset' else (p.attributes.get('MPN') or p.attributes.get('MP'))==v for k,v in selector.items())]
                observed=len(refs)
                if type(expected)==int and expected>=0: status='pass' if observed==expected else 'fail'
            elif kind=='channels':
                rows=r['assignments']; observed=rows; expected='Unique alias, physical channel, ADC channel, and connector position'
                if rows and all(set(x)=={'alias','physical','adc','adc_channel','connector','position'} for x in rows):
                    keys=[lambda x:x['alias'],lambda x:x['physical'],lambda x:(x['adc'],x['adc_channel']),lambda x:(x['connector'],x['position'])]
                    status='pass' if all(len({key(x) for x in rows})==len(rows) for key in keys) else 'fail'
            elif kind in ('connector','rail_membership'):
                observed={}; expected=r['pads'] if kind=='connector' else r['net']; missing=False
                if kind=='connector':
                    refs=[r['ref']]
                    for pad,want in r['pads'].items():
                        pins=[p for p in schematic.pins if p.part==r['ref'] and str(pad) in (p.pad or '').split()]
                        nets={net_of(schematic,p) for p in pins}
                        observed[pad]=sorted(nets,key=str); missing|=not pins
                    status='unknown' if missing else 'pass' if all(observed[pad]==[want] for pad,want in r['pads'].items()) else 'fail'
                else:
                    for target in r['pins']:
                        try: p=pin(schematic,target); observed['.'.join(target[k] for k in ('part','gate','pin'))]=net_of(schematic,p)
                        except ValueError: missing=True
                    status='unknown' if missing or not observed else 'pass' if all(v==expected for v in observed.values()) else 'fail'
            elif kind=='voltage':
                lo,hi=map(number,r['range'])
                if lo>hi: raise ValueError('Reversed voltage range')
                expected=[lo,hi]; n=schematic.nets.get(r['net']); observed={}; missing=False; bad=False
                for ref in n.pins if n else []:
                    key=f'{ref.part}.{ref.gate}.{ref.pin}'; rating=r['ratings'].get(key); observed[key]=rating
                    if not rating or not rating.get('evidence'): missing=True; continue
                    a,b=map(number,rating['range'])
                    if a>b: raise ValueError('Reversed component rating range')
                    bad|=lo<a or hi>b
                status='fail' if bad else 'unknown' if not observed or missing else 'pass'
            elif kind=='isolation':
                status,observed=isolation(schematic,r)
                expected='No conductive path between declared domains except explicit crossing components'
            elif kind not in ('physical_clearance','creepage','insulation','regulatory'):
                raise ValueError(f'Unknown rule type {kind!r}')
        out.append(finding(rid,status,observed,expected,refs,r.get('evidence',[])))
    return out


def isolation(sch,rule):
    parent={n:n for n in sch.nets}; domains={}; missing=any(not targets for targets in rule['domains'].values()); covered=set()
    def find(n):
        while parent[n]!=n: n=parent[n]
        return n
    for group in rule.get('conductive_groups',[]):
        nets=[]
        covered.update((p['part'],p['gate'],p['pin']) for p in group)
        for target in group:
            try: n=net_of(sch,pin(sch,target))
            except ValueError: n=None
            if n is None: missing=True
            else: nets.append(n)
        for a,b in zip(nets,nets[1:]): parent[find(a)]=find(b)
    for domain,targets in rule['domains'].items():
        for target in targets:
            try: n=net_of(sch,pin(sch,target))
            except ValueError: n=None
            if n is None: missing=True; continue
            domains.setdefault(find(n),set()).add(domain)
    conflicts={n:sorted(ds) for n,ds in domains.items() if len(ds)>1}
    if conflicts: return 'fail',conflicts
    crossings={x['part']:x for x in rule.get('allowed_crossings',[]) if x.get('evidence') and x.get('pin_domains')}
    unexplained=[]
    # Require classification for every connected component pin, including intermediate nets.
    for part in sch.parts:
        connected=[p for p in sch.pins if p.part==part and net_of(sch,p) is not None]
        if len(connected)<2: continue
        declaration=crossings.get(part)
        classified=all((p.part,p.gate,p.pin) in covered for p in connected)
        insulated=declaration and all(f'{p.gate}.{p.pin}' in declaration['pin_domains'] for p in connected)
        if not classified and not insulated: unexplained.append(part)
    for part in sch.parts:
        touched={}
        for p in sch.pins:
            if p.part!=part: continue
            n=net_of(sch,p)
            ds=domains.get(find(n),set()) if n else set()
            if ds: touched[f'{p.gate}.{p.pin}']=next(iter(ds))
        if len(set(touched.values()))>1:
            declaration=crossings.get(part)
            if not declaration or any(declaration['pin_domains'].get(k)!=v for k,v in touched.items()): unexplained.append(part)
    return ('unknown' if missing or unexplained or len(rule['domains'])<2 else 'pass'), {'unclassified_crossings':unexplained,'missing_connections':missing}
