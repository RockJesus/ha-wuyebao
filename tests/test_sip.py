"""Unit tests for the minimal SIP client (Digest math and message building).

No network is used: only pure functions and message formatting are exercised.
"""

import os
import sys
import types
import unittest

_COMPONENTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "custom_components"
)
sys.path.insert(0, _COMPONENTS_DIR)

from wuyebao.sip import (  # noqa: E402
    SipClient,
    build_digest_response,
    build_proxy_auth,
    mask_secret,
    parse_challenge,
    parse_status,
)

# RFC 2617 sample values (section 3.5) - the RFC example uses qop; the
# no-qop MD5 response is validated against the RFC intermediate HA1 and the
# no-qop formula.
RFC_USERNAME = "Mufasa"
RFC_REALM = "testrealm@host.com"
RFC_PASSWORD = "Circle Of Life"
RFC_NONCE = "dcd98b7102dd2f0e8b11d0f600bfb0c093"
RFC_METHOD = "GET"
RFC_URI = "/dir/index.html"
RFC_HA1 = "939e7578ed9e3c518a452acee763bce9"


class TestSipDigest(unittest.TestCase):
    def test_digest_response_no_qop_formula(self):
        import hashlib

        def md5(s):
            return hashlib.md5(s.encode()).hexdigest()

        got = build_digest_response(
            RFC_USERNAME, RFC_REALM, RFC_NONCE, RFC_PASSWORD, RFC_METHOD, RFC_URI
        )
        # HA1 intermediate value must match the RFC document.
        self.assertEqual(md5(f"{RFC_USERNAME}:{RFC_REALM}:{RFC_PASSWORD}"), RFC_HA1)
        expected = md5(f"{RFC_HA1}:{RFC_NONCE}:{md5(f'{RFC_METHOD}:{RFC_URI}')}")
        self.assertEqual(got, expected)

    def test_proxy_auth_header_shape(self):
        header = build_proxy_auth(
            RFC_USERNAME, RFC_REALM, RFC_NONCE, RFC_PASSWORD, RFC_METHOD, RFC_URI
        )
        self.assertTrue(header.startswith("Proxy-Authorization: Digest "))
        self.assertIn(f'username="{RFC_USERNAME}"', header)
        self.assertIn("response=", header)
        self.assertIn("algorithm=MD5", header)

    def test_parse_status(self):
        self.assertEqual(parse_status("SIP/2.0 407 Proxy Authentication Required\r\n\r\n"), (407, "Proxy Authentication Required"))
        self.assertEqual(parse_status("SIP/2.0 200 OK\r\n"), (200, "OK"))
        self.assertEqual(parse_status("garbage"), (0, ""))

    def test_parse_challenge(self):
        resp = (
            'SIP/2.0 407 Proxy Authentication Required\r\n'
            'Proxy-Authenticate: Digest realm="new-sip.jhws.top", '
            'nonce="6aa8023500004d0f603616f7cbe6a9e5fdb09877401891ca"\r\n\r\n'
        )
        realm, nonce = parse_challenge(resp)
        self.assertEqual(realm, "new-sip.jhws.top")
        self.assertTrue(nonce.startswith("6aa8023"))

    def test_mask_secret(self):
        self.assertEqual(mask_secret("abcdefghijklmnopqrstuvwxyz"), "abcdefgh...wxyz")
        self.assertEqual(mask_secret("short"), "*****")
        self.assertEqual(mask_secret(None), "")

    def test_build_message_shape(self):
        client = SipClient(local_ip="10.0.0.9", transport="udp")
        msg = client._build_message("REGISTER", "sip:new-sip.jhws.top", "13000000000", 1)
        self.assertIn("REGISTER sip:new-sip.jhws.top SIP/2.0", msg)
        self.assertIn("Via: SIP/2.0/UDP 10.0.0.9:5060", msg)
        self.assertIn("CSeq: 1 REGISTER", msg)
        self.assertTrue(msg.endswith("\r\n\r\n"))

    def test_invite_uri_format(self):
        client = SipClient(local_ip="10.0.0.9")
        msg = client._build_message(
            "INVITE", "sip:abc-123@new-sip.jhws.top", "13000000000", 1
        )
        self.assertIn("INVITE sip:abc-123@new-sip.jhws.top SIP/2.0", msg)

    def test_tcp_transport_message_shape(self):
        client = SipClient(local_ip="10.0.0.9", transport="tcp")
        msg = client._build_message("REGISTER", "sip:new-sip.jhws.top", "13000000000", 1)
        self.assertIn("Via: SIP/2.0/TCP 10.0.0.9:5060", msg)
        self.assertIn("Contact: <sip:13000000000@10.0.0.9:5060;transport=tcp>", msg)
        self.assertTrue(msg.endswith("\r\n\r\n"))

    def test_udp_transport_message_shape(self):
        client = SipClient(local_ip="10.0.0.9", transport="udp")
        msg = client._build_message("REGISTER", "sip:new-sip.jhws.top", "13000000000", 1)
        self.assertIn("Via: SIP/2.0/UDP 10.0.0.9:5060", msg)
        self.assertIn("Contact: <sip:13000000000@10.0.0.9:5060>", msg)

    def test_register_with_challenge_sends_preauth(self):
        client = SipClient(local_ip="10.0.0.9", transport="tcp")
        captured = {}

        def fake_exchange(payload):
            captured["payload"] = payload
            return "SIP/2.0 403 Forbidden\r\n\r\n"

        client._exchange = fake_exchange
        res = client.register(
            "13000000000", "secret", challenge=("realm-x", "nonce-y")
        )
        self.assertEqual(res["status"], 403)
        self.assertTrue(res.get("preauth"))
        self.assertIn("Proxy-Authorization: Digest", captured["payload"])
        self.assertIn('username="13000000000"', captured["payload"])
        self.assertIn('nonce="nonce-y"', captured["payload"])
        self.assertTrue(captured["payload"].endswith("\r\n\r\n"))

    def test_invite_with_challenge_sends_preauth(self):
        client = SipClient(local_ip="10.0.0.9", transport="tcp")
        captured = {}

        def fake_exchange(payload):
            captured["payload"] = payload
            return "SIP/2.0 486 Busy Here\r\n\r\n"

        client._exchange = fake_exchange
        res = client.invite(
            "13000000000",
            "secret",
            "sip:abc-123@sip.jhws.top",
            challenge=("realm-x", "nonce-y"),
        )
        self.assertEqual(res["status"], 486)
        self.assertIn("Proxy-Authorization: Digest", captured["payload"])
        self.assertIn("INVITE sip:abc-123@sip.jhws.top SIP/2.0", captured["payload"])


if __name__ == "__main__":
    unittest.main()
