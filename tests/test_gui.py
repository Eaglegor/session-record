"""Smoke test of the desktop GUI: listen, receive OSC, manual take, songs editor, export preview."""

import socket
import time
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture
def app(tmp_path, monkeypatch):
    from tkinter import messagebox

    from session_record import gui

    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    errors = []
    for name in ("showerror", "showwarning", "showinfo"):
        monkeypatch.setattr(messagebox, name, lambda *a, **k: errors.append(a))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: False)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[osc]\nlisten_host = "127.0.0.1"\nlisten_port = {port}\n[songs]\n2 = "Highway Star"\n',
                   encoding="utf-8")
    try:
        a = gui.App(str(cfg))
    except tk.TclError as e:  # no display available
        pytest.skip(f"Tk not usable here: {e}")
    a.errors = errors
    yield a
    if a.service is not None:
        a.service.stop()
    a.destroy()


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.02)


def test_gui_flow(app, tmp_path):
    from pythonosc.udp_client import SimpleUDPClient

    # songs editor writes a CSV and points the config at it
    app.setup_tab.num_var.set("7")
    app.setup_tab.name_var.set("Child in Time")
    app.setup_tab.set_song()
    assert "7,Child in Time" in (tmp_path / "songs.csv").read_text(encoding="utf-8")
    assert app.config_obj.source.read_text(encoding="utf-8").startswith('songs_file = "songs.csv"')

    rec = app.record_tab
    rec.toggle()
    pump(app, 0.3)
    assert app.listening, app.errors
    client = SimpleUDPClient("127.0.0.1", app.service.port)
    client.send_message("/rehearsal/song/start", [2])
    pump(app, 0.5)
    assert rec.state_lbl.cget("text").startswith("●  RECORDING")
    assert "Highway Star" in rec.song_lbl.cget("text")
    client.send_message("/rehearsal/song/stop", [2])
    pump(app, 0.5)

    rec.song_var.set("7 — Child in Time")
    rec.manual("start")
    pump(app, 0.5)
    assert "Child in Time" in rec.song_lbl.cget("text")
    rec.manual("stop")
    pump(app, 0.5)
    assert len(rec.take_tree.get_children()) == 2

    rec.toggle()
    pump(app, 1.0)
    assert not app.listening

    app.sessions_tab.refresh()
    assert len(app.sessions_tab.sess_tree.get_children()) == 1

    audio = tmp_path / "ls"
    for i in (1, 2):
        (audio / f"R{i}").mkdir(parents=True)
        (audio / f"R{i}" / "Ch01.wav").write_bytes(b"RIFF")
    et = app.export_tab
    et.refresh()
    et.audio_var.set(str(audio))
    et.out_var.set(str(tmp_path / "out"))
    et.preview()
    assert [Path(a.dst).parent.name for a in et.actions] == ["01 - Highway Star", "02 - Child in Time"]
    et.export()
    pump(app, 1.0)
    assert (tmp_path / "out" / "02 - Child in Time" / "Child in Time - Ch01.wav").exists()
    assert app.errors == []
