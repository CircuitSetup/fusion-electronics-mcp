import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fusion_offline.kicad_import import footprint_to_part
from fusion_mcp import library as L, server as S


PAD='(pad "41" smd rect (at 0 0) (size 3 3) (layers "F.Cu" "F.Paste" "F.Mask"))'
THERMAL='(pad "41" thru_hole circle (at 1 1) (size 0.6 0.6) (drill 0.2) (layers "*.Cu" "*.Mask"))'
APERTURE='(pad "" smd rect (at 0 0) (size 0.9 0.9) (layers "F.Paste"))'
KEEPOUT='(zone (net 0) (net_name "") (layers "*.Cu") (keepout (tracks not_allowed) (vias not_allowed)) (polygon (pts (xy 0 0) (xy 1 0) (xy 1 1))))'


class ConversionGuardsTest(unittest.TestCase):
    def convert(self, body):
        return footprint_to_part('(footprint "Module" '+body+')',part_id='module',deviceset='MODULE',prefix='U')

    def test_duplicate_physical_pad_names_are_rejected_across_pad_types(self):
        for body in (PAD+THERMAL,PAD+PAD,PAD.replace('"41"','"a"')+PAD.replace('"41"','"A"')):
            with self.subTest(body=body),self.assertRaisesRegex(ValueError,'duplicate.*pad|pad.*duplicate'):
                self.convert(body)

    def test_paste_and_mask_only_pads_are_explicitly_unsupported(self):
        for pad in (APERTURE,APERTURE.replace('F.Paste','F.Mask'),APERTURE.replace('pad ""','pad "P1"'),
                    PAD.replace(' "F.Paste"','')):
            with self.subTest(pad=pad),self.assertRaisesRegex(ValueError,'Unsupported.*(paste|mask|aperture|SMD)'):
                self.convert(PAD+pad)

    def test_keepouts_and_rule_areas_are_rejected(self):
        for area in (KEEPOUT,'(rule_area (layers "F.Cu"))'):
            with self.subTest(area=area),self.assertRaisesRegex(ValueError,'Unsupported.*(keepout|zone|rule area)'):
                self.convert(PAD+area)

    def test_supported_footprint_keeps_all_distinct_physical_connections(self):
        part,_=self.convert(PAD+THERMAL.replace('"41"','"42"'))
        self.assertEqual({p['name'] for p in part['package']['smds']+part['package']['pads']},{'41','42'})
        self.assertEqual({p['pad'] for p in part['symbol']['pins']},{'41','42'})

    def test_unsupported_conversion_does_not_replace_existing_part(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); fp=base/'module.kicad_mod'; parts=base/'parts'
            fp.write_text('(footprint "Module" '+PAD+')')
            S.create_library_part(str(fp),'module','MODULE','U','MODULE',library_directory=str(parts))
            original=(parts/'module.json').read_bytes()
            for body in (PAD+THERMAL,PAD+APERTURE,PAD+KEEPOUT):
                fp.write_text('(footprint "Module" '+body+')')
                with self.assertRaises(S.ToolError):
                    S.create_library_part(str(fp),'module','MODULE','U','MODULE',overwrite=True,library_directory=str(parts))
                self.assertEqual((parts/'module.json').read_bytes(),original)

    def test_cached_duplicate_definition_is_rejected_before_any_native_call(self):
        part,_=self.convert(PAD)
        part['package']['pads']=[{'name':'41','x':1,'y':1,'drill':0.2,'diameter':0.6}]
        with tempfile.TemporaryDirectory() as directory:
            L.Library(directory).add(part)
            with patch.object(S,'session') as session:
                for operation in (lambda:S.insert_library_part('module',library_directory=directory),
                                  lambda:S.add_library_variant('module','EXISTING','NEW',library_directory=directory)):
                    with self.assertRaisesRegex(S.ToolError,'duplicate.*pad|pad.*duplicate'):
                        operation()
                self.assertEqual(session.mock_calls,[])
        for reuse in (False,True):
            with self.subTest(reuse=reuse),self.assertRaisesRegex(ValueError,'duplicate.*pad|pad.*duplicate'):
                L.build_script(L.Part(copy.deepcopy(part)),reuse_package=reuse)
            with self.subTest(reuse=reuse),self.assertRaisesRegex(ValueError,'duplicate.*pad|pad.*duplicate'):
                L.variant_script(L.Part(copy.deepcopy(part)),'EXISTING','NEW',{'G$1.GND':'41'},{'':{}},reuse_package=reuse)


if __name__=='__main__': unittest.main()
