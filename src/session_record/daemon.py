"""OSC listener that turns song start/stop commands into takes and camera recordings."""

from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from .cameras import CameraRecorder, stop_all
from .config import Config
from .db import Database, Take

log = logging.getLogger(__name__)


def song_number_from_args(args: tuple) -> int | None:
    """First numeric argument (int, float or numeric string) is the song number."""
    for a in args:
        if isinstance(a, bool):
            continue
        if isinstance(a, (int, float)):
            return int(a)
        if isinstance(a, str) and a.strip().lstrip("-").isdigit():
            return int(a)
    return None


class Controller:
    """State machine: idle <-> recording. Thread-safe; one instance per running session.

    Every start/stop message received increments the marker counter, mirroring the Wing,
    which places one marker per message it receives from the same MIDI-to-OSC source.
    """

    def __init__(self, config: Config, db: Database, session_id: int,
                 recorder_factory: Callable = CameraRecorder, clock: Callable[[], float] = time.time,
                 forwarder: Callable[[str, tuple], None] | None = None):
        self.config = config
        self.db = db
        self.session_id = session_id
        self.recorder_factory = recorder_factory
        self.clock = clock
        self.forwarder = forwarder
        self._lock = threading.Lock()
        self._take: Take | None = None
        self._recorders: list[tuple[CameraRecorder, int]] = []
        self._finalizers: list[threading.Thread] = []

        stale = db.open_take(session_id)
        if stale is not None:  # resumed after a crash: the take was interrupted
            log.warning("closing take #%d left open by a previous run", stale.seq)
            db.close_take(stale.id, self.clock(), None)

    # OSC entry point ----------------------------------------------------------
    def handle(self, address: str, *args) -> None:
        osc = self.config.osc
        if address == osc.start_address:
            self._forward(osc.forward_start_address or address, args)
            self.song_start(song_number_from_args(args), address, args)
        elif address == osc.stop_address:
            self._forward(osc.forward_stop_address or address, args)
            self.song_stop(song_number_from_args(args), address, args)
        else:
            log.debug("ignoring OSC %s %s", address, args)

    def _forward(self, address: str, args: tuple) -> None:
        if self.forwarder is None:
            return
        try:
            self.forwarder(address, args)
        except Exception:  # never let a relay failure block recording
            log.exception("failed to forward %s to the mixer", address)

    def manual(self, action: str, song: int | None = None) -> None:
        """Start/stop triggered from the UI rather than VSTLive.

        With a mixer relay configured the command is forwarded so the Wing places a marker and the
        numbering stays in sync; otherwise the event is stored without a marker number."""
        osc = self.config.osc
        address = osc.start_address if action == "start" else osc.stop_address
        args = (song,) if song is not None else ()
        if self.forwarder is not None:
            self.handle(address, *args)
        elif action == "start":
            self.song_start(song, "manual", args, count_marker=False)
        else:
            self.song_stop(song, "manual", args, count_marker=False)

    # commands -----------------------------------------------------------------
    def song_start(self, song: int | None, address: str | None = None, args: tuple = (),
                   count_marker: bool = True) -> None:
        with self._lock:
            ts = self.clock()
            marker = self.db.next_marker(self.session_id) if count_marker else None
            note = None
            if self._take is not None and self._take.song_number == song:
                note = "duplicate start ignored (song already recording)"
            elif self._take is not None:
                note = f"implicit stop of take #{self._take.seq}"
                self._finish_take(ts, stop_marker=None)
            self.db.add_event(self.session_id, ts, "start", song, marker, address, args, note)
            if note and note.startswith("duplicate"):
                log.warning("%s: start song %s: %s", _m(marker), song, note)
                return
            self._take = self.db.create_take(self.session_id, song, ts, marker)
            log.info("%s: START take #%d song %s '%s'", _m(marker), self._take.seq, song,
                     self.config.song_name(song))
            self._start_cameras(self._take, ts)

    def song_stop(self, song: int | None = None, address: str | None = None, args: tuple = (),
                  count_marker: bool = True) -> None:
        with self._lock:
            ts = self.clock()
            marker = self.db.next_marker(self.session_id) if count_marker else None
            note = None
            if self._take is None:
                note = "stop while idle"
            elif song is not None and self._take.song_number is not None and song != self._take.song_number:
                note = f"stop for song {song} while song {self._take.song_number} recording; stopping anyway"
            self.db.add_event(self.session_id, ts, "stop", song, marker, address, args, note)
            if self._take is None:
                log.warning("%s: stop received while idle", _m(marker))
                return
            if note:
                log.warning("%s: %s", _m(marker), note)
            log.info("%s: STOP  take #%d song %s", _m(marker), self._take.seq, self._take.song_number)
            self._finish_take(ts, stop_marker=marker)

    def shutdown(self) -> None:
        with self._lock:
            if self._take is not None:
                log.info("shutting down: closing take #%d", self._take.seq)
                self._finish_take(self.clock(), stop_marker=None)
        self.wait_finalized()
        self.db.end_session(self.session_id, self.clock())

    @property
    def recording(self) -> bool:
        return self._take is not None

    @property
    def current_take(self) -> Take | None:
        return self._take

    @property
    def finalizing(self) -> int:
        """Number of takes whose camera files are still being finalized."""
        return sum(t.is_alive() for t in self._finalizers)

    def camera_states(self) -> list[tuple[str, bool]]:
        """(camera name, ffmpeg running) for the take being recorded."""
        return [(rec.camera.name, rec.is_running()) for rec, _ in list(self._recorders)]

    # internals ----------------------------------------------------------------
    def take_dir(self) -> Path:
        row = self.db.get_session(self.session_id)
        date = datetime.fromtimestamp(row["started_at"]).strftime("%Y-%m-%d_%H%M")
        return self.config.recordings_dir / f"session{self.session_id:04d}_{date}"

    def _start_cameras(self, take: Take, ts: float) -> None:
        self._recorders = []
        for cam in self.config.cameras:
            rec = self.recorder_factory(cam, self.config.recording)
            song = f"song{take.song_number:03d}" if take.song_number is not None else "song-unknown"
            path = self.take_dir() / f"take{take.seq:03d}_{song}_{_safe(cam.name)}.{rec.extension}"
            video_id = self.db.add_video(take.id, cam.name, str(path), ts)
            try:
                rec.start(path)
            except Exception:
                log.exception("camera %s failed to start", cam.name)
                self.db.finish_video(video_id, ts, "failed")
                continue
            self._recorders.append((rec, video_id))

    def _finish_take(self, ts: float, stop_marker: int | None) -> None:
        assert self._take is not None
        self.db.close_take(self._take.id, ts, stop_marker)
        recorders, self._recorders, self._take = self._recorders, [], None
        for rec, _ in recorders:
            rec.request_stop()
        # Finalising files can take a while; don't hold up the next song's start.
        t = threading.Thread(target=self._finalize, args=(recorders,), daemon=True)
        self._finalizers.append(t)
        t.start()

    def _finalize(self, recorders: list[tuple[CameraRecorder, int]]) -> None:
        results = stop_all([r for r, _ in recorders])
        done = self.clock()
        for (_, video_id), ok in zip(recorders, results):
            self.db.finish_video(video_id, done, "ok" if ok else "failed")

    def wait_finalized(self) -> None:
        for t in self._finalizers:
            t.join()
        self._finalizers = []


