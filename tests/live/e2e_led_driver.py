"""Build a 5 V LED driver in an EMPTY, linked disposable Fusion design.

Run with the target schematic active:
    python tests/live/e2e_led_driver.py --document-id session:... --output-dir PATH

Uses public MCP tools; never saves/closes a document. Stops at the first failure,
retaining exports and a JSON report. A later --stage requires inspecting the failed
run first; it is not an automatic retry. Libraries must already be installed.
"""
import argparse
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from fusion_mcp import server as S
from fusion_mcp.custom_ulp import xml_hash


# Exact symbol pins, deliberately separate from expected PCB pads below.
NETS = {
    '+5V': ['J1.1', 'R1.1', 'C1.1'],
    'GND': ['J1.2', 'Q1.E', 'R3.2', 'C1.2'],
    'CTRL': ['J1.3', 'R2.1'],
    'BASE': ['R2.2', 'R3.1', 'Q1.B'],
    'LED_A': ['R1.2', 'LED1.A'],
    'LED_K': ['LED1.C', 'Q1.C'],
}
PADS = {
    '+5V': {'J1.1', 'R1.1', 'C1.1'},
    'GND': {'J1.2', 'Q1.E', 'R3.2', 'C1.2'},
    'CTRL': {'J1.3', 'R2.1'},
    'BASE': {'R2.2', 'R3.1', 'Q1.B'},
    'LED_A': {'R1.2', 'LED1.A'},
    'LED_K': {'LED1.K', 'Q1.C'},
}
PARTS = [
    # ref, device, library, value (None means fixed), schematic x/y/angle
    ('J1', 'PINHD-1X3', 'pinhead', '5V_GND_CTRL', 25.4, 76.2, 0),
    ('R1', 'R-US_0207/10', 'BeagleBone_Black_Wireless', '330', 76.2, 101.6, 0),
    ('LED1', 'TLLR4400', 'led2', None, 127.0, 101.6, 0),
    ('Q1', '2N3904-NPN-TO92-EBC', 'transistor-neu-to92', '2N3904', 127.0, 50.8, 0),
    ('R2', 'R-US_0207/10', 'BeagleBone_Black_Wireless', '2.2k', 76.2, 50.8, 0),
    ('R3', 'R-US_0207/10', 'BeagleBone_Black_Wireless', '100k', 101.6, 25.4, 90),
    ('C1', 'C-US025-024X044', 'BeagleBone_Black_Wireless', '100n', 25.4, 25.4, 0),
]
PLACEMENT = [
    {'ref': ref, 'x_mm': x, 'y_mm': y, 'angle': angle, 'bottom': False}
    for ref, x, y, angle in [('J1', 5, 15, 90), ('C1', 11, 11, 0),
                           ('R1', 23, 7, 0), ('LED1', 35, 8, 90),
                           ('Q1', 34, 19, 180), ('R2', 18, 21, 0), ('R3', 26, 26, 0)]
]


def preview_state(snap):
    board = ET.fromstring(snap.board_xml)
    rules = board.find('.//board/designrules')
    if rules is not None:
        # Fusion can recode translated rule prose after DRC; keep every actual rule/geometry field.
        for description in rules.findall('description'): rules.remove(description)
    return xml_hash(ET.tostring(board)), xml_hash(snap.sch_xml)


def check_connections(snap):
    sch, board = snap.schematic(), snap.board()
    refs = {'J1', 'R1', 'R2', 'R3', 'LED1', 'Q1', 'C1'}
    assert set(sch.parts) == set(board.element_names) == refs, 'Unexpected/missing components'
    actual = {n: {f'{p.part}.{p.pin}' for p in net.pins} for n, net in sch.nets.items()}
    assert actual == {n: set(pins) for n, pins in NETS.items()}, 'Schematic netlist mismatch'
    actual = {n: {f'{ref}.{pad}' for ref, pad in net.contacts} for n, net in board.signals.items()}
    assert actual == PADS, 'PCB pad netlist mismatch'
    values = {'R1': '330', 'R2': '2.2k', 'R3': '100k', 'C1': '100n', 'Q1': '2N3904'}
    board_values = {e.name: e.value for e in snap.fab().elements}
    for ref, value in values.items():
        assert sch.parts[ref].value == board_values[ref] == value, f'Wrong value: {ref}'


