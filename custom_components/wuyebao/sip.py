"""Minimal UDP SIP client for the JHCloud cloud-intercom open-door path.

The official 物业宝 app opens doors over SIP (OpenSIPS proxy at
new-sip.jhws.top:58583, realm "new-sip.jhws.top", Digest/Proxy auth).
This module implements just enough SIP (OPTIONS / REGISTER / INVITE with
Digest-MD5 Proxy-Authorization) to discover the working credentials and the
callee identity in the user's environment. No external SIP library is needed.

Security: credentials are used only inside the user's Home Assistant; log
output is masked.
"""

from __future__ import annotations

import hashlib
import logging
import re
import socket
import time

_LOGGER = logging.getLogger(__name__)

SIP_DEFAULT_HOST = "new-sip.jhws.top"
SIP_DEFAULT_PORT = 58583
SIP_TIMEOUT = 6.0
SIP_RETRIES = 2
SIP_GAP = 1.0


def mask_secret(value: str | None) -> str:
    """Mask a token/password for log output."""
    if not value:
        return ""
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:8]}...{value[-4:]}"


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def build_digest_response(
    username: str,
    realm: str,
    nonce: str,
    password: str,
    method: str,
    uri: str,
) -> str:
    """RFC 2617 Digest (MD5, no qop) response value."""
    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    return _md5(f"{ha1}:{nonce}:{ha2}")


def build_proxy_auth(
    username: str,
    realm: str,
    nonce: str,
    password: str,
    method: str,
    uri: str,
) -> str:
    response = build_digest_response(username, realm, nonce, password, method, uri)
    return (
        f'Proxy-Authorization: Digest username="{username}", realm="{realm}", '
        f'nonce="{nonce}", uri="{uri}", response="{response}", algorithm=MD5'
    )


def parse_status(resp: str) -> tuple[int, str]:
    m = re.search(r"SIP/2\.0 (\d{3}) ?([^\r\n]*)", resp)
    if not m:
        return 0, ""
    return int(m.group(1)), m.group(2).strip()


def parse_challenge(resp: str) -> tuple[str | None, str | None]:
    m = re.search(r'(?:Proxy-|WWW-)?Authenticate: Digest realm="([^"]+)"', resp)
    n = re.search(r'nonce="([^"]+)"', resp)
    return (m.group(1) if m else None), (n.group(1) if n else None)


