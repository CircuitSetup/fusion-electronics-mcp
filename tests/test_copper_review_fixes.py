import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory

from fusion_offline import gerbers, si
from fusion_offline.design import BoardDesign, Signal, Wire


class CopperReviewTest(unittest.TestCase):
    def test_diff_pair_reports_copper_and_marks_unrouted_skew_unknown(self):
        for routed in (True, False):
            with self.subTest(routed=routed):
                p = Signal("D_P", "", wires=[Wire(0, 0, 10, 0, 0, 19)])
                n = Signal("D_N", "", wires=[Wire(0, 1, 10, 1, 0, 19)])
                if routed:
                    p.wires.append(Wire(0, 0, 2, 0, 0.2, 1))
                    n.wires.append(Wire(0, 0.5, 2, 0.5, 0.2, 1))
                board = BoardDesign("", {}, {p.name: p, n.name: n}, {}, {}, [])
                report = si.pair_report(board, p.name, n.name, max_skew_mm=0.1)
                self.assertEqual(report["length_p_mm"], 2 if routed else 0)
                self.assertEqual(report["length_n_mm"], 2 if routed else 0)
                self.assertEqual(report["widths_mm"], [0.2] if routed else [])
                self.assertEqual(report["layers"], [1] if routed else [])
                self.assertEqual(report["gap_mm"], 0.3 if routed else None)
                self.assertFalse(report["routing_complete"])
                self.assertIsNone(report["within_limit"])

    def test_cam_checker_requires_drills_in_board_coordinates(self):
        root = ET.fromstring("""<eagle><drawing><board><plain>
            <wire x1="0" y1="0" x2="20" y2="20" layer="20"/>
            <hole x="2" y="3" drill="0.8"/>
            <hole x="5" y="7" drill="0.8"/>
        </plain></board></drawing></eagle>""")
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ("copper_top", "copper_bottom", "mask_top",
                         "mask_bottom", "silk_top", "profile"):
                (folder / f"{name}.gbr").write_text(
                    "%FSLAX24Y24*%\nX0Y0D02*\nX200000Y200000D01*\n", encoding="utf-8"
                )
            for hits, aligned in (
                ("X2.00Y3.00\nX5.00Y7.00", True),
                ("X12.00Y13.00\nX15.00Y17.00", False),
                ("X2.00Y7.00\nX5.00Y3.00", False),
            ):
                with self.subTest(hits=hits):
                    (folder / "board.drl").write_text(
                        f"M48\nMETRIC\nT01C0.8\n%\nT01\n{hits}\nM30\n", encoding="utf-8"
                    )
                    report = gerbers.check(tmp, root, copper_layers=2)
                    self.assertEqual(report["ok"], aligned, report)
