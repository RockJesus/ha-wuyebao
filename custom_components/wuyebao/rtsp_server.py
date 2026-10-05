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
# Idle-stop policy (seconds without RTSP clients -> BYE):
#  - IDLE_STOP_AFTER: session that HAS delivered media and is then closed.
#    A normal "stop live view" sends TEARDOWN (immediate stop); this is the
#    fallback for clients that drop TCP without TEARDOWN, so 15s is enough
#    for a transient reconnect to reuse the established call.
#  - IDLE_STOP_AFTER_WARMUP: session still waiting for the device's first
#    media (unit doors can take 10-30s to push RTP after answering INVITE).
#    The client's first DESCRIBE may time out and reconnect; keep the
#    session for 2 minutes so the reconnect reuses the warming-up call
#    instead of restarting the whole INVITE dance.
IDLE_STOP_AFTER = 15  # seconds without RTSP clients after media flowed -> BYE
IDLE_STOP_AFTER_WARMUP = 120  # seconds without clients while warming up -> BYE

# Watchdog rebuild cooldown: when a connected client exists but the media
# stream goes silent, the watchdog rebuilds the SIP session.  A stuck
# client that keeps reconnecting (misconfigured go2rtc etc.) must not
# cause an endless loop of requests, so rebuilds are rate-limited.
MAX_REBUILDS_PER_WINDOW = 4  # rebuilds allowed per window per gate
REBUILD_WINDOW = 60.0  # seconds


