"""Compare rule intent and normalized copper restrictions after a native round trip."""

import unittest

from fusion_offline.compiler.roundtrip import compare_native_board


def board(rules, classes, shapes):
    return f"""<eagle><drawing><board><plain>{shapes}</plain><classes>{classes}</classes>
    <designrules><param name="layerSetup" value="(1*2*15*16)"/>{rules}</designrules>
    <signals><signal name="A" class="1"/></signals></board></drawing></eagle>""".encode()


class RoundTripTest(unittest.TestCase):
    def test_units_symmetric_class_clearances_and_combined_outer_shapes(self):
        classes = """<class number="0" name="default" width="0" drill="0">
          <clearance class="1" value=".20"/></class>
          <class number="1" name="signal" width=".20" drill=".20">
          <clearance class="0" value=".2mm"/></class>"""
        sparse = classes.replace('<clearance class="0" value=".2mm"/>', '')
        shape = '<rectangle x1="1" y1="2" x2="3" y2="4" layer="{}" {}/>'
        legacy = shape.format(41, "") + shape.format(42, "") + shape.format(43, "")
        native = shape.format(41, 'restrictlayers="16,1"') + shape.format(43, "")
        candidate = board('<param name="msDrill" value="0.20mm"/>', classes, legacy)
        exported = board('<param name="msDrill" value="200um"/><param name="msWidth" value=".25mm"/>',
                         sparse, native)
        result = compare_native_board(candidate, exported)
        self.assertEqual(result["rules"]["status"], "pass")
        self.assertEqual(result["rules"]["observed"]["parameters"]["msDrill"], 0.2)
        self.assertEqual(result["rules"]["observed"]["class_clearances_mm"], {"0:1": 0.2})
        self.assertEqual(result["restrictions"]["status"], "pass")

        rules = b'<designrules><rule type="Minimum Drill Size" value="0.25mm" onescope="all"/></designrules>'
        result = compare_native_board(candidate, exported, rules)
        self.assertEqual(result["rules"]["status"], "fail")
        self.assertEqual(result["rules"]["observed"]["parameters"]["msDrill"], 0.25)

        rules = b'<designrules><rule type="Copper Clearance" value="0.25mm" onescope="classes=1" otherscope="classes=0"/></designrules>'
        result = compare_native_board(candidate, exported, rules)
        self.assertEqual(result["rules"]["status"], "fail")
        self.assertEqual(result["rules"]["observed"]["class_clearances_mm"], {"0:1": 0.25})

        result = compare_native_board(candidate, exported.replace(b"16,1", b"1,2"))
        self.assertEqual(result["restrictions"]["status"], "fail")

    def test_polygon_with_same_bounds_and_different_contour_fails(self):
        rectangle = '<rectangle x1="1" y1="2" x2="3" y2="4" layer="41"/>'
        triangle = """<polygon width="0" layer="41"><vertex x="1" y="2"/>
          <vertex x="3" y="2"/><vertex x="3" y="4"/></polygon>"""
        result = compare_native_board(board("", "", rectangle), board("", "", triangle))
        self.assertEqual(result["restrictions"]["status"], "fail")

    def test_package_via_restriction_change_is_separate(self):
        def mounted(polygon):
            raw = board("", "", '<rectangle x1="1" y1="2" x2="3" y2="4" layer="41"/>')
            package = f"""<libraries><library name="L"><packages><package name="PKG">
                {polygon}</package></packages></library></libraries>
                <elements><element name="U4" library="L" package="PKG" x="10" y="20" rot="R90"/></elements>"""
            return raw.replace(b"</board>", package.encode() + b"</board>")

        legacy = '<rectangle x1="0" y1="0" x2="4" y2="4" layer="43"/>'
        native = """<polygon width=".2" layer="43"><vertex x=".1" y=".1"/><vertex x="3.9" y=".1"/>
            <vertex x="3.9" y="3.9"/><vertex x=".1" y="3.9"/></polygon>"""
        result = compare_native_board(mounted(legacy), mounted(native))
        self.assertEqual(result["rules"]["status"], "pass")
        self.assertEqual(result["restrictions"]["board_status"], "pass")
        self.assertEqual(result["restrictions"]["package_status"], "fail")
        self.assertEqual(set(result["restrictions"]["changes"]), {"packages"})
        self.assertEqual(result["restrictions"]["observed"]["packages"][0][:3], ("U4", "polygon", "via"))
