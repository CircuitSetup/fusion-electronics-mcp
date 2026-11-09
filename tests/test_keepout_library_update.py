import copy
import importlib
import importlib.util
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from fusion_mcp import server


def library():
    root = ET.fromstring('''<eagle version="9.7.0"><drawing><library urn="library:unchanged">
    <packages><package name="MODULE" urn="footprint:unchanged"/><package name="OTHER">
    <circle x="0" y="0" radius="1" width="0" layer="41"/></package></packages>
    <packages3d><package3d name="MODULE3D" urn="model:unchanged"><packageinstances>
    <packageinstance name="MODULE"/></packageinstances></package3d></packages3d>
    <symbols><symbol name="MODULE_SYMBOL"><pin name="GND" x="0" y="0"/></symbol></symbols>
    <devicesets><deviceset name="MODULE_DEVICE"><gates><gate name="G$1" symbol="MODULE_SYMBOL"/></gates>
    <devices><device name="" package="MODULE"><connects><connect gate="G$1" pin="GND" pad=""/>
    </connects><package3dinstances><package3dinstance package3d_urn="model:unchanged"/></package3dinstances>
    </device></devices></deviceset></devicesets></library></drawing></eagle>''')
    package = root.find('.//package[@name="MODULE"]')
    names = [str(n) for n in range(1, 41)] + [f'41.{n}' for n in range(1, 14)]
    for n, name in enumerate(names):
        ET.SubElement(package, 'smd', name=name, x=str(n), y='0', dx='.5', dy='.5', layer='1')
    root.find('.//connect').set('pad', ' '.join(names[40:]))
    for n in range(9):
        ET.SubElement(package, 'rectangle', x1=str(n), x2=str(n+.2), y1='1', y2='1.2', layer='31')
    for layer in (39, 41, 42, 43):
        polygon = ET.SubElement(package, 'polygon', layer=str(layer), width='.1')
        for x, y in ((-2, 5), (2, 5), (2, 10), (-2, 10)):
            ET.SubElement(polygon, 'vertex', x=str(x), y=str(y))
    return root


class KeepoutLibraryUpdateTest(unittest.TestCase):
    def check(self, before, after):
        self.assertIsNotNone(importlib.util.find_spec('fusion_offline.library_update'), 'Source footprint preservation check is missing')
        module = importlib.import_module('fusion_offline.library_update')
        return module.check_keepout_update(ET.tostring(before), ET.tostring(after), 'MODULE')

    def test_keepout_only_change_preserves_53_pads_connects_paste_and_model_declarations(self):
        before = library(); after = copy.deepcopy(before)
        for polygon in after.findall('.//package[@name="MODULE"]/polygon'):
            polygon[1].set('x', '1.5')
            polygon[2].set('x', '1.5')
        report = self.check(before, after)
        self.assertEqual(report['status'], 'source_geometry_only')
        self.assertEqual(report['physical_pads'], 53)
        self.assertTrue(report['electrical_and_model_definitions_preserved'])
        self.assertEqual(report['native_model_link'], 'unverified')
        self.assertFalse(report['native_apply_supported'])

    def test_any_non_keepout_change_rejects_before_native_update(self):
        for mutation in ('rename_pad', 'move_pad', 'delete_pad', 'paste', 'connect', 'model',
                         'package_identity', 'device_identity', 'other_package'):
            with self.subTest(mutation=mutation):
                before = library(); after = copy.deepcopy(before)
                package = after.find('.//package[@name="MODULE"]')
                if mutation == 'rename_pad': package.find('smd').set('name', 'OTHER')
                if mutation == 'move_pad': package.find('smd').set('x', '1.001')
                if mutation == 'delete_pad': package.remove(package.find('smd'))
                if mutation == 'paste': package.remove(package.find('rectangle'))
                if mutation == 'connect': after.find('.//connect').set('pad', '41.1')
                if mutation == 'model': after.find('.//package3dinstance').set('package3d_urn', 'different:model')
                if mutation == 'package_identity': package.set('urn', 'different:footprint')
                if mutation == 'device_identity': after.find('.//deviceset').set('name', 'NEW_DEVICE')
                if mutation == 'other_package': after.find('.//package[@name="OTHER"]/circle').set('radius', '2')
                with self.assertRaises(ValueError): self.check(before, after)

    def test_duplicate_physical_pad_identifiers_reject_even_if_both_libraries_match(self):
        root = library()
        root.findall('.//package[@name="MODULE"]/smd')[1].set('name', '1')
        with self.assertRaises(ValueError): self.check(root, root)

    def test_missing_model_declarations_cannot_certify_native_model_link(self):
        before = library()
        before.find('.//library').remove(before.find('.//packages3d'))
        device = before.find('.//device')
        device.remove(device.find('package3dinstances'))
        report = self.check(before, copy.deepcopy(before))
        self.assertEqual(report['native_model_link'], 'unverified')

    def test_public_preflight_only_reads_source_files(self):
        before = library(); after = copy.deepcopy(before)
        after.find('.//package[@name="MODULE"]/polygon/vertex').set('y', '4')
        self.assertTrue(callable(getattr(server, 'check_library_keepout_update', None)), 'Read-only source preflight tool is missing')
        with tempfile.TemporaryDirectory() as folder:
            paths = [os.path.join(folder, name) for name in ('before.lbr', 'after.lbr')]
            for path, root in zip(paths, (before, after)):
                with open(path, 'wb') as output: output.write(ET.tostring(root))
            report = server.check_library_keepout_update(paths[0], paths[1], 'MODULE')
        self.assertEqual(report['physical_pads'], 53)
        self.assertFalse(report['native_apply_supported'])
