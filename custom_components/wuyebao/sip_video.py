"""SIP video monitor call for 物业宝.

Implements the full monitor session captured from the app:
    REGISTER -> INVITE -> 183/200 OK -> ACK -> (RTP H264 media)
    -> BYE

Key facts (from packet capture):
- Video media: RTP/AVP 97 H264/90000 (no fmtp in answer)
- Media server answers with a FreeSWITCH address (e.g. 47.107.228.158),
  but actual RTP comes back from a cluster node after the app first
  sends UDP packets (NAT hole punch).
- The app binds a local RTP port (offer m=video) and the server sends
  video RTP to that port after hole punching.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import struct
import threading
import time
import uuid

_LOGGER = logging.getLogger(__name__)

SIP_REALM = "jhws.top"
SIP_UA = "JHCloud-android-m-SV:1.0-V:1.1.1.53"
SIP_ROUTE = "<sip:new-sip.jhws.top;transport=tcp;lr>"


def _write_interleaved(writer, frame: bytes) -> None:
    """Thread-safe frame writer for RTP-over-TCP (runs in the event loop)."""
    try:
        writer.write(frame)
    except Exception:  # noqa: BLE001 - subscriber may have gone away
        pass


def _gen_branch() -> str:
    return "z9hG4bK" + uuid.uuid4().hex[:24]


def _gen_tag() -> str:
    return uuid.uuid4().hex[:16]


def _extract_record_routes(msg: str) -> list[str] | None:
    """Return the Record-Route headers (as Route: header values) if present."""
    routes: list[str] | None = None
    for line in msg.split("\r\n"):
        if line.lower().startswith("record-route:"):
            val = line.split(":", 1)[1].strip()
            # a single header may carry several comma-separated route entries
            entries = [e.strip() for e in val.split(",") if e.strip()]
            if entries:
                routes = entries
    return routes


def _split_sip_messages(raw: str) -> list[str]:
    """Split a raw TCP buffer into complete SIP messages.

    The server often coalesces several responses into one TCP segment
    (e.g. 183 + the device's INFO picture_fast_update).  _recv_full returns
    the whole segment, so we must split it and process each message - the
    INFO must be answered or the device never starts the media stream.
    """
    msgs: list[str] = []
    rest = raw
    while rest.strip():
        head, sep, body = rest.partition("\r\n\r\n")
        if not sep:
            msgs.append(rest)
            break
        cl = 0
        for line in head.split("\r\n"):
            ln = line.lower()
            if ln.startswith("content-length:") or ln.startswith("l:"):
                try:
                    cl = int(line.split(":", 1)[1].strip())
                except ValueError:
                    cl = 0
        total = len(head) + 4 + cl
        if len(rest) < total:
            msgs.append(rest)
            break
        msgs.append(rest[:total])
        rest = rest[total:]
    return msgs


def _recv_full(sock: socket.socket, timeout: float, max_len: int = 65535) -> str:
    """Read one complete SIP message (headers + content-length body)."""
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > max_len:
            break
    head, _, body = buf.partition(b"\r\n\r\n")
    content_length = 0
    for line in head.split(b"\r\n"):
        ln = line.lower()
        if ln.startswith(b"content-length:") or ln.startswith(b"l:"):
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


def _parse_sdp_answer(sdp: str) -> dict:
    """Extract media server IP and video/audio RTP+RTCP ports from answer SDP."""
    info = {
        "media_ip": None,
        "video_port": None,
        "audio_port": None,
        "video_rtcp": None,
        "audio_rtcp": None,
    }
    current = None
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            info["media_ip"] = line.split(" ", 2)[2]
        elif line.startswith("m=audio "):
            current = "audio"
            parts = line.split(" ")
            if len(parts) > 1:
                try:
                    info["audio_port"] = int(parts[1])
                except ValueError:
                    pass
        elif line.startswith("m=video "):
            current = "video"
            parts = line.split(" ")
            if len(parts) > 1:
                try:
                    info["video_port"] = int(parts[1])
                except ValueError:
                    pass
        elif line.startswith("a=rtcp:") and current:
            try:
                port = int(line.split(":")[1].split(" ")[0])
                info[f"{current}_rtcp"] = port
            except (ValueError, IndexError):
                pass
    return info


class SipMonitorCall:
    """A single SIP video monitor session (synchronous, run in a thread)."""

    def __init__(
        self,
        user: str,
        jwt: str,
        sid: str,
        gt_uri: str,
        display_name: str,
        host: str = "new-sip.jhws.top",
        port: int = 58583,
        rtp_port: int | None = None,
        timeout: float = 12.0,
    ) -> None:
        self.user = user
        self.jwt = jwt
        self.sid = sid
        self.gt_uri = gt_uri
        self.display_name = display_name
        self.host = host
        self.port = port
        self.timeout = timeout

        self._local_ip = self._discover_local_ip()
        if rtp_port is None:
            # empirically the media server only reliably streams back to
            # ports in the 56xxx range; keep the default there.
            base = 56000 + (uuid.uuid4().int % 1000 // 2) * 2
            rtp_port = base
        self.rtp_video_port = rtp_port
        self.rtp_audio_port = rtp_port + 2
        self.rtcp_video_port = rtp_port + 1
        self.rtcp_audio_port = rtp_port + 3

        self._sock: socket.socket | None = None
        self._rtp_sock: socket.socket | None = None
        self._rtcp_sock: socket.socket | None = None
        self._rtp_thread: threading.Thread | None = None
        self._running = threading.Event()
        # Timestamp of the last video RTP packet (0.0 = none yet).
        # The stream-health watchdog uses this to detect a stalled gate
        # (device stops pushing RTP after ~30s) and automatically rebuild.
        self.last_rtp_time: float = 0.0
        self._lock = threading.Lock()
        self._ssrc: int | None = None
        # Live stream metadata so injected parameter-set packets can share
        # the stream's SSRC/timestamp/sequence (ffmpeg ignores RTP packets
        # whose SSRC differs from the media stream).
        self._stream_ssrc: int | None = None
        self._last_ts: int | None = None
        self._last_seq: int | None = None

        self.media_ip: str | None = None
        self.media_video_port: int | None = None
        self.media_audio_port: int | None = None
        self.media_video_rtcp: int | None = None
        self.media_audio_rtcp: int | None = None
        self.call_id: str | None = None
        self._from_tag: str | None = None
        self._remote_tag: str | None = None
        self._remote_contact: str | None = None
        self._pending_msg: str | None = None
        self._acked = False
        self._seq = 0
        self._record_routes: list[str] | None = None

        # H264 parameter sets captured from RTP (for RTSP SDP)
        self.sps: bytes | None = None
        self.pps: bytes | None = None
        self._sps_pps_lock = threading.Lock()

        # RTSP subscribers: (socket, (ip, port)) to forward RTP to (UDP)
        self._subscribers: list[tuple[socket.socket, tuple]] = []
        # RTP-over-TCP subscribers: (loop, writer, channel) (interleaved)
        self._tcp_subscribers: list[tuple[object, object, int]] = []

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

    # ------------------------------------------------------------------
    # SIP signaling
    # ------------------------------------------------------------------
    def _register(self) -> bool:
        branch = _gen_branch()
        tag = _gen_tag()
        call_id = uuid.uuid4().hex[:24]
        contact = f"<sip:{self.user}@{self._local_ip}:5060;transport=TCP;ob>"
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
            # official app advertises this Allow list in REGISTER
            "Allow: PRACK, INVITE, ACK, BYE, CANCEL, UPDATE, INFO, SUBSCRIBE, NOTIFY, REFER, MESSAGE, OPTIONS",
            "Content-Length: 0",
            "",
            "",
        ]
        payload = "\r\n".join(lines)
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        code, _reason, _raw = self._send_and_recv(payload)
        return code == 200

    def _build_offer(self) -> str:
        """Build INVITE SDP offer (mirrors the app)."""
        local_ip = self._local_ip
        lines = [
            "v=0",
            f"o=- {uuid.uuid4().int % 10000000000} {uuid.uuid4().int % 10000000000} IN IP4 {local_ip}",
            "s=pjmedia",
            "b=AS:151",
            "t=0 0",
            "a=X-nat:0",
            f"m=audio {self.rtp_audio_port} RTP/AVP 120 119 118 113 112 111 96",
            f"c=IN IP4 {local_ip}",
            "b=TIAS:128000",
            f"a=rtcp:{self.rtcp_audio_port} IN IP4 {local_ip}",
            "a=sendrecv",
            "a=rtpmap:120 opus/48000/2",
            "a=rtpmap:119 G722/16000/2",
            "a=rtpmap:118 PCMA/8000/2",
            "a=rtpmap:113 L16/32000/2",
            "a=rtpmap:112 L16/16000/2",
            "a=rtpmap:111 L16/8000/2",
            "a=rtpmap:96 telephone-event/8000",
            "a=fmtp:96 0-16",
            f"m=video {self.rtp_video_port} RTP/AVP 97",
            f"c=IN IP4 {local_ip}",
            f"a=rtcp:{self.rtcp_video_port} IN IP4 {local_ip}",
            "a=sendrecv",
            "a=rtpmap:97 H264/90000",
            "",
        ]
        return "\r\n".join(lines)

    def _invite(self) -> dict:
        branch = _gen_branch()
        tag = _gen_tag()
        call_id = uuid.uuid4().hex[:24]
        self.call_id = call_id
        self._from_tag = tag
        contact = f"<sip:{self.user}@{self._local_ip}:5060;transport=TCP;ob>"
        sdp = self._build_offer()
        lines = [
            f"INVITE sip:{self.gt_uri}@{SIP_REALM};transport=tcp SIP/2.0",
            f"Via: SIP/2.0/TCP {self._local_ip}:5060;rport;branch={branch};alias",
            "Max-Forwards: 70",
            # official app uses bare From/To URIs (no angle brackets) in INVITE
            f"From: sip:{self.user}@{SIP_REALM};tag={tag}",
            f"To: sip:{self.gt_uri}@{SIP_REALM}",
            f"Contact: {contact}",
            f"Call-ID: {call_id}",
            "CSeq: 2 INVITE",
            f"Route: {SIP_ROUTE}",
            "Supported: replaces, 100rel, timer, norefersub",
            "Session-Expires: 1800",
            "Min-SE: 90",
            f"User-Agent: {SIP_UA}",
            f"X-displayName: {self.display_name}",
            "Content-Type: application/sdp",
            f"Content-Length: {len(sdp.encode('utf-8'))}",
            "",
            sdp,
        ]
        payload = "\r\n".join(lines)
        return self._send_and_recv(payload)

    def _send_and_recv(self, payload: str) -> tuple[int, str, str]:
        if self._sock is None:
            return 0, "", ""
        try:
            self._sock.sendall(payload.encode("utf-8"))
            reply = _recv_full(self._sock, self.timeout)
        except OSError as err:
            _LOGGER.debug("SIP send/recv error: %s", err)
            return 0, "", str(err)
        first_line = reply.split("\r\n", 1)[0] if reply else ""
        code = 0
        reason = ""
        # The server may coalesce "100 Trying" with the real final
        # response (183/200/486) into a single TCP segment.  Scanning only
        # the first status line would then drop the final response and make
        # us treat a successful call as a timeout.  Scan ALL status lines
        # and keep the last one.
        for m in re.finditer(rb"SIP/2\.0 (\d{3}) ([^\r\n]*)", reply.encode("latin-1", "replace")):
            code = int(m.group(1))
            reason = m.group(2).decode("latin-1", "replace")
        if first_line.startswith("SIP/2.0 ") and code == 0:
            parts = first_line[8:].split(" ", 1)
            try:
                code = int(parts[0])
            except ValueError:
                code = 0
            reason = parts[1].strip() if len(parts) > 1 else ""
        return code, reason, reply

    def _reply_200_to_info(self, info_request: str) -> None:
        """Answer the device's in-dialog INFO (picture_fast_update).

        Packet capture shows the device asks for a fast video update right
        after 183 (early media) by sending an INFO (media_control XML); the
        official app answers it with a 200 OK.  If we leave it unanswered the
        device waits for the reply and never starts the RTP stream.
        """
        if self._sock is None:
            return
        head = info_request.split("\r\n\r\n", 1)[0]
        lines = head.split("\r\n")
        def get(hdr: str) -> str | None:
            for ln in lines:
                if ln.lower().startswith(hdr.lower() + ":"):
                    return ln.split(":", 1)[1].strip()
                if ln.lower().startswith(hdr.lower() + " "):  # compact form "i:..."
                    return ln[2:].strip()
            return None
        via = get("Via") or get("v")
        frm = get("From") or get("f")
        to = get("To") or get("t")
        call_id = get("Call-ID") or get("i")
        cseq = get("CSeq") or get("CSeq")
        if not (via and frm and to and call_id and cseq):
            _LOGGER.debug("INFO reply skipped: missing headers in %s", head[:200])
            return
        branch = _gen_branch()
        reply = "\r\n".join([
            "SIP/2.0 200 OK",
            f"Via: {via}",
            f"From: {to};tag={self._from_tag or _gen_tag()}",
            f"To: {frm}",
            f"Call-ID: {call_id}",
            f"CSeq: {cseq}",
            f"User-Agent: {SIP_UA}",
            "Content-Length: 0",
            "",
            "",
        ])
        try:
            self._sock.sendall(reply.encode("utf-8"))
            _LOGGER.info("Replied 200 OK to device INFO (%s)", self.gt_uri)
        except OSError:
            pass

    def _ack(self) -> None:
        if self._sock is None:
            return
        branch = _gen_branch()
        # The app ACKs the device contact from the response (m: header), not
        # the logical gt_uri@realm address, and routes via the Record-Route
        # headers from the response.
        target = self._remote_contact or f"sip:{self.gt_uri}@{SIP_REALM}"
        lines = [
            f"ACK {target} SIP/2.0",
            f"Via: SIP/2.0/TCP {self._local_ip}:5060;rport;branch={branch};alias",
            "Max-Forwards: 70",
            f"From: <sip:{self.user}@{SIP_REALM}>;tag={self._from_tag or _gen_tag()}",
            f"To: <sip:{self.gt_uri}@{SIP_REALM}>;tag={self._remote_tag or ''}",
            f"Call-ID: {self.call_id or ''}",
            "CSeq: 2 ACK",
        ]
        if self._record_routes:
            for rr in self._record_routes:
                lines.append(f"Route: {rr}")
        else:
            lines.append(f"Route: {SIP_ROUTE}")
        lines.extend([
            "Supported: timer, path, replaces",
            f"User-Agent: {SIP_UA}",
            "Content-Length: 0",
            "",
            "",
        ])
        try:
            self._sock.sendall(("\r\n".join(lines)).encode("utf-8"))
        except OSError:
            pass

    def _send_picture_fast_update(self) -> None:
        """Send INFO (media_control picture_fast_update) to the device.

        The device uses this primitive to request a fast video update
        (key frame).  Sending it right after ACK makes the encoder push
        an IDR so the H264 stream becomes decodable quickly.
        """
        if self._sock is None:
            return
        body = (
            '<?xml version="1.0" encoding="utf-8" ?>\r\n'
            "<media_control>\r\n"
            " <vc_primitive>\r\n"
            "  <to_encoder>\r\n"
            "   <picture_fast_update>\r\n"
            "   </picture_fast_update>\r\n"
            "  </to_encoder>\r\n"
            " </vc_primitive>\r\n"
            "</media_control>\r\n"
        )
        branch = _gen_branch()
        lines = [
            f"INFO sip:{self.gt_uri}@{SIP_REALM} SIP/2.0",
            f"Via: SIP/2.0/TCP {self._local_ip}:5060;rport;branch={branch};alias",
            "Max-Forwards: 70",
            f"From: <sip:{self.user}@{SIP_REALM}>;tag={self._from_tag or _gen_tag()}",
            f"To: <sip:{self.gt_uri}@{SIP_REALM}>;tag={self._remote_tag or ''}",
            f"Call-ID: {self.call_id or ''}",
            "CSeq: 2 INFO",
            f"Route: {SIP_ROUTE}",
            "Content-Type: application/media_control+xml",
            f"Content-Length: {len(body.encode('utf-8'))}",
            f"User-Agent: {SIP_UA}",
            "",
            body.rstrip("\r\n"),
            "",
        ]
        try:
            self._sock.sendall(("\r\n".join(lines)).encode("utf-8"))
        except OSError:
            pass

    def _bye(self) -> None:
        if self._sock is None:
            return
        branch = _gen_branch()
        lines = [
            f"BYE sip:{self.gt_uri}@{SIP_REALM} SIP/2.0",
            f"Via: SIP/2.0/TCP {self._local_ip}:5060;rport;branch={branch};alias",
            "Max-Forwards: 70",
            f"From: <sip:{self.user}@{SIP_REALM}>;tag={self._from_tag or _gen_tag()}",
            f"To: <sip:{self.gt_uri}@{SIP_REALM}>;tag={self._remote_tag or ''}",
            f"Call-ID: {self.call_id or ''}",
            "CSeq: 3 BYE",
            f"Route: {SIP_ROUTE}",
            f"User-Agent: {SIP_UA}",
            "Content-Length: 0",
            "",
            "",
        ]
        try:
            self._sock.sendall(("\r\n".join(lines)).encode("utf-8"))
        except OSError:
            pass

    # ------------------------------------------------------------------
    # RTP receive + H264 + forwarding
    # ------------------------------------------------------------------
    def _hole_punch(self) -> None:
        """Send NAT hole-punch probes exactly like the app.

        The app (PJSIP/cloudrtc) repeatedly sends the 12-byte ASCII
        packet "probing data" plus an 8-byte empty RTCP RR from each
        media source port to the server's RTP ports.  The FreeSWITCH
        media node learns the client's NAT mapping from these and then
        sends the video RTP back to the offer port.
        """
        if self.media_ip is None or self.media_video_port is None:
            return
        if self._rtp_sock is None:
            return
        # Send a legal RTP packet (PT 97 H264 with a filler NAL) instead of
        # a bare ASCII probe: FreeSWITCH's media node only learns the NAT
        # mapping from incoming packets it recognises as RTP/RTCP.  The app
        # (PJSIP) sends real media packets for the same purpose.
        if self._ssrc is None:
            self._ssrc = uuid.uuid4().int & 0xFFFFFFFF
        self._seq = (self._seq or 0) + 1
        rtp = struct.pack(
            ">BBHII", 0x80, 97, self._seq & 0xFFFF, self._ssrc, int(time.time() * 1000) & 0xFFFFFFFF
        )
        pkt = rtp + b"\x00\x00\x00\x01\x09"  # H264 filler NAL
        # legal RTCP Sender Report (32 bytes)
        sr = (
            struct.pack(">BBHII", 0x80, 0xC8, 6, self._ssrc, int(time.time() * 1000) & 0xFFFFFFFF)
            + struct.pack(">II", 0, 0)
            + struct.pack(">II", 0, 0)
        )
        targets: list[tuple[str, int]] = [
            (self.media_ip, self.media_video_port),
            (
                self.media_ip,
                self.media_video_rtcp
                if self.media_video_rtcp
                else self.media_video_port + 1,
            ),
        ]
        if self.media_audio_port:
            targets.append((self.media_ip, self.media_audio_port))
            targets.append(
                (
                    self.media_ip,
                    self.media_audio_rtcp
                    if self.media_audio_rtcp
                    else self.media_audio_port + 1,
                )
            )
        for i, target in enumerate(targets):
            try:
                if i % 2 == 0:
                    self._rtp_sock.sendto(pkt, target)
                else:
                    self._rtp_sock.sendto(sr, target)
            except OSError:
                pass

    def _rtp_loop(self) -> None:
        """Receive RTP, extract SPS/PPS, forward raw RTP to subscribers.

        Continuously hole-punches the media server every 2s until the
        first video RTP packet arrives (FreeSWITCH activates the media
        session only after ACK and only sends RTP after it has seen
        incoming UDP from the client's offer port).
        """
        self._running.set()
        received_rtp = False
        last_punch = 0.0
        try:
            while self._running.is_set():
                try:
                    self._rtp_sock.settimeout(1.0)
                    data, addr = self._rtp_sock.recvfrom(65535)
                except socket.timeout:
                    data = None
                    addr = None
                except OSError:
                    break
                if data is None:
                    # periodic hole punch until media flows (0.5s interval so
                    # slow devices / media nodes get the NAT mapping quickly)
                    now = time.time()
                    if not received_rtp and now - last_punch > 0.5:
                        self._hole_punch()
                        last_punch = now
                    continue
                if len(data) < 12:
                    continue
                version = (data[0] >> 6) & 0x03
                if version != 2:
                    _LOGGER.debug("Non-RTP UDP from %s: first=%s len=%d", addr, data[:4].hex(), len(data))
                    continue
                pt = data[1] & 0x7F
                if pt != 97:
                    _LOGGER.debug("Non-video RTP from %s: pt=%d len=%d", addr, pt, len(data))
                    continue
                received_rtp = True
                self._video_received = True
                self._video_packet_count = getattr(self, "_video_packet_count", 0) + 1
                self.last_rtp_time = time.time()
                # Track the stream's SSRC/timestamp/sequence so injected
                # parameter-set packets look like part of the media stream.
                self._stream_ssrc = int.from_bytes(data[8:12], "big")
                self._last_ts = int.from_bytes(data[4:8], "big")
                self._last_seq = int.from_bytes(data[2:4], "big")
                if _LOGGER.isEnabledFor(logging.DEBUG) and received_rtp and not getattr(self, "_logged_first_rtp", False):
                    self._logged_first_rtp = True
                    _LOGGER.debug("First video RTP from %s len=%d", addr, len(data))

                # Extract SPS/PPS from H264 payload (for RTSP DESCRIBE)
                payload = data[12:]
                if payload:
                    nal_type = payload[0] & 0x1F
                    if nal_type == 7 and len(payload) >= 4:  # SPS
                        with self._sps_pps_lock:
                            self.sps = payload
                    elif nal_type == 8 and len(payload) >= 3:  # PPS
                        with self._sps_pps_lock:
                            self.pps = payload
                    elif nal_type == 28 and len(payload) >= 2:  # FU-A
                        fu_header = payload[1]
                        start_bit = (fu_header >> 7) & 0x01
                        if start_bit:
                            nri = (payload[0] >> 5) & 0x03
                            real_type = fu_header & 0x1F
                            if real_type == 7:  # SPS in FU-A
                                nal = bytes([(nri << 5) | 7]) + payload[2:]
                                with self._sps_pps_lock:
                                    if len(nal) >= 4:
                                        self.sps = nal
                            elif real_type == 8:  # PPS in FU-A
                                nal = bytes([(nri << 5) | 8]) + payload[2:]
                                with self._sps_pps_lock:
                                    if len(nal) >= 3:
                                        self.pps = nal

                # Forward raw RTP to RTSP subscribers
                with self._lock:
                    subs = list(self._subscribers)
                    tcp_subs = list(self._tcp_subscribers)
                dead = []
                for s, target in subs:
                    try:
                        s.sendto(data, target)
                    except OSError:
                        dead.append((s, target))
                if dead:
                    with self._lock:
                        for d in dead:
                            if d in self._subscribers:
                                self._subscribers.remove(d)
                # RTP-over-TCP: wrap in interleaved frames ($ <ch> <len> <rtp>)
                for loop, writer, channel in tcp_subs:
                    frame = (
                        b"$"
                        + bytes([channel])
                        + len(data).to_bytes(2, "big")
                        + data
                    )
                    try:
                        loop.call_soon_threadsafe(_write_interleaved, writer, frame)
                    except RuntimeError:
                        # event loop closed
                        with self._lock:
                            if (loop, writer, channel) in self._tcp_subscribers:
                                self._tcp_subscribers.remove((loop, writer, channel))
        finally:
            self._running.clear()

    def add_subscriber(self, s: socket.socket, target: tuple) -> None:
        with self._lock:
            if (s, target) not in self._subscribers:
                self._subscribers.append((s, target))

    def add_tcp_subscriber(self, loop, writer, channel: int = 0) -> None:
        with self._lock:
            if (loop, writer, channel) not in self._tcp_subscribers:
                self._tcp_subscribers.append((loop, writer, channel))

    def remove_subscriber(self, s: socket.socket, target: tuple) -> None:
        with self._lock:
            if (s, target) in self._subscribers:
                self._subscribers.remove((s, target))

    def remove_tcp_subscriber(self, loop, writer, channel: int = 0) -> None:
        with self._lock:
            if (loop, writer, channel) in self._tcp_subscribers:
                self._tcp_subscribers.remove((loop, writer, channel))

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers) + len(self._tcp_subscribers)

    def is_stream_alive(self, silence_threshold: float) -> bool:
        """True while video RTP is still arriving.

        The gate devices stop pushing RTP roughly 30s after the INVITE
        (no keepalive on the media path), which freezes the picture while
        the session stays open.  The watchdog calls this with a threshold
        (e.g. 10s) and rebuilds the session when it returns False.
        """
        if self.last_rtp_time <= 0:
            return False
        return (time.time() - self.last_rtp_time) < silence_threshold

    def get_sps_pps(self) -> tuple[bytes | None, bytes | None]:
        with self._sps_pps_lock:
            return self.sps, self.pps

    def build_paramset_packets(self) -> list[bytes]:
        """Build RTP packets carrying the latest SPS/PPS.

        A freshly connected RTSP client (ffmpeg/PyAV) may join the stream
        mid-call, right after the device rotated to a new parameter-set id;
        its first IDR then references a PPS it has not seen and decode aborts
        ("non-existing PPS"). Injecting the latest SPS/PPS right after PLAY
        lets the client register the parameter sets before the next IDR. The
        device re-sends SPS/PPS every ~1.2s so any transient mismatch heals.
        """
        with self._sps_pps_lock:
            sps, pps = self.sps, self.pps
        if not sps or not pps:
            return []
        ssrc = self._stream_ssrc or 0x5A5A5A5A
        ts = self._last_ts if self._last_ts is not None else int(time.time() * 90000) & 0xFFFFFFFF
        seq = (self._last_seq or 0) & 0xFFFF
        pkts: list[bytes] = []
        for nal in (sps, pps):
            seq = (seq + 1) & 0xFFFF
            pkts.append(struct.pack(">BBHII", 0x80, 97, seq, ts, ssrc) + nal)
        return pkts

    def wait_for_video(self, timeout: float) -> bool:
        """Block until the first H264 video frame (SPS/PPS) is seen."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._sps_pps_lock:
                if self.sps is not None or self.pps is not None:
                    return True
            time.sleep(0.2)
        return False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> dict:
        """REGISTER once, then INVITE with retries until video RTP flows.

        Packet capture shows the official app retries the monitor call for a
        busy / non-answering gate (typically 4-5 INVITEs) until the device
        finally answers and streams; the last attempt is the one that shows
        video ("最后一次才有画面").  We mirror that behaviour: each attempt
        that fails (busy 486, timeout, answered-but-no-media) is retried.
        """
        if not self._register():
            return {"ok": False, "status": 0, "error": "REGISTER failed"}
        self._running.set()

        max_attempts = getattr(self, "max_monitor_attempts", 6)
        retry_delay = getattr(self, "monitor_retry_delay", 2.0)
        last = {"ok": False, "status": 0, "error": "INVITE failed"}
        for attempt in range(1, max_attempts + 1):
            if not self._running.is_set():
                break
            if attempt > 1:
                _LOGGER.info(
                    "Monitor %s retry %d/%d (delay %.1fs)",
                    self.gt_uri, attempt, max_attempts, retry_delay,
                )
                time.sleep(retry_delay)
                self._reset_session()
                if not self._register():
                    last = {"ok": False, "status": 0, "error": "REGISTER failed"}
                    continue
            last = self._start_attempt()
            if last.get("ok"):
                return last
            _LOGGER.warning(
                "Monitor %s attempt %d/%d failed: %s",
                self.gt_uri, attempt, max_attempts, last.get("error"),
            )
        return last

    def _start_attempt(self) -> dict:
        """Single INVITE attempt: INVITE -> ACK -> hole punch -> RTP receiver."""
        self._video_received = False
        self._video_packet_count = 0
        # bind RTP sockets BEFORE INVITE so the offer ports are listening.
        # Retry with a fresh random port if the chosen one is taken.
        for _attempt in range(5):
            self._rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._rtp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self._rtp_sock.bind(("0.0.0.0", self.rtp_video_port))
                break
            except OSError as err:
                self._rtp_sock.close()
                self._rtp_sock = None
                base = 56000 + (uuid.uuid4().int % 1000 // 2) * 2
                self.rtp_video_port = base
                self.rtp_audio_port = base + 2
                self.rtcp_video_port = base + 1
                self.rtcp_audio_port = base + 3
                if _attempt == 4:
                    return {"ok": False, "status": 0, "error": f"bind rtp: {err}"}

        code, reason, raw = self._invite()
        if code not in (100, 180, 183):
            # log the full reply for 486/4xx diagnostics (Retry-After etc.)
            _LOGGER.warning(
                "INVITE %s not accepted: code=%s reason=%s reply=%s",
                self.gt_uri, code, reason, raw[:400],
            )
            self._cleanup_after_fail()
            return {"ok": False, "status": code, "error": f"INVITE {reason}"}

        # Wait for a response that carries the answer SDP (183 early-media or
        # 200 OK) and ACK it, mirroring the official app.
        #
        # IMPORTANT (from packet capture + local repro): gates that answer
        # with 183 "Session Progress" (early media) MUST be ACKed right away
        # using the 183 SDP.  The device starts sending video after the ACK;
        # if we keep waiting for the final 200 OK before ACKing, the device /
        # FreeSWITCH media session gives up and no RTP ever arrives (observed
        # for north wall gate GT-b and unit doors, while gates that answer a
        # plain 200 OK work).  The app ACKs immediately on 183 and also
        # answers the device's INFO "picture_fast_update" with 200 OK.
        sdp = raw.partition("\r\n\r\n")[2]
        final_code = code
        acked = False
        deadline = time.time() + 25.0
        while time.time() < deadline:
            try:
                extra = _recv_full(self._sock, min(2.0, deadline - time.time()))
            except socket.timeout:
                # no data yet: if we already ACKed early media, we are done
                # waiting for a final response; otherwise keep waiting
                if acked:
                    break
                continue
            except OSError:
                break
            if not extra:
                break  # connection closed
            # A segment may hold several SIP messages (e.g. 183 + the
            # device's INFO).  Split and handle each one.
            for msg in _split_sip_messages(extra):
                up = msg.upper()
                if up.startswith("SIP/2.0 "):
                    try:
                        final_code = int(msg.split(" ")[1])
                    except (ValueError, IndexError):
                        pass
                    if self._record_routes is None:
                        self._record_routes = _extract_record_routes(msg)
                    if final_code >= 200 and not acked:
                        body = msg.partition("\r\n\r\n")[2]
                        if "m=video" in body or "m=audio" in body:
                            sdp = body
                            # 200 OK answered: any early-media packets seen
                            # before this point were ringtone - restart the
                            # real-video confirmation counter.
                            self._video_packet_count = 0
                            for line in msg.split("\r\n"):
                                if line.lower().startswith("t:") and ";tag=" in line:
                                    self._remote_tag = line.split(";tag=")[1].strip().rstrip("\r")
                                elif line.lower().startswith("m:"):
                                    self._remote_contact = line[2:].strip()
                            self._pending_msg = extra  # keep INFO that followed
                            break
                        if sdp:
                            # 183 already supplied the answer SDP; the final
                            # 200 OK without a body is normal - ACK it.
                            for line in msg.split("\r\n"):
                                if line.lower().startswith("t:") and ";tag=" in line:
                                    self._remote_tag = line.split(";tag=")[1].strip().rstrip("\r")
                                elif line.lower().startswith("m:"):
                                    self._remote_contact = line[2:].strip()
                            self._pending_msg = extra
                            break
                        # no SDP anywhere yet - keep waiting (FreeSWITCH
                        # occasionally answers 183/200 without SDP)
                        continue
                    # 183/180: take the SDP and ACK early media right away
                    if final_code in (180, 183) and not acked:
                        body = msg.partition("\r\n\r\n")[2]
                        if "m=video" in body or "m=audio" in body:
                            sdp = body
                            for line in msg.split("\r\n"):
                                if line.lower().startswith("t:") and ";tag=" in line:
                                    self._remote_tag = line.split(";tag=")[1].strip().rstrip("\r")
                                elif line.lower().startswith("m:"):
                                    self._remote_contact = line[2:].strip()
                            self._pending_msg = extra  # keep INFO that followed
                            break
                elif up.startswith("INFO "):
                    # device INFO (picture_fast_update): answer 200 OK so the
                    # device proceeds to stream media (the app does the same)
                    self._reply_200_to_info(msg)
                # extract remote tag + Contact (m:) from any response
                for line in msg.split("\r\n"):
                    if line.lower().startswith("t:") and ";tag=" in line:
                        self._remote_tag = line.split(";tag=")[1].strip().rstrip("\r")
                    elif line.lower().startswith("m:"):
                        self._remote_contact = line[2:].strip()
            else:
                continue
            break  # got the answer SDP

        if not acked and final_code not in (183, 200):
            self._cleanup_after_fail()
            return {"ok": False, "status": final_code, "error": f"INVITE {final_code}"}

        # parse answer SDP
        _LOGGER.info("Monitor answer SDP for %s (len=%d): %.300s", self.gt_uri, len(sdp), sdp)
        info = _parse_sdp_answer(sdp)
        self.media_ip = info.get("media_ip")
        self.media_video_port = info.get("video_port")
        self.media_audio_port = info.get("audio_port")
        self.media_video_rtcp = info.get("video_rtcp")
        self.media_audio_rtcp = info.get("audio_rtcp")
        _LOGGER.info(
            "Monitor session for %s: media=%s video=%s(rtcp=%s) audio=%s(rtcp=%s)",
            self.gt_uri, self.media_ip, self.media_video_port,
            self.media_video_rtcp, self.media_audio_port, self.media_audio_rtcp,
        )
        if not self.media_ip or not self.media_video_port:
            self._cleanup_after_fail()
            return {"ok": False, "status": final_code, "error": "No video media in answer"}

        # start RTP receiver thread (it also keeps hole-punching)
        self._rtp_thread = threading.Thread(target=self._rtp_loop, daemon=True, name="pb-rtp")
        self._rtp_thread.start()

        # For 183 early media, the device sends INFO (picture_fast_update)
        # right after 183 and waits for the 200 OK before it starts
        # streaming.  The official app answers that INFO BEFORE sending the
        # ACK.  Drain up to 3s answering INFO; if a final 200 OK arrives
        # first, refresh the answer SDP.
        def _drain_and_answer(duration: float) -> None:
            # First process the tail of the segment that carried the answer
            # SDP (the device INFO that arrived in the same TCP segment).
            pending = getattr(self, "_pending_msg", None)
            if pending:
                msgs = _split_sip_messages(pending)
                for idx, m in enumerate(msgs):
                    if idx == 0:
                        continue  # the 183/200 answer already handled
                    _handle_tail(m)
                self._pending_msg = None
            deadline = time.time() + duration
            while time.time() < deadline:
                try:
                    extra = _recv_full(self._sock, 0.5)
                except (socket.timeout, OSError):
                    return
                if not extra:
                    return
                for m in _split_sip_messages(extra):
                    _handle_tail(m)

        def _handle_tail(msg: str) -> None:
            up = msg.upper()
            if self._record_routes is None:
                self._record_routes = _extract_record_routes(msg)
            # extract the device contact and To-tag BEFORE answering INFO /
            # sending the ACK so the ACK targets the device, like the app
            for line in msg.split("\r\n"):
                if line.lower().startswith("t:") and ";tag=" in line:
                    self._remote_tag = line.split(";tag=")[1].strip().rstrip("\r")
                elif line.lower().startswith("m:"):
                    self._remote_contact = line[2:].strip()
            if up.startswith("INFO "):
                self._reply_200_to_info(msg)
                # Do NOT ACK yet: the app ACKs only after the final
                # 200 OK (INVITE) arrives.  An ACK for a provisional
                # 183 is ignored by FreeSWITCH, and if we mark acked
                # early we never send the real ACK -> media never
                # activates.  The 200 OK branch below sends the ACK.
            elif up.startswith("SIP/2.0 ") and " 200 " in up[:64]:
                new_body = msg.partition("\r\n\r\n")[2]
                if "m=video" in new_body:
                    # final 200 OK: restart the real-video counter (any
                    # early-media packets were ringtone, not the device stream)
                    self._video_packet_count = 0
                    new_info = _parse_sdp_answer(new_body)
                    if (
                        new_info.get("media_ip")
                        and new_info.get("video_port")
                        and (
                            new_info["media_ip"] != self.media_ip
                            or new_info["video_port"] != self.media_video_port
                        )
                    ):
                        _LOGGER.info(
                            "Answer SDP updated: %s:%s -> %s:%s",
                            self.media_ip, self.media_video_port,
                            new_info["media_ip"], new_info["video_port"],
                        )
                        self.media_ip = new_info["media_ip"]
                        self.media_video_port = new_info["video_port"]
                        self.media_audio_port = new_info.get("audio_port")
                        self.media_video_rtcp = new_info.get("video_rtcp")
                        self.media_audio_rtcp = new_info.get("audio_rtcp")
                        self._hole_punch()
                if not self._acked:
                    self._acked = True
                    self._ack()

        if final_code in (180, 183):
            _drain_and_answer(3.0)

        # ACK first - FreeSWITCH activates the media session on ACK,
        # then punch a hole from the offer port so it can send RTP back.
        # (may already have been sent right after answering the device INFO)
        if not self._acked:
            self._acked = True
            self._ack()
        acked = True
        self._hole_punch()

        # After the ACK keep draining briefly: late INFO and the final
        # 200 OK may arrive now - answer INFO and refresh media target.
        _drain_and_answer(2.0)

        # Confirm real video actually flows before declaring success: a
        # 183 early-media gate that rings then answers streams only a few
        # probe packets (the app abandons those attempts too - only the
        # attempt where the device answers straight away streams video).
        # Require a *sustained* packet stream: enough packets AND no quiet
        # gap, so a ringtone trickle is never mistaken for a live stream.
        min_packets = getattr(self, "min_video_packets", 10)
        deadline = time.time() + 5.0
        last_pkt = 0.0
        count = 0
        while time.time() < deadline:
            c = getattr(self, "_video_packet_count", 0)
            if c > count:
                count = c
                last_pkt = time.time()
            if count >= min_packets and time.time() - last_pkt > 1.2:
                break  # stream went quiet -> early-media trickle, retry
            if count >= min_packets * 4:
                break  # strong real stream
            time.sleep(0.1)
        sustained = count >= min_packets and (
            time.time() - last_pkt <= 1.2 or count >= min_packets * 4
        )
        if not sustained:
            try:
                self._bye()
            except Exception:
                pass
            self._cleanup_after_fail()
            return {
                "ok": False,
                "status": final_code,
                "error": f"answered but no sustained video ({count} pkts)",
            }

        _LOGGER.info("Monitor call active: %s", self.gt_uri)
        return {
            "ok": True,
            "status": final_code,
            "media_ip": self.media_ip,
            "media_video_port": self.media_video_port,
            "rtp_video_port": self.rtp_video_port,
        }

    def _cleanup_after_fail(self) -> None:
        """Close sockets after a failed start."""
        if self._rtp_sock is not None:
            try:
                self._rtp_sock.close()
            except OSError:
                pass
            self._rtp_sock = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _reset_session(self) -> None:
        """Reset per-call state before a retry attempt."""
        if self._rtp_sock is not None:
            try:
                self._rtp_sock.close()
            except OSError:
                pass
            self._rtp_sock = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._pending_msg = None
        self._acked = False
        self._record_routes = None
        self._remote_tag = None
        self._remote_contact = None
        self._video_received = False
        self._video_packet_count = 0
        self._rtp_thread = None
        self._stream_ssrc = None
        self._last_ts = None
        self._last_seq = None
        self.sps = None
        self.pps = None

    def stop(self) -> None:
        """Stop RTP receiver, send BYE, close sockets."""
        self._running.clear()
        if self._rtp_sock is not None:
            try:
                # unblock recvfrom
                self._rtp_sock.close()
            except OSError:
                pass
        if self._rtp_thread is not None:
            self._rtp_thread.join(timeout=2)
        if self._sock is not None:
            try:
                self._bye()
            except Exception:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._rtp_thread = None
        _LOGGER.info("Monitor call stopped: %s", self.gt_uri)