def check_positions(snap):
    elements = {e.name: e for e in snap.fab().elements}
    for move in PLACEMENT:
        el = elements[move['ref']]
        assert math.isclose(el.x, move['x_mm'], abs_tol=.001), move['ref']
        assert math.isclose(el.y, move['y_mm'], abs_tol=.001), move['ref']
        assert math.isclose(el.angle % 360, move['angle'], abs_tol=.05), move['ref']
        assert not el.mirror, move['ref']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--document-id', required=True, help='Exact active SCHEMATIC ID from get_context')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--stage', choices=['all', 'build', 'place', 'route', 'verify'], default='all')
    args = parser.parse_args()
    out = args.output_dir; out.mkdir(parents=True, exist_ok=True)
    report = {'stage': args.stage, 'results': {}}

    def record(name, result):
        report['results'][name] = result
        (out / f'{args.stage}-report.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
        print(name + ': ' + json.dumps(result, default=str)[:500], flush=True)
        return result

    def export(name):
        snap = S.session.snapshot()
        assert snap.sch_xml is not None and snap.board_xml is not None, 'Need a linked schematic and PCB'
        (out / f'{name}.sch').write_bytes(snap.sch_xml)
        (out / f'{name}.brd').write_bytes(snap.board_xml)
        return snap

    def guard(editor='schematic'):
        S.session.activate('schematic')
        doc = S.get_context()['active_document']
        assert doc['document_id'] == args.document_id, 'Wrong target schematic; no further writes'
        if editor != 'schematic': S.session.activate(editor)

    # Do not activate an arbitrary similarly named document at startup.
    ctx = S.get_context()
    assert ctx['active_editor'] == 'schematic' and ctx['active_document']['document_id'] == args.document_id
    try:
        initial = export(args.stage + '-before')
        if args.stage in ('all', 'build'):
            assert not initial.schematic().parts and not initial.board().element_names, 'Build needs an empty test family'
            for device, lib in sorted({(p[1], p[2]) for p in PARTS}):
                matches = S.search_fusion_libraries(device, library_name=lib, limit=100)['parts']
                matches = [p for p in matches if p['device'] == device and p['active'] and p['downloaded']]
                assert len(matches) == 1 and not matches[0]['ambiguous_library_name'], f'Ambiguous/missing {device}@{lib}'
            for ref, device, lib, value, x, y, angle in PARTS:
                guard()
                record('add_' + ref, S.add_part(device, lib, ref, x, y, angle))
                if value is not None: record('value_' + ref, S.set_part_value(ref, value))
            for net, pins in NETS.items():
                guard(); record('connect_' + net, S.connect_pins(net, pins))
            check_connections(export('connected'))
            erc = record('erc', S.run_erc())
            assert erc['count'] == 0, 'ERC findings need inspection'
        check_connections(S.session.snapshot())
        if args.stage in ('all', 'place'):
            guard(); record('outline', S.set_board_outline(45, 32, replace=True))
            before = export('preview-before')
            record('placement_preview', S.move_parts(PLACEMENT, dry_run=True))
            after = export('preview-after')
            assert preview_state(before) == preview_state(after), 'Preview changed CAD'
            guard('board'); record('placement', S.move_parts(PLACEMENT))
            check_positions(export('placed'))
            placement = record('placement_check', S.check_placement(derive_missing_courtyards=True))
            assert placement['courtyards']['overlap_count'] == 0, 'Components overlap'
            assert placement['pad_gaps']['count'] == 0, 'Pad clearance violations'
        if args.stage in ('all', 'route'):
            check_positions(S.session.snapshot())
            for net in ['LED_A', 'LED_K', 'BASE', 'CTRL', '+5V', 'GND']:
                guard()
                result = record('route_' + net, S.route_net(net, width_mm=.3, dry_run=False))
                assert result['net_done'], f'Routing incomplete: {net}'
        if args.stage in ('all', 'verify'):
            check_positions(S.session.snapshot())
            status = record('routing', S.routing_status())
            assert status['unrouted_connections'] == 0, 'Unrouted connections remain'
            drc = record('drc', S.run_drc())
            assert drc['count'] == 0, 'DRC findings need inspection'
            erc = record('final_erc', S.run_erc())
            assert erc['count'] == 0, 'ERC findings need inspection'
        check_connections(export(args.stage + '-after'))
        record('status', 'passed')
    except Exception as exc:
        record('failure', str(exc))
        raise


if __name__ == '__main__':
    main()
