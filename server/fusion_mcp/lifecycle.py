"""Reload once, then observe the independent Fusion supervisor and new bridge."""
import json
from pathlib import Path
import time
import uuid

from .addin.FusionElectronicsMCP.management import request_id, U
from .bridge import BridgeOpError, BridgeUnavailable


def read_json(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


def reload_status(bridge, rid):
    path = Path(bridge.info_path).parent / 'reloads' / (request_id(rid) + '.json')
    try:
        record = read_json(path)
    except FileNotFoundError:
        return {'request_id': rid, 'status': 'not_found'}
    # Never send the bridge authentication token back in MCP results.
    result = {key: record[key] for key in ('request_id', 'status', 'error', 'pid', 'target_folder') if key in record}
    receipt = record.get('bridge_session') or {}
    bound = (receipt.get('reload_request_id') == rid and receipt.get('pid') == record.get('pid')
             and bool(receipt.get('token') and receipt.get('event_session_id'))
             and receipt['token'] != record.get('old_token'))
    result.update(current_session=False, reachable=None, verification_available=bound)
    if bound:
        result.update(addin_version=receipt['addin_version'], event_session_id=receipt['event_session_id'])
    if bound and record['status'] in ('restarted', 'verified'):
        try:
            info = read_json(bridge.info_path)
            keys = ('pid', 'token', 'event_session_id', 'reload_request_id', 'addin_version')
            result['current_session'] = all(info.get(key) == receipt.get(key) for key in keys)
            if result['current_session'] and bridge.transport() == 'addin':
                result['reachable'] = False
                ping = bridge.call('ping', timeout=2)
                current = read_json(bridge.info_path)
                result['current_session'] = all(current.get(key) == receipt.get(key) for key in keys)
                if (result['current_session'] and ping.get('pid') == receipt['pid']
                        and ping.get('addin_version') == receipt['addin_version']):
                    if record['status'] != 'verified':
                        record.update(status='verified', verified_at=time.time())
                        U.atomic_json(path, record)
                    result.update(status='verified', reachable=True)
        except (OSError, ValueError, BridgeUnavailable, BridgeOpError):
            pass
    result['verified_at'] = record.get('verified_at')
    return result


def reload_addin(bridge, timeout=40):
    with bridge.lock.hold('reload_mcp_addin'):
        if bridge.transport() != 'addin':
            raise ValueError('Reload requires the running MCP add-in 0.14.14+ and its installed helper')
        rid = uuid.uuid4().hex
        deadline = time.monotonic() + timeout
        try:
            bridge.call('reload_addin', {'request_id': rid}, timeout=10)
        except BridgeOpError as ex:
            if ex.code in ('unknown_op', 'needs_addin', 'unauthorized', 'bad_args'):
                raise
            # An exception may follow a successful submission. Observe; never resubmit.
        except (BridgeUnavailable, OSError):
            pass
        while True:
            result = reload_status(bridge, rid)
            if result['status'] in ('verified', 'failed'):
                return result
            if time.monotonic() >= deadline:
                return dict(result, status='unknown', supervisor_status=result['status'],
                            warning='Reload outcome is uncertain. Inspect get_mcp_reload_status with this request_id; do not automatically retry.')
            time.sleep(0.2)
