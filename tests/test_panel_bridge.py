"""Verify web commands retain the existing lifecycle and reject stale/cross-site input."""
import json
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from unittest.mock import Mock

import pytest
from client.panel_bridge import PanelBridge


@pytest.fixture
def bridge():
    value = PanelBridge(port=0)
    value.snapshot = dict(online=True, updated=time.time(), generation=4, buttons={'暂停':True})
    yield value
    value.close()


def request(bridge, path, headers=None, method='POST'):
    req = Request(f'http://127.0.0.1:{bridge.port}'+path, method=method,
                  headers=headers or {'X-PvZ-Panel':'local'})
    return build_opener(ProxyHandler({})).open(req, timeout=2)


def test_actions_are_queued_then_run_on_panel_thread(bridge):
    panel = Mock(generation=4)
    with request(bridge, '/action/pause') as response:
        assert response.status == 202
    panel._on_pause.assert_not_called()
    bridge.drain(panel)
    panel._on_pause.assert_called_once_with()
    panel._on_start.assert_not_called()


def test_generation_change_discards_stale_actions(bridge):
    with request(bridge, '/action/pause'): pass
    panel = Mock(generation=5)
    bridge.drain(panel)
    panel._on_pause.assert_not_called()


@pytest.mark.parametrize('headers', [{}, {'X-PvZ-Panel':'local','Origin':'https://untrusted.example'},
                                      {'X-PvZ-Panel':'local','Host':'untrusted.example'}])
def test_cross_site_requests_rejected(bridge, headers):
    with pytest.raises(HTTPError) as error:
        request(bridge, '/action/pause', headers={'Other':'1', **headers})
    assert error.value.code == 403
    assert bridge.commands.empty()


def test_disabled_or_stale_client_cannot_receive_commands(bridge):
    with pytest.raises(HTTPError) as error:
        request(bridge, '/action/start')
    assert error.value.code == 409
    bridge.snapshot['updated'] = time.time()-5
    with pytest.raises(HTTPError) as error:
        request(bridge, '/action/pause')
    assert error.value.code == 409
    assert bridge.commands.empty()


def test_snapshot_transport(bridge):
    with request(bridge, '/status', method='GET') as response:
        assert json.load(response) == bridge.snapshot
