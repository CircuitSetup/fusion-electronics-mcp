"""Route many connections on a working copy of the board, with rip-up and reroute.

The free-router step of the user's order (layer pours, big pours, GND vias, close hops,
fan-outs, then this): each connection is routed with router.route (taps into the net's
copper allowed). When one cannot be routed, it is tried again allowed to cross the traces this
run laid, at a cost; every net it crosses is ripped up (whole net, so no tap is left hanging
off missing copper) and queued again. Traces that existed before the run (bus lanes, hand
routes) are never ripped. Rounds are limited per connection.
"""

from __future__ import annotations

import copy
import math
import xml.etree.ElementTree as ET
from collections import Counter, deque
from dataclasses import dataclass, field

from . import router as RT
from .junctions import canonicalize_signal
from .routing_budget import RoutingBudget, RoutingJob, SearchDiagnostics, SearchProgress


@dataclass
class Conn:
    id: str
    net: str
    start: tuple
    start_layers: tuple
    goal: tuple
    goal_layers: tuple
    width: float = 0.25
    label: str = ""


@dataclass
class Result:
    routes: dict = field(default_factory=dict)       # conn id -> Route
    failed: dict = field(default_factory=dict)       # conn id -> reason
    rips: int = 0
    root: ET.Element | None = None
    diagnostics: dict[str, SearchDiagnostics] = field(default_factory=dict)


def _sig(root, net):
    return next(s for s in root.iterfind("./drawing/board/signals/signal") if s.get("name") == net)


def canonicalize_routed_signals(root, nets, **options):
    for net in sorted(set(nets)):
        signal = _sig(root, net)
        anchors = []
        for contact in signal.findall("contactref"):
            x, y, layers, _ = RT.pad_target(root, contact.attrib["element"], contact.attrib["pad"])
            anchors.extend((round(x, 4), round(y, 4), layer) for layer in layers)
        canonicalize_signal(signal, anchors, **options)


def _commit(root, c: Conn, r):
    sg = _sig(root, c.net)
    for l, pts, w in r.pieces(c.width):
        for p, q in zip(pts, pts[1:]):
            ET.SubElement(sg, "wire", {"x1": str(p[0]), "y1": str(p[1]), "x2": str(q[0]), "y2": str(q[1]),
                                       "width": str(w), "layer": str(l), "mcp_conn": c.id})
    for vx, vy in r.vias:
        ET.SubElement(sg, "via", {"x": str(vx), "y": str(vy), "extent": '-'.join(map(str, r.via_extent)), "drill": str(r.via_drill),
                                  "diameter": str(r.via_diameter), "mcp_conn": c.id})


def _rip(root, ids: set):
    for sg in root.iterfind("./drawing/board/signals/signal"):
        for el in list(sg):
            if el.get("mcp_conn") in ids:
                sg.remove(el)


def _not_joined(root, c: Conn, checkpoint=None) -> set:
    frag, _ = RT.fragment(root, c.net, c.goal, c.goal_layers, include_layers=True, checkpoint=checkpoint)
    key = lambda p: (round(p[0], 3), round(p[1], 3))
    out = set()
    for layer, a, b in RT._net_traces(root, c.net):
        if checkpoint:
            checkpoint()
        k = (layer, key(a), key(b))
        if k not in frag and (layer, k[2], k[1]) not in frag:
            out.add(k)
    return out


def route_all(root: ET.Element, conns: list[Conn], vias: bool = True, rounds: int = 3,
              rip_cost: float = 15.0, policies=None, priorities=None, *,
              budget: RoutingBudget | None = None, protected_nets=(), clock=None,
              canonicalize=True, **kw) -> Result:
    if budget is not None:
        job = RoutingJob(budget) if clock is None else RoutingJob(budget, clock)
        result = _bounded_route_all(root, conns, vias, rip_cost, policies or {}, priorities or {},
                                    job, set(protected_nets), kw)
    else:
        result = _legacy_route_all(root, conns, vias, rounds, rip_cost, policies, priorities, **kw)
    if canonicalize:
        canonicalize_routed_signals(result.root, (route.net for route in result.routes.values()))
    return result


