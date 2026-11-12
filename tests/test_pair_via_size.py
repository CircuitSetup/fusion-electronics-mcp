import unittest
import xml.etree.ElementTree as ET

from test_route_pair import board, plan


class PairViaSizeTest(unittest.TestCase):
    def test_pair_uses_board_via_diameter_for_spacing_and_writes(self):
        root = board()
        rules = root.find("./drawing/board/designrules")
        ET.SubElement(rules, "param", name="rlMinViaOuter", value="8mil")
        changes = [{"at": [7, 0]}, {"at": [13, 0]}]
        result = plan(root, layer_changes=changes)
        self.assertTrue(result["ok"], result["conflicts"])
        self.assertEqual(result["via_diameter_mm"], 0.7064)
        self.assertEqual(result["rules"]["via_pair_spacing_mm"], 0.8564)
        with self.assertRaisesRegex(ValueError, "via diameter.*0.7064"):
            plan(root, layer_changes=changes, via_diameter=0.6)
        self.assertTrue(plan(root, via_diameter=0.6)["ok"])

    def test_pair_rejects_drill_below_board_minimum(self):
        root = board()
        rules = root.find("./drawing/board/designrules")
        ET.SubElement(rules, "param", name="msDrill", value="0.35mm")
        changes = [{"at": [7, 0]}, {"at": [13, 0]}]
        with self.assertRaisesRegex(ValueError, "via drill.*0.35"):
            plan(root, layer_changes=changes)
        self.assertTrue(plan(root, layer_changes=changes, via_drill=0.35)["ok"])
        self.assertTrue(plan(root)["ok"])

    def test_route_pair_writes_the_planned_diameter(self):
        from unittest import mock

        from fusion_mcp import server

        root = board()
        rules = root.find("./drawing/board/designrules")
        ET.SubElement(rules, "param", name="rlMinViaOuter", value="8mil")
        snap = mock.Mock(board_xml=ET.tostring(root))
        with (
            mock.patch.object(server, "_snap", return_value=snap),
            mock.patch.object(server, "_v2_rules", return_value=(None, "offline fixture")),
            mock.patch.object(server, "_layer_depths", return_value=({1: 0.0, 16: 1.6}, "test")),
            mock.patch.object(server, "_write_layer", side_effect={"top": 1, "bottom": 16}.__getitem__),
            mock.patch.object(server.session, "require_design", return_value="test"),
            mock.patch.object(server.session, "verified_write", return_value=(None, "test")) as write,
            mock.patch.object(server.C, "add_via", wraps=server.C.add_via) as add_via,
        ):
            result = server.route_pair(
                "DP", "DN", [[1, 0], [19, 0]], 0.2, 0.3,
                layer_changes=[{"at": [7, 0]}, {"at": [13, 0]}],
            )
        self.assertTrue(result["written"], result)
        write.assert_called_once()
        self.assertEqual([call.args[-1] for call in add_via.call_args_list], [0.7064] * 4)
