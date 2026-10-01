import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from session_record.config import CameraConfig, load_config
from session_record.daemon import Controller, song_number_from_args
from session_record.db import Database
from session_record.exporter import ExportError, execute, parse_time, plan_export


class FakeRecorder:
    """Stands in for CameraRecorder: writes a file on start, 'finalises' on wait."""
    instances = []

    def __init__(self, camera, recording):
        self.camera = camera
        self.extension = "mkv"
        self.path = None
        FakeRecorder.instances.append(self)

    def start(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video:" + self.camera.name.encode())
        self.path = path

    def request_stop(self):
        pass

    def wait(self):
        return True


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def config(tmp_path):
    cfg = tmp_path / "config.toml"
    (tmp_path / "songs.csv").write_text("number,name\n5,Smoke on the Water\n", encoding="utf-8")
    cfg.write_text(
        """
songs_file = "songs.csv"
[osc]
listen_port = 0
[[cameras]]
name = "front"
input_args = []
[[cameras]]
name = "side cam"
input_args = []
[songs]
7 = "Child in Time"
""",
        encoding="utf-8",
    )
    return load_config(cfg)


@pytest.fixture
def db(config):
    d = Database(config.database)
    yield d
    d.close()


def make_controller(config, db, clock):
    sid = db.create_session("test", clock())
    return Controller(config, db, sid, recorder_factory=FakeRecorder, clock=clock)


def test_config_songs(config):
    assert config.song_name(5) == "Smoke on the Water"
    assert config.song_name(7) == "Child in Time"
    assert config.song_name(9) == "Song 9"


def test_song_number_from_args():
    assert song_number_from_args((12,)) == 12
    assert song_number_from_args((3.0,)) == 3
    assert song_number_from_args(("x", "4")) == 4
    assert song_number_from_args(()) is None


def test_start_stop_records_takes_markers_and_videos(config, db):
    clock = Clock()
    c = make_controller(config, db, clock)
    c.handle("/rehearsal/song/start", 5)
    clock.advance(240)
    c.handle("/rehearsal/song/stop", 5)
    clock.advance(30)
    c.handle("/rehearsal/song/start", 7)
    clock.advance(300)
    c.handle("/rehearsal/song/stop")
    c.handle("/some/other/address", 1)
    c.shutdown()

    takes = db.takes(c.session_id)
    assert [(t.song_number, t.start_marker, t.stop_marker) for t in takes] == [(5, 1, 2), (7, 3, 4)]
    assert takes[0].stopped_at - takes[0].started_at == 240
    videos = db.videos(takes[1].id)
    assert [v.camera for v in videos] == ["front", "side cam"]
    assert all(v.status == "ok" for v in videos)
    assert Path(videos[1].path).name == "take002_song007_side_cam.mkv"
    assert [e["marker_seq"] for e in db.events(c.session_id)] == [1, 2, 3, 4]
    assert db.get_session(c.session_id)["ended_at"] is not None


def test_edge_cases_keep_marker_numbering(config, db):
    clock = Clock()
    c = make_controller(config, db, clock)
    c.song_stop(None)            # marker 1: stop while idle
    c.song_start(5)              # marker 2
    c.song_start(5)              # marker 3: duplicate, ignored
    clock.advance(10)
    c.song_start(7)              # marker 4: implicit stop of song 5
    clock.advance(10)
    c.song_stop(7)               # marker 5
    c.wait_finalized()
    takes = db.takes(c.session_id)
    assert [(t.song_number, t.start_marker, t.stop_marker) for t in takes] == [(5, 2, None), (7, 4, 5)]
    notes = [e["note"] for e in db.events(c.session_id)]
    assert notes[0] == "stop while idle" and notes[2].startswith("duplicate")


def test_resume_closes_stale_take(config, db):
    clock = Clock()
    c = make_controller(config, db, clock)
    c.song_start(5)
    # simulate crash: new controller on the same session
    c2 = Controller(config, db, c.session_id, recorder_factory=FakeRecorder, clock=clock)
    assert db.open_take(c.session_id) is None
    c2.song_start(7)
    c2.shutdown()
    assert db.takes(c.session_id)[1].start_marker == 2


def test_forwarding(config, db):
    config.osc.forward_host = "wing"
    sent = []
    sid = db.create_session(None, 0)
    c = Controller(config, db, sid, recorder_factory=FakeRecorder,
                   forwarder=lambda a, args: sent.append((a, args)))
    c.handle("/rehearsal/song/start", 5)
    c.handle("/rehearsal/song/stop", 5)
    c.wait_finalized()
    assert sent == [("/rehearsal/song/start", (5,)), ("/rehearsal/song/stop", (5,))]


def _session_with_two_songs_and_retake(config, db):
    clock = Clock()
    c = make_controller(config, db, clock)
    for song in (5, 7, 5):
        c.song_start(song)
        clock.advance(60)
        c.song_stop(song)
        clock.advance(20)
    c.wait_finalized()
    return c.session_id


def test_export_subdirs(config, db, tmp_path):
    sid = _session_with_two_songs_and_retake(config, db)
    audio = tmp_path / "export"
    for i in (1, 2, 3):
        d = audio / f"Range {i}"
        d.mkdir(parents=True)
        for ch in ("Ch01 Kick", "Ch02 Snare"):
            (d / f"{ch}.wav").write_bytes(b"RIFF")
    out = tmp_path / "out"
    actions = plan_export(config, db, sid, audio, out)
    execute(actions)
    files = sorted(str(p.relative_to(out)).replace("\\", "/") for p in out.rglob("*") if p.is_file())
    assert files == [
        "01 - Smoke on the Water (take 1)/Smoke on the Water - Ch01 Kick.wav",
        "01 - Smoke on the Water (take 1)/Smoke on the Water - Ch02 Snare.wav",
        "01 - Smoke on the Water (take 1)/Smoke on the Water - front.mkv",
        "01 - Smoke on the Water (take 1)/Smoke on the Water - side cam.mkv",
        "02 - Child in Time/Child in Time - Ch01 Kick.wav",
        "02 - Child in Time/Child in Time - Ch02 Snare.wav",
        "02 - Child in Time/Child in Time - front.mkv",
        "02 - Child in Time/Child in Time - side cam.mkv",
        "03 - Smoke on the Water (take 2)/Smoke on the Water - Ch01 Kick.wav",
        "03 - Smoke on the Water (take 2)/Smoke on the Water - Ch02 Snare.wav",
        "03 - Smoke on the Water (take 2)/Smoke on the Water - front.mkv",
        "03 - Smoke on the Water (take 2)/Smoke on the Water - side cam.mkv",
    ]
    with pytest.raises(ExportError):
        execute(plan_export(config, db, sid, audio, out))  # refuses to overwrite


def test_export_regex_by_marker(config, db, tmp_path):
    sid = _session_with_two_songs_and_retake(config, db)  # start markers 1, 3, 5
    audio = tmp_path / "flat"
    audio.mkdir()
    for marker in (3, 5):
        (audio / f"M{marker:02d}_Vocals.wav").write_bytes(b"RIFF")
    actions = plan_export(config, db, sid, audio, tmp_path / "o", pattern=r"M(?P<segment>\d+)_(?P<track>.+)\.wav",
                          match="marker", include_video=False)
    assert [a.dst.relative_to(tmp_path / "o").as_posix() for a in actions] == [
        "02 - Child in Time/Child in Time - Vocals.wav",
        "03 - Smoke on the Water (take 2)/Smoke on the Water - Vocals.wav",
    ]


def test_parse_time():
    assert parse_time("90") == 90
    assert parse_time("01:30.5") == 90.5
    assert parse_time("1:00:00") == 3600


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_export_split_full_session(config, db, tmp_path):
    sid = _session_with_two_songs_and_retake(config, db)  # takes at 0-60, 80-140, 160-220 s
    audio = tmp_path / "full"
    audio.mkdir()
    # 4 minute session WAV, first marker at 10 s into the recording
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "240",
                    "-c:a", "pcm_s16le", str(audio / "Ch01.wav")], check=True)
    out = tmp_path / "out"
    actions = plan_export(config, db, sid, audio, out, sync_time=10.0, include_video=False)
    assert [(a.start, a.duration) for a in actions] == [(10.0, 60.0), (90.0, 60.0), (170.0, 60.0)]
    execute(actions)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                            str(out / "02 - Child in Time" / "Child in Time - Ch01.wav")],
                           capture_output=True, text=True, check=True)
    assert abs(float(probe.stdout) - 60.0) < 0.1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_osc_end_to_end_with_real_ffmpeg(tmp_path):
    """Real UDP OSC server + real ffmpeg recording a synthetic test source."""
    from pythonosc.dispatcher import Dispatcher
    from pythonosc.osc_server import BlockingOSCUDPServer
    from pythonosc.udp_client import SimpleUDPClient

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[[cameras]]
name = "test"
input_args = ["-re", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10"]
output_args = ["-c:v", "mpeg4"]
""",
        encoding="utf-8",
    )
    config = load_config(cfg)
    db = Database(config.database)
    controller = Controller(config, db, db.create_session(None, time.time()))
    dispatcher = Dispatcher()
    dispatcher.set_default_handler(controller.handle)
    server = BlockingOSCUDPServer(("127.0.0.1", 0), dispatcher)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = SimpleUDPClient("127.0.0.1", server.server_address[1])

    client.send_message("/rehearsal/song/start", [3])
    deadline = time.time() + 5
    while not controller.recording and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(2)
    client.send_message("/rehearsal/song/stop", [3])
    deadline = time.time() + 15
    while controller.recording and time.time() < deadline:
        time.sleep(0.05)
    server.shutdown()
    server.server_close()
    controller.wait_finalized()

    take = db.takes(controller.session_id)[0]
    video = db.videos(take.id)[0]
    assert video.status == "ok"
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                            video.path], capture_output=True, text=True, check=True)
    assert float(probe.stdout) > 1.0
    db.close()
