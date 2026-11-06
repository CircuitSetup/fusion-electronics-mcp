"""Autorouting must dispatch under the caller's transaction lock."""

import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from fusion_mcp import autoroute as AR
from fusion_mcp.bridge import Bridge, BridgeOpError
from fusion_mcp.fusion_lock import FusionLock


class AutorouteLockTest(unittest.TestCase):
    def test_dispatch_and_dialog_handling_while_transaction_holds_real_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(watch_dialogs=False)
            bridge.lock = FusionLock(os.path.join(tmp, "fusion.lock"))
            owner = threading.get_ident()
            dispatched = threading.Event()
            applied = threading.Event()
            dispatch_threads = []

            def transport(op, args, timeout, answers, forms):
                dispatch_threads.append(threading.get_ident())
                dispatched.set()
                applied.wait(timeout)
                return {}

            dialog = SimpleNamespace(title="Routing Variants", is_toast=False)
            def action(w, actions, **kwargs):
                applied.set()
                return ",".join(f"{step['do']}:{step['target']}" for step in actions)

            with mock.patch.object(AR, "Bridge", return_value=bridge), \
                    mock.patch.object(bridge, "_info", return_value={"pid": 1}), \
                    mock.patch.object(bridge, "_call", side_effect=transport), \
                    mock.patch.object(AR.dg, "supported", return_value=True), \
                    mock.patch.object(AR.dg, "windows", side_effect=lambda pid: [dialog] if dispatched.is_set() else []), \
                    mock.patch.object(AR.dg, "read", return_value={"items": ["1 completed Optimize4: 100.0% Vias: 0"]}), \
                    mock.patch.object(AR.dg, "act", side_effect=action):
                with bridge.lock.hold("autoroute"):
                    result = AR.run(bridge, timeout_s=0.5, poll_s=0.01)
                    self.assertEqual(dispatch_threads, [owner])
                    self.assertTrue(applied.is_set())
                    self.assertEqual(result["applied"]["percent"], 100.0)

    def test_bridge_timeout_does_not_leave_a_worker_that_can_dispatch_later(self):
        for message, error in (
            ("before Fusion started it (not run)", BridgeOpError),
            ("while running in Fusion; it may still complete", AR.AutorouteStateUnknown),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                bridge = Bridge(watch_dialogs=False)
                bridge.lock = FusionLock(os.path.join(tmp, "fusion.lock"))
                call = BridgeOpError("timeout", message)
                real_thread = threading.Thread
                watchers = []

                def track_thread(*args, **kwargs):
                    watcher = real_thread(*args, **kwargs)
                    watchers.append(watcher)
                    return watcher

                with mock.patch.object(AR, "Bridge", return_value=bridge), \
                        mock.patch.object(bridge, "_info", return_value={"pid": 1}), \
                        mock.patch.object(bridge, "_call", side_effect=call) as transport, \
                        mock.patch.object(AR.dg, "supported", return_value=True), \
                        mock.patch.object(AR.dg, "windows", return_value=[]), \
                        mock.patch.object(AR.threading, "Thread", side_effect=track_thread):
                    with bridge.lock.hold("autoroute"), self.assertRaisesRegex(error, message.split(" (")[0]):
                        AR.run(bridge, timeout_s=0.01, poll_s=0.005)
                    transport.assert_called_once()
                    self.assertEqual(len(watchers), 1)
                    self.assertFalse(watchers[0].is_alive())

    def test_failed_variant_action_leaves_outcome_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(watch_dialogs=False)
            bridge.lock = FusionLock(os.path.join(tmp, "fusion.lock"))
            attempted = threading.Event()
            label = "1 completed Optimize4: 100.0% Vias: 0"

            def action(*args, **kwargs):
                attempted.set()
                return f"select:{label},missing:{label},press:End Job"

            with mock.patch.object(AR, "Bridge", return_value=bridge), \
                    mock.patch.object(bridge, "_info", return_value={"pid": 1}), \
                    mock.patch.object(bridge, "_call", side_effect=lambda *args: attempted.wait(1)), \
                    mock.patch.object(AR.dg, "supported", return_value=True), \
                    mock.patch.object(AR.dg, "windows", return_value=[SimpleNamespace(title="Routing Variants", is_toast=False)]), \
                    mock.patch.object(AR.dg, "read", return_value={"items": [label]}), \
                    mock.patch.object(AR.dg, "act", side_effect=action):
                with bridge.lock.hold("autoroute"), self.assertRaisesRegex(AR.AutorouteStateUnknown, "missing"):
                    AR.run(bridge, timeout_s=1, poll_s=0.01)

    def test_dialog_read_that_outlasts_watcher_does_not_click(self):
        stop = threading.Event()
        dialog = SimpleNamespace(title="Fusion", is_toast=False)
        info = {"texts": ["run the autorouter anyway"], "buttons": ["Yes", "No"],
                "ids": [], "radios": [], "checks": [], "edits": []}

        def read(window):
            stop.set()
            return info

        with mock.patch.object(AR.dg, "read", side_effect=read), mock.patch.object(AR.dg, "act") as act:
            seen = AR.dg.handle(dialog, [("autorouter", "Yes")], may_act=lambda: not stop.is_set())
        act.assert_not_called()
        self.assertIsNone(seen.answer)
