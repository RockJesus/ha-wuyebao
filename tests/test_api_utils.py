"""Unit tests for the pure API parsing helpers (run outside Home Assistant)."""

import os
import sys
import unittest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "custom_components",
        "wuyebao",
    ),
)

from api_utils import (  # noqa: E402
    build_auth_headers,
    build_login_payload,
    build_open_path,
    find_gates,
    find_refresh_token,
    find_token,
    get_data,
    is_success,
    normalize_gates,
)


class TestEnvelope(unittest.TestCase):
    def test_success_code_zero(self):
        self.assertTrue(is_success({"code": 0, "data": []}))

    def test_success_string_code(self):
        self.assertTrue(is_success({"code": "0"}))

    def test_success_no_code(self):
        self.assertTrue(is_success({"data": [{"id": 1}]}))
        self.assertTrue(is_success([1, 2]))

    def test_failure_code(self):
        self.assertFalse(is_success({"code": 1003, "data": "帐号或密码错误！"}))
        self.assertFalse(is_success({"code": -1, "data": "x"}))

    def test_get_data(self):
        self.assertEqual(get_data({"code": 0, "data": [1]}), [1])
        self.assertEqual(get_data({"code": 0}), {"code": 0})
        self.assertEqual(get_data([1, 2]), [1, 2])


class TestFindToken(unittest.TestCase):
    def test_access_token_nested(self):
        payload = {"code": 0, "data": {"accessToken": "abc"}}
        self.assertEqual(find_token(payload), "abc")

    def test_refresh_token(self):
        payload = {"code": 0, "data": {"refreshToken": "rt1"}}
        self.assertEqual(find_refresh_token(payload), "rt1")

    def test_token_in_list(self):
        payload = {"code": 0, "list": [{"token": "tok-1"}]}
        self.assertEqual(find_token(payload), "tok-1")

    def test_empty_token_ignored(self):
        self.assertIsNone(find_token({"data": {"accessToken": ""}}))

    def test_no_token(self):
        self.assertIsNone(find_token({"data": {"code": 1, "msg": "ok"}}))


class TestFindGates(unittest.TestCase):
    def test_top_level_list(self):
        payload = {"code": 0, "data": [{"id": 1, "name": "北门"}]}
        self.assertEqual(find_gates(payload), [{"id": 1, "name": "北门"}])

    def test_gates_key(self):
        payload = {"code": 0, "data": {"gates": [{"gateId": "g1"}]}}
        self.assertEqual(find_gates(payload), [{"gateId": "g1"}])

    def test_no_gates(self):
        self.assertIsNone(find_gates({"data": {"msg": "ok"}}))


class TestNormalizeGates(unittest.TestCase):
    def test_auto_detect_id_and_name(self):
        raw = [{"gateId": "g1", "gateName": "小区北门"}]
        out = normalize_gates(raw)
        self.assertEqual(out[0]["gate_id"], "g1")
        self.assertEqual(out[0]["name"], "小区北门")

    def test_fallback_id_candidates(self):
        raw = [{"deviceNumber": "3001", "name": "单元门"}]
        out = normalize_gates(raw)
        self.assertEqual(out[0]["gate_id"], "3001")
        self.assertEqual(out[0]["name"], "单元门")

    def test_display_name_from_address(self):
        raw = [{"id": "9", "communityName": "金花围小区", "buildingName": "1栋", "roomName": "302"}]
        out = normalize_gates(raw)
        self.assertEqual(out[0]["name"], "金花围小区 1栋 302")

    def test_fallback_name(self):
        raw = [{"id": "d9"}]
        self.assertEqual(normalize_gates(raw)[0]["name"], "门禁 d9")

    def test_skips_items_without_id(self):
        raw = [{"name": "无名"}]
        self.assertEqual(normalize_gates(raw), [])


class TestBuilders(unittest.TestCase):
    def test_login_payload(self):
        self.assertEqual(
            build_login_payload("13800000000", "secret"),
            {"username": "13800000000", "password": "secret"},
        )

    def test_auth_headers(self):
        self.assertEqual(
            build_auth_headers("tok"),
            {"Authorization": "Bearer tok"},
        )

    def test_open_path_substitution(self):
        self.assertEqual(
            build_open_path("api/device/grant/gates/{gateId}/unlock", "g1"),
            "api/device/grant/gates/g1/unlock",
        )
        self.assertEqual(build_open_path("{gate_id}/open", "g2"), "g2/open")
        self.assertEqual(build_open_path("{id}", "g3"), "g3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
