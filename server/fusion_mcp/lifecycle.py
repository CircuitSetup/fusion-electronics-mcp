"""Reload once, then observe the independent Fusion supervisor and new bridge."""
import json
from pathlib import Path
import time
import uuid

from .addin.FusionElectronicsMCP.management import request_id
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
    if record['status'] == 'restarted':
        try:
            info = read_json(bridge.info_path)
            if (info.get('token') and info['token'] != record['old_token']
                    and info.get('pid') == record['pid'] and info.get('event_session_id')
                    and bridge.transport() == 'addin'):
                ping = bridge.call('ping', timeout=2)
                current = read_json(bridge.info_path)
                if (current.get('token') == info['token'] and ping.get('pid') == info['pid']
                        and ping.get('addin_version') == info.get('addin_version')):
                    result.update(status='verified', addin_version=ping['addin_version'],
                                  event_session_id=info['event_session_id'])
        except (OSError, ValueError, BridgeUnavailable, BridgeOpError):
            pass
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
