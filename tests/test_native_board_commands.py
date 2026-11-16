import types
import unittest
from unittest.mock import Mock, patch

from fusion_mcp import bridge as B
from fusion_mcp import server as S
from test_server import load_addin


class NativeBoardCommandsTest(unittest.TestCase):
    def setUp(self):
        self.addin = load_addin()
        self.doc = object()
        self.ui = types.SimpleNamespace(activeCommand="Electron::Group")
        self.addin._app = types.SimpleNamespace(activeDocument=self.doc)
        self.addin._ui = self.ui
        self.addin._product_kind = lambda: "board"
        self.addin._settle = Mock()
        self.command = types.SimpleNamespace(isNative=True, execute=Mock(return_value=True))
        self.ui.commandDefinitions = types.SimpleNamespace(itemById=Mock(return_value=self.command))
        self.args = {"expected_document_id": self.addin._document_id(self.doc)}

    def test_refill_and_stitch_launch_the_exact_native_commands(self):
        refill = self.addin.OPS["native_board_command"](self.args | {"action": "refill_pours"})
        self.ui.commandDefinitions.itemById.assert_called_once_with("Electron::PolyRefill")
        self.assertEqual(refill["status"], "requested")

        self.command.execute.side_effect = lambda: setattr(self.ui, "activeCommand", "Electron::Stitch") or True
        stitch = self.addin.OPS["native_board_command"](self.args | {"action": "start_via_stitching"})
        self.ui.commandDefinitions.itemById.assert_called_with("Electron::Stitch")
        self.assertEqual(stitch["status"], "dialog_open")
        self.assertEqual(self.command.execute.call_count, 2)

    def test_wrong_editor_document_busy_or_extra_input_never_starts_a_command(self):
        run = self.addin.OPS["native_board_command"]
        args = self.args | {"action": "refill_pours"}
        for bad in ({"action": "anything"}, {"command_id": "Other"},
                    {"expected_document_id": "stale"}, {"expected_document_id": ""}):
            with self.subTest(bad=bad), self.assertRaises(self.addin.BridgeError):
                run(args | bad)
        self.ui.activeCommand = "Electron::Route"
        with self.assertRaises(self.addin.BridgeError):
            run(args)
        self.ui.activeCommand = "Electron::Group"
        self.addin._product_kind = lambda: "schematic"
        with self.assertRaises(self.addin.BridgeError):
            run(args)
        self.command.execute.assert_not_called()

    def test_stitch_already_open_is_not_restarted_and_refusal_is_an_error(self):
        run = self.addin.OPS["native_board_command"]
        self.ui.activeCommand = "Electron::Stitch"
        self.assertEqual(run(self.args | {"action": "start_via_stitching"})["status"], "dialog_open")
        self.command.execute.assert_not_called()
        self.ui.activeCommand = "Electron::Group"
        self.command.execute.return_value = False
        with self.assertRaises(self.addin.BridgeError):
            run(self.args | {"action": "refill_pours"})

    def test_two_tools_expose_only_the_reviewed_commands(self):
        with patch.object(S.session.bridge, "call", return_value={}) as call:
            S.refill_pours("board-id")
            call.assert_called_with("native_board_command",
                                    {"expected_document_id": "board-id", "action": "refill_pours"})
            S.start_via_stitching("board-id")
            call.assert_called_with("native_board_command",
                                    {"expected_document_id": "board-id", "action": "start_via_stitching"})

    def test_requires_new_addin_and_leaves_native_dialog_focus_alone(self):
        bridge = B.Bridge(watch_dialogs=True, keep_focus=True)
        with patch.object(bridge, "transport", return_value="builtin"), self.assertRaisesRegex(B.BridgeOpError, "add-in"):
            bridge._call("native_board_command", self.args)
        with patch.object(bridge, "transport", return_value="addin"), \
                patch.object(bridge, "_info", return_value={"addin_version": "0.14.23"}), \
                self.assertRaisesRegex(B.BridgeOpError, "0.14.24"):
            bridge._call("native_board_command", self.args)

        conn = unittest.mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.recv.return_value = b'{"ok":true,"result":{"status":"dialog_open"}}\n'
        with patch.object(bridge, "transport", return_value="addin"), \
                patch.object(bridge, "_info", return_value={"protocol": B.PROTOCOL, "addin_version": "0.14.24",
                                                            "pid": 123, "port": 1, "token": "test"}), \
                patch.object(B.socket, "create_connection", return_value=conn), \
                patch.object(B.dialogs, "supported", side_effect=AssertionError("don't dismiss the native dialog")), \
                patch.object(B.dialogs, "foreground", side_effect=AssertionError("don't steal native dialog focus")):
            self.assertEqual(bridge._call("native_board_command", self.args), {"status": "dialog_open"})
