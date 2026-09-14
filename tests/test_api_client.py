"""End-to-end tests for the JHCloud API client (Home Assistant stubbed).

Verifies how requests are built (URL, headers, JSON body) and how JHCloud
responses are parsed, without needing a Home Assistant runtime or network.
"""

import asyncio
import os
import sys
import types
import unittest

# --- Stub aiohttp so api.py can be imported outside Home Assistant ---
_aiohttp = types.ModuleType("aiohttp")


class _ClientTimeout:
    def __init__(self, total=None):
        self.total = total


class _ClientError(Exception):
    pass


_aiohttp.ClientTimeout = _ClientTimeout
_aiohttp.ClientError = _ClientError
sys.modules["aiohttp"] = _aiohttp

# --- Stub homeassistant helper modules ---
_ha = types.ModuleType("homeassistant")
_ha_helpers = types.ModuleType("homeassistant.helpers")
_ahc = types.ModuleType("homeassistant.helpers.aiohttp_client")
sys.modules["homeassistant"] = _ha
sys.modules["homeassistant.helpers"] = _ha_helpers
sys.modules["homeassistant.helpers.aiohttp_client"] = _ahc

_COMPONENTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "custom_components"
)
sys.path.insert(0, _COMPONENTS_DIR)

_wuyebao_pkg = types.ModuleType("wuyebao")
_wuyebao_pkg.__path__ = [os.path.join(_COMPONENTS_DIR, "wuyebao")]
sys.modules["wuyebao"] = _wuyebao_pkg

FAKE_SESSION = None


def _get_session(hass):
    return FAKE_SESSION


_ahc.async_get_clientsession = _get_session

from wuyebao.api import (  # noqa: E402
    WuyeBaoAPI,
    WuyeBaoAuthError,
    WuyeBaoConnectionError,
)
from wuyebao.const import (  # noqa: E402
    API_GATES_PATH,
    API_LOGIN_PATH,
    API_REFRESH_PATH,
    DEFAULT_CLIENT_ID,
)


class FakeResponse:
    def __init__(self, status, text, json_data):
        self.status = status
        self._text = text
        self._json = json_data

    async def text(self):
        return self._text

    async def json(self):
        return self._json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, routes, strict=True):
        self.routes = routes
        self.calls = []
        self.strict = strict

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        key = f"{method} {url}"
        params = kwargs.get("params")
        if params:
            from urllib.parse import urlencode

            key = f"{method} {url}?{urlencode(params)}"
        if key in self.routes:
            return self.routes[key]
        if f"{method} {url}" in self.routes:
            return self.routes[f"{method} {url}"]
        if not self.strict:
            return FakeResponse(404, "Not Found", {})
        raise AssertionError(f"Unexpected request: {key}")


def _make_api(**kwargs):
    defaults = dict(
        hass=object(),
        phone="13800000000",
        password="secret",
        base_url="https://wuye.jhws.top/",
        client_id=DEFAULT_CLIENT_ID,
    )
    defaults.update(kwargs)
    return WuyeBaoAPI(**defaults)


