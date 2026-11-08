"""Wire geometry, rather than shared names, must connect local schematic pins."""
import copy
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import xml.etree.ElementTree as ET

from fusion_offline import design as D, edit_plan as P
from fusion_mcp import edit_plan as Live
from fusion_mcp import server as Server
from fusion_mcp.session import Snapshot, WriteFailed
from mcp.server.mcpserver.exceptions import ToolError


def drawing():
    root = ET.parse(Path(__file__).parent / 'fixtures/mini.sch').getroot()
    sheets = root.find('.//schematic/sheets')
    sheets.remove(sheets[1])
    sheets[0].find('nets').clear()
    instances = sheets[0].find('instances')
    instances.find("instance[@part='R1']").attrib.update(x='0', y='0', rot='R0')
    instances.find("instance[@part='R2']").attrib.update(x='40.64', y='0', rot='R0')
    instances.find("instance[@part='U1']").attrib.update(x='100', y='50', rot='R0')
    return root


def connect(*pins, net='SIGNAL', **extra):
    return dict(op='connect', net=net, pins=[dict(part=r, gate='G$1', pin=p) for r, p in pins], **extra)


def reachable(root, start, end, net='SIGNAL', sheet=1):
    wires = root.findall(f'.//sheets/sheet[{sheet}]/nets/net[@name="{net}"]/segment/wire')
    edges = [(tuple(float(w.get(k)) for k in ('x1', 'y1')),
              tuple(float(w.get(k)) for k in ('x2', 'y2'))) for w in wires]
    points = {start, end} | {p for edge in edges for p in edge}
    def on(p, a, b):
        return (abs((p[0]-a[0])*(b[1]-a[1])-(p[1]-a[1])*(b[0]-a[0])) < 1e-5
                and min(a[0], b[0])-1e-5 <= p[0] <= max(a[0], b[0])+1e-5
                and min(a[1], b[1])-1e-5 <= p[1] <= max(a[1], b[1])+1e-5)
    seen = {start}
    while True:
        more = {p for a, b in edges if any(on(s, a, b) for s in seen) for p in points if on(p, a, b)}
        if more <= seen:
            return end in seen
        seen |= more


