"""JHCloud SIP client: JWT-authenticated REGISTER + MESSAGE unlock.

Reverse-engineered from the official 物业宝 app (MuMu pcap capture,
2026-09-19). The real cloud-intercom protocol is:

  * Server ....... new-sip.jhws.top:58583 (TCP, OpenSIPS 2.1.2)
  * Realm/AOR .... jhws.top (NOT new-sip.jhws.top / sip.jhws.top)
  * User ......... <phone>@jhws.top
  * Auth ......... custom headers, NOT Digest:
                     SID: <16-hex client session id>
                     JWT: <client-token JWT, scope=["read"], ~7-day expiry>
  * Open door .... SIP MESSAGE (NOT INVITE) to
                     sip:GT-<communityCode>-<areaCode>-<buildingCode>-<unitCode>-<floorCode>-<deviceNumber>@jhws.top
                   with body:
                     {"id":"<uuid>","type":"unlock",
                      "content":{"device":"wall|outdoor",
                                 "ownerId":"<owner record id>",
                                 "deviceNumber":"<deviceNumber>"}}
  * Route header .. <sip:new-sip.jhws.top;transport=tcp;lr>

Security: secrets stay inside the user's HA; logs mask them.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import uuid

_LOGGER = logging.getLogger(__name__)

SIP_DEFAULT_HOST = "new-sip.jhws.top"
SIP_DEFAULT_PORT = 58583
SIP_REALM = "jhws.top"
SIP_ROUTE = "<sip:new-sip.jhws.top;transport=tcp;lr>"
SIP_UA = "JHCloud-android-m-SV:1.0-V:1.1.1.51"
SIP_TIMEOUT = 8.0


def mask_secret(value: str | None) -> str:
    """Mask a token/password for log output."""
    if not value:
        return ""
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:8]}...{value[-4:]}"


def _gen_branch() -> str:
    return "z9hG4bK" + uuid.uuid4().hex[:24]


def _gen_tag() -> str:
    return uuid.uuid4().hex[:16]


def _recv_full(sock: socket.socket, timeout: float) -> str:
    """Read one complete SIP message (header + Content-Length body)."""
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    content_length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            try:
                content_length = int(line.split(b":", 1)[1].strip())
            except ValueError:
                content_length = 0
    while len(body) < content_length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        body = buf.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in buf else b""
    return buf.decode("latin-1", errors="replace")


class JHSipClient:
    """Blocking TCP SIP client for the JHCloud open-door path.

    All methods are synchronous; call from asyncio.to_thread in HA.
    """

    def __init__(
        self,
        user: str,
        jwt: str,
        sid: str,
        host: str = SIP_DEFAULT_HOST,
        port: int = SIP_DEFAULT_PORT,
        timeout: float = SIP_TIMEOUT,
    ) -> None:
        self.user = user
        self.jwt = jwt
        self.sid = sid
        self.host = host
        self.port = port
        self.timeout = timeout
        self._local_ip = self._discover_local_ip()

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

    # -- low-level --------------------------------------------------------

    def _send_and_recv(self, sock: socket.socket, payload: str) -> tuple[int, str, str]:
        sock.sendall(payload.encode("utf-8"))
        reply = _recv_full(sock, self.timeout)
        first_line = reply.split("\r\n", 1)[0] if reply else ""
        code = 0
        reason = ""
        if first_line.startswith("SIP/2.0 "):
            parts = first_line[8:].split(" ", 1)
            try:
                code = int(parts[0])
            except ValueError:
                code = 0
            reason = parts[1].strip() if len(parts) > 1 else ""
        return code, reason, reply

    # -- public API -------------------------------------------------------

    def register(self) -> dict:
        """REGISTER with custom SID + JWT headers. Returns (ok, raw_reply)."""
        branch = _gen_branch()
        tag = _gen_tag()
        call_id = uuid.uuid4().hex[:24]
        contact = (
            f"<sip:{self.user}@{self._local_ip}:5060;transport=TCP;ob>"
        )
        lines = [
            f"REGISTER sip:new-sip.jhws.top;transport=tcp SIP/2.0",
            f"Via: SIP/2.0/TCP {self._local_ip}:5060;rport;branch={branch};alias",
            f"Route: {SIP_ROUTE}",
            "Max-Forwards: 70",
            f"From: <sip:{self.user}@{SIP_REALM}>;tag={tag}",
            f"To: <sip:{self.user}@{SIP_REALM}>",
            f"Call-ID: {call_id}",
            "CSeq: 1 REGISTER",
            f"SID: {self.sid}",
            f"JWT: {self.jwt}",
            f"User-Agent: {SIP_UA}",
            f"Contact: {contact}",
            "Expires: 300",
            "Content-Length: 0",
            "",
            "",
        ]
        payload = "\r\n".join(lines)
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as err:
            return {"ok": False, "status": 0, "error": f"connect: {err!r}"}
        try:
            code, reason, raw = self._send_and_recv(sock, payload)
            return {
                "ok": code == 200,
                "status": code,
                "reason": reason,
                "raw": raw,
                "_sock": sock,  # keep open for MESSAGE
            }
        except OSError as err:
            try:
                sock.close()
            finally:
                pass
            return {"ok": False, "status": 0, "error": f"recv: {err!r}"}

    def unlock(
        self,
        gate: dict,
        owner_id: str,
        device: str,
        device_number: str,
        community_code: str,
        area_code: str = "0",
        building_code: str = "0",
        unit_code: str = "0",
        floor_code: str = "0",
    ) -> dict:
        """Send SIP MESSAGE unlock over a fresh connection (REGISTER + MESSAGE)."""
        # 1) REGISTER
        reg = self.register()
        if not reg.get("ok"):
            return reg
        sock = reg.get("_sock")
        if sock is None:
            return {"ok": False, "status": 0, "error": "no socket after REGISTER"}

        try:
            # 2) MESSAGE
            gt_uri = (
                f"GT-{community_code}-{area_code}-{building_code}-"
                f"{unit_code}-{floor_code}-{device_number}"
            )
            body = json.dumps(
                {
                    "id": str(uuid.uuid4()),
                    "type": "unlock",
                    "content": {
                        "device": device,
                        "ownerId": str(owner_id),
                        "deviceNumber": str(device_number),
                    },
                },
                separators=(",", ":"),
            )
            branch = _gen_branch()
            tag = _gen_tag()
            call_id = uuid.uuid4().hex[:24]
            lines = [
                f"MESSAGE sip:{gt_uri}@{SIP_REALM} SIP/2.0",
                f"Via: SIP/2.0/TCP {self._local_ip}:5060;rport;branch={branch};alias",
                "Max-Forwards: 70",
                f"From: <sip:{self.user}@{SIP_REALM}>;tag={tag}",
                f"To: <sip:{gt_uri}@{SIP_REALM}>",
                f"Call-ID: {call_id}",
                "CSeq: 2 MESSAGE",
                f"Route: {SIP_ROUTE}",
                f"User-Agent: {SIP_UA}",
                "Content-Type: text/plain",
                f"Content-Length: {len(body)}",
                "",
                body,
            ]
            payload = "\r\n".join(lines)
            code, reason, raw = self._send_and_recv(sock, payload)
            return {
                "ok": code == 200,
                "status": code,
                "reason": reason,
                "raw": raw,
                "gt_uri": gt_uri,
            }
        finally:
            try:
                sock.close()
            except Exception:
                pass