class TestApiClient(unittest.TestCase):
    def setUp(self):
        global FAKE_SESSION
        self.base = "https://wuye.jhws.top"
        self.routes = {
            f"POST {self.base}/{API_LOGIN_PATH}": FakeResponse(
                200,
                '{"code":0,"data":{"accessToken":"tok123","refreshToken":"rt9"}}',
                {"code": 0, "data": {"accessToken": "tok123", "refreshToken": "rt9"}},
            ),
            f"GET {self.base}/{API_GATES_PATH}": FakeResponse(
                200,
                '{"code":0,"data":[{"gateId":"g1","gateName":"北门"},{"gateId":"g2","gateName":"单元门"}]}',
                {"code": 0, "data": [
                    {"gateId": "g1", "gateName": "北门"},
                    {"gateId": "g2", "gateName": "单元门"},
                ]},
            ),
        }
        FAKE_SESSION = FakeSession(self.routes)

    def _run(self, coro):
        return asyncio.run(coro)

    def test_login_returns_tokens_and_sends_client_id(self):
        api = _make_api()
        access, refresh = self._run(api.login())
        self.assertEqual(access, "tok123")
        self.assertEqual(refresh, "rt9")
        call = FAKE_SESSION.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{self.base}/{API_LOGIN_PATH}")
        self.assertEqual(call["json"], {"username": "13800000000", "password": "secret"})
        self.assertEqual(call["headers"]["client_id"], DEFAULT_CLIENT_ID)

    def test_login_bad_credentials_raises_auth_error(self):
        self.routes[f"POST {self.base}/{API_LOGIN_PATH}"] = FakeResponse(
            400,
            '{"code":1003,"data":"帐号或密码错误！"}',
            {"code": 1003, "data": "帐号或密码错误！"},
        )
        api = _make_api()
        with self.assertRaises(WuyeBaoAuthError):
            self._run(api.login())

    def test_login_missing_token_raises_auth_error(self):
        self.routes[f"POST {self.base}/{API_LOGIN_PATH}"] = FakeResponse(
            200, '{"code":0,"data":{"msg":"no token"}}', {"code": 0, "data": {"msg": "no token"}}
        )
        api = _make_api()
        with self.assertRaises(WuyeBaoAuthError):
            self._run(api.login())

    def test_refresh_uses_get_with_refresh_token_param(self):
        self.routes[f"GET {self.base}/{API_REFRESH_PATH}"] = FakeResponse(
            200,
            '{"code":0,"data":{"accessToken":"tok2","refreshToken":"rt2"}}',
            {"code": 0, "data": {"accessToken": "tok2", "refreshToken": "rt2"}},
        )
        api = _make_api()
        access, refresh = self._run(api.refresh("rt9"))
        self.assertEqual(access, "tok2")
        self.assertEqual(refresh, "rt2")
        call = FAKE_SESSION.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["params"], {"refreshToken": "rt9"})
        self.assertEqual(call["headers"]["client_id"], DEFAULT_CLIENT_ID)

    def test_get_gates_sends_bearer_and_normalizes(self):
        api = _make_api()
        gates = self._run(api.get_gates("tok123"))
        self.assertEqual(len(gates), 2)
        self.assertEqual(gates[0]["gate_id"], "g1")
        self.assertEqual(gates[0]["name"], "北门")
        call = FAKE_SESSION.calls[0]
        self.assertEqual(call["headers"]["Authorization"], "Bearer tok123")
        self.assertEqual(call["headers"]["client_id"], DEFAULT_CLIENT_ID)

    def test_get_gates_with_community_param(self):
        api = _make_api()
        gates = self._run(api.get_gates("tok123", community_id="c100"))
        self.assertEqual(len(gates), 2)
        call = FAKE_SESSION.calls[0]
        self.assertEqual(call["params"], {"communityId": "c100"})

    def test_get_gates_falls_back_to_community_code(self):
        self.routes[f"GET {self.base}/{API_GATES_PATH}?communityId=c100"] = FakeResponse(
            200,
            '{"code":-1,"data":"必须选择一个小区才能查询信息！"}',
            {"code": -1, "data": "必须选择一个小区才能查询信息！"},
        )
        self.routes[f"GET {self.base}/{API_GATES_PATH}?communityCode=c100"] = FakeResponse(
            200,
            '{"code":0,"data":[{"gateId":"g1","gateName":"北门"}]}',
            {"code": 0, "data": [{"gateId": "g1", "gateName": "北门"}]},
        )
        api = _make_api()
        gates = self._run(api.get_gates("tok123", community_id="c100"))
        self.assertEqual(gates[0]["gate_id"], "g1")
        calls = FAKE_SESSION.calls
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["params"], {"communityId": "c100"})
        self.assertEqual(calls[1]["params"], {"communityCode": "c100"})

    def test_get_gates_community_rejected_raises(self):
        self.routes[f"GET {self.base}/{API_GATES_PATH}?communityId=c100"] = FakeResponse(
            200,
            '{"code":-1,"data":"必须选择一个小区才能查询信息！"}',
            {"code": -1, "data": "必须选择一个小区才能查询信息！"},
        )
        self.routes[f"GET {self.base}/{API_GATES_PATH}?communityCode=c100"] = FakeResponse(
            200,
            '{"code":-1,"data":"必须选择一个小区才能查询信息！"}',
            {"code": -1, "data": "必须选择一个小区才能查询信息！"},
        )
        api = _make_api()
        with self.assertRaises(WuyeBaoConnectionError):
            self._run(api.get_gates("tok123", community_id="c100"))
        self.assertEqual(len(FAKE_SESSION.calls), 2)

    def test_open_gate_diag_reports_all_variants(self):
        FAKE_SESSION.strict = False
        api = _make_api()
        raw = {
            "id": "g1",
            "uid": "uid-abc",
            "password": "390844",
            "areaId": "a1",
            "communityId": "c100",
        }
        results = self._run(api.open_gate_diag("tok123", "g1", raw, community_id="c100"))
        self.assertTrue(len(results) >= 9, f"expected >=9 variants, got {len(results)}")
        for r in results:
            self.assertIn("method", r)
            self.assertIn("path", r)
            self.assertIn("ok", r)
        # Every variant must have hit the server (404 counts as a result).
        self.assertTrue(all("error" in r or r.get("ok") for r in results))

    def test_open_gate_diag_finds_working_variant(self):
        # Simulate: the /remoteOpen variant is the one the server accepts.
        hit_key = (
            f"POST {self.base}/api/device/grant/gates/g1/remoteOpen"
            "?communityId=c100&password=390844&uid=uid-abc"
        )
        self.routes[hit_key] = FakeResponse(
            200,
            '{"code":0,"data":"success"}',
            {"code": 0, "data": "success"},
        )
        FAKE_SESSION.strict = False
        api = _make_api()
        raw = {"id": "g1", "uid": "uid-abc", "password": "390844", "communityId": "c100"}
        results = self._run(api.open_gate_diag("tok123", "g1", raw, community_id="c100"))
        hits = [r for r in results if r.get("ok")]
        self.assertEqual(len(hits), 1)
        self.assertIn("remoteOpen", hits[0]["path"])

    def test_get_communities(self):
        self.routes[f"GET {self.base}/api/owner/anon/owners/community"] = FakeResponse(
            200,
            '{"code":0,"data":[{"communityId":"c1","communityName":"金花围小区","communityCode":835}]}',
            {"code": 0, "data": [
                {"communityId": "c1", "communityName": "金花围小区", "communityCode": 835}
            ]},
        )
        api = _make_api()
        communities = self._run(api.get_communities())
        self.assertEqual(communities[0]["communityName"], "金花围小区")
        call = FAKE_SESSION.calls[0]
        self.assertEqual(call["params"], {"phoneNumber": "13800000000"})

    def test_open_gate_post_with_id_in_path(self):
        self.routes[f"POST {self.base}/api/device/grant/gates/g1/unlock"] = FakeResponse(
            200, '{"code":0}', {"code": 0}
        )
        api = _make_api()
        self._run(api.open_gate("tok123", "g1"))
        call = FAKE_SESSION.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(
            call["url"], f"{self.base}/api/device/grant/gates/g1/unlock"
        )
        self.assertEqual(call["headers"]["Authorization"], "Bearer tok123")

    def test_open_gate_get_method(self):
        self.routes[f"GET {self.base}/api/device/grant/gates/g7/unlock"] = FakeResponse(
            200, '{"code":0}', {"code": 0}
        )
        api = _make_api(open_method="GET")
        self._run(api.open_gate("tok123", "g7"))
        call = FAKE_SESSION.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(
            call["url"], f"{self.base}/api/device/grant/gates/g7/unlock"
        )
        self.assertIsNone(call.get("json"))

    def test_token_invalid_raises_auth_error(self):
        self.routes[f"GET {self.base}/{API_GATES_PATH}"] = FakeResponse(
            401,
            '{"code":1001,"data":"无效Token！"}',
            {"code": 1001, "data": "无效Token！"},
        )
        api = _make_api()
        with self.assertRaises(WuyeBaoAuthError):
            self._run(api.get_gates("bad"))

    def test_http_500_raises_connection_error(self):
        self.routes[f"POST {self.base}/{API_LOGIN_PATH}"] = FakeResponse(
            500, "boom", None
        )
        api = _make_api()
        with self.assertRaises(WuyeBaoConnectionError):
            self._run(api.login())


if __name__ == "__main__":
    unittest.main(verbosity=2)
