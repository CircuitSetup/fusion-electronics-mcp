import unittest
import xml.etree.ElementTree as ET

from fusion_offline.routing_budget import RoutingBudget

from test_routing_budgets import board, route


class RoutingDiagnosticsTests(unittest.TestCase):
    def test_wall_blocker_and_expansions_are_repeatable(self):
        root = board(wall=True)
        one = route(root, RoutingBudget(), layers=(1,), clock=lambda: 0)
        two = route(root, RoutingBudget(), layers=(1,), clock=lambda: 0)
        diagnostics = one.as_dict()["diagnostics"]
        self.assertEqual(diagnostics, two.as_dict()["diagnostics"])
        self.assertEqual(diagnostics["termination"], "no_path")
        self.assertGreater(diagnostics["expanded_nodes"], 0)
        self.assertGreater(diagnostics["closest_distance_mm"], 0)
        self.assertEqual(diagnostics["candidate_blockers"],
                         [{"type": "wire", "net": "X", "reference": None, "count": 1}])

    def test_via_keepout_is_reported_only_when_vias_are_enabled(self):
        root = board(wall=True)
        routed = route(root, RoutingBudget(), clock=lambda: 0)
        self.assertEqual(routed.diagnostics.termination, "routed")
        self.assertEqual(routed.diagnostics.vias_used, 2)
        ET.SubElement(root.find(".//plain"), "circle", x="20", y="10", radius="30", width="0", layer="43")
        blocked = route(root, RoutingBudget(), clock=lambda: 0)
        self.assertEqual(blocked.diagnostics.termination, "no_path")
        self.assertIn({"type": "keepout", "net": None, "reference": None, "count": 1},
                      blocked.diagnostics.as_dict()["candidate_blockers"])
        for options in ({"vias": False}, {"layers": (1,)}):
            with self.subTest(options=options):
                planar = route(root, RoutingBudget(), clock=lambda: 0, **options)
                self.assertNotIn("keepout", {row["type"] for row in planar.diagnostics.candidate_blockers})

    def test_invalid_layers_and_via_technology_are_structured(self):
        for options in ({"layers": (99,)}, {"layers": (1, 1)}, {"via_spans": [(1, 2)]}):
            with self.subTest(options=options):
                result = route(board(), RoutingBudget(), **options)
                self.assertEqual(result.diagnostics.termination, "unsupported_geometry")
                self.assertFalse(result.legs)

    def test_endpoints_excluded_by_policy(self):
        result = route(board(), RoutingBudget(), layers=(16,))
        self.assertEqual(result.diagnostics.termination, "no_path")
        self.assertEqual(result.diagnostics.expanded_nodes, 0)
