"""Post-session export: pair LiveSessions multitrack WAVs and camera files with takes, rename by song."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .db import Database, Take

log = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".wav", ".w64", ".bwf", ".flac", ".aif", ".aiff", ".rf64"}
_WINDOWS_RESERVED = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ExportError(Exception):
    pass


@dataclass
class Segment:
    """A group of audio files belonging to one exported range (one take)."""
    key: int
    label: str
    files: list[tuple[Path, str]] = field(default_factory=list)  # (path, track name)


@dataclass
class Action:
    kind: str  # 'copy' | 'move' | 'cut'
    src: Path
    dst: Path
    start: float | None = None
    duration: float | None = None

    def describe(self) -> str:
        if self.kind == "cut":
            dur = f"{self.duration:.2f}s" if self.duration is not None else "to end"
            return f"cut  {self.src} [{format_time(self.start or 0)} +{dur}] -> {self.dst}"
        return f"{self.kind:<4} {self.src} -> {self.dst}"


def natural_key(s: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def sanitize(name: str) -> str:
    name = _WINDOWS_RESERVED.sub("_", name).strip().rstrip(".")
    return name or "_"


def parse_time(value: str) -> float:
    """Accepts seconds ('192.5'), 'MM:SS(.fff)' or 'HH:MM:SS(.fff)'."""
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"invalid time: {value!r}")
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + float(p)
    return seconds


def format_time(seconds: float) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def _audio_files(directory: Path) -> list[Path]:
    return sorted((p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS),
                  key=lambda p: natural_key(p.name))


def _first_int(text: str) -> int | None:
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


# --- discovering segments -----------------------------------------------------

def segments_from_subdirs(audio_dir: Path) -> list[Segment]:
    dirs = sorted((d for d in audio_dir.iterdir() if d.is_dir() and _audio_files(d)),
                  key=lambda d: natural_key(d.name))
    segments = []
    for i, d in enumerate(dirs, 1):
        seg = Segment(key=i, label=d.name)
        seg.files = [(f, f.stem) for f in _audio_files(d)]
        segments.append(seg)
    return segments


def segments_from_regex(audio_dir: Path, pattern: str) -> list[Segment]:
    rx = re.compile(pattern)
    if "segment" not in rx.groupindex:
        raise ExportError("--pattern must contain a named group (?P<segment>...)")
    by_key: dict[int, Segment] = {}
    for f in sorted(audio_dir.rglob("*"), key=lambda p: natural_key(str(p))):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        m = rx.search(f.name)
        if not m:
            continue
        key = int(m.group("segment"))
        track = m.group("track") if "track" in rx.groupindex and m.group("track") else f.stem
        by_key.setdefault(key, Segment(key=key, label=str(key))).files.append((f, track))
    return [by_key[k] for k in sorted(by_key)]


def match_segments(takes: list[Take], segments: list[Segment], match: str) -> dict[int, Segment]:
    """Map take.id -> Segment, either by position or by Wing marker number."""
    result: dict[int, Segment] = {}
    if match == "order":
        if len(segments) != len(takes):
            log.warning("%d audio segment(s) but %d take(s): pairing in order, extra items are skipped",
                        len(segments), len(takes))
        for take, seg in zip(takes, segments):
            result[take.id] = seg
    elif match == "marker":
        by_key = {s.key: s for s in segments}
        for take in takes:
            seg = by_key.pop(take.start_marker, None) if take.start_marker is not None else None
            if seg is None:
                log.warning("take #%d (start marker %s): no audio segment found", take.seq, take.start_marker)
            else:
                result[take.id] = seg
        for key in sorted(by_key):
            log.warning("audio segment %s does not match any take start marker", key)
    else:
        raise ExportError(f"unknown match mode {match!r}")
    return result


# --- planning -----------------------------------------------------------------

class _Names:
    def __init__(self, config: Config, takes: list[Take]):
        self.config = config
        self.counts: dict[int | None, int] = {}
        self.totals: dict[int | None, int] = {}
        for t in takes:
            self.totals[t.song_number] = self.totals.get(t.song_number, 0) + 1

    def folder(self, take: Take) -> tuple[str, dict]:
        n = self.counts[take.song_number] = self.counts.get(take.song_number, 0) + 1
        song = sanitize(self.config.song_name(take.song_number))
        fields = {
            "seq": take.seq,
            "song": song,
            "song_number": take.song_number if take.song_number is not None else 0,
            "take": n,
            "take_suffix": f" (take {n})" if self.totals[take.song_number] > 1 else "",
        }
        return sanitize(self.config.export.take_folder.format(**fields)), fields


def plan_export(config: Config, db: Database, session_id: int, audio_dir: Path | None, out_dir: Path, *,
                mode: str = "auto", pattern: str | None = None, match: str = "order",
                sync_time: float | None = None, sync_marker: int = 1, pre_roll: float = 0.0,
                post_roll: float = 0.0, move: bool = False, include_video: bool = True) -> list[Action]:
    if db.get_session(session_id) is None:
        raise ExportError(f"session #{session_id} not found")
    takes = db.takes(session_id)
    if not takes:
        raise ExportError(f"session #{session_id} has no takes")
    copy_kind = "move" if move else "copy"

    if audio_dir is not None and mode == "auto":
        if pattern:
            mode = "regex"
        elif sync_time is not None:
            mode = "split"
        elif segments_from_subdirs(audio_dir):
            mode = "subdirs"
        else:
            raise ExportError(
                "cannot tell how the audio export is organised: no per-song subfolders found. "
                "Use --pattern for per-song files in one folder, or --sync-time for full-session files.")

    segments: dict[int, Segment] = {}
    if audio_dir is not None and mode in ("subdirs", "regex"):
        found = segments_from_subdirs(audio_dir) if mode == "subdirs" else segments_from_regex(audio_dir, pattern or "")
        if not found:
            raise ExportError(f"no audio segments found in {audio_dir} (mode {mode})")
        segments = match_segments(takes, found, match)

    sync_offset = None
    if audio_dir is not None and mode == "split":
        if sync_time is None:
            raise ExportError("split mode needs --sync-time (position of the sync marker in the audio)")
        ev = next((e for e in db.events(session_id) if e["marker_seq"] == sync_marker), None)
        if ev is None:
            raise ExportError(f"marker {sync_marker} not found in session #{session_id}")
        sync_offset = sync_time - ev["ts"]  # audio position = wall clock ts + offset
        full_tracks = _audio_files(audio_dir)
        if not full_tracks:
            raise ExportError(f"no audio files in {audio_dir}")

    names = _Names(config, takes)
    tmpl = config.export
    actions: list[Action] = []
    for take in takes:
        folder, fields = names.folder(take)
        dst_dir = out_dir / folder

        if sync_offset is not None:
            start = max(0.0, take.started_at + sync_offset - pre_roll)
            duration = None
            if take.stopped_at is not None:
                duration = take.stopped_at + sync_offset + post_roll - start
            for f in full_tracks:
                name = sanitize(tmpl.audio_file.format(**fields, track=f.stem)) + f.suffix
                actions.append(Action("cut", f, dst_dir / name, start, duration))
        elif take.id in segments:
            for f, track in segments[take.id].files:
                name = sanitize(tmpl.audio_file.format(**fields, track=track)) + f.suffix
                actions.append(Action(copy_kind, f, dst_dir / name))

        if include_video:
            for v in db.videos(take.id):
                src = Path(v.path)
                if not src.exists():
                    log.warning("take #%d camera %s: file missing (%s)", take.seq, v.camera, src)
                    continue
                if v.status != "ok":
                    log.warning("take #%d camera %s: recording marked %s, copying anyway", take.seq, v.camera, v.status)
                name = sanitize(tmpl.video_file.format(**fields, camera=v.camera)) + src.suffix
                actions.append(Action(copy_kind, src, dst_dir / name))

    seen: set[Path] = set()
    for a in actions:
        if a.dst in seen:
            raise ExportError(f"two files would be written to {a.dst}; adjust the export templates")
        seen.add(a.dst)
    return actions


def _audio_codec(ffmpeg: str, src: Path) -> str:
    """Codec of the first audio stream, so cuts are re-encoded losslessly in the same PCM format.

    (Stream copy would only cut on packet boundaries, i.e. up to ~0.2 s off.)"""
    exe = Path(ffmpeg)
    # ffprobe ships next to ffmpeg; use the sibling when ffmpeg is configured by full path
    ffprobe = str(exe.with_name("ffprobe" + exe.suffix)) if exe.parent != Path(".") else "ffprobe"
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
                          "-of", "csv=p=0", str(src)], capture_output=True, text=True, check=True)
    return out.stdout.strip() or "pcm_s24le"


def execute(actions: list[Action], ffmpeg: str = "ffmpeg", overwrite: bool = False) -> None:
    for a in actions:
        if a.dst.exists() and not overwrite:
            raise ExportError(f"{a.dst} already exists (use --overwrite)")
    codecs: dict[Path, str] = {}
    for a in actions:
        a.dst.parent.mkdir(parents=True, exist_ok=True)
        log.info(a.describe())
        if a.kind == "copy":
            shutil.copy2(a.src, a.dst)
        elif a.kind == "move":
            shutil.move(str(a.src), str(a.dst))
        elif a.kind == "cut":
            if a.src not in codecs:
                codecs[a.src] = _audio_codec(ffmpeg, a.src)
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{a.start:.3f}", "-i", str(a.src)]
            if a.duration is not None:
                cmd += ["-t", f"{a.duration:.3f}"]
            cmd += ["-map", "0:a", "-c:a", codecs[a.src]]
            if a.dst.suffix.lower() == ".wav":
                cmd += ["-rf64", "auto"]  # long multitrack sessions can exceed 4 GB
            cmd.append(str(a.dst))
            subprocess.run(cmd, check=True)
