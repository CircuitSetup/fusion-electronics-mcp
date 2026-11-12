"""Make straight copper junctions explicit in emitted EAGLE XML."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from math import dist
import xml.etree.ElementTree as ET


# Four-decimal routing can put a tap half a unit off either axis.
_TAP_TOLERANCE = 0.000071
_VIA_TOLERANCE = 0.01


def canonicalize_signal(signal, anchors=()):
    """Snap unambiguous via landings and split host wires at same-layer endpoints."""
    anchors = set(anchors)
    vias = defaultdict(set)
    for via in signal.findall("via"):
        first, last = map(int, via.attrib["extent"].split("-"))
        point = float(via.attrib["x"]), float(via.attrib["y"])
        for layer in range(first, last + 1):
            vias[layer].add(point)

    wires = {}
    points = defaultdict(set)
    for wire in signal.findall("wire"):
        layer = int(wire.attrib["layer"])
        if layer == 19 or float(wire.get("curve", "0")):
            continue
        ends = []
        for index in (1, 2):
            point = tuple(float(wire.attrib[f"{axis}{index}"]) for axis in "xy")
            if (*point, layer) not in anchors and point not in vias[layer]:
                matches = [center for center in vias[layer] if dist(point, center) <= _VIA_TOLERANCE]
                if len(matches) == 1:
                    point = matches[0]
                    for axis, value in zip("xy", point):
                        wire.set(f"{axis}{index}", str(value))
            ends.append(point)
        wires[wire] = (layer, *ends)
        points[layer].update(ends)

    for layer, centers in vias.items():
        points[layer].update(centers)
    for x, y, layer in anchors:
        points[layer].add((x, y))
    ordered = {layer: sorted(layer_points) for layer, layer_points in points.items()}

    for wire, (layer, a, b) in wires.items():
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = dist(a, b)
        if not length:
            continue
        candidates = ordered[layer]
        left = bisect_left(candidates, (min(a[0], b[0]) - _TAP_TOLERANCE, -float("inf")))
        right = bisect_right(candidates, (max(a[0], b[0]) + _TAP_TOLERANCE, float("inf")))
        taps = []
        for x, y in candidates[left:right]:
            along = ((x - a[0]) * dx + (y - a[1]) * dy) / length
            cross = abs((x - a[0]) * dy - (y - a[1]) * dx) / length
            if _TAP_TOLERANCE < along < length - _TAP_TOLERANCE and cross <= _TAP_TOLERANCE:
                taps.append((along, (x, y)))
        if not taps:
            continue
        # Keep the original wire and its owner attributes on each subdivided piece.
        index = list(signal).index(wire)
        signal.remove(wire)
        path = [a, *(point for _, point in sorted(taps)), b]
        for start, end in zip(path, path[1:]):
            piece = ET.Element("wire", {**wire.attrib, "x1": str(start[0]), "y1": str(start[1]),
                                        "x2": str(end[0]), "y2": str(end[1])})
            signal.insert(index, piece)
            index += 1
