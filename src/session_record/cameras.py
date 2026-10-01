"""Camera capture via one ffmpeg process per camera."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from .config import CameraConfig, RecordingConfig

log = logging.getLogger(__name__)


class CameraRecorder:
    """Records a single camera into a file until stop() is called."""

    def __init__(self, camera: CameraConfig, recording: RecordingConfig):
        self.camera = camera
        self.recording = recording
        self._proc: subprocess.Popen | None = None
        self._log_fh = None
        self.path: Path | None = None

    @property
    def extension(self) -> str:
        return self.camera.extension or self.recording.extension

    def command(self, path: Path) -> list[str]:
        return [
            self.recording.ffmpeg, "-hide_banner", "-nostats", "-loglevel", "warning", "-y",
            *self.camera.input_args, *self.camera.output_args, str(path),
        ]

    def start(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._log_fh = open(path.with_suffix(path.suffix + ".log"), "wb")
        kwargs: dict = {}
        if os.name == "nt":
            # Keep Ctrl+C in our console from killing ffmpeg before it finalizes the file.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        cmd = self.command(path)
        log.debug("camera %s: %s", self.camera.name, subprocess.list2cmdline(cmd))
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                      stderr=self._log_fh, **kwargs)

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def request_stop(self) -> None:
        """Ask ffmpeg to finish gracefully ('q' on stdin) so the file gets a proper trailer."""
        if self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(b"q")
            self._proc.stdin.flush()
            self._proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def wait(self) -> bool:
        """Wait for the process to exit; escalate if it hangs. Returns True if the file looks usable."""
        if self._proc is None:
            return False
        try:
            self._proc.wait(timeout=self.recording.stop_timeout)
        except subprocess.TimeoutExpired:
            log.warning("camera %s: ffmpeg did not stop in time, terminating", self.camera.name)
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        ok = self.path is not None and self.path.exists() and self.path.stat().st_size > 0
        if not ok:
            log.error("camera %s: recording failed (exit code %s), see %s.log",
                      self.camera.name, self._proc.returncode, self.path)
        self._proc = None
        return ok


def stop_all(recorders: list[CameraRecorder]) -> list[bool]:
    """Stop all recorders in parallel: signal everyone first, then wait."""
    for r in recorders:
        r.request_stop()
    return [r.wait() for r in recorders]


def list_devices_hint() -> str:
    if sys.platform == "win32":
        return "ffmpeg -hide_banner -list_devices true -f dshow -i dummy"
    if sys.platform == "darwin":
        return "ffmpeg -hide_banner -f avfoundation -list_devices true -i \"\""
    return "v4l2-ctl --list-devices   (or: ls /dev/video*)"
