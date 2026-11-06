"""Drive Fusion's autorouter (EAGLE AUTO) end to end.

Observed on Fusion 2705.1.15 (Windows): `AUTO;` opens the "Routing Variants
Dialog" (automation id cRoutersDialog) and starts routing immediately in
several variants, listed as rows like
    "7 completed Optimize4:  100.0%  Vias: 0"
    "1 running TopRouter:   37.5%  Vias: 0 (TopRouter)"
Buttons: End Job (apply the selected variant), Cancel, Start (restart;
pressing it closes the dialog without applying), >>.
So: wait until no row is running, select the best row (highest %, then
fewest vias), press End Job. On small boards Fusion may finish and close
the job itself before that (it applied a variant: the board changed), so
callers judge success by the board, not by whether a row was selected. Any other dialog during the job gets the safe
answer. Existing traces are kept by the autorouter; it routes airwires.
"""

from __future__ import annotations

import re
import threading
import time

from . import dialogs as dg
from .bridge import Bridge, BridgeOpError

# Asked when a copper layer that holds objects (typically an inner GND plane
# pour) is not enabled for routing. Running anyway is right for planes: the
# autorouter keeps signals off them, and the pour refills around new vias.
PLANE_LAYERS_PROMPT = r"used but not enabled!.*run the autorouter anyway"

# Fusion's TopRouter variant (row 1 of the job) was seen to hang at the share
# already routed (68%) on a board with pre-routed pairs, pours and a corridor of
# parts; End Job then applied that variant, i.e. nothing. The other variants
# finished at 100%. The control file switches it: TopRouterVariant = 0/1.
TOP_ROUTER_KEY = re.compile(r"^(\s*TopRouterVariant\s*=\s*)\d+", re.M)


def ctl_without_top_router(text: str) -> str:
    if TOP_ROUTER_KEY.search(text):
        return TOP_ROUTER_KEY.sub(r"\g<1>0", text)
    return text.replace("[Default]", "[Default]\n  TopRouterVariant  = 0", 1)


ROW = re.compile(r"^(\d+)\s+(\w+)\s+(.+?):\s+([\d.]+)%\s+Vias:\s*(\d+)")


def parse_rows(items: list[str]) -> list[dict]:
    rows = []
    for it in items:
        m = ROW.match(it.strip())
        if m:
            rows.append({"index": int(m.group(1)), "status": m.group(2), "router": m.group(3).strip(),
                         "percent": float(m.group(4)), "vias": int(m.group(5)), "label": it})
    return rows


def best(rows: list[dict]) -> dict | None:
    done = [r for r in rows if r["status"] != "running"]
    return max(done, key=lambda r: (r["percent"], -r["vias"]), default=None)


class AutorouteStateUnknown(RuntimeError):
    """The AUTO call may have changed Fusion; inspect it before retrying."""


def run(bridge: Bridge, commands: str = "AUTO;", timeout_s: float = 600.0, poll_s: float = 1.0,
        answers: list[tuple[str, str]] | None = None, stall_s: float = 45.0) -> dict:
    """stall_s: when at least one variant has completed at 100% and the still-running ones
    have not progressed for this long, stop waiting and apply the best completed one
    (a TopRouter variant was seen to hang at 68% indefinitely)."""
    if not dg.supported():
        raise RuntimeError("the Fusion autorouter flow needs Windows UI Automation (not available on this OS)")
    pid = int(bridge._info().get("pid") or 0)
    quiet = Bridge(bridge.info_path, watch_dialogs=False)
    quiet.lock = bridge.lock
    stop = threading.Event()
    t0 = time.monotonic()
    deadline = t0 + timeout_s
    chosen, rows, seen, stalled_rows = None, [], [], []
    observer_error = None

    def watching() -> bool:
        return not stop.is_set() and time.monotonic() < deadline

    def observe():
        nonlocal chosen, rows, stalled_rows, observer_error
        last_running, last_change = None, t0
        try:
            while watching() and not stop.wait(min(poll_s, max(0.0, deadline - time.monotonic()))):
                for w in dg.windows(pid):
                    if not watching():
                        return
                    if w.is_toast:
                        continue
                    if "Routing Variants" in w.title:
                        info = dg.read(w)
                        rows = parse_rows(info.get("items", []))
                        running = tuple((r["index"], r["percent"]) for r in rows if r["status"] == "running")
                        if running != last_running:
                            last_running, last_change = running, time.monotonic()
                        done_full = any(r["status"] != "running" and r["percent"] >= 100.0 for r in rows)
                        stalled = running and done_full and time.monotonic() - last_change > stall_s
                        if rows and (not running or stalled) and chosen is None and watching():
                            stalled_rows = [r["label"] for r in rows if r["status"] == "running"] if stalled else []
                            variant = best(rows)
                            # End Job applies the loaded variant; invoking the row loads it.
                            label = variant["label"]
                            outcome = dg.act(w, [{"do": "select", "target": label},
                                                 {"do": "activate", "target": label},
                                                 {"do": "press", "target": "End Job"}], strict=True)
                            if outcome != f"select:{label},activate:{label},press:End Job":
                                raise RuntimeError(f"Could not apply the routing variant ({outcome or 'no confirmation'})")
                            chosen = variant
                    else:
                        seen.append(dg.handle(w, answers, may_act=watching).as_dict())
        except Exception as ex:
            observer_error = ex

    watcher = threading.Thread(target=observe, daemon=True)
    watcher.start()
    try:
        # Bridge.call re-enters the transaction lock only on its owning thread.
        quiet.call("run", {"commands": commands, "editor": "board"}, timeout=timeout_s)
    except Exception as ex:
        if isinstance(ex, BridgeOpError) and ex.code == "timeout" and "before Fusion started it" in ex.message:
            raise
        raise AutorouteStateUnknown(f"Fusion did not confirm the AUTO result. Inspect Fusion before retrying: {ex}") from ex
    finally:
        stop.set()
        watcher.join()
    if observer_error is not None:
        raise AutorouteStateUnknown(
            f"Could not observe the autorouter. Inspect Fusion before retrying: {observer_error}") from observer_error
    return {"variants": rows, "applied": chosen, "other_dialogs": seen, "stalled": stalled_rows,
            "seconds": round(time.monotonic() - t0, 1), "timed_out": False}
