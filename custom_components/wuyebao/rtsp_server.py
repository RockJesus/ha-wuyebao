"""Minimal RTSP server for 物业宝 live video.

Each gate device is exposed as  rtsp://127.0.0.1:8555/<gate-id>.
When a client (go2rtc / ffmpeg / HA stream) connects and plays the
stream, the manager starts a SIP monitor call if needed and forwards
the incoming H264 RTP packets to the RTSP client (raw RTP passthrough).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import socket
import time
import uuid

from .sip_video import SipMonitorCall

_LOGGER = logging.getLogger(__name__)

RTSP_HOST = "0.0.0.0"
RTSP_PORT = 8556
IDLE_STOP_AFTER = 30  # seconds without RTSP clients -> BYE


class VideoSessionManager:
    """Manages one SipMonitorCall per gate device id."""

    def __init__(self) -> None:
        self._sessions: dict[str, SipMonitorCall] = {}
        self._gates: dict[str, dict] = {}
        self._client: object | None = None  # WuYeBaoClient
        self._lock = asyncio.Lock()

    def register_gate(self, gate_id: str, gate: dict, client) -> None:
        self._gates[gate_id] = {"gate": gate, "client": client}

    def get_gate(self, gate_id: str) -> dict | None:
        entry = self._gates.get(gate_id)
        return entry.get("gate") if entry else None

    def get_session(self, gate_id: str) -> SipMonitorCall | None:
        """Return the active monitor session without starting a new call."""
        return self._sessions.get(gate_id)

    async def get_or_start(self, gate_id: str) -> SipMonitorCall | None:
        """Return the active monitor session for gate_id, starting it if needed."""
        async with self._lock:
            sess = self._sessions.get(gate_id)
            if sess is not None:
                return sess
            entry = self._gates.get(gate_id)
            if not entry:
                return None
            gate = entry["gate"]
            client = entry["client"]

            gt_uri = _build_gt_uri(gate)
            if not gt_uri:
                return None
            display_name = _build_display_name(gate)

            # make sure the SIP JWT is fresh before placing the call
            try:
                await client.ensure_sip_token()
            except Exception as err:
                _LOGGER.warning("Failed to refresh SIP token for video: %s", err)

            if not getattr(client, "sip_jwt", None) or not getattr(client, "owner_id", None):
                return None

            for attempt in range(5):
                # Packet capture of the official app (v1.1.1.53, 2026-10-04)
                # proves the monitor flow is a plain INVITE: the app does
                # NOT send a "monitor" SIP MESSAGE before the call (its
                # MESSAGEs are only unlock requests).  The pre-INVITE
                # activation MESSAGE added in 6.6.3 was based on a wrong
                # assumption and did not fix the "486 Busy Here" replies.
                # Keep the INVITE-only flow; retries use a growing backoff
                # (10/20/30/45s) so a gate can release its busy state
                # (the app keeps a monitor session ~30s after BYE) before
                # retrying.  5 attempts cover the typical release window.
                call = SipMonitorCall(
                    user=client.username,
                    jwt=client.sip_jwt,
                    sid=client.sip_sid,
                    gt_uri=gt_uri,
                    display_name=display_name,
                )
                _LOGGER.info(
                    "Starting monitor call for %s (%s) attempt %d/5",
                    gate_id, gt_uri, attempt + 1,
                )
                result = await asyncio.to_thread(call.start)
                if not result.get("ok"):
                    _LOGGER.error(
                        "Monitor call start failed for %s: %s", gate_id, result
                    )
                    if attempt < 4:
                        # 486 Busy Here / 100 Trying w/o final response: the
                        # gate may still be clearing the previous session.
                        # Back off longer than the app's session hold time so
                        # the device can release the busy state.
                        await asyncio.sleep(10 + attempt * 10 if attempt < 3 else 15 + (attempt - 2) * 15)
                        continue
                    return None
                # wait for actual video RTP (SPS/PPS); retry on failure.
                # North gate / some devices take >10s to stream media after
                # the 200 OK, so wait longer than the old 10s.
                got_video = await asyncio.to_thread(call.wait_for_video, 20.0)
                if got_video:
                    self._sessions[gate_id] = call
                    return call
                _LOGGER.warning(
                    "No video media for %s, tearing down and retrying", gate_id
                )
                await asyncio.to_thread(call.stop)
                if attempt < 4:
                    await asyncio.sleep(10 + attempt * 10 if attempt < 3 else 15 + (attempt - 2) * 15)
            return None

    def add_subscriber(self, gate_id: str, s: socket.socket, target: tuple) -> bool:
        sess = self._sessions.get(gate_id)
        if sess is None:
            return False
        sess.add_subscriber(s, target)
        return True

    def add_tcp_subscriber(self, gate_id: str, loop, writer, channel: int = 0) -> bool:
        sess = self._sessions.get(gate_id)
        if sess is None:
            return False
        sess.add_tcp_subscriber(loop, writer, channel)
        return True

    def remove_subscriber(self, gate_id: str, s: socket.socket, target: tuple) -> None:
        sess = self._sessions.get(gate_id)
        if sess is None:
            return
        sess.remove_subscriber(s, target)
        # schedule idle stop
        loop = asyncio.get_event_loop()
        loop.call_later(
            IDLE_STOP_AFTER,
            lambda gid=gate_id: asyncio.ensure_future(self._idle_stop(gid)),
        )

    def remove_tcp_subscriber(self, gate_id: str, loop, writer, channel: int = 0) -> None:
        sess = self._sessions.get(gate_id)
        if sess is None:
            return
        sess.remove_tcp_subscriber(loop, writer, channel)
        # schedule idle stop
        loop = asyncio.get_event_loop()
        loop.call_later(
            IDLE_STOP_AFTER,
            lambda gid=gate_id: asyncio.ensure_future(self._idle_stop(gid)),
        )

    async def _idle_stop(self, gate_id: str) -> None:
        async with self._lock:
            sess = self._sessions.get(gate_id)
            if sess is None:
                return
            if sess.subscriber_count() > 0:
                return
            self._sessions.pop(gate_id, None)
        await asyncio.to_thread(sess.stop)

    async def shutdown(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for sess in sessions:
            try:
                await asyncio.to_thread(sess.stop)
            except Exception:
                pass


def _build_gt_uri(gate: dict) -> str | None:
    """Build the SIP device URI (GT-/OD-...) from gate data."""
    community_code = str(gate.get("communityCode") or "0")
    area_code = str(gate.get("areaCode") or "0")
    building_code = str(gate.get("buildingCode") or "0")
    unit_code = str(gate.get("unitCode") or "0")
    device_number = str(gate.get("deviceNumber") or "")
    device_type = str(gate.get("type") or "outdoor")
    if not device_number:
        return None
    if device_type == "wall":
        return f"GT-{community_code}-{area_code}-0-0-0-{device_number}"
    return (
        f"OD-{community_code}-{area_code}-{building_code}-{unit_code}-0-{device_number}"
    )


def _build_display_name(gate: dict) -> str:
    parts = []
    for key in ("communityName", "areaName", "buildingName", "unitName"):
        val = gate.get(key)
        if val:
            parts.append(str(val))
    return "-".join(parts) if parts else "物业宝监控"


class RtspServer:
    """Asyncio RTSP/1.0 server exposing each gate as a stream."""

    def __init__(self, manager: VideoSessionManager, host: str = RTSP_HOST, port: int = RTSP_PORT) -> None:
        self._manager = manager
        self._host = host
        self._port = port
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> bool:
        try:
            self._server = await asyncio.start_server(
                self._handle_client, self._host, self._port
            )
            _LOGGER.info("RTSP server listening on %s:%s", self._host, self._port)
            return True
        except OSError as err:
            _LOGGER.error("Failed to start RTSP server: %s", err)
            return False

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        client = RtspClientConnection(self._manager, reader, writer)
        await client.run()


class RtspClientConnection:
    """One RTSP client connection (OPTIONS/DESCRIBE/SETUP/PLAY/TEARDOWN)."""

    def __init__(self, manager: VideoSessionManager, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._manager = manager
        self._reader = reader
        self._writer = writer
        self._loop = asyncio.get_event_loop()
        self._gate_id: str | None = None
        self._rtp_sock: socket.socket | None = None
        self._client_rtp_port: int | None = None
        self._server_rtcp_sock: socket.socket | None = None
        self._playing = False
        self._session_id: str | None = None
        # RTP-over-TCP (interleaved) support
        self._tcp_transport = False
        self._channel = 0
        # leftover bytes after a parsed request (pipelined/coalesced requests)
        self._pending = b""

    async def run(self) -> None:
        try:
            while True:
                request = await self._read_request()
                if not request:
                    break
                if not await self._dispatch(request):
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            await self._teardown()
            try:
                self._writer.close()
            except Exception:
                pass

    async def _read_request(self) -> dict | None:
        header = self._pending
        self._pending = b""
        while b"\r\n\r\n" not in header:
            chunk = await self._reader.read(4096)
            if not chunk:
                return None
            header += chunk
            if len(header) > 65536:
                return None
        head, _, body = header.partition(b"\r\n\r\n")
        # Keep any bytes that follow this request for the next _read_request,
        # otherwise a pipelined request (e.g. OPTIONS + DESCRIBE coalesced in
        # one TCP segment, which ffmpeg does) would be silently dropped and
        # the client would hang waiting for a response.
        self._pending = body
        lines = head.decode("latin-1", errors="replace").split("\r\n")
        if not lines or not lines[0]:
            _LOGGER.info("RTSP empty request line from %s", self._writer.get_extra_info("peername"))
            return None
        parts = lines[0].split(" ")
        if len(parts) < 3:
            return None
        req: dict = {"method": parts[0], "uri": parts[1], "version": parts[2], "headers": {}}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                req["headers"][k.strip().lower()] = v.strip()
        return req

    async def _send(self, status: str, headers: dict, body: str = "") -> None:
        lines = [f"RTSP/1.0 {status}"]
        cseq = headers.pop("CSeq", None)
        if cseq is not None:
            lines.append(f"CSeq: {cseq}")
        for k, v in headers.items():
            lines.append(f"{k}: {v}")
        if body:
            lines.append(f"Content-Length: {len(body.encode('utf-8'))}")
        # Join the header lines, terminate the header block with exactly one
        # blank line ("\r\n\r\n"), then append the body. Using join with empty
        # trailing elements would insert an extra CRLF between the header and
        # the body, misaligning Content-Length and leaving stray bytes that
        # corrupt the next RTSP response (ffmpeg then sees CSeq 0 and aborts).
        packet = "\r\n".join(lines) + "\r\n\r\n"
        if body:
            packet += body
        self._writer.write(packet.encode("utf-8"))
        await self._writer.drain()

    async def _dispatch(self, req: dict) -> bool:
        method = req["method"].upper()
        cseq = req["headers"].get("cseq", "1")
        uri = req["uri"]
        # path after host
        path = uri.split("/", 3)[-1] if "/" in uri else uri
        gate_id = path.split("/")[0].split("?")[0]

        if method == "OPTIONS":
            _LOGGER.info("RTSP OPTIONS cseq=%s from %s", cseq, self._writer.get_extra_info("peername"))
            await self._send(
                "200 OK",
                {
                    "CSeq": cseq,
                    "Public": "OPTIONS, DESCRIBE, SETUP, TEARDOWN, PLAY",
                    "Session": "",
                },
            )
            return True

        if method == "DESCRIBE":
            self._gate_id = gate_id
            sess = await self._manager.get_or_start(gate_id)
            if sess is None:
                await self._send("404 Not Found", {"CSeq": cseq})
                return True
            # briefly wait for SPS/PPS so the SDP can carry
            # sprop-parameter-sets (better ffmpeg/go2rtc compatibility)
            for _ in range(15):
                if sess.get_sps_pps()[0] is not None:
                    break
                await asyncio.sleep(0.2)
            sdp = self._build_sdp(sess)
            await self._send(
                "200 OK",
                {"CSeq": cseq, "Content-Type": "application/sdp", "Content-Base": uri},
                sdp,
            )
            return True

        if method == "SETUP":
            transport = req["headers"].get("transport", "")
            _LOGGER.info("RTSP SETUP cseq=%s transport=%s from %s", cseq, transport[:60], self._writer.get_extra_info("peername"))
            # RTP over TCP (interleaved) - used by ffmpeg / HA stream component
            if "RTP/AVP/TCP" in transport.upper() or "interleaved=" in transport.lower():
                channel = 0
                for part in transport.split(";"):
                    if part.strip().lower().startswith("interleaved="):
                        try:
                            channel = int(part.strip().split("=")[1].split("-")[0])
                        except (ValueError, IndexError):
                            channel = 0
                self._tcp_transport = True
                self._channel = channel
                self._session_id = uuid.uuid4().hex[:16]
                await self._send(
                    "200 OK",
                    {
                        "CSeq": cseq,
                        "Session": self._session_id,
                        "Transport": f"RTP/AVP/TCP;unicast;interleaved={channel}-{channel + 1}",
                    },
                )
                return True

            client_port = None
            for part in transport.split(";"):
                if part.strip().startswith("client_port="):
                    try:
                        client_port = int(part.strip().split("=")[1].split("-")[0])
                    except (ValueError, IndexError):
                        client_port = None
            if client_port is None:
                await self._send("461 Unsupported Transport", {"CSeq": cseq})
                return True
            self._client_rtp_port = client_port
            # our RTP sender socket (bound to an ephemeral local port)
            self._rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._rtp_sock.bind(("0.0.0.0", 0))
            server_rtp_port = self._rtp_sock.getsockname()[1]
            # RTCP receiver socket (announced server_port)
            self._server_rtcp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._server_rtcp_sock.bind(("0.0.0.0", 0))
            server_rtcp_port = self._server_rtcp_sock.getsockname()[1]
            self._session_id = uuid.uuid4().hex[:16]
            await self._send(
                "200 OK",
                {
                    "CSeq": cseq,
                    "Session": self._session_id,
                    "Transport": f"RTP/AVP/UDP;unicast;client_port={client_port}-{client_port + 1};server_port={server_rtp_port}-{server_rtcp_port}",
                },
            )
            return True

        if method == "PLAY":
            if self._gate_id is None:
                await self._send("454 Session Not Found", {"CSeq": cseq})
                return True
            # Send the 200 OK FIRST so the client (ffmpeg/PyAV) sees a clean
            # RTSP response before any interleaved RTP frames arrive. If the
            # subscriber is registered first, RTP frames can be interleaved
            # into the middle of the response and the client aborts with
            # "Invalid data found when processing input".
            await self._send(
                "200 OK",
                {
                    "CSeq": cseq,
                    "Session": self._session_id,
                    "RTP-Info": f"url=rtsp://127.0.0.1:8556/{self._gate_id}/track1",
                },
            )
            if self._tcp_transport:
                self._manager.add_tcp_subscriber(
                    self._gate_id, self._loop, self._writer, self._channel
                )
                # Inject the latest SPS/PPS right after PLAY so the first IDR
                # is decodable: the device rotates parameter-set ids every
                # ~1.2s, so a client joining mid-call would otherwise see
                # "non-existing PPS" on its first frame and PyAV/HA stream
                # would abort instead of recovering.
                sess = self._manager.get_session(self._gate_id)
                if sess is not None:
                    for pkt in sess.build_paramset_packets():
                        frame = (
                            b"$"
                            + bytes([self._channel])
                            + len(pkt).to_bytes(2, "big")
                            + pkt
                        )
                        self._writer.write(frame)
                    await self._writer.drain()
            elif self._rtp_sock is not None and self._client_rtp_port is not None:
                peer = self._writer.get_extra_info("peername")
                client_ip = peer[0] if peer else "127.0.0.1"
                self._manager.add_subscriber(
                    self._gate_id, self._rtp_sock, (client_ip, self._client_rtp_port)
                )
            else:
                await self._send("454 Session Not Found", {"CSeq": cseq})
                return True
            self._playing = True
            return True

        if method == "TEARDOWN":
            await self._teardown()
            await self._send("200 OK", {"CSeq": cseq, "Session": self._session_id})
            return False

        await self._send("405 Method Not Allowed", {"CSeq": cseq})
        return True

    def _build_sdp(self, sess: SipMonitorCall) -> str:
        lines = [
            "v=0",
            "o=- 1 1 IN IP4 127.0.0.1",
            "s=WuYeBao Live",
            "t=0 0",
            "m=video 0 RTP/AVP 97",
            "c=IN IP4 127.0.0.1",
            "a=rtpmap:97 H264/90000",
        ]
        sps, pps = sess.get_sps_pps()
        # Always advertise packetization-mode=1 so ffmpeg/PyAV parses
        # FU-A fragments correctly, even if SPS/PPS have not arrived yet.
        fmtp = "a=fmtp:97 packetization-mode=1"
        if sps and len(sps) > 4:
            # profile-level-id is stable per codec level; omit
            # sprop-parameter-sets on purpose: the cached SPS/PPS can come
            # from a different call/session and referencing the wrong IDs
            # makes ffmpeg fail with "non-existing PPS xx referenced".
            # ffmpeg extracts parameter sets from the RTP stream itself.
            fmtp += f";profile-level-id={sps[1:4].hex()}"
        lines.append(fmtp)
        lines.append("a=control:track1")
        return "\r\n".join(lines) + "\r\n"

    async def _teardown(self) -> None:
        if self._tcp_transport:
            if self._gate_id is not None:
                self._manager.remove_tcp_subscriber(
                    self._gate_id, self._loop, self._writer, self._channel
                )
        elif self._gate_id and self._rtp_sock is not None and self._client_rtp_port is not None:
            peer = self._writer.get_extra_info("peername")
            client_ip = peer[0] if peer else "127.0.0.1"
            self._manager.remove_subscriber(
                self._gate_id, self._rtp_sock, (client_ip, self._client_rtp_port)
            )
        if self._rtp_sock is not None:
            try:
                self._rtp_sock.close()
            except OSError:
                pass
            self._rtp_sock = None
        if self._server_rtcp_sock is not None:
            try:
                self._server_rtcp_sock.close()
            except OSError:
                pass
            self._server_rtcp_sock = None
