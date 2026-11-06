import unittest
import copy
from pathlib import Path
from fusion_offline import design as D, symbols as S
from fusion_mcp import commands as C
from test_server import load_addin

class MultiUnitTest(unittest.TestCase):
    def setUp(self):
        self.root=D.read_xml(str(Path(__file__).parent/'fixtures/mini.sch'))
        ds=self.root.find('.//deviceset[@name="LDO"]')
        g=copy.deepcopy(ds.find('./gates/gate')); g.set('name','B'); ds.find('gates').append(g)
        dv=ds.find('./devices/device'); c=copy.deepcopy(dv.find('./connects/connect')); c.set('gate','B'); c.set('pad','8 9'); dv.find('connects').append(c)
        inst=copy.deepcopy(self.root.find('.//instance[@part="U1"]')); inst.set('gate','B'); inst.set('x','70')
        self.root.find('.//sheet/instances').append(inst)
    def test_gate_lookup_and_ambiguity(self):
        s=D.parse_schematic_design(self.root)
        with self.assertRaises(ValueError): s.pin('U1','VIN')
        with self.assertRaises(ValueError): s.net_of('U1','VIN')
        self.assertEqual(s.pin('U1','VIN',gate='B').pad,'8 9')
        self.assertEqual(s.pin('U1','VIN',gate='G$1').pad,'1')
    def test_all_unit_geometry_and_invoke_allowlist(self):
        geo=S.unit_geometry(self.root)
        self.assertEqual(geo[('LDO','B')].pins['VIN'].pads,['8','9'])
        self.assertIn(('LDO','G$1'),geo)
        cmd=C.invoke_unit('U1','B',20,30,90,2)
        load_addin().validate(cmd)
        self.assertIn("INVOKE 'U1' 'B'",cmd)
    def test_resolver_refuses_ambiguous_pad_and_accepts_gate(self):
        from fusion_mcp.server import _resolve_pin
        s=D.parse_schematic_design(self.root)
        with self.assertRaises(ValueError): _resolve_pin(s,'U1.VIN')
        self.assertEqual(_resolve_pin(s,'U1.B.VIN').pad,'8 9')


    def test_duplicate_names_are_allowed_only_with_distinct_explicit_units(self):
        from fusion_mcp.library import configure_part, Part, build_script
        from fusion_mcp.library import Library
        p=copy.deepcopy(next(iter(Library(str(Path(__file__).parents[1]/'library/parts')).parts.values())).data)
        p['symbol']['pins']=[{'name':'IN','unit':'A','pad':'1','direction':'in'}, {'name':'IN','unit':'B','pad':'2','direction':'in'}]
        configure_part(p)
        script=build_script(Part(p)); load_addin().validate(script)
        self.assertIn("'A.IN'",script); self.assertIn("'B.IN'",script)
        p['symbol']['pins'][1]['unit']='A'
        with self.assertRaises(ValueError): configure_part(p)

if __name__=='__main__': unittest.main()
