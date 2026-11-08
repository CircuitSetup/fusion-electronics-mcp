import contextlib
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from fusion_mcp import server as S
from fusion_mcp import autoroute as AR
from fusion_mcp.session import WriteFailed
from test_library_variants import plug_part, LIB_XML
from fusion_mcp.library import deviceset_info
import xml.etree.ElementTree as ET


class ReviewRegressions(unittest.TestCase):
    def test_exact_numeric_pin_precedes_another_pins_pad_alias(self):
        named = NS(part='U1', gate='G$1', pin='1', pad='2')
        alias = NS(part='U1', gate='G$1', pin='2', pad='1')
        sch = NS(pins=[named, alias])
        self.assertIs(S._resolve_pin(sch, 'U1.1'), named)
        self.assertIs(S._resolve_pin(sch, 'U1.G$1.1'), named)
        alias.pad = '3 4'
        self.assertIs(S._resolve_pin(sch, 'U1.4'), alias)

    def test_airwire_only_signal_is_not_a_routed_net(self):
        b = NS(copper_layers=[1, 16], layers={}, rules={}, signals={
            'AIR': NS(wires=[NS(layer=19)], vias=[]),
            'CU': NS(wires=[NS(layer=1), NS(layer=19)], vias=[])})
        snap = NS(board=lambda: b, board_xml='unused', fab=lambda: NS(size_mm=(1, 1), outline=[], elements=[]))
        with mock.patch.object(S, '_snap', return_value=snap), mock.patch.object(S, '_v2_rules', return_value=(None, None)), \
             mock.patch.object(S.D, 'read_xml'), mock.patch('fusion_offline.net_classes.effective', return_value={}):
            self.assertEqual(S.get_board_summary.__wrapped__()['routed_nets'], 1)

    def variant_result(self, extra=None, changed=False):
        part = plug_part()
        info = deviceset_info(ET.fromstring(LIB_XML), 'CONN_USBC')
        connects, techs = S.plan_variant(part, info, '_NEW', None, None, False)
        got = {t: dict(a, **(extra or {})) for t, a in techs.items()}
        if changed:
            got['']['VALUE'] = 'WRONG'
        variant = {'variant': '_NEW', 'package': part.data['package']['name'], 'connects': connects, 'technologies': got}
        with mock.patch.object(S.library, 'get', return_value=part), mock.patch.object(S, 'session'), \
             mock.patch.object(S.D, 'read_xml', return_value=ET.fromstring(LIB_XML)), \
             mock.patch('fusion_mcp.library.deviceset_info', side_effect=[info, {'variants': [variant]}]):
            return S.add_library_variant.__wrapped__('plug-test', 'CONN_USBC', '_NEW')

    def test_variant_readback_accepts_only_blank_fusion_defaults(self):
        self.assertTrue(self.variant_result({'CATEGORY_L1': '', 'CATEGORY_L2': '', 'DATASHEET': '',
            'MANUFACTURER': '', 'MPN': '', 'VENDOR': '', 'VPN': ''})['ok'])
        for extras, changed in [({'MPN': 'unexpected'}, False), ({'UNKNOWN': ''}, False), ({}, True)]:
            with self.subTest(extras=extras, changed=changed), self.assertRaises(WriteFailed):
                self.variant_result(extras, changed)

    def test_autoroute_unknown_does_not_read_or_restore_while_job_may_run(self):
        bridge = mock.Mock(lock=None)
        sess = mock.Mock(bridge=bridge)
        sess.context.return_value = {'active_document': {'kind': 'board', 'name': 'fixture'}}
        with mock.patch.object(S, 'session', sess), \
             mock.patch.object(AR, 'run', side_effect=AR.AutorouteStateUnknown('still running')):
            result = S.autoroute.__wrapped__(nets=['TEST'], top_router=True)
        self.assertEqual(result['status'], 'state_unknown')
        self.assertFalse(result['ok'])
        self.assertEqual(sess.snapshot.call_count, 1)
        bridge.call.assert_not_called()
