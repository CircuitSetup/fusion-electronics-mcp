import math
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from fusion_offline import route_all as RA, router as R, stitch
from fusion_offline.routing_budget import RoutingBudget

import test_offline


def board(*, wall=False):
    xml = test_offline.RouterTest.BOARD
    if wall:
        xml = xml.replace('y1="2" x2="20" y2="18"', 'y1="-1" x2="20" y2="21"')
    return ET.fromstring(xml)


def conn(ident="s", net="S", goal=(35.11, 10.02)):
    return RA.Conn(ident, net, (5.03, 10.07), (1,), goal, (1,))


def short_conn(ident="short", net="X"):
    return RA.Conn(ident, net, (6, 8), (1,), (8, 8), (1,))


def crossing():
    root = board()
    signal = root.find(".//signal[@name='X']")
    signal.remove(signal.find("wire"))
    across = RA.Conn("x", "X", (20, .5), (1,), (20, 19.5), (1,))
    return root, [across, conn()]


def route(root, budget, **options):
    c = conn()
    return R.route(root, c.net, c.start, c.start_layers, c.goal, c.goal_layers,
                   budget=budget, step=.254, **options)


class RoutingBudgetTests(unittest.TestCase):
    def test_incremental_route_taps_shared_escape_at_the_effective_width(self):
        root = board()
        signal = root.find(".//signal[@name='S']")
        ET.SubElement(signal, "wire", x1="5.03", y1="10.07", x2="12", y2="10.07", layer="1", width=".27")
        source = (5.03, 10.07)
        added = RA.Conn("branch", "S", source, (1,), (15, 14), (1,), width=.18)
        result = RA.route_all(root, [added], vias=False, budget=RoutingBudget(),
                              policies={"S": {"min_width_mm": .27}})
        route = result.routes["branch"]
        self.assertEqual(route.joined, "existing trace")
        self.assertNotEqual(route.legs[0][1][0], source)
        self.assertTrue(all(width == .27 for _, _, width in route.pieces(added.width)))

    def test_grid_and_final_route_check_block_drill_spacing_on_own_net(self):
        root = board()
        ET.SubElement(root.find(".//designrules"), "param", name="mdDrill", value="6mil")
        ET.SubElement(root.find(".//signal[@name='S']"), "via", x="12", y="12", drill=".3", extent="1-16")
        grid = R.Grid(root, "S", .25, None, .1, (12, 12), via_drill=.2)
        self.assertTrue(grid.via_blocked[grid.ij(12.3, 12)])
        self.assertFalse(grid.via_blocked[grid.ij(12.5, 12)])
        route = R.Route("S", vias=[(12.3, 12)], via_drill=.2)
        self.assertIn("drill spacing", R.check_route(grid, route)[0])

    def test_final_route_check_rejects_copper_outside_outline_or_edge_clearance(self):
        root = board()
        plain = root.find(".//plain")
        for wire in list(plain.findall("wire[@layer='20']")):
            plain.remove(wire)
        points = [(0, 0), (40, 0), (40, 20), (22, 20), (22, 7), (18, 7), (18, 20), (0, 20)]
        for a, b in zip(points, points[1:] + points[:1]):
            ET.SubElement(plain, "wire", x1=str(a[0]), y1=str(a[1]), x2=str(b[0]), y2=str(b[1]),
                          layer="20", width="0")
        root.find(".//signal[@name='X']").clear()
        grid = R.Grid(root, "S", .25, None, .254, (5, 10))
        for points in ([(5, 10), (35, 10)], [(5, .1), (15, .1)], [(5, -1), (15, -1)]):
            with self.subTest(points=points):
                self.assertFalse(R._seg_clear(grid, 1, *points))
                result = R.Route("S", legs=[(1, points)])
                self.assertTrue(R.check_route(grid, result))
        self.assertTrue(R._seg_clear(grid, 1, (5, 2), (15, 2)))

    def test_narrow_notch_cannot_fall_between_clearance_samples(self):
        root = board()
        plain = root.find(".//plain")
        for wire in list(plain.findall("wire[@layer='20']")):
            plain.remove(wire)
        outline = [(0, 0), (40, 0), (40, 20), (10.03, 20),
                   (10.03, 7), (10.02, 7), (10.02, 20), (0, 20)]
        for a, b in zip(outline, outline[1:] + outline[:1]):
            ET.SubElement(plain, "wire", x1=str(a[0]), y1=str(a[1]), x2=str(b[0]), y2=str(b[1]),
                          layer="20", width="0")
        root.find(".//signal[@name='X']").clear()
        root.find(".//param[@name='mdCopperDimension']").set("value", "0mm")
        grid = R.Grid(root, "S", .01, None, .254, (5, 10))
        self.assertFalse(R._seg_clear(grid, 1, (5, 10), (35, 10)))
        self.assertTrue(R.check_route(grid, R.Route("S", legs=[(1, [(5, 10), (35, 10)])])))
        result = R.route(root, "S", (5, 10), (1,), (35, 10), (1,), width=.01, step=.254,
                         layers=(1,), budget=RoutingBudget(), clock=lambda: 0)
        # The notch is narrower than the search grid: reject its shortcut rather than report success.
        self.assertEqual(result.diagnostics.termination, "no_path")
        self.assertTrue(any("board outline" in problem for problem in result.problems))

    def test_one_attempt_stops_at_exact_node_limit(self):
        root = board()
        before = ET.tostring(root)
        result = route(root, RoutingBudget(max_nodes_per_attempt=5))
        self.assertEqual(result.diagnostics.termination, "node_limit")
        self.assertEqual(result.diagnostics.expanded_nodes, 5)
        self.assertEqual(result.diagnostics.attempts, 1)
        self.assertFalse(result.legs)
        self.assertEqual(ET.tostring(root), before)

    def test_direction_pass_uses_only_remaining_nodes(self):
        result = route(board(wall=True), RoutingBudget(max_nodes_per_attempt=8_000), layers=(1,))
        self.assertEqual(result.diagnostics.termination, "node_limit")
        self.assertEqual(result.diagnostics.expanded_nodes, 8_000)
        self.assertEqual(result.diagnostics.attempts, 1)

    def test_attempt_deadline_includes_preprocessing(self):
        times = iter((0, 0, 0, 2_000_000_000))
        result = route(board(), RoutingBudget(max_seconds_per_attempt=1),
                       clock=lambda: next(times, 2_000_000_000))
        self.assertEqual(result.diagnostics.termination, "time_limit")
        self.assertEqual(result.diagnostics.expanded_nodes, 0)

    def test_deadline_interrupts_keepout_intersection_checks(self):
        points = [(10 + math.cos(i * math.tau / 24), 5 + math.sin(i * math.tau / 24))
                  for i in range(24)]
        intersect = stitch.segments_intersect
        for native in (False, True):
            with self.subTest(native=native):
                root = board()
                plain = root.find(".//plain")
                if native:
                    shape = ET.SubElement(plain, "polygonshape", layer="41", restrictlayers="1")
                    objects = ET.SubElement(shape, "polygonoutlineobjects")
                    segments = ET.SubElement(shape, "polygonoutlinesegments")
                    groups = ((objects, points), (segments, points + points[:1]))
                else:
                    groups = ((ET.SubElement(plain, "polygon", layer="41"), points),)
                for group, vertices in groups:
                    for x, y in vertices:
                        ET.SubElement(group, "vertex", x=str(x), y=str(y))

                pairs_checked = 0

                def advance_on_pair(*args):
                    nonlocal pairs_checked
                    pairs_checked += 1
                    return intersect(*args)

                with patch.object(stitch, "segments_intersect", advance_on_pair):
                    result = route(root, RoutingBudget(max_seconds_per_attempt=1),
                                   clock=lambda: pairs_checked * 20_000_000)
                self.assertEqual(result.diagnostics.termination, "time_limit")
                self.assertEqual(result.diagnostics.expanded_nodes, 0)
                self.assertFalse(result.legs)
                # The clock expires at pair 50; cancellation may take one 64-pair interval.
                self.assertGreaterEqual(pairs_checked, 50)
                self.assertLess(pairs_checked, 114)

    def test_attempt_deadline_interrupts_an_active_search(self):
        times = iter([0] * 128)
        result = route(board(wall=True), RoutingBudget(max_seconds_per_attempt=1), layers=(1,),
                       clock=lambda: next(times, 2_000_000_000))
        self.assertEqual(result.diagnostics.termination, "time_limit")
        self.assertGreater(result.diagnostics.expanded_nodes, 0)
        self.assertFalse(result.legs)

    def test_job_deadline_is_shared_across_connections(self):
        times = iter((0, 2_000_000_000))
        result = RA.route_all(board(), [conn(), conn("other", "X")], step=.254,
                              budget=RoutingBudget(max_seconds_per_job=1),
                              clock=lambda: next(times, 2_000_000_000))
        self.assertEqual(set(result.failed), {"s", "other"})
        self.assertEqual({d.termination for d in result.diagnostics.values()}, {"job_deadline"})
        self.assertEqual({d.attempts for d in result.diagnostics.values()}, {0})

    def test_attempt_limit_still_allows_independent_connection(self):
        root, conns = crossing()
        ET.SubElement(root.find(".//signals"), "signal", name="Y")
        original = ET.tostring(root)
        result = RA.route_all(root, [*conns, short_conn("z", "Y")], step=.254,
                              budget=RoutingBudget(max_attempts_per_connection=1),
                              policies={"S": {"allowed_layers": (1,)}}, priorities={"X": 1})
        self.assertEqual(set(result.routes), {"x", "z"})
        self.assertEqual(result.diagnostics["s"].attempts, 1)
        self.assertEqual(result.diagnostics["s"].termination, "attempt_limit")
        self.assertEqual(ET.tostring(root), original)

    def test_ripup_counts_attempts_for_both_connections(self):
        root, conns = crossing()
        result = RA.route_all(root, conns, budget=RoutingBudget(), step=.254,
                              policies={"S": {"allowed_layers": (1,)}}, priorities={"X": 1})
        self.assertEqual(set(result.routes), {"s", "x"})
        self.assertEqual(result.rips, 1)
        self.assertEqual({d.attempts for d in result.diagnostics.values()}, {2})
        self.assertEqual(len(result.routes["x"].vias), 2)

    def test_protected_net_cannot_be_routed_or_ripped(self):
        root = board(wall=True)
        existing = root.find(".//signal[@name='X']/wire")
        result = RA.route_all(root, [short_conn(), conn()], budget=RoutingBudget(), step=.254,
                              protected_nets={"X"}, policies={"S": {"allowed_layers": (1,)}})
        self.assertFalse(result.routes)
        self.assertEqual(result.diagnostics["short"].termination, "protected_net")
        self.assertEqual(result.diagnostics["short"].attempts, 0)
        self.assertEqual(result.rips, 0)
        self.assertEqual(result.root.find(".//signal[@name='X']/wire").attrib, existing.attrib)

    def test_connection_node_limit_caps_retry_after_first_passes(self):
        root, conns = crossing()
        result = RA.route_all(root, conns, budget=RoutingBudget(max_nodes_per_connection=52_000), step=.254,
                              policies={"S": {"allowed_layers": (1,)}}, priorities={"X": 1})
        self.assertEqual(set(result.routes), {"x"})
        failure = result.diagnostics["s"]
        self.assertEqual(failure.termination, "node_limit")
        self.assertEqual(failure.expanded_nodes, 52_000)
        self.assertEqual(failure.attempts, 2)
        self.assertEqual(result.rips, 0)

    def test_ripup_round_limit_applies_to_requeued_connections(self):
        root, conns = crossing()
        result = RA.route_all(root, conns, budget=RoutingBudget(max_attempts_per_connection=8, ripup_rounds=1),
                              step=.254, vias=False, layers=(1,), priorities={"X": 1})
        self.assertEqual(result.rips, 2)
        self.assertEqual(set(result.routes), {"x"})
        self.assertEqual(result.diagnostics["s"].attempts, 3)

    def test_max_vias_is_hard_and_per_net_can_reduce_it(self):
        root = board(wall=True)
        result = RA.route_all(root, [conn()], budget=RoutingBudget(max_vias_per_connection=2),
                              policies={"S": {"max_vias_per_connection": 1}}, step=.254)
        self.assertEqual(result.diagnostics["s"].termination, "max_vias")
        self.assertEqual(result.diagnostics["s"].via_limit, 1)
        self.assertFalse(result.routes)
        two = route(root, RoutingBudget(max_vias_per_connection=2))
        self.assertEqual(len(two.vias), 2)
        self.assertEqual(two.diagnostics.vias_used, 2)

    def test_direct_via_cap_enables_bounded_search_without_a_budget(self):
        result = route(board(wall=True), None, max_vias_per_connection=1)
        self.assertFalse(result.legs)
        self.assertEqual(result.diagnostics.termination, "max_vias")
        self.assertEqual(result.diagnostics.via_limit, 1)

    def test_search_respects_layer_policy(self):
        result = RA.route_all(board(), [conn()], budget=RoutingBudget(), step=.254,
                              policies={"S": {"allowed_layers": (1,)}})
        self.assertEqual(result.diagnostics["s"].layers_attempted, (1,))
        self.assertEqual({layer for layer, _ in result.routes["s"].legs}, {1})
        self.assertFalse(result.routes["s"].vias)

    def test_existing_copper_cannot_impersonate_a_ripup_connection(self):
        root = board(wall=True)
        existing = root.find(".//signal[@name='X']/wire")
        existing.set("mcp_conn", "short")
        result = RA.route_all(root, [short_conn(), conn()], step=.254,
                              budget=RoutingBudget(), policies={"S": {"allowed_layers": (1,)}},
                              priorities={"X": 1})
        self.assertEqual(set(result.routes), {"short"})
        self.assertEqual(result.rips, 0)
        self.assertIn(existing.attrib, [w.attrib for w in result.root.findall(".//signal[@name='X']/wire")])

    def test_via_span_policy_can_forbid_a_transition_around_a_wall(self):
        root = board(wall=True)
        ET.SubElement(root.find(".//designrules"), "param", name="layerSetup", value="(1*2*15*16)")
        result = route(root, RoutingBudget(), layers=(1, 16), via_spans=[])
        self.assertEqual(result.diagnostics.termination, "no_path")
        self.assertFalse(result.vias)

    def test_expanded_state_space_rejected_without_grid_allocation(self):
        with patch.object(R.np, "arange", side_effect=AssertionError("grid allocated before state limit")):
            c = conn()
            result = R.route(board(), c.net, c.start, c.start_layers, c.goal, c.goal_layers,
                             budget=RoutingBudget(max_vias_per_connection=8), step=.04)
        self.assertEqual(result.diagnostics.termination, "unsupported_geometry")
        self.assertIn("state", result.problems[0])
        self.assertEqual(result.diagnostics.expanded_nodes, 0)