def _m(marker: int | None) -> str:
    return f"marker {marker}" if marker is not None else "manual (no marker)"


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


class Service:
    """OSC listener + controller running in a background thread (used by the CLI and the GUI)."""

    def __init__(self, config: Config, session_name: str | None = None, resume: bool = False):
        self.config = config
        self.session_name = session_name
        self.resume = resume
        self.db: Database | None = None
        self.controller: Controller | None = None
        self._server = None
        self._thread: threading.Thread | None = None

    @property
    def session_id(self) -> int | None:
        return self.controller.session_id if self.controller else None

    def start(self) -> None:
        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import BlockingOSCUDPServer
        from pythonosc.udp_client import SimpleUDPClient

        osc = self.config.osc
        dispatcher = Dispatcher()
        # bind first so a busy port fails before a session is created
        server = BlockingOSCUDPServer((osc.listen_host, osc.listen_port), dispatcher)

        db = Database(self.config.database)
        session_id = db.latest_session_id() if self.resume else None
        if session_id is None:
            session_id = db.create_session(self.session_name, time.time())
            log.info("new session #%d%s", session_id, f" '{self.session_name}'" if self.session_name else "")
        else:
            log.info("resuming session #%d (marker count %d)", session_id,
                     db.get_session(session_id)["marker_count"])

        forwarder = None
        if osc.forward_host:
            client = SimpleUDPClient(osc.forward_host, osc.forward_port)
            forwarder = lambda addr, args: client.send_message(addr, list(args))  # noqa: E731
            log.info("relaying start/stop to %s:%d", osc.forward_host, osc.forward_port)

        self.db = db
        self.controller = Controller(self.config, db, session_id, forwarder=forwarder)
        dispatcher.set_default_handler(self.controller.handle)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="osc-server", daemon=True)
        self._thread.start()
        log.info("listening for OSC on %s:%d  (start=%s stop=%s, %d camera(s))", osc.listen_host,
                 server.server_address[1], osc.start_address, osc.stop_address, len(self.config.cameras))

    @property
    def port(self) -> int | None:
        return self._server.server_address[1] if self._server else None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self.controller.shutdown()
        self.db.close()
        log.info("session #%d closed", self.controller.session_id)
        self._server = self._thread = None


def run(config: Config, session_name: str | None = None, resume: bool = False) -> None:
    """Run the OSC listener until Ctrl+C."""
    service = Service(config, session_name, resume)
    service.start()

    def _interrupt(signum, frame):
        raise KeyboardInterrupt
    # also finalize recordings on service stop / console window close
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _interrupt)
    try:
        while service.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        service.stop()
