"""Revision G Ethernet branch geometry, reduced from the pre-import export."""

from collections import Counter
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from fusion_offline.design import parse_board_design
from fusion_offline.requirements import path_length
from fusion_offline.route_all import canonicalize_routed_signals


class RevisionGBranchesTest(unittest.TestCase):
    def test_canonical_joins_preserve_main_paths(self):
        path = Path(__file__).parent / "fixtures/compiler/revision-g-branches.brd"
        root = ET.parse(path).getroot()
        cases = {
            "ETH_TX_P": ("U10.2", "J7.1", "R49.1", (116.1454, 68.6, 15)),
            "ETH_TX_N": ("U10.1", "J7.2", "R50.1", (115.23, 67.22, 15)),
            "ETH_RX_P": ("U10.6", "C45.1", "R52.1", (115.6, 73.15, 1)),
            "ETH_RX_N": ("U10.5", "C46.1", "R53.1", (113.1546, 69.5, 16)),
        }
        lengths = {}
        for net, (source, sink, branch, _) in cases.items():
            self.assertIsNone(path_length(root, [source, branch], net), net)
            lengths[net] = path_length(root, [source, sink], net)
            self.assertIsNotNone(lengths[net])
        canonicalize_routed_signals(root, cases, via_tolerance=0.1)
        root = ET.fromstring(ET.tostring(root))
        design = parse_board_design(root)
        for net, (source, sink, branch, join) in cases.items():
            with self.subTest(net=net):
                self.assertIsNotNone(path_length(root, [source, branch], net))
                self.assertAlmostEqual(path_length(root, [source, sink], net), lengths[net], places=8)
                ends = Counter(point for w in design.signals[net].wires
                               for point in ((w.x1, w.y1, w.layer), (w.x2, w.y2, w.layer)))
                self.assertEqual(ends[join], 2 if net == "ETH_RX_P" else 3)
                self.assertNotIn((115.7, 73.15, 1), ends)

    def test_default_splits_host_but_does_not_move_the_branch_endpoint(self):
        root = ET.parse(Path(__file__).parent / "fixtures/compiler/revision-g-branches.brd").getroot()
        net = "ETH_RX_P"
        canonicalize_routed_signals(root, [net])
        signal = root.find(f".//signal[@name='{net}']")
        self.assertIsNotNone(signal.find("wire[@x1='115.7'][@y1='74.05'][@x2='115.7'][@y2='73.15']"))
        ET.SubElement(signal, "via", x="115.75", y="73.15", extent="1-16", drill=".2", diameter=".45")
        canonicalize_routed_signals(root, [net], via_tolerance=0.1)
        self.assertIsNotNone(signal.find("wire[@x1='115.7'][@y1='74.05'][@x2='115.7'][@y2='73.15']"))