class VideoSessionManager:
    """Manages one SipMonitorCall per gate device id."""

    def __init__(self) -> None:
        self._sessions: dict[str, SipMonitorCall] = {}
        self._gates: dict[str, dict] = {}
        self._client: object | None = None  # WuYeBaoClient
        self._lock = asyncio.Lock()
        self._watchdog_task: asyncio.Task | None = None
        self._restarting: set[str] = set()
        self._restart_tasks: set[asyncio.Task] = set()
        # rebuild timestamps per gate (monotonic) for watchdog cooldown
        self._restart_times: dict[str, list[float]] = {}
        # DESCRIBE/PLAY background session-starts (shielded, may outlive the
        # client request); cancelled on shutdown so unload never waits on them
        self._pending_tasks: set[asyncio.Task] = set()

    def _track(self, task: asyncio.Task) -> asyncio.Task:
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return task

    def register_gate(self, gate_id: str, gate: dict, client) -> None:
        self._gates[gate_id] = {"gate": gate, "client": client}

    def get_gate(self, gate_id: str) -> dict | None:
        entry = self._gates.get(gate_id)
        return entry.get("gate") if entry else None

    def get_session(self, gate_id: str) -> SipMonitorCall | None:
        """Return the active monitor session without starting a new call."""
        return self._sessions.get(gate_id)

    async def stop_session(self, gate_id: str) -> None:
        """Immediately stop and remove the SIP session (client closed the
        live view via TEARDOWN).  Idempotent; never blocks for long."""
        sess = self._sessions.pop(gate_id, None)
        if sess is None:
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(sess.stop), 15.0)
        except Exception:
            pass

    async def get_or_start(self, gate_id: str) -> SipMonitorCall | None:
        """Return the active monitor session for gate_id, starting it if needed."""
        async with self._lock:
            return await self._start_locked(gate_id)

    async def _start_locked(self, gate_id: str) -> SipMonitorCall | None:
        """Start (or reuse) the monitor session for gate_id. Caller holds the lock."""
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

        # Outer retry backoff (seconds).  Short enough that an RTSP client
        # (go2rtc / HA player) keeps waiting instead of disconnecting and
        # reconnecting in a loop; the inner SipMonitorCall.start() already
        # retries the INVITE 8x quickly for the "busy then answers on the
        # last attempt" behaviour of the official app.
        backoffs = (3, 5, 8, 12)
        for attempt in range(5):
            # v6.6.6 (verified working on ALL gates incl. north + unit
            # doors) places a DIRECT INVITE with no activation MESSAGE.
            # The activation MESSAGE reintroduced in v6.7.5 made north gate
            # answer "486 Busy Here" / unit doors answer without pushing
            # media; direct INVITE is what the app's working flow uses.
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
            try:
                result = await asyncio.to_thread(call.start)
            except Exception as exc:  # noqa: BLE001 - never let the monitor thread die silently
                _LOGGER.error("Monitor call start EXCEPTION for %s: %s", gate_id, exc)
                result = {"ok": False, "status": 0, "error": f"EXCEPTION {exc}"}
                await asyncio.to_thread(call.stop)
            if not result.get("ok"):
                _LOGGER.error(
                    "Monitor call start failed for %s: %s", gate_id, result
                )
                if attempt < 4:
                    await asyncio.sleep(backoffs[attempt])
                    continue
                return None
            # wait for actual video RTP (SPS/PPS); retry on failure.
            # North gate / some devices take >10s to stream media after
            # the 200 OK (up to 30s observed), so wait generously: killing
            # the call here just because the device is slow would make the
            # client loop forever.
            got_video = await asyncio.to_thread(call.wait_for_video, 60.0)
            if got_video:
                self._sessions[gate_id] = call
                return call
            _LOGGER.warning(
                "No video media for %s, tearing down and retrying | %s | nals=%s",
                gate_id,
                getattr(call, "last_debug", "") or "no-debug",
                dict(getattr(call, "_nal_stats", {})),
            )
            await asyncio.to_thread(call.stop)
            if attempt < 4:
                await asyncio.sleep(backoffs[attempt])
        return None

    # ------------------------------------------------------------------
    # stream-health watchdog: gates stop pushing RTP after ~30s, which
    # freezes the picture; detect the silence and rebuild the session so
    # the live view continues without user interaction.
    # ------------------------------------------------------------------
    SILENCE_THRESHOLD = 10.0  # seconds without video RTP after media flowed -> rebuild
    FIRST_MEDIA_WAIT = 60.0  # seconds to wait for the device's FIRST media
    # before the watchdog considers the session stalled (unit doors answer
    # INVITE but can take 10-30s to push RTP)
    WATCHDOG_INTERVAL = 3.0

    def start_watchdog(self) -> None:
        """Start the background watchdog loop (idempotent)."""
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.get_event_loop().create_task(
            self._watchdog_loop()
        )

    async def stop_watchdog(self) -> None:
        """Stop the watchdog and cancel pending restart tasks.

        Must never block for long: unload waits on this and a stuck
        restart (long SIP INVITE retries holding the lock) would otherwise
        hang the whole integration removal.
        """
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await asyncio.wait_for(self._watchdog_task, 2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                pass
            self._watchdog_task = None
        for t in list(self._restart_tasks):
            t.cancel()
        self._restart_tasks.clear()

    async def _watchdog_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.WATCHDOG_INTERVAL)
                try:
                    await self._watchdog_tick()
                except Exception as err:
                    _LOGGER.debug("Watchdog tick error: %s", err)
        except asyncio.CancelledError:
            pass

    async def _watchdog_tick(self) -> None:
        async with self._lock:
            now = time.monotonic()
            stalled = []
            for gid, sess in self._sessions.items():
                if sess.subscriber_count() <= 0:
                    continue
                # A session that has never delivered media (still waiting
                # for the device's first RTP) gets a long grace period;
                # only a session that delivered media and then went silent
                # is rebuilt quickly (frozen-picture recovery).
                threshold = (
                    self.SILENCE_THRESHOLD
                    if sess.has_media()
                    else self.FIRST_MEDIA_WAIT
                )
                if not sess.is_stream_alive(threshold):
                    stalled.append((gid, threshold))
            for gid, threshold in stalled:
                if gid in self._restarting:
                    continue
                # rebuild cooldown: a client that keeps reconnecting and
                # never receives media must not trigger endless SIP calls.
                recent = [
                    t for t in self._restart_times.get(gid, [])
                    if now - t < REBUILD_WINDOW
                ]
                if len(recent) >= MAX_REBUILDS_PER_WINDOW:
                    _LOGGER.warning(
                        "Monitor stream %s silent and rebuilds rate-limited - "
                        "stopping session until the client reconnects",
                        gid,
                    )
                    sess = self._sessions.pop(gid, None)
                    if sess is not None:
                        await asyncio.to_thread(sess.stop)
                    continue
                self._restarting.add(gid)
                sess = self._sessions.pop(gid, None)
                if sess is not None:
                    _LOGGER.warning(
                        "Monitor stream %s silent for %.0fs (media=%s) - rebuilding session | %s",
                        gid, threshold, sess.has_media(),
                        getattr(sess, "last_debug", "") or "no-debug",
                    )
                    task = asyncio.ensure_future(self._restart(gid))
                    self._restart_tasks.add(task)
                    task.add_done_callback(self._restart_tasks.discard)
        # keep the set from growing unbounded across many rebuilds
        if len(self._restarting) > 64:
            self._restarting.clear()

    async def _restart(self, gate_id: str) -> None:
        """Rebuild a stalled session (runs outside the lock)."""
        task = asyncio.current_task()
        try:
            self._restart_times.setdefault(gate_id, []).append(time.monotonic())
            async with self._lock:
                sess = self._sessions.get(gate_id)
                if sess is not None:
                    await asyncio.to_thread(sess.stop)
                    self._sessions.pop(gate_id, None)
                await self._start_locked(gate_id)
        finally:
            self._restarting.discard(gate_id)
            if task is not None:
                self._restart_tasks.discard(task)

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
        # schedule idle stop: long warm-up window while the device has not
        # pushed media yet (unit doors), short window after media flowed
        delay = IDLE_STOP_AFTER if sess.has_media() else IDLE_STOP_AFTER_WARMUP
        loop = asyncio.get_event_loop()
        loop.call_later(
            delay,
            lambda gid=gate_id: asyncio.ensure_future(self._idle_stop(gid)),
        )

    def remove_tcp_subscriber(self, gate_id: str, loop, writer, channel: int = 0) -> None:
        sess = self._sessions.get(gate_id)
        if sess is None:
            return
        sess.remove_tcp_subscriber(loop, writer, channel)
        # schedule idle stop
        delay = IDLE_STOP_AFTER if sess.has_media() else IDLE_STOP_AFTER_WARMUP
        loop = asyncio.get_event_loop()
        loop.call_later(
            delay,
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
        """Tear down all sessions. Never blocks the config-entry unload:
        lock acquisition and each session stop are bounded by timeouts.
        """
        await self.stop_watchdog()
        for t in list(self._pending_tasks):
            t.cancel()
        self._pending_tasks.clear()
        sessions: list = []
        try:
            await asyncio.wait_for(self._lock.acquire(), 3.0)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "video manager lock busy during shutdown - forcing cleanup"
            )
        except Exception:
            return
        else:
            try:
                sessions = list(self._sessions.values())
                self._sessions.clear()
            finally:
                self._lock.release()
        for sess in sessions:
            try:
                await asyncio.wait_for(asyncio.to_thread(sess.stop), 15.0)
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
        # active RTSP client connections; closed forcefully on stop()
        self._connections: set[RtspClientConnection] = set()

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
            # Force-close active RTSP client connections.  wait_closed()
            # waits for EVERY connection handler to finish, and a client
            # that keeps its socket open (live monitoring playing) blocks
            # config-entry unload forever ("reload has no response").
            for conn in list(self._connections):
                conn.abort()
            self._connections.clear()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 5.0)
            except asyncio.TimeoutError:
                _LOGGER.warning("RTSP server did not stop within 5s - forcing")
            self._server = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        client = RtspClientConnection(self._manager, reader, writer)
        self._connections.add(client)
        try:
            await client.run()
        finally:
            self._connections.discard(client)


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

    def abort(self) -> None:
        """Force-close the client socket (used on server stop).  The blocked
        read in run() raises IncompleteReadError and teardown runs."""
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
            if self._manager.get_gate(gate_id) is None:
                await self._send("404 Not Found", {"CSeq": cseq})
                return True
            # Bounded wait: for unit doors establishing the SIP call can take
            # 30-60s, longer than most RTSP clients wait for DESCRIBE.  We
            # give the session 25s here; if it is not ready, still answer 200
            # with the (possibly empty) SDP so the client proceeds to
            # SETUP/PLAY - the session keeps building in the background (the
            # task is shielded so the timeout does NOT cancel it) and the
            # client's automatic reconnect (go2rtc/ffmpeg) will find it
            # already established (IDLE_STOP_AFTER keeps it alive).
            task = self._manager._track(
                asyncio.ensure_future(self._manager.get_or_start(gate_id))
            )
            try:
                sess = await asyncio.wait_for(asyncio.shield(task), 25.0)
            except asyncio.TimeoutError:
                sess = None
                _LOGGER.warning(
                    "DESCRIBE %s: session still starting, answering empty SDP",
                    gate_id,
                )
            # briefly wait for SPS/PPS so the SDP can carry
            # sprop-parameter-sets (better ffmpeg/go2rtc compatibility)
            if sess is not None:
                for _ in range(15):
                    if sess.get_sps_pps()[0] is not None:
                        break
                    await asyncio.sleep(0.2)
            sdp = self._build_sdp(sess) if sess is not None else self._empty_sdp()
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
            # If the SIP session is still being established (DESCRIBE may
            # have answered early with an empty SDP), wait up to 25s for it
            # before subscribing.  Slow unit doors can take this long after
            # the 200 OK to push video; the bounded wait (shielded - the
            # establishment continues in the background if we time out) plus
            # the client's reconnect keeps the flow alive.
            sess = self._manager.get_session(self._gate_id)
            if sess is None:
                task = self._manager._track(
                    asyncio.ensure_future(
                        self._manager.get_or_start(self._gate_id)
                    )
                )
                try:
                    sess = await asyncio.wait_for(asyncio.shield(task), 25.0)
                except asyncio.TimeoutError:
                    sess = None
                if sess is None:
                    _LOGGER.warning(
                        "PLAY %s: session not established after 25s", self._gate_id
                    )
                    await self._send("454 Session Not Found", {"CSeq": cseq})
                    return True
            if self._tcp_transport:
                self._manager.add_tcp_subscriber(
                    self._gate_id, self._loop, self._writer, self._channel
                )
                # Inject the latest SPS/PPS right after PLAY so the first IDR
                # is decodable: the device rotates parameter-set ids every
                # ~1.2s, so a client joining mid-call would otherwise see
                # "non-existing PPS" on its first frame and PyAV/HA stream
                # would abort instead of recovering.
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
            # Explicit "stop live view": tear the SIP session down right
            # away so no requests continue after the user closed the view.
            if self._gate_id is not None:
                sess = self._manager.get_session(self._gate_id)
                if sess is not None and sess.subscriber_count() == 0:
                    await self._manager.stop_session(self._gate_id)
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
        # Advertise packetization-mode=1 plus sprop-parameter-sets.  The
        # SPS/PPS are extracted from this session's own RTP stream
        # (in-band bare NALs preferred, STAP-A as fallback), so the IDs
        # match what the stream references.  Pre-setting them lets ffmpeg
        # decode immediately even if it connects after the stream's first
        # parameter sets have already passed.
        fmtp = "a=fmtp:97 packetization-mode=1"
        if sps and len(sps) > 4:
            fmtp += f";profile-level-id={sps[1:4].hex()}"
            if pps and len(pps) > 3:
                fmtp += (
                    f";sprop-parameter-sets={base64.b64encode(sps).decode()},"
                    f"{base64.b64encode(pps).decode()}"
                )
        lines.append(fmtp)
        lines.append("a=control:track1")
        return "\r\n".join(lines) + "\r\n"

    def _empty_sdp(self) -> str:
        """Minimal SDP for a session that is still establishing."""
        return "\r\n".join([
            "v=0",
            "o=- 1 1 IN IP4 127.0.0.1",
            "s=WuYeBao Live",
            "t=0 0",
            "m=video 0 RTP/AVP 97",
            "c=IN IP4 127.0.0.1",
            "a=rtpmap:97 H264/90000",
            "a=fmtp:97 packetization-mode=1",
            "a=control:track1",
        ]) + "\r\n"

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