class SchematicWiringTest(unittest.TestCase):
    def simulate(self, root, op):
        return ET.fromstring(P.simulate(ET.tostring(root, encoding='unicode'), [op]))

    def test_local_net_is_a_continuous_orthogonal_wire(self):
        root = self.simulate(drawing(), connect(('R1', 'P$2'), ('R2', 'P$1')))
        self.assertTrue(reachable(root, (10.16, 0), (40.64, 0)))
        for w in root.findall('.//net/segment/wire'):
            self.assertTrue(w.get('x1') == w.get('x2') or w.get('y1') == w.get('y2'))
        self.assertLessEqual(len(root.findall('.//net/segment/label')), 1)

    def test_branch_and_existing_labeled_pieces_are_joined(self):
        root = drawing()
        root.find(".//instance[@part='U1']").attrib.update(x='70', y='25.4')
        op = connect(('R1', 'P$2'), ('R2', 'P$1'), ('U1', 'VIN'))
        root = self.simulate(root, dict(op, style='labels'))
        root = self.simulate(root, op)
        for point in ((40.64, 0), (62.38, 25.4)):
            self.assertTrue(reachable(root, (10.16, 0), point))

    def test_router_avoids_intervening_symbol_and_unselected_pin(self):
        root = drawing()
        root.find(".//instance[@part='U1']").attrib.update(x='25.4', y='0')
        root = self.simulate(root, connect(('R1', 'P$2'), ('R2', 'P$1')))
        self.assertTrue(reachable(root, (10.16, 0), (40.64, 0)))
        self.assertFalse(reachable(root, (10.16, 0), (17.78, 0)))  # U1.VIN
        self.assertTrue(any(float(w.get('y1')) != 0 or float(w.get('y2')) != 0
                            for w in root.findall('.//net/segment/wire')))

    def test_cross_sheet_pieces_remain_explicitly_named(self):
        root = drawing(); sheets = root.find('.//sheets')
        sh = ET.SubElement(sheets, 'sheet'); ins = ET.SubElement(sh, 'instances'); ET.SubElement(sh, 'nets')
        inst = sheets[0].find("instances/instance[@part='R2']")
        sheets[0].find('instances').remove(inst); ins.append(inst)
        root = self.simulate(root, connect(('R1', 'P$2'), ('R2', 'P$1')))
        self.assertEqual(len(root.findall('.//net/segment/label')), 2)

    def test_compiler_and_preview_use_the_same_connected_paths(self):
        xml = ET.tostring(drawing(), encoding='unicode')
        op = connect(('R1', 'P$2'), ('R2', 'P$1'))
        commands = Live.compile_operation(xml, op)
        self.assertIn('(10.16 0)', commands)
        self.assertIn('(40.64 0)', commands)
        # A connecting NET command must span the gap; two short labeled NETs cannot pass.
        self.assertTrue(any('(12.7 0)' in cmd and '(38.1 0)' in cmd for cmd in commands.split(';')))

    def test_readback_contract_detects_missing_wires_even_with_matching_pinrefs(self):
        root = self.simulate(drawing(), connect(('R1', 'P$2'), ('R2', 'P$1')))
        altered = copy.deepcopy(root)
        for seg in altered.findall('.//net/segment'):
            for w in seg.findall('wire'): seg.remove(w)
        self.assertNotEqual(P.electrical(ET.tostring(root)), P.electrical(ET.tostring(altered)))

    def test_labels_remain_an_explicit_compatibility_style(self):
        root = self.simulate(drawing(), connect(('R1', 'P$2'), ('R2', 'P$1'), style='labels'))
        self.assertFalse(reachable(root, (10.16, 0), (40.64, 0)))
        self.assertEqual(len(root.findall('.//net/segment/label')), 2)

    def test_other_net_cannot_be_crossed_or_merged(self):
        root = drawing()
        n = ET.SubElement(root.find('.//sheet/nets'), 'net', name='OTHER', **{'class':'0'})
        seg = ET.SubElement(n,'segment')
        ET.SubElement(seg,'wire',x1='25.4',y1='-5.08',x2='25.4',y2='5.08',width='0',layer='91')
        result = self.simulate(root,connect(('R1','P$2'),('R2','P$1')))
        self.assertTrue(reachable(result,(10.16,0),(40.64,0)))
        for w in result.findall('.//net[@name="SIGNAL"]/segment/wire'):
            a,b = [tuple(float(w.get(k)) for k in keys) for keys in (('x1','y1'),('x2','y2'))]
            if a[1] == b[1] and min(a[0],b[0]) <= 25.4 <= max(a[0],b[0]):
                self.assertGreater(abs(a[1]),5.08)
        self.assertEqual(ET.tostring(result.find('.//net[@name="OTHER"]')),ET.tostring(n))

    def test_rotated_and_mirrored_pins_connect_at_real_endpoints(self):
        root = drawing()
        root.find(".//instance[@part='R1']").set('rot','R90')
        root.find(".//instance[@part='U1']").attrib.update(x='40.64',y='25.4',rot='MR0')
        result = self.simulate(root,connect(('R1','P$2'),('U1','VOUT')))
        self.assertTrue(reachable(result,(0,10.16),(33.02,25.4)))

    def test_existing_single_wire_may_be_split_without_invalidating_readback(self):
        root = self.simulate(drawing(),connect(('R1','P$2'),('R2','P$1')))
        original = P.electrical(ET.tostring(root))
        seg = root.find('.//net/segment'); w = seg.find('wire')
        end = w.get('x2'); mid = str((float(w.get('x1'))+float(end))/2)
        other = copy.deepcopy(w); w.set('x2',mid); other.set('x1',mid); seg.append(other)
        self.assertEqual(P.electrical(ET.tostring(root)),original)

    def test_live_default_rejects_named_stubs_and_accepts_a_merged_continuous_wire(self):
        before = Snapshot(sch_xml=ET.tostring(drawing()))
        after_root = drawing(); nets = after_root.find('.//sheet/nets')
        n = ET.SubElement(nets,'net',name='SIGNAL',**{'class':'0'}); seg = ET.SubElement(n,'segment')
        for ref,pin in (('R1','P$2'),('R2','P$1')):
            ET.SubElement(seg,'pinref',part=ref,gate='G$1',pin=pin)
        ET.SubElement(seg,'wire',x1='10.16',y1='0',x2='40.64',y2='0',width='0',layer='91')
        ET.SubElement(seg,'label',x='12.7',y='0',size='1.27',layer='95')
        for mode in ('stubs','curve','wrong_layer','connected'):
            after = copy.deepcopy(after_root)
            if mode == 'stubs':
                w = after.find('.//net/segment/wire'); w.set('x2','12.7')
                ET.SubElement(after.find('.//net/segment'),'wire',x1='38.1',y1='0',x2='40.64',y2='0',width='0',layer='91')
            elif mode == 'curve': after.find('.//net/segment/wire').set('curve','180')
            elif mode == 'wrong_layer': after.find('.//net/segment/wire').set('layer','94')
            def write(editor,commands,verify,**kwargs):
                result = Snapshot(sch_xml=ET.tostring(after))
                ok,detail = verify(before,result)
                if not ok: raise WriteFailed(detail)
                return result,detail
            session = SimpleNamespace(snapshot=lambda *a,**kw: before,verified_write=write)
            with self.subTest(mode=mode), patch.object(Server,'session',session), patch.object(Server,'get_net',return_value={}):
                if mode == 'connected':
                    self.assertTrue(Server.connect_pins('signal',['R1.2','R2.1'])['ok'])
                else:
                    with self.assertRaisesRegex(ToolError,'wire geometry'):
                        Server.connect_pins('signal',['R1.2','R2.1'])

    def test_blocked_pin_fails_before_any_live_write(self):
        root = drawing(); root.find(".//instance[@part='R2']").set('x','10.16')
        session = SimpleNamespace(snapshot=lambda *a,**kw: Snapshot(sch_xml=ET.tostring(root)))
        with patch.object(Server,'session',session), self.assertRaisesRegex(ToolError,'Blocked pin escape'):
            Server.connect_pins('SIGNAL',['R1.2','R2.2'])

    def test_existing_remote_piece_gets_a_label_when_a_new_sheet_joins(self):
        root = drawing(); sheets = root.find('.//sheets')
        sh = ET.SubElement(sheets,'sheet'); ins = ET.SubElement(sh,'instances'); ns = ET.SubElement(sh,'nets')
        inst = sheets[0].find("instances/instance[@part='R2']")
        sheets[0].find('instances').remove(inst); ins.append(inst)
        n = ET.SubElement(ns,'net',name='SIGNAL',**{'class':'0'}); seg = ET.SubElement(n,'segment')
        ET.SubElement(seg,'pinref',part='R2',gate='G$1',pin='P$1')
        ET.SubElement(seg,'wire',x1='40.64',y1='0',x2='35.56',y2='0',width='0',layer='91')
        result = self.simulate(root,connect(('R1','P$2')))
        self.assertEqual(len(result.findall('.//net/segment/label')),2)
        self.assertTrue(result.findall('.//sheet[2]/nets/net/segment/label'))

    def test_wire_commands_stop_and_resume_at_existing_same_net_intersections(self):
        from fusion_offline import sch_wiring as W
        path = W.cut_at_wires([(0,0),(20,0)],[((10,-5),(10,5))])
        self.assertEqual(path,[(0,0),(10,0),(20,0)])

    def test_reconnecting_an_already_wired_net_is_a_noop(self):
        op = connect(('R1','P$2'),('R2','P$1'))
        root = self.simulate(drawing(),op)
        xml = ET.tostring(root,encoding='unicode')
        self.assertEqual(Live.compile_operation(xml,op),'')
        self.assertEqual(P.simulate(xml,[op]),xml)

    def test_pin_escape_stops_and_resumes_when_it_meets_an_existing_wire(self):
        from fusion_offline import sch_wiring as W
        root = drawing(); n = ET.SubElement(root.find('.//sheet/nets'),'net',name='SIGNAL',**{'class':'0'})
        seg = ET.SubElement(n,'segment')
        ET.SubElement(seg,'wire',x1='11.43',y1='0',x2='11.43',y2='10',width='0',layer='91')
        sch = D.parse_schematic_design(root)
        plan = W.plan(root,'SIGNAL',[sch.pin('R1','P$2','G$1')])
        self.assertEqual(plan['paths'][0][1],[(10.16,0),(11.43,0),(12.7,0)])
        self.assertIn((1,(11.43,0)),plan['junctions'])
        commands = Live.compile_operation(ET.tostring(root,encoding='unicode'),connect(('R1','P$2')))
        self.assertIn("NET 'SIGNAL' (10.16 0) (11.43 0);",commands)
        self.assertIn("NET 'SIGNAL' (11.43 0) (12.7 0);",commands)


if __name__ == '__main__': unittest.main()
