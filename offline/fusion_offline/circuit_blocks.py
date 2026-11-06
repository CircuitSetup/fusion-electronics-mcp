"""Expand explicit circuit templates into the same typed edit operations as manual plans."""
import re
from .edit_plan import name, number
from .pin_evidence import finding, review_status


def validate_block(block,evidence,project_rules):
    out=[finding('block_review','pass' if review_status(block)=='approved' else 'unknown',review_status(block),'approved')]
    if not block.get('parts') or not block.get('nets'): out.append(finding('block_definition','unknown'))
    if not project_rules: out.append(finding('project_rules','unknown'))
    for alias,part in block.get('parts',{}).items():
        if not part.get('units') or not part.get('evidence_path'): out.append(finding('part_units_and_evidence','unknown',refs=[alias]))
    return out


def expand_block(block,instances,occupied_refs,port_connections,occupied_nets=()):
    if block.get('version')!=1 or not block.get('parts') or not 1<=len(instances)<=64: raise ValueError('Expected a version-1 block and 1..64 explicit instances')
    name(block['id']); used={r.casefold() for r in occupied_refs}; used_nets={n.casefold() for n in occupied_nets}; ids=set(); ops=[]; domains={}
    if set(port_connections)!={i['id'] for i in instances}: raise ValueError('Provide connections for every instance, without extras')
    for inst in instances:
        iid=name(inst['id'])
        if iid.casefold() in ids: raise ValueError('Duplicate instance ID')
        ids.add(iid.casefold()); ports=port_connections[iid]
        if set(ports)!=set(block['ports']): raise ValueError('Every external port must be connected explicitly')
        params={}
        if set(inst.get('parameters',{}))-set(block.get('parameters',{})): raise ValueError('Unknown parameter')
        for param,spec in block.get('parameters',{}).items():
            value=number(inst.get('parameters',{}).get(param,spec.get('default')))
            if not number(spec['min'])<=value<=number(spec['max']): raise ValueError('Parameter outside bounds')
            params[param]=value
        def val(v): return params[v[1:]] if isinstance(v,str) and v.startswith('$') else number(v)
        refs={}
        for alias,part in block['parts'].items():
            name(alias); prefix=part['prefix']
            if not re.fullmatch('[A-Za-z]+',prefix): raise ValueError('Reference prefix must contain letters')
            index=1
            while (prefix+str(index)).casefold() in used: index+=1
            ref=prefix+str(index); used.add(ref.casefold()); refs[alias]=ref
            gates=set()
            for n,unit in enumerate(part['units']):
                gate=name(unit['gate'])
                if gate in gates: raise ValueError('Duplicate unit')
                gates.add(gate)
                placement={'gate':gate,'sheet':unit['sheet'],'x':number(inst['x'])+val(unit['x']),
                           'y':number(inst['y'])+val(unit['y']),'rotation':val(unit['rotation'])}
                if n==0:
                    ops.append(dict(op='add_part',ref=ref,**{k:part[k] for k in ('library_path','evidence_path','library','deviceset','device')},**placement))
                else: ops.append(dict(op='place_unit',part=ref,**placement))
            if not gates: raise ValueError('Part must place its units')
        external={}
        for port,spec in block['ports'].items():
            if spec.get('direction') not in ('in','out','io','power') or not spec.get('domain'): raise ValueError('Typed port direction and domain required')
            net=name(ports[port]); local=spec['net']
            if local not in block['nets']: raise ValueError('Port references missing net')
            if local in external and external[local]!=net: raise ValueError('Conflicting connections for one port net')
            external[local]=net; domains.setdefault(net.casefold(),set()).add(spec['domain'])
            if len(domains[net.casefold()])>1: raise ValueError('External connection would join isolation domains')
        for local,pins in block['nets'].items():
            name(local); net=external.get(local,f'{block["id"]}_{iid}_{local}')
            if local not in external:
                if net.casefold() in used_nets or net.casefold() in {x.casefold() for c in port_connections.values() for x in c.values()}: raise ValueError('Private net collision')
                used_nets.add(net.casefold())
            if not pins: raise ValueError('Empty block net')
            ops.append({'op':'connect','net':net,'pins':[{'part':refs[p['part']],'gate':p['gate'],'pin':p['pin']} for p in pins]})
    return ops
