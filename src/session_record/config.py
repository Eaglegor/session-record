"""Configuration loading (TOML)."""

from __future__ import annotations

import csv
import shutil
import sys
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
    source: Path | None = None
    songs_file: Path | None = None

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


def app_dir() -> Path:
    """Folder of the frozen .exe, or of the package when running from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def resolve_tool(name: str, base: Path) -> str:
    """Use an explicit path as is; otherwise prefer PATH, then a copy next to the config or the app."""
    if Path(name).parent != Path(".") or shutil.which(name):
        return name
    for folder in (base, app_dir(), app_dir() / "ffmpeg", base / "ffmpeg"):
        for candidate in (folder / name, folder / f"{name}.exe", folder / "bin" / f"{name}.exe"):
            if candidate.is_file():
                return str(candidate)
    return name


EXAMPLE_CONFIG = Path(__file__).with_name("config.example.toml")


def write_example_config(path: str | Path) -> Path:
    path = Path(path)
    path.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def save_songs_csv(path: Path, songs: dict[int, str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["number", "name"])
        for number in sorted(songs):
            w.writerow([number, songs[number]])


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    base = path.parent

    def resolve(p: str) -> Path:
        q = Path(p).expanduser()
        return q if q.is_absolute() else base / q

    storage = raw.get("storage", {})
    # CSV wins over [songs] so the list edited in the GUI takes effect
    songs = {int(k): str(v) for k, v in raw.get("songs", {}).items()}
    songs_file = resolve(raw["songs_file"]) if "songs_file" in raw else None
    if songs_file is not None and songs_file.exists():
        songs.update(_load_songs_csv(songs_file))
    recording = RecordingConfig(**raw.get("recording", {}))
    recording.ffmpeg = resolve_tool(recording.ffmpeg, base)

    return Config(
        base_dir=base,
        database=resolve(storage.get("database", "rehearsals.db")),
        recordings_dir=resolve(storage.get("recordings_dir", "recordings")),
        osc=OscConfig(**raw.get("osc", {})),
        recording=recording,
        export=ExportConfig(**raw.get("export", {})),
        cameras=[CameraConfig(**c) for c in raw.get("cameras", [])],
        songs=songs,
        source=path,
        songs_file=songs_file,
    )
