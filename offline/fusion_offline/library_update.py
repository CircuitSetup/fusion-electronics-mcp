"""Source-only preflight for a footprint's restrict/courtyard geometry update."""

import hashlib
import xml.etree.ElementTree as ET


def check_keepout_update(source_xml: bytes, proposed_xml: bytes, package_name: str) -> dict:
    """Require every non-keepout library definition to remain identical; never apply an update."""
    if not isinstance(package_name, str) or not package_name:
        raise ValueError('An exact footprint name is required')
    signatures, packages, counts = [], [], []
    for raw in (source_xml, proposed_xml):
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as error:
            raise ValueError(f'Expected EAGLE library XML: {error}') from None
        library = root.find('./drawing/library')
        if library is None:
            raise ValueError('Expected an EAGLE library document')
        matches = [p for p in library.findall('./packages/package')
                   if p.get('name', '').casefold() == package_name.casefold()]
        if len(matches) != 1:
            raise ValueError('Target footprint is missing or ambiguous')
        package = matches[0]
        names = [p.get('name', '').casefold() for p in package if p.tag in ('pad', 'smd')]
        if any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError('Physical pad identifiers must be unique and nonempty')
        counts.append(len(names))
        packages.append(hashlib.sha256(ET.canonicalize(ET.tostring(package, encoding='unicode'), strip_text=True).encode()).hexdigest())
        for shape in list(package):
            if (shape.tag in ('wire', 'circle', 'rectangle', 'polygon')
                    and shape.get('layer') in ('39', '40', '41', '42', '43')):
                package.remove(shape)
        signatures.append(ET.canonicalize(ET.tostring(root, encoding='unicode'), strip_text=True))
    if signatures[0] != signatures[1]:
        raise ValueError('Update changes pads, connections, identities, model definitions, paste or unrelated library content')
    return {'status': 'source_geometry_only', 'package': package_name, 'physical_pads': counts[0],
            'source_sha256': hashlib.sha256(source_xml).hexdigest(),
            'proposed_sha256': hashlib.sha256(proposed_xml).hexdigest(),
            'source_package_sha256': packages[0], 'proposed_package_sha256': packages[1],
            'changed': packages[0] != packages[1], 'electrical_and_model_definitions_preserved': True,
            'native_apply_supported': False, 'native_model_link': 'unverified',
            'notice': 'Source preflight only. Native targeted update/undo and model-link read-back remain unverified; do not fall back to Update All.'}
