"""Read-only UI and BRep inspection; called on Fusion's main thread."""
import itertools
import math
import time


def page(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError('limit must be 1..100 and offset nonnegative')


def point_mm(point):
    values = [float(point.x) * 10, float(point.y) * 10, float(point.z) * 10]
    if not all(math.isfinite(v) for v in values): raise ValueError('Non-finite geometry coordinate')
    return values


def selection(ui, limit=25, offset=0):
    page(limit, offset)
    result = {'active_command_id': ui.activeCommand, 'selections': [],
              'coverage': 'Native activeSelections only. Electronics group membership is separate: use run_bundled_ulp(probe). Command-input selections may not be exposed here.'}
    try:
        selections = ui.activeSelections
        count = selections.count
    except Exception as ex:
        return dict(result, available=False, error=str(ex)[:500], total=None, next_offset=None)
    for i in range(offset, min(count, offset + limit)):
        row = {'index': i}
        try:
            selected = selections.item(i); entity = selected.entity
            row['valid'] = bool(entity and entity.isValid)
            if row['valid']:
                row['object_type'] = entity.objectType
                for key in ('name',):
                    try: row[key] = str(getattr(entity, key))[:512]
                    except Exception: pass
                try:
                    context = entity.assemblyContext
                    if context: row['occurrence_path'] = context.fullPathName
                except Exception: pass
                try: row['point_mm'] = point_mm(selected.point)
                except Exception: row['point_mm'] = None
        except Exception as ex:
            row['error'] = str(ex)[:500]
        result['selections'].append(row)
    return dict(result, available=True, total=count, next_offset=offset+limit if offset+limit < count else None)


def command_info(command):
    result = {'id': command.id, 'name': command.name, 'native': command.isNative,
              'tooltip': command.tooltip[:2000]}
    try:
        control = command.controlDefinition
        result.update(control_type=control.objectType, enabled=control.isEnabled, visible=control.isVisible)
    except Exception as ex:
        # Some native definitions have no public control; retain independently available flags.
        for attr, key in (('isEnabled', 'enabled'), ('isVisible', 'visible')):
            try: result[key] = getattr(command.controlDefinition, attr)
            except Exception: result[key] = None
        result['control_error'] = str(ex)[:500]
    return result


def commands(ui, query='', command_id=None, limit=25, offset=0):
    page(limit, offset)
    if not isinstance(query, str) or (command_id is not None and not isinstance(command_id, str)):
        raise ValueError('query and command_id must be strings')
    definitions = ui.commandDefinitions; active_id = ui.activeCommand
    active = definitions.itemById(active_id) if active_id else None
    candidates = ([definitions.itemById(command_id)] if command_id is not None else
                  [definitions.item(i) for i in range(definitions.count)])
    matches = [d for d in candidates if d and query.casefold() in (d.id + ' ' + d.name).casefold()]
    matches.sort(key=lambda d: d.id)
    rows = []
    for definition in matches[offset:offset+limit]:
        try: rows.append(command_info(definition))
        except Exception as ex: rows.append({'id': definition.id, 'error': str(ex)[:500]})
    try: active_info = command_info(active) if active else None
    except Exception as ex: active_info = {'id': active_id, 'error': str(ex)[:500]}
    return {'active_command_id': active_id, 'active_command': active_info, 'commands': rows,
            'total': len(matches), 'next_offset': offset+limit if offset+limit < len(matches) else None,
            'coverage': 'Public command definitions and control flags; not proof a command can execute in this context. Active native dialog inputs are not exposed. No commands are executed.'}


def bodies(design):
    root = design.rootComponent
    occurrences = root.allOccurrences
    if occurrences.count > 5000: raise ValueError('Design exceeds the 5000-occurrence inspection limit')
    sources = [('<root>', root)] + [(o.fullPathName, o) for i in range(occurrences.count) if (o := occurrences.item(i))]
    rows, native, unsupported = [], {}, []
    for path, source in sources:
        meshes = source.meshBodies.count if path == '<root>' else len(source.meshBodies)
        if meshes: unsupported.append({'occurrence_path': path, 'kind': 'mesh', 'count': meshes})
        for i in range(source.bRepBodies.count):
            if len(rows) >= 10000: raise ValueError('Design exceeds the 10000-body inspection limit')
            body = source.bRepBodies.item(i)
            identity = f'{path}/body[{i}]'
            row = {'body_id': identity, 'name': body.name, 'occurrence_path': path,
                   'solid': body.isSolid, 'valid': body.isValid}
            rows.append(row)
            if row['solid'] and row['valid']: native[identity] = body
            else: unsupported.append({'body_id': identity, 'kind': 'non_solid_or_invalid'})
    return rows, native, unsupported


def clearance(app, design, core, minimum_clearance_mm, body_ids=None):
    if (isinstance(minimum_clearance_mm, bool) or not isinstance(minimum_clearance_mm, (int, float))
            or not math.isfinite(minimum_clearance_mm) or minimum_clearance_mm < 0):
        raise ValueError('minimum_clearance_mm must be finite and nonnegative')
    rows, native, unsupported = bodies(design)
    if body_ids is not None:
        if not isinstance(body_ids, list) or not all(isinstance(v, str) for v in body_ids):
            raise ValueError('body_ids must be a list of exact IDs from get_3d_bodies')
        if len(set(body_ids)) != len(body_ids): raise ValueError('Duplicate body IDs')
        if any(v not in native for v in body_ids): raise ValueError('Body ID missing, invalid or not a solid; refresh get_3d_bodies')
        native = {v: native[v] for v in body_ids}
    if not 2 <= len(native) <= 32:
        raise ValueError('Select 2..32 solid bodies using body_ids from get_3d_bodies; larger checks must be split explicitly')
    pairs = []; complete = not unsupported or body_ids is not None
    deadline = time.monotonic() + 60
    for (a_id, a), (b_id, b) in itertools.combinations(native.items(), 2):
        if time.monotonic() >= deadline:
            complete = False
            break
        pair = {'body_one': a_id, 'body_two': b_id}
        try:
            entities = core.ObjectCollection.create(); entities.add(a); entities.add(b)
            inputs = design.createInterferenceInput(entities)
            if inputs is None: raise ValueError('Fusion could not create interference input')
            inputs.areCoincidentFacesIncluded = False
            overlaps = design.analyzeInterference(inputs)
            if overlaps is None: raise ValueError('Fusion returned no interference analysis result')
            volume = sum(overlaps.item(i).interferenceBody.volume for i in range(overlaps.count)) * 1000
            if not math.isfinite(volume) or volume < 0 or (overlaps.count and volume == 0):
                raise ValueError('Fusion returned an invalid interference volume')
            if volume > 0:
                pair.update(status='interference', overlap_volume_mm3=volume, minimum_distance_mm=0.0,
                            closest_points_mm=None)
            else:
                measured = app.measureManager.measureMinimumDistance(a, b)
                if measured is None: raise ValueError('Fusion returned no minimum-distance result')
                distance = measured.value * 10
                if not math.isfinite(distance) or distance < 0: raise ValueError('Invalid minimum distance')
                pair.update(status='contact' if distance == 0 else 'below_clearance' if distance < minimum_clearance_mm else 'clear',
                            overlap_volume_mm3=0.0, minimum_distance_mm=distance,
                            closest_points_mm=[point_mm(measured.positionOne), point_mm(measured.positionTwo)])
            pair['meets_requested_clearance'] = volume == 0 and pair['minimum_distance_mm'] >= minimum_clearance_mm
        except Exception as ex:
            complete = False
            pair.update(status='error', error=str(ex)[:500], meets_requested_clearance=None)
        pairs.append(pair)
    expected = len(native) * (len(native)-1) // 2
    return {'method': 'Fusion BRep interference and minimum-distance APIs', 'minimum_clearance_mm': minimum_clearance_mm,
            'scope': 'explicit_bodies' if body_ids is not None else 'all_loaded_solid_bodies',
            'bodies': [row for row in rows if row['body_id'] in native], 'unsupported_geometry': unsupported,
            'pairs': pairs, 'total_pairs': expected, 'checked_pairs': len(pairs), 'complete': complete,
            'meets_requested_clearance': all(p['meets_requested_clearance'] for p in pairs) if complete else None,
            'coverage': 'Loaded solid BRep geometry, including hidden bodies and intra-component pairs. Missing models are not detected. No geometry is created, synchronized, recomputed, saved or repaired. Interference has zero clearance; closest points apply only to non-overlapping bodies.'}