class SipClient:
    """A tiny blocking UDP SIP client (run via asyncio.to_thread in HA)."""

    def __init__(
        self,
        host: str = SIP_DEFAULT_HOST,
        port: int = SIP_DEFAULT_PORT,
        local_ip: str | None = None,
        timeout: float = SIP_TIMEOUT,
        retries: int = SIP_RETRIES,
        transport: str = "tcp",
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self.transport = transport.lower()
        self.local_ip = local_ip or self._discover_local_ip()

    @staticmethod
    def _discover_local_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            finally:
                s.close()
        except Exception:
            return "127.0.0.1"

    def _build_message(
        self,
        method: str,
        uri: str,
        user: str,
        cseq: int,
        auth: str | None = None,
        extra_headers: str = "",
    ) -> str:
        transport = self.transport.upper()
        contact_transport = ";transport=tcp" if self.transport == "tcp" else ""
        lines = [
            f"{method} {uri} SIP/2.0",
            (
                f"Via: SIP/2.0/{transport} {self.local_ip}:5060;"
                f"branch=z9hG4bK-{int(time.time()*1000)}{cseq};rport"
            ),
            "Max-Forwards: 70",
            f"From: <sip:{user}@{self.host}>;tag=t{cseq}",
            f"To: <sip:{user}@{self.host}>",
            f"Call-ID: wb-{int(time.time()*1000)}-{cseq}@{self.local_ip}",
            f"CSeq: {cseq} {method}",
            f"Contact: <sip:{user}@{self.local_ip}:5060{contact_transport}>",
            "User-Agent: wuyebao-ha/2.6.0 (pjsua-compatible)",
        ]
        if auth:
            lines.append(auth)
        if extra_headers:
            lines.append(extra_headers)
        lines.append("Content-Length: 0")
        lines.append("")
        # "\r\n".join([...]) already ends with "\r\n"; add one more blank line
        # so the header/body separator (CRLF CRLF) is present.
        return "\r\n".join(lines) + "\r\n"

    def _exchange(self, payload: str) -> str:
        """Send one SIP message over UDP or TCP and collect the response."""
        if self.transport == "tcp":
            return self._exchange_tcp(payload)
        return self._exchange_udp(payload)

    def _exchange_udp(self, payload: str) -> str:
        """UDP variant of _exchange."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            last = ""
            for _ in range(self.retries):
                sock.sendto(payload.encode("utf-8"), (self.host, self.port))
                chunks = []
                try:
                    while True:
                        data, _addr = sock.recvfrom(65536)
                        if not data:
                            break
                        chunks.append(data.decode("utf-8", "replace"))
                        if len(b"".join(c.encode() for c in chunks)) > 30000:
                            break
                except socket.timeout:
                    pass
                last = "".join(chunks)
                if last:
                    return last
                time.sleep(SIP_GAP)
            return last
        finally:
            sock.close()

    def _exchange_tcp(self, payload: str) -> str:
        """TCP variant of _exchange (fresh connection per message)."""
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as err:
            return f"ERROR: {err!r}"
        sock.settimeout(self.timeout)
        try:
            sock.sendall(payload.encode("utf-8"))
            chunks = []
            try:
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data.decode("utf-8", "replace"))
                    if len(b"".join(c.encode() for c in chunks)) > 30000:
                        break
            except socket.timeout:
                pass
            return "".join(chunks)
        finally:
            sock.close()

    def _request(
        self,
        method: str,
        uri: str,
        user: str,
        password: str | None,
        cseq: int,
        extra_headers: str = "",
        challenge: tuple[str, str] | None = None,
    ) -> tuple[int, str, dict]:
        """Send a request; on 407/401 challenge, retry once with Digest auth.

        If `challenge=(realm, nonce)` is given, the request is sent once with
        a pre-computed Proxy-Authorization header instead (needed because the
        JHCloud OpenSIPS proxy silently drops unauthenticated REGISTER/INVITE).

        Returns (status_code, reason, info) where info carries challenge
        details, the masked credential used, etc.
        """
        if challenge:
            realm, nonce = challenge
            if realm and nonce and password:
                auth = build_proxy_auth(user, realm, nonce, password, method, uri)
                resp = self._exchange(
                    self._build_message(
                        method, uri, user, cseq + 1, auth=auth, extra_headers=extra_headers
                    )
                )
                status, reason = parse_status(resp)
                return (
                    status,
                    reason,
                    {"raw": resp, "realm": realm, "nonce": nonce, "preauth": True},
                )
        first = self._exchange(self._build_message(method, uri, user, cseq))
        status, reason = parse_status(first)
        if status not in (401, 407):
            return status, reason, {"raw": first}

        realm, nonce = parse_challenge(first)
        info = {"challenge": f"{status}", "realm": realm, "nonce": nonce}
        if realm and nonce and password:
            auth = build_proxy_auth(user, realm, nonce, password, method, uri)
            second = self._exchange(
                self._build_message(method, uri, user, cseq + 1, auth=auth, extra_headers=extra_headers)
            )
            status, reason = parse_status(second)
            info["raw"] = second
            return status, reason, info
        info["raw"] = first
        return status, reason, info

    def options(self, user: str, password: str | None = None) -> dict:
        uri = f"sip:{self.host}"
        status, reason, info = self._request("OPTIONS", uri, user, password, 1)
        return {"method": "OPTIONS", "status": status, "reason": reason, **info}

    def register(
        self,
        user: str,
        password: str | None = None,
        challenge: tuple[str, str] | None = None,
    ) -> dict:
        """REGISTER. If challenge=(realm, nonce) is supplied, the message is
        sent with a pre-computed Proxy-Authorization header (the OpenSIPS
        proxy silently drops unauthenticated REGISTER, so the standard
        challenge-then-retry flow never completes for REGISTER/INVITE)."""
        uri = f"sip:{self.host}"
        status, reason, info = self._request(
            "REGISTER", uri, user, password, 1, challenge=challenge
        )
        return {"method": "REGISTER", "status": status, "reason": reason, **info}

    def invite(
        self,
        from_user: str,
        password: str | None,
        to_uri: str,
        cseq: int = 1,
        challenge: tuple[str, str] | None = None,
    ) -> dict:
        sdp = (
            "Content-Type: application/sdp\r\n\r\n"
            "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\n"
            "t=0 0\r\nm=audio 4000 RTP/AVP 0 8 18 101\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
        )
        status, reason, info = self._request(
            "INVITE", to_uri, from_user, password, cseq, extra_headers=sdp, challenge=challenge
        )
        return {"method": "INVITE", "status": status, "reason": reason, **info}