def _legacy_route_all(root, conns, vias, rounds, rip_cost, policies, priorities, **kw):
    work = copy.deepcopy(root)
    res = Result(root=work)
    by_id = {c.id: c for c in conns}
    tries = Counter()
    queue = sorted(conns, key=lambda c: -(priorities or {}).get(c.net, 0))
    base_options = kw.copy()
    while queue:
        c = queue.pop(0)
        kw = {**base_options, **(policies or {}).get(c.net, {})}
        allow_vias = vias and kw.pop('vias', True)
        if c.id in res.routes:
            continue
        # tap only copper already joined to the goal: tapping a piece that is not (a bus lane no
        # pin has reached yet) would look routed and leave the connection open
        kw["tap_exclude"] = _not_joined(work, c)
        r = RT.route(work, c.net, c.start, c.start_layers, c.goal, c.goal_layers, width=c.width,
                     vias=allow_vias, join_existing=True, **kw)
        if r.legs and not r.problems:
            _commit(work, c, r)
            res.routes[c.id] = r
            res.failed.pop(c.id, None)
            continue
        tries[c.id] += 1
        if tries[c.id] > rounds:
            res.failed[c.id] = (r.problems or ["no route"])[0]
            continue
        laid = {i for i in res.routes}
        kw["tap_exclude"] = _not_joined(work, c)
        r2 = RT.route(work, c.net, c.start, c.start_layers, c.goal, c.goal_layers, width=c.width, vias=allow_vias,
                      join_existing=True, soft_tags=laid, rip_cost=rip_cost, **kw)
        if not r2.legs or r2.problems:
            res.failed[c.id] = (r2.problems or r.problems or ["no route"])[0]
            continue
        # rip every net it crosses (whole nets: no tap left hanging), then lay it and requeue them
        hit_nets = {by_id[t].net for t in r2.conflicts if t in by_id}
        rip = {i for i in res.routes if by_id[i].net in hit_nets}
        _rip(work, rip)
        for i in rip:
            del res.routes[i]
            queue.append(by_id[i])
        res.rips += len(rip)
        kw["tap_exclude"] = _not_joined(work, c)
        r3 = RT.route(work, c.net, c.start, c.start_layers, c.goal, c.goal_layers, width=c.width,
                      vias=allow_vias, join_existing=True, **kw)
        if r3.legs and not r3.problems:
            _commit(work, c, r3)
            res.routes[c.id] = r3
            res.failed.pop(c.id, None)
        else:
            queue.append(c)
    return res


def _bounded_route_all(root, conns, vias, rip_cost, policies, priorities, job, protected, options):
    work = copy.deepcopy(root)
    res = Result(root=work)
    by_id = {c.id: c for c in conns}
    if len(by_id) != len(conns) or "" in by_id:
        raise ValueError("Routing connection IDs must be nonempty and unique")

    # Existing copper can carry tags from a previous job with the same IDs.
    old_tags = [(el, tag) for el in work.iter() if (tag := el.get("mcp_conn")) in by_id]
    for el, _ in old_tags:
        el.set("mcp_conn", "")

    progress = {c.id: SearchProgress(job, job.budget.max_vias_per_connection) for c in conns}
    rip_rounds = Counter()
    queue = deque(sorted(conns, key=lambda c: (-priorities.get(c.net, 0), c.id)))
    while queue:
        c = queue.popleft()
        state = progress[c.id]
        if c.net in protected:
            res.failed[c.id] = f"Net {c.net} is protected from routing"
            res.diagnostics[c.id] = state.finish("protected_net")
            continue
        kw = {**options, **policies.get(c.net, {})}
        if "allowed_layers" in kw:
            kw["layers"] = kw.pop("allowed_layers")
        allow_vias = vias and kw.pop("vias", True)
        kw.pop("cache", None)  # Always use current copper on the working board.
        kw.pop("soft_tags", None)
        kw.pop("tap_exclude", None)

        def search(soft_tags=None):
            return RT.route(work, c.net, c.start, c.start_layers, c.goal, c.goal_layers, width=c.width,
                            vias=allow_vias, join_existing=True, soft_tags=soft_tags, rip_cost=rip_cost,
                            _progress=state, _tap_exclude=lambda check: _not_joined(work, c, check), **kw)

        route = search()
        if route.diagnostics.termination in ("no_path", "max_vias"):
            laid = {i for i in res.routes if by_id[i].net != c.net}
            if laid and rip_rounds[c.id] < job.budget.ripup_rounds:
                rip_rounds[c.id] += 1
                route = search(laid)
                if route.diagnostics.termination == "routed":
                    # The checked path crosses only the reported soft obstacles. Remove whole
                    # run-owned nets so no remaining route taps copper that was removed.
                    hit_nets = {by_id[i].net for i in route.conflicts}
                    rip = sorted(i for i in laid if by_id[i].net in hit_nets)
                    if rip:
                        _rip(work, set(rip))
                    for i in rip:
                        del res.routes[i]
                        queue.append(by_id[i])
                    res.rips += len(rip)

        res.diagnostics[c.id] = route.diagnostics
        if route.diagnostics.termination == "routed":
            _commit(work, c, route)
            res.routes[c.id] = route
            res.failed.pop(c.id, None)
        else:
            res.failed[c.id] = (route.problems or ["no route"])[0]

    for el, tag in old_tags:
        el.set("mcp_conn", tag)
    return res
