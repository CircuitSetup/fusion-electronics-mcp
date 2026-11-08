"""One-shot reload supervisor. Fixed sibling target; no socket or arbitrary code input."""
import importlib.util
import contextlib
import os
from pathlib import Path
import threading

import adsk.core

OWNER = Path(__file__).resolve().parent.parent / 'FusionElectronicsMCP'
spec = importlib.util.spec_from_file_location('femcp_management', OWNER / 'management.py')
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
INFO = Path(os.environ.get('APPDATA', os.path.expanduser('~'))) / 'fusion-electronics-mcp' / 'bridge.json'
EVENT = 'fusion_electronics_mcp_reload'
_app = None
_event = None
_handler = None
_timer = None
_request = None
_status = None
_ticket = None


def schedule():
    global _timer
    _timer = threading.Timer(0.25, lambda: _app.fireCustomEvent(EVENT))
    _timer.daemon = True; _timer.start()


class ReloadHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        global _ticket
        try:
            if _ticket is None:
                _ticket = M.perform_reload(_app, OWNER, INFO, _request, adsk.core)
                schedule()
                return
            receipt = _ticket.get('bridge_session')
            if not receipt:
                raise ValueError('MCP startup did not acknowledge this reload; no session verified')
            _request.update(status='restarted', bridge_session=receipt)
        except Exception as ex:
            _request.update(status='failed', error=str(ex))
        try:
            M.U.atomic_json(_status, _request)
        finally:
            stop(None)
            adsk.terminate()


def run(context):
    global _app, _event, _handler, _timer, _request, _status, _ticket
    _ticket = None
    _app = adsk.core.Application.get()
    rid = M.request_id(M.U.read_json(INFO.parent / 'reload-request.json')['request_id'])
    _status = INFO.parent / 'reloads' / (rid + '.json')
    _request = M.U.read_json(_status)
    if _request['status'] != 'queued': raise ValueError('Reload request was already handled')
    _request['status'] = 'supervising'; M.U.atomic_json(_status, _request)
    try:
        _event = _app.registerCustomEvent(EVENT)
        _handler = ReloadHandler()
        if _event.add(_handler) is False: raise ValueError('Cannot attach reload event')
        adsk.autoTerminate(False)
        # Queue after the requesting bridge callback has returned, then run on Fusion's main thread.
        schedule()
    except Exception as ex:
        _request.update(status='failed', error=str(ex)); M.U.atomic_json(_status, _request)
        stop(None)
        raise


def stop(context):
    global _timer, _event, _handler
    if _timer: _timer.cancel()
    M.clear_reload(adsk.core, _ticket)
    if _event and _handler:
        with contextlib.suppress(Exception): _event.remove(_handler)
    if _event:
        with contextlib.suppress(Exception): _app.unregisterCustomEvent(EVENT)
    _timer = _event = _handler = None
