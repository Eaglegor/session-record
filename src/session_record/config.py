"""Configuration loading (TOML)."""

from __future__ import annotations

import csv
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class OscConfig:
    listen_host: str = "0.0.0.0"
    listen_port: int = 9000
    start_address: str = "/rehearsal/song/start"
    stop_address: str = "/rehearsal/song/stop"
    # Optional relay: re-send every start/stop message to the Wing, for setups
    # where the Midi-to-OSC plugin can only target a single host.
    forward_host: str | None = None
    forward_port: int = 2223
    forward_start_address: str | None = None
    forward_stop_address: str | None = None


@dataclass
class CameraConfig:
    name: str
    input_args: list[str]
    output_args: list[str] = field(default_factory=lambda: ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])
    extension: str | None = None


@dataclass
class RecordingConfig:
    ffmpeg: str = "ffmpeg"
    extension: str = "mkv"
    stop_timeout: float = 10.0


@dataclass
class ExportConfig:
    take_folder: str = "{seq:02d} - {song}{take_suffix}"
    audio_file: str = "{song} - {track}"
    video_file: str = "{song} - {camera}"


@dataclass
class Config:
    base_dir: Path
    database: Path
    recordings_dir: Path
    osc: OscConfig = field(default_factory=OscConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    cameras: list[CameraConfig] = field(default_factory=list)
    songs: dict[int, str] = field(default_factory=dict)

    def song_name(self, number: int | None) -> str:
        if number is None:
            return "Unknown song"
        return self.songs.get(number, f"Song {number}")


def _load_songs_csv(path: Path) -> dict[int, str]:
    songs: dict[int, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if not row or not row[0].strip() or row[0].lstrip().startswith("#"):
                continue
            try:
                number = int(row[0])
            except ValueError:
                continue  # header line
            songs[number] = ",".join(row[1:]).strip()
    return songs


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    base = path.parent

    def resolve(p: str) -> Path:
        q = Path(p).expanduser()
        return q if q.is_absolute() else base / q

    storage = raw.get("storage", {})
    songs: dict[int, str] = {}
    if "songs_file" in raw:
        songs.update(_load_songs_csv(resolve(raw["songs_file"])))
    songs.update({int(k): str(v) for k, v in raw.get("songs", {}).items()})

    return Config(
        base_dir=base,
        database=resolve(storage.get("database", "rehearsals.db")),
        recordings_dir=resolve(storage.get("recordings_dir", "recordings")),
        osc=OscConfig(**raw.get("osc", {})),
        recording=RecordingConfig(**raw.get("recording", {})),
        export=ExportConfig(**raw.get("export", {})),
        cameras=[CameraConfig(**c) for c in raw.get("cameras", [])],
        songs=songs,
    )
