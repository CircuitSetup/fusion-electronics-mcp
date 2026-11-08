import asyncio
import math
import sys
import types
import uuid
import unittest
from unittest.mock import MagicMock, patch


class Items:
    def __init__(self, *items): self.values = list(items)
    @property
    def count(self): return len(self.values)
    def item(self, i): return self.values[i]
    def itemById(self, identity): return next((v for v in self.values if v.id == identity), None)
    def add(self, item): self.values.append(item); return True


def point(x=0, y=0, z=0): return types.SimpleNamespace(x=x, y=y, z=z)
def body(name, solid=True):
    return types.SimpleNamespace(name=name, isSolid=solid, isValid=True, entityToken=uuid.uuid4().hex,
                                 objectType='adsk::fusion::BRepBody')


class InspectionTest(unittest.TestCase):
    def setUp(self):
        from fusion_mcp.addin.FusionElectronicsMCP import inspection as I
        self.I = I

    def test_selection_paging_units_and_unavailable_properties(self):
        entity = body('Box'); entity.entityToken = 'not-an-edit-handle'
        ui = types.SimpleNamespace(activeCommand='Select', activeSelections=Items(
            types.SimpleNamespace(entity=entity, point=point(1, 2, 3)),
            types.SimpleNamespace(entity=types.SimpleNamespace(isValid=False))))
        result = self.I.selection(ui, limit=1)
        self.assertEqual(result['selections'][0]['point_mm'], [10, 20, 30])
        self.assertEqual(result['next_offset'], 1)
        self.assertEqual(self.I.selection(ui, offset=1)['selections'][0]['valid'], False)
        self.assertNotIn('entityToken', str(result))
        with self.assertRaises(ValueError): self.I.selection(ui, limit=0)

    def test_commands_inspect_native_metadata_without_executing(self):
        command = types.SimpleNamespace(id='Move', name='Move object', tooltip='Move the object', isNative=True,
                                        controlDefinition=types.SimpleNamespace(isEnabled=False, isVisible=True),
                                        execute=MagicMock(side_effect=AssertionError('must not execute')))
        ui = types.SimpleNamespace(activeCommand='Move', commandDefinitions=Items(command))
        result = self.I.commands(ui, query='move')
        self.assertEqual(result['active_command']['id'], 'Move')
        self.assertFalse(result['commands'][0]['enabled'])
        self.assertEqual(self.I.commands(ui, command_id='missing')['commands'], [])
        self.assertEqual(self.I.commands(ui, query='absent')['total'], 0)
        command.execute.assert_not_called()

    def geometry(self, overlap=0, distance=0.03):
        a, b = body('A'), body('B')
        proxy = body('Nested instance')
        root = types.SimpleNamespace(bRepBodies=Items(a, b), meshBodies=Items(), allOccurrences=Items(
            types.SimpleNamespace(fullPathName='Parent:1+Part:2', entityToken='occurrence-token', bRepBodies=Items(proxy), meshBodies=[])))
        design = types.SimpleNamespace(rootComponent=root,
            createInterferenceInput=MagicMock(side_effect=lambda values: types.SimpleNamespace(entities=values)),
            analyzeInterference=MagicMock(return_value=Items(*([types.SimpleNamespace(interferenceBody=types.SimpleNamespace(volume=overlap))] if overlap else []))))
        app = types.SimpleNamespace(measureManager=types.SimpleNamespace(measureMinimumDistance=MagicMock(
            return_value=types.SimpleNamespace(value=distance, positionOne=point(), positionTwo=point(distance)))))
        core = types.SimpleNamespace(ObjectCollection=types.SimpleNamespace(create=Items))
        return app, design, core, proxy

    def test_geometry_uses_root_context_proxies_and_converts_distance(self):
        app, design, core, proxy = self.geometry()
        rows, native, unsupported = self.I.bodies(design)
        self.assertIs(native[rows[2]['body_id']], proxy)
        result = self.I.clearance(app, design, core, 0.4, [rows[0]['body_id'], rows[2]['body_id']])
        self.assertTrue(result['complete']); self.assertFalse(result['meets_requested_clearance'])
        pair = result['pairs'][0]
        self.assertAlmostEqual(pair['minimum_distance_mm'], 0.3)
        self.assertEqual(pair['closest_points_mm'], [[0, 0, 0], [0.3, 0, 0]])
        self.assertEqual(pair['status'], 'below_clearance')
        self.assertIs(design.createInterferenceInput.call_args.args[0].item(1), proxy)

    def test_overlap_is_actual_volume_not_surface_gap_or_bounding_box(self):
        app, design, core, _ = self.geometry(overlap=0.002, distance=1)
        result = self.I.clearance(app, design, core, 0, None)
        self.assertTrue(result['complete']); self.assertFalse(result['meets_requested_clearance'])
        self.assertEqual(result['pairs'][0]['overlap_volume_mm3'], 2)
        self.assertEqual(result['pairs'][0]['status'], 'interference')
        app.measureManager.measureMinimumDistance.assert_not_called()

    def test_selected_ids_cannot_alias_after_delete_reorder_or_replacement(self):
        for edit in ('delete', 'reorder', 'replace', 'occurrence_replace'):
            app, design, core, _ = self.geometry()
            rows, _, _ = self.I.bodies(design)
            chosen = [rows[0]['body_id'], rows[2]['body_id']]
            if edit == 'delete': design.rootComponent.bRepBodies.values.pop(0)
            elif edit == 'reorder': design.rootComponent.bRepBodies.values.reverse()
            elif edit == 'replace': design.rootComponent.bRepBodies.values[0] = body('A')
            else: design.rootComponent.allOccurrences.item(0).entityToken = 'replacement-instance'
            with self.subTest(edit=edit), self.assertRaisesRegex(ValueError, 'refresh|get_3d_bodies'):
                self.I.clearance(app, design, core, 0.2, chosen)
            design.analyzeInterference.assert_not_called()
        fresh = self.I.bodies(design)[0]
        self.assertTrue(self.I.clearance(app, design, core, 0.2, [fresh[0]['body_id'], fresh[2]['body_id']])['complete'])

    def test_missing_identity_and_token_rotation_fail_closed(self):
        app, design, core, _ = self.geometry()
        ids = [row['body_id'] for row in self.I.bodies(design)[0]]
        design.rootComponent.bRepBodies.item(0).entityToken = 'rotated-token'
        with self.assertRaises(ValueError): self.I.clearance(app, design, core, 0.2, ids)
        design.rootComponent.bRepBodies.item(0).entityToken = ''
        with self.assertRaises(ValueError): self.I.bodies(design)
        design.analyzeInterference.assert_not_called()

    def test_unsupported_body_uses_the_inventory_bound_id(self):
        _, design, _, _ = self.geometry()
        design.rootComponent.bRepBodies.item(0).isSolid = False
        rows, _, unsupported = self.I.bodies(design)
        self.assertEqual(unsupported[0]['body_id'], rows[0]['body_id'])

    def test_failures_unsupported_and_invalid_inputs_never_pass(self):
        app, design, core, _ = self.geometry()
        design.analyzeInterference.return_value = None
        result = self.I.clearance(app, design, core, 0.1, None)
        self.assertFalse(result['complete']); self.assertIsNone(result['meets_requested_clearance'])
        self.assertEqual(result['pairs'][0]['status'], 'error')
        design.analyzeInterference.return_value = Items()
        design.rootComponent.meshBodies = Items(object())
        self.assertFalse(self.I.clearance(app, design, core, 0.1, None)['complete'])
        for threshold in (-1, math.inf, math.nan, True):
            with self.assertRaises(ValueError): self.I.clearance(app, design, core, threshold, None)
        with self.assertRaises(ValueError): self.I.clearance(app, design, core, 0.1, ['missing', '<root>/body[0]'])
        design.rootComponent.bRepBodies = Items(*(body(str(i)) for i in range(33)))
        with self.assertRaises(ValueError): self.I.clearance(app, design, core, 0.1, None)

    def test_touching_equal_threshold_and_native_measure_failure(self):
        app, design, core, _ = self.geometry(distance=0)
        result = self.I.clearance(app, design, core, 0, None)
        self.assertTrue(result['meets_requested_clearance']); self.assertEqual(result['pairs'][0]['status'], 'contact')
        app.measureManager.measureMinimumDistance.return_value.value = 0.1
        self.assertTrue(self.I.clearance(app, design, core, 1, None)['meets_requested_clearance'])
        app.measureManager.measureMinimumDistance.return_value.value = math.nan
        self.assertFalse(self.I.clearance(app, design, core, 1, None)['complete'])

    def test_partial_time_budget_is_not_a_pass(self):
        app, design, core, _ = self.geometry()
        with patch.object(self.I.time, 'monotonic', side_effect=[0, 61]):
            result = self.I.clearance(app, design, core, 0.1, None)
        self.assertFalse(result['complete']); self.assertEqual(result['checked_pairs'], 0)
        self.assertEqual(result['total_pairs'], 3); self.assertIsNone(result['meets_requested_clearance'])

    def test_occurrence_mesh_vector_counts_unsupported_geometry(self):
        _, design, _, _ = self.geometry()
        design.rootComponent.allOccurrences.item(0).meshBodies = [object(), object()]
        _, _, unsupported = self.I.bodies(design)
        self.assertEqual(unsupported, [{'occurrence_path':'Parent:1+Part:2', 'kind':'mesh', 'count':2}])

    def test_bridge_document_guard_and_active_root_only_design(self):
        from test_server import load_addin
        addin = load_addin()
        app, design, core, _ = self.geometry()
        doc = types.SimpleNamespace(name='Mechanical', isModified=False)
        app.activeProduct = design; app.activeDocument = doc
        addin._app = app; addin._inspection_module = self.I
        fusion = types.ModuleType('adsk.fusion'); fusion.Design = types.SimpleNamespace(cast=lambda value: value)
        with patch.dict(sys.modules, {'adsk.fusion': fusion}), patch.object(addin.adsk, 'fusion', fusion, create=True):
            self.assertEqual(addin._inspection_design(), (design, doc))
        with patch.object(addin, '_inspection_design', return_value=(design, doc)), \
             patch.object(addin, '_document_id', return_value='current'), \
             patch.object(addin, '_doc_info', return_value={'document_id':'current'}), \
             patch.object(addin.adsk, 'core', core):
            with self.assertRaises(addin.BridgeError): addin.op_geometry_clearance({'expected_document_id': 'other'})
            design.analyzeInterference.assert_not_called()
            self.assertEqual(addin.op_geometry_bodies({})['total'], 3)
            self.assertTrue(addin.op_geometry_clearance({'expected_document_id':'current'})['complete'])

    def test_read_only_tools_and_no_dialog_watchdog_or_builtin_fallback(self):
        from fusion_mcp import bridge as B
        from fusion_mcp.server import mcp
        toolset = {t.name:t for t in asyncio.run(mcp.list_tools())}
        for name in ('get_fusion_selection', 'inspect_fusion_commands', 'get_3d_bodies', 'check_3d_clearance'):
            self.assertTrue(toolset[name].annotations.read_only_hint)
            self.assertFalse(toolset[name].annotations.destructive_hint)
        bridge = B.Bridge(); connection = MagicMock(); connection.__enter__.return_value = connection
        connection.recv.return_value = b'{"ok":true,"result":{}}\n'
        for op in B.INSPECTION_OPS:
            with patch.object(bridge, 'transport', return_value='builtin'), self.assertRaises(B.BridgeOpError): bridge._call(op)
            with patch.object(bridge, 'transport', return_value='addin'), \
                 patch.object(bridge, '_info', return_value={'protocol':1,'pid':123,'port':1,'token':'test','addin_version':'0.14.16'}), \
                 patch.object(B.socket, 'create_connection', return_value=connection), \
                 patch.object(B.dialogs, 'supported', side_effect=AssertionError('must not dismiss a dialog')), \
                 patch.object(B.dialogs, 'foreground', side_effect=AssertionError('must not change focus')):
                self.assertEqual(bridge._call(op), {})

    def test_geometry_requires_addin_with_inventory_bound_ids(self):
        from fusion_mcp import bridge as B
        bridge = B.Bridge()
        for version in ('0.14.15', '', 'unknown'):
            with patch.object(bridge, 'transport', return_value='addin'), \
                 patch.object(bridge, '_info', return_value={'addin_version':version}), \
                 patch.object(B.socket, 'create_connection') as connect:
                with self.assertRaises(B.BridgeOpError): bridge._call('geometry_clearance', {})
                connect.assert_not_called()


if __name__ == '__main__': unittest.main()
