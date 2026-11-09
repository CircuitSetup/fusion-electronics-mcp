"""Pinned, flat EAGLE pair validation; no Fusion calls or command strings."""
import hashlib
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def request_id(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise ValueError('Use a fresh 32-character lowercase hex request_id; reuse it after an uncertain result')
    return value


def signature(schematic, board):
    sch = schematic.find('./drawing/schematic'); brd = board.find('./drawing/board')
    if sch is None or brd is None:
        raise ValueError('Expected an EAGLE schematic and board')
    if sch.findall('.//moduleinst'):
        raise ValueError('Hierarchical module imports are not supported by this verifier')
    part_rows = sch.findall('./parts/part')
    parts = {p.get('name'): p for p in part_rows}
    if len(parts) != len(part_rows) or None in parts:
        raise ValueError('Schematic part names must be unique')
    elements = [e.get('name') for e in brd.findall('./elements/element')]
    if len(elements) != len(set(elements)) or not set(elements) <= parts.keys():
        raise ValueError('Board elements must be unique schematic parts')
    libraries = {l.get('name'): l for l in sch.findall('./libraries/library')}
    devices = {}
    for name,part in parts.items():
        library = libraries[part.get('library')]
        matches = [d for ds in library.findall('./devicesets/deviceset')
                   if ds.get('name','').casefold() == part.get('deviceset','').casefold()
                   for d in ds.findall('./devices/device')
                   if d.get('name','').casefold() == part.get('device','').casefold()]
        if len(matches) != 1: raise ValueError('Ambiguous schematic device')
        devices[name] = matches[0]
    if {name for name,device in devices.items() if device.get('package')} != set(elements):
        raise ValueError('Physical schematic parts differ from board elements')
    contacts = set()
    for sheet in sch.findall('./sheets/sheet'):
        for net in sheet.findall('./nets/net'):
            for ref in net.findall('./segment/pinref'):
                name = ref.get('part')
                if name not in elements: continue
                device = devices[name]
                matches = [c for c in device.findall('./connects/connect')
                           if c.get('gate','').casefold() == ref.get('gate','').casefold()
                           and c.get('pin','').casefold() == ref.get('pin','').casefold()]
                if len(matches) != 1: raise ValueError('Ambiguous schematic pin connection')
                contacts.update((net.get('name'), name, pad.casefold()) for pad in matches[0].get('pad','').split())
    board_contacts = [(signal.get('name'), ref.get('element'), ref.get('pad','').casefold())
                      for signal in brd.findall('./signals/signal') for ref in signal.findall('./contactref')]
    if len(board_contacts) != len(set(board_contacts)) or set(board_contacts) != contacts:
        raise ValueError('Schematic/board contact mappings differ')
    assignments = {}
    for net, part, pad in contacts:
        if (part,pad) in assignments and assignments[part,pad] != net:
            raise ValueError('A physical pad belongs to multiple nets')
        assignments[part,pad] = net
    return {'parts': sorted(parts), 'elements': sorted(elements),
            'sheets': len(sch.findall('./sheets/sheet')), 'contacts': sorted(contacts)}


def read_pair(args):
    paths = [Path(args[key]) for key in ('schematic_path','board_path')]
    if (not all(p.is_absolute() for p in paths)
            or [p.suffix.casefold() for p in paths] != ['.sch','.brd']
            or paths[0].parent.resolve() != paths[1].parent.resolve()
            or paths[0].stem.casefold() != paths[1].stem.casefold()):
        raise ValueError('Use absolute, same-directory, same-basename .sch/.brd inputs')
    raw = []
    for path, key in zip(paths, ('schematic_sha256','board_sha256')):
        expected = args[key]
        if not isinstance(expected,str) or not re.fullmatch('[a-fA-F0-9]{64}', expected):
            raise ValueError('Pin both source SHA-256 hashes')
        with path.open('rb') as stream: data = stream.read(32*1024*1024+1)
        if len(data) > 32*1024*1024: raise ValueError('EAGLE source exceeds 32 MiB')
        if hashlib.sha256(data).hexdigest() != expected.lower(): raise ValueError(f'Source hash changed: {path}')
        text = data.decode('utf-8-sig')
        if '<!ENTITY' in text: raise ValueError('XML entities are not supported')
        raw.append(data)
    try:
        result = signature(*(ET.fromstring(data) for data in raw))
    except (KeyError, StopIteration, ET.ParseError) as ex:
        raise ValueError(f'Invalid EAGLE pair: {ex}') from None
    return raw, result


def summary(value):
    return {'physical_parts':len(value['elements']), 'schematic_parts':len(value['parts']),
            'sheets':value['sheets'], 'contacts':len(value['contacts'])}


def readback(schematic, board):
    return signature(ET.parse(schematic).getroot(), ET.parse(board).getroot())
