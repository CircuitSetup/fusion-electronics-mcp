import copy
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from fusion_offline import eagle, stitch, placement_check as PC, gerbers
from fusion_mcp.library import same_package


SLOT='<pad name="S1" x="0" y="0" drill="0.6" diameter="1.0" shape="slot" slotLength="1.7" rot="R90"/>'


def board(pad=SLOT, rot='R0'):
    return ET.fromstring('<eagle><drawing><board><plain/><libraries><library name="L"><packages>'
                         '<package name="P">'+pad+'</package></packages></library></libraries>'
                         '<elements><element name="J1" library="L" package="P" x="10" y="20" rot="'+rot+'"/>'
                         '</elements><signals/><designrules/></board></drawing></eagle>')


class SlotGeometryTest(unittest.TestCase):
    def test_parser_retains_declared_slot_parameters_without_certifying_dimensions(self):
        package=eagle._parse_package('L',ET.fromstring('<package name="P">'+SLOT+'</package>'))
        pad=package.pads[0]
        self.assertEqual((pad.name,pad.drill,pad.diameter,pad.shape,pad.rot,pad.slot_length),
                         ('S1',0.6,1.0,'slot',90,1.7))

    def test_shared_clearance_and_drill_checks_never_substitute_a_circle(self):
        for rot in ('R0','R90','MR90'):
            for check in (stitch.board_obstacles,PC.pads,gerbers.design_holes):
                with self.subTest(rot=rot,check=check.__name__),self.assertRaisesRegex(ValueError,'Unverified.*SLOT.*J1.S1'):
                    check(board(rot=rot))

    def test_missing_length_or_slot_length_without_shape_is_still_unverified(self):
        for pad in (SLOT.replace(' slotLength="1.7"',''),SLOT.replace(' shape="slot"','')):
            with self.subTest(pad=pad),self.assertRaisesRegex(ValueError,'Unverified.*SLOT'):
                stitch.board_obstacles(board(pad))

    def test_placement_and_cam_reports_are_explicitly_unverified(self):
        placement=PC.check(board())
        self.assertEqual(placement['status'],'unverified')
        self.assertIn('J1.S1',placement['summary'])
        with tempfile.TemporaryDirectory() as directory:
            cam=gerbers.check(directory,board(),2)
        self.assertFalse(cam['ok'])
        self.assertEqual(cam['status'],'unverified')
        self.assertTrue(any('SLOT' in p for p in cam['problems']))

    def test_reuse_compares_declared_shape_length_and_axis(self):
        pkg={'smds':[],'pads':[{'name':'S1','x':0,'y':0,'drill':0.6,'diameter':1,
                              'shape':'slot','slot_length':1.7,'rot':90}]}
        exported=ET.fromstring('<package name="P">'+SLOT+'</package>')
        self.assertTrue(same_package(exported,pkg))
        self.assertTrue(same_package(ET.fromstring('<package>'+SLOT.replace('R90','R270')+'</package>'),pkg))
        for attr,value in (('shape','round'),('slotLength','1.4'),('rot','R0')):
            changed=copy.deepcopy(exported); changed.find('pad').set(attr,value)
            with self.subTest(attr=attr): self.assertFalse(same_package(changed,pkg))
        missing=copy.deepcopy(exported); del missing.find('pad').attrib['slotLength']
        self.assertFalse(same_package(missing,pkg))

    def test_existing_round_pad_checks_keep_working(self):
        root=board(SLOT.replace(' shape="slot"','').replace(' slotLength="1.7"',''))
        self.assertEqual(stitch.board_obstacles(root)[0][0].kind,'circle')
        self.assertEqual(len(PC.pads(root)),1)
        self.assertEqual(gerbers.design_holes(root)[0].holes,[(0.6,10.0,20.0)])

    def test_routed_cam_commands_are_not_misread_as_round_drills(self):
        for command in ('X10.0Y20.0G85X10.0Y21.7','G00X10.0Y20.0\nM15\nG01X10.0Y21.7\nM16'):
            with self.subTest(command=command),tempfile.TemporaryDirectory() as directory:
                drill=Path(directory)/'board.drl'
                drill.write_text('M48\nMETRIC\nT01C0.6\n%\nT01\n'+command+'\nM30\n')
                with self.assertRaisesRegex(ValueError,'Unverified.*Excellon'):
                    gerbers.parse_excellon(str(drill))
                root=board(SLOT.replace(' shape="slot"','').replace(' slotLength="1.7"',''))
                result=gerbers.check(directory,root,2)
                self.assertEqual(result['status'],'unverified')
                self.assertFalse(result['ok'])

    def test_sourced_geometry_requirement_is_unverified_instead_of_passing(self):
        from fusion_offline import requirements as R
        from test_design_requirements import fixture,req
        root,spec,evidence=fixture()
        root.find('.//package[@name="QFN"]').append(ET.fromstring(SLOT))
        spec['requirements']=[req('keepout',region_mm=[20,0,25,5],layers=[1],allowed_nets=[])]
        report=R.check(root,spec,evidence)
        self.assertEqual(report['status'],'unverified')
        self.assertIn('SLOT',report['findings'][-1]['observed'])


if __name__=='__main__': unittest.main()
