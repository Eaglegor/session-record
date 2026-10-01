"""Desktop interface (Tkinter) for the camera PC: run the recorder, browse sessions, export."""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .cameras import CameraRecorder, list_devices
from .config import Config, load_config, save_songs_csv, write_example_config
from .daemon import Service
from .db import Database
from .exporter import Action, ExportError, execute, parse_time, plan_export

log = logging.getLogger("session_record.gui")

APP_TITLE = "Session Record"
POLL_MS = 250

COLORS = {
    "stopped": ("#6b7280", "white"),
    "idle": ("#2563eb", "white"),
    "recording": ("#dc2626", "white"),
}


# --- helpers ------------------------------------------------------------------

def settings_path() -> Path:
    base = os.environ.get("APPDATA") or os.path.join(Path.home(), ".config")
    return Path(base) / "session-record" / "gui.json"


def load_settings() -> dict:
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        p = settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def open_path(path: Path) -> None:
    """Open a file or folder with the system default application."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def fmt_ts(ts: float | None, with_date: bool = True) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put((record.levelno, self.format(record)))


def make_tree(parent, columns: list[tuple[str, str, int]], height: int = 8, **kw) -> ttk.Treeview:
    """Treeview with a vertical scrollbar; columns = [(id, heading, width)]."""
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height, **kw)
    for cid, heading, width in columns:
        tree.heading(cid, text=heading, anchor="w")
        tree.column(cid, width=width, anchor="w", stretch=width >= 200)
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    tree.frame = frame  # type: ignore[attr-defined]
    return tree


def run_in_thread(app: "App", work, done=None, error_title: str = "Error") -> None:
    """Run `work()` off the UI thread; call `done(result)` back on the UI thread."""
    def target():
        try:
            result = work()
        except Exception as e:  # noqa: BLE001
            log.debug(traceback.format_exc())
            app.call_soon(lambda: messagebox.showerror(error_title, str(e), parent=app))
            return
        if done is not None:
            app.call_soon(lambda: done(result))
    threading.Thread(target=target, daemon=True).start()


# --- main window ----------------------------------------------------------------

class App(tk.Tk):
    def __init__(self, config_path: str | None = None):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1100x760")
        self.minsize(900, 600)
        self.settings = load_settings()
        self.config_obj: Config | None = None
        self.service: Service | None = None
        self.busy = False

        base = tkfont.nametofont("TkDefaultFont")
        self.font_big = base.copy()
        self.font_big.configure(size=base.cget("size") + 12, weight="bold")
        self.font_mid = base.copy()
        self.font_mid.configure(size=base.cget("size") + 3)
        self.font_bold = base.copy()
        self.font_bold.configure(weight="bold")

        self.log_queue: queue.Queue = queue.Queue()
        self.ui_queue: queue.Queue = queue.Queue()  # callbacks from worker threads
        handler = QueueLogHandler(self.log_queue)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        path = config_path or self.settings.get("config")
        if not path and Path("config.toml").exists():
            path = "config.toml"
        if path and Path(path).exists():
            self.load_config(path)
        else:
            self.notebook.select(self.setup_tab)
            log.info("No configuration loaded. Open an existing config.toml or create one on the Setup tab.")
        self.after(POLL_MS, self.poll)

    # layout --------------------------------------------------------------------
    def _build(self) -> None:
        top = ttk.Frame(self, padding=(8, 6))
        top.pack(fill="x")
        # buttons are packed first (right side) so a long path never pushes them off-screen
        ttk.Button(top, text="Edit", command=self.edit_config).pack(side="right")
        ttk.Button(top, text="Reload", command=self.reload_config).pack(side="right", padx=4)
        ttk.Button(top, text="Open…", command=self.choose_config).pack(side="right")
        ttk.Label(top, text="Config:").pack(side="left")
        self.config_var = tk.StringVar(value="(none)")
        ttk.Label(top, textvariable=self.config_var, font=self.font_bold).pack(side="left", padx=(4, 8))

        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.notebook = ttk.Notebook(paned)
        paned.add(self.notebook, weight=4)

        self.record_tab = RecordTab(self.notebook, self)
        self.sessions_tab = SessionsTab(self.notebook, self)
        self.export_tab = ExportTab(self.notebook, self)
        self.setup_tab = SetupTab(self.notebook, self)
        self.notebook.add(self.record_tab, text="  Record  ")
        self.notebook.add(self.sessions_tab, text="  Sessions  ")
        self.notebook.add(self.export_tab, text="  Export  ")
        self.notebook.add(self.setup_tab, text="  Setup  ")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab)

        logframe = ttk.LabelFrame(paned, text="Log", padding=4)
        paned.add(logframe, weight=1)
        self.log_text = ScrolledText(logframe, height=8, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("WARNING", foreground="#b45309")
        self.log_text.tag_configure("ERROR", foreground="#dc2626")

    def _on_tab(self, _event=None) -> None:
        tab = self.notebook.nametowidget(self.notebook.select())
        if hasattr(tab, "refresh"):
            tab.refresh()

    # config ----------------------------------------------------------------------
    def choose_config(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="Open configuration",
                                          filetypes=[("TOML config", "*.toml"), ("All files", "*.*")])
        if path:
            self.load_config(path)

    def load_config(self, path: str | Path) -> bool:
        if self.listening:
            messagebox.showwarning(APP_TITLE, "Stop listening before changing the configuration.", parent=self)
            return False
        try:
            cfg = load_config(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Could not load {path}:\n\n{e}", parent=self)
            return False
        self.config_obj = cfg
        self.config_var.set(str(cfg.source))
        self.settings["config"] = str(cfg.source)
        save_settings(self.settings)
        log.info("Loaded %s: %d camera(s), %d song(s), OSC port %d", cfg.source, len(cfg.cameras),
                 len(cfg.songs), cfg.osc.listen_port)
        for tab in (self.record_tab, self.sessions_tab, self.export_tab, self.setup_tab):
            tab.on_config()
        return True

    def reload_config(self) -> None:
        if self.config_obj and self.config_obj.source:
            self.load_config(self.config_obj.source)

    def edit_config(self) -> None:
        if self.config_obj and self.config_obj.source:
            open_path(self.config_obj.source)

    def db(self) -> Database:
        assert self.config_obj is not None
        return Database(self.config_obj.database)

    # service -----------------------------------------------------------------------
    @property
    def listening(self) -> bool:
        return self.service is not None and self.service.running

    def start_listening(self, name: str | None, resume: bool) -> None:
        if self.config_obj is None:
            messagebox.showwarning(APP_TITLE, "Load a configuration first.", parent=self)
            return
        service = Service(self.config_obj, session_name=name or None, resume=resume)
        try:
            service.start()
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Cannot listen on UDP port {self.config_obj.osc.listen_port}:\n\n{e}\n\n"
                                 "Is another instance (or the CLI) already running?", parent=self)
            return
        self.service = service

    def stop_listening(self, then=None) -> None:
        service, self.service = self.service, None
        if service is None:
            if then:
                then()
            return
        self.busy = True
        log.info("Stopping… (finalizing camera files)")

        def done(_):
            self.busy = False
            self.sessions_tab.refresh()
            if then:
                then()
        run_in_thread(self, service.stop, done)

    def on_close(self) -> None:
        if self.listening:
            if not messagebox.askyesno(APP_TITLE, "The recorder is listening. Stop it (finalizing any running "
                                       "camera recordings) and quit?", parent=self):
                return
            self.stop_listening(then=self.destroy)
        else:
            self.destroy()

    def call_soon(self, fn) -> None:
        """Thread-safe: run fn on the UI thread (Tk must not be touched from workers)."""
        self.ui_queue.put(fn)

    # periodic UI update ----------------------------------------------------------
    def poll(self) -> None:
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.error("UI callback failed:\n%s", traceback.format_exc())
        while True:
            try:
                level, line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            tag = "ERROR" if level >= logging.ERROR else "WARNING" if level >= logging.WARNING else ""
            self.log_text.insert("end", line + "\n", tag)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        try:
            self.record_tab.update_status()
        except Exception:  # noqa: BLE001 - never let the poll loop die
            log.debug(traceback.format_exc())
        self.after(POLL_MS, self.poll)


# --- Record tab ---------------------------------------------------------------------

class RecordTab(ttk.Frame):
    def __init__(self, master, app: App):
        super().__init__(master, padding=10)
        self.app = app
        self._last_take_key = None

        left = ttk.Frame(self)
        left.pack(side="left", fill="y", padx=(0, 10))
        right = ttk.Frame(self)
        right.pack(side="left", fill="both", expand=True)

        # listener
        lf = ttk.LabelFrame(left, text="Session", padding=8)
        lf.pack(fill="x")
        ttk.Label(lf, text="Name").grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar(value=datetime.now().strftime("Rehearsal %Y-%m-%d"))
        self.name_entry = ttk.Entry(lf, textvariable=self.name_var, width=30)
        self.name_entry.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.resume_var = tk.BooleanVar(value=False)
        self.resume_check = ttk.Checkbutton(lf, text="Resume latest session (after a restart)",
                                            variable=self.resume_var)
        self.resume_check.grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        self.listen_btn = ttk.Button(lf, text="▶  Start listening", command=self.toggle)
        self.listen_btn.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0), ipady=6)
        self.listen_info = ttk.Label(lf, text="", foreground="#555", wraplength=280)
        self.listen_info.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        lf.columnconfigure(1, weight=1)

        # manual control
        mf = ttk.LabelFrame(left, text="Manual control", padding=8)
        mf.pack(fill="x", pady=(10, 0))
        ttk.Label(mf, text="Song").grid(row=0, column=0, sticky="w")
        self.song_var = tk.StringVar()
        self.song_combo = ttk.Combobox(mf, textvariable=self.song_var, width=28)
        self.song_combo.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        bf = ttk.Frame(mf)
        bf.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.man_start = ttk.Button(bf, text="Start song", command=lambda: self.manual("start"))
        self.man_start.pack(side="left", fill="x", expand=True)
        self.man_stop = ttk.Button(bf, text="Stop", command=lambda: self.manual("stop"))
        self.man_stop.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.manual_hint = ttk.Label(mf, text="", foreground="#555", wraplength=280)
        self.manual_hint.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        mf.columnconfigure(1, weight=1)

        # cameras
        cf = ttk.LabelFrame(left, text="Cameras", padding=8)
        cf.pack(fill="both", expand=True, pady=(10, 0))
        self.cam_tree = make_tree(cf, [("camera", "Camera", 140), ("state", "State", 120)], height=6)
        self.cam_tree.frame.pack(fill="both", expand=True)
        self.cam_tree.tag_configure("rec", foreground="#dc2626")
        self.cam_tree.tag_configure("fail", foreground="#b45309")

        # status
        sf = tk.Frame(right, bg=COLORS["stopped"][0], padx=16, pady=12)
        sf.pack(fill="x")
        self.status_frame = sf
        self.state_lbl = tk.Label(sf, text="NOT LISTENING", font=self.app.font_big, anchor="w")
        self.state_lbl.pack(fill="x")
        self.song_lbl = tk.Label(sf, text="", font=self.app.font_mid, anchor="w")
        self.song_lbl.pack(fill="x")
        self.detail_lbl = tk.Label(sf, text="", anchor="w")
        self.detail_lbl.pack(fill="x")
        self._status_labels = (self.state_lbl, self.song_lbl, self.detail_lbl)

        tf = ttk.LabelFrame(right, text="Takes in this session", padding=6)
        tf.pack(fill="both", expand=True, pady=(10, 0))
        self.take_tree = make_tree(tf, [("seq", "#", 40), ("song", "Song", 260), ("start", "Start", 90),
                                        ("dur", "Length", 70), ("markers", "Wing markers", 100),
                                        ("videos", "Video", 120)], height=10)
        self.take_tree.frame.pack(fill="both", expand=True)

        self.update_status()

    def on_config(self) -> None:
        cfg = self.app.config_obj
        self.song_combo["values"] = [f"{n} — {name}" for n, name in sorted(cfg.songs.items())]
        if cfg.osc.forward_host:
            self.manual_hint.configure(text=f"Commands are also relayed to {cfg.osc.forward_host}, "
                                            "so the Wing places markers.")
        else:
            self.manual_hint.configure(text="No mixer relay configured: manual takes get no Wing marker "
                                            "(export them with 'order' matching).")
        self._last_take_key = None
        self.update_status()

    def selected_song(self) -> int | None:
        text = self.song_var.get().strip()
        if not text:
            return None
        head = text.split("—")[0].strip()
        try:
            return int(head)
        except ValueError:
            for n, name in self.app.config_obj.songs.items():
                if name.lower() == text.lower():
                    return n
            raise ValueError(f"Unknown song: {text!r}. Pick from the list or type a number.") from None

    def toggle(self) -> None:
        if self.app.listening:
            self.app.stop_listening()
        else:
            self.app.start_listening(self.name_var.get().strip(), self.resume_var.get())
        self._last_take_key = None
        self.update_status()

    def manual(self, action: str) -> None:
        if not self.app.listening:
            return
        try:
            song = self.selected_song()
        except ValueError as e:
            messagebox.showwarning(APP_TITLE, str(e), parent=self)
            return
        if action == "start" and song is None:
            messagebox.showwarning(APP_TITLE, "Choose a song first.", parent=self)
            return
        controller = self.app.service.controller
        run_in_thread(self.app, lambda: controller.manual(action, song))

    def _set_status(self, mode: str, state: str, song: str, detail: str) -> None:
        bg, fg = COLORS[mode]
        self.status_frame.configure(bg=bg)
        for lbl, text in zip(self._status_labels, (state, song, detail)):
            lbl.configure(text=text, bg=bg, fg=fg)

    def update_status(self) -> None:
        app = self.app
        listening = app.listening
        cfg = app.config_obj
        has_cfg = cfg is not None
        busy = app.busy

        self.listen_btn.configure(text="■  Stop listening" if listening else "▶  Start listening",
                                  state="disabled" if busy or not has_cfg else "normal")
        entry_state = "disabled" if listening or busy else "normal"
        self.name_entry.configure(state=entry_state)
        self.resume_check.configure(state=entry_state)
        for b in (self.man_start, self.man_stop):
            b.configure(state="normal" if listening and not busy else "disabled")

        if not listening:
            self._set_status("stopped", "STOPPING…" if busy else "NOT LISTENING",
                             "" if has_cfg else "Load a configuration to begin",
                             f"OSC port {cfg.osc.listen_port}" if has_cfg else "")
            self.listen_info.configure(text="")
            self._fill_cameras(None)
            return

        service = app.service
        controller = service.controller
        session = service.db.get_session(service.session_id)
        self.listen_info.configure(
            text=f"Session #{service.session_id} · listening on {cfg.osc.listen_host}:{service.port}\n"
                 f"start = {cfg.osc.start_address}\nstop  = {cfg.osc.stop_address}")
        markers = session["marker_count"] if session else 0
        take = controller.current_take
        if take is not None:
            elapsed = time.time() - take.started_at
            self._set_status("recording", f"●  RECORDING   {fmt_duration(elapsed)}",
                             f"{take.song_number if take.song_number is not None else '?'} — "
                             f"{cfg.song_name(take.song_number)}",
                             f"Take #{take.seq} · started at Wing marker {take.start_marker or '–'} · "
                             f"markers so far: {markers}")
        else:
            self._set_status("idle", "WAITING FOR SONG", "Listening for VSTLive start command",
                             f"Session #{service.session_id} · markers so far: {markers}")
        self._fill_cameras(controller)

        key = (service.session_id, take.seq if take else None, markers, controller.finalizing)
        if key != self._last_take_key:
            self._last_take_key = key
            self._fill_takes(service.db, service.session_id)

    def _fill_cameras(self, controller) -> None:
        cfg = self.app.config_obj
        states = dict(controller.camera_states()) if controller and controller.recording else {}
        self.cam_tree.delete(*self.cam_tree.get_children())
        for cam in (cfg.cameras if cfg else []):
            if cam.name in states:
                running = states[cam.name]
                self.cam_tree.insert("", "end", values=(cam.name, "● recording" if running else "✖ stopped/failed"),
                                     tags=("rec" if running else "fail",))
            elif controller is not None and controller.recording:
                self.cam_tree.insert("", "end", values=(cam.name, "✖ failed to start"), tags=("fail",))
            else:
                self.cam_tree.insert("", "end", values=(cam.name, "idle"))

    def _fill_takes(self, db: Database, session_id: int) -> None:
        cfg = self.app.config_obj
        self.take_tree.delete(*self.take_tree.get_children())
        for t in db.takes(session_id):
            videos = db.videos(t.id)
            ok = sum(v.status == "ok" for v in videos)
            vtext = "recording" if t.stopped_at is None else f"{ok}/{len(videos)} ok"
            self.take_tree.insert("", 0, values=(
                t.seq, f"{t.song_number} — {cfg.song_name(t.song_number)}", fmt_ts(t.started_at, False),
                fmt_duration((t.stopped_at - t.started_at) if t.stopped_at else None),
                f"{t.start_marker or '–'} → {t.stop_marker or '–'}", vtext))


# --- Sessions tab ---------------------------------------------------------------------

class SessionsTab(ttk.Frame):
    def __init__(self, master, app: App):
        super().__init__(master, padding=10)
        self.app = app

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(bar, text="Export selected…", command=self.export_selected).pack(side="left", padx=6)
        ttk.Button(bar, text="Open recordings folder", command=self.open_recordings).pack(side="left")
        self.show_events = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Show raw events / markers", variable=self.show_events,
                        command=self.show_details).pack(side="right")

        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, pady=(8, 0))
        top = ttk.Frame(paned)
        bottom = ttk.Frame(paned)
        paned.add(top, weight=1)
        paned.add(bottom, weight=2)

        self.sess_tree = make_tree(top, [("id", "#", 50), ("name", "Name", 260), ("start", "Started", 170),
                                         ("end", "Ended", 170), ("takes", "Takes", 60),
                                         ("markers", "Markers", 70)], height=7, selectmode="browse")
        self.sess_tree.frame.pack(fill="both", expand=True)
        self.sess_tree.bind("<<TreeviewSelect>>", lambda e: self.show_details())
        self.sess_tree.bind("<Double-1>", lambda e: self.export_selected())

        self.detail_tree = make_tree(bottom, [("a", "", 60), ("b", "", 260), ("c", "", 170), ("d", "", 90),
                                              ("e", "", 110), ("f", "", 300)], height=12)
        self.detail_tree.frame.pack(fill="both", expand=True)

    def on_config(self) -> None:
        self.refresh()

    def selected_session(self) -> int | None:
        sel = self.sess_tree.selection()
        return int(sel[0]) if sel else None

    def refresh(self) -> None:
        if self.app.config_obj is None:
            return
        keep = self.selected_session()
        db = self.app.db()
        try:
            rows = db.list_sessions()
        finally:
            db.close()
        self.sess_tree.delete(*self.sess_tree.get_children())
        for s in reversed(rows):
            self.sess_tree.insert("", "end", iid=str(s["id"]), values=(
                s["id"], s["name"] or "", fmt_ts(s["started_at"]),
                fmt_ts(s["ended_at"]) or ("recording…" if self.app.service and
                                          self.app.service.session_id == s["id"] else ""),
                s["take_count"], s["marker_count"]))
        if keep is not None and self.sess_tree.exists(str(keep)):
            self.sess_tree.selection_set(str(keep))
        elif rows:
            self.sess_tree.selection_set(str(rows[-1]["id"]))
        self.show_details()

    def _headings(self, names: list[str]) -> None:
        for cid, name in zip("abcdef", names):
            self.detail_tree.heading(cid, text=name)

    def show_details(self) -> None:
        self.detail_tree.delete(*self.detail_tree.get_children())
        sid = self.selected_session()
        if sid is None or self.app.config_obj is None:
            return
        cfg = self.app.config_obj
        db = self.app.db()
        try:
            if self.show_events.get():
                self._headings(["Marker", "Command", "Time", "Song", "Address", "Note"])
                for e in db.events(sid):
                    self.detail_tree.insert("", "end", values=(
                        e["marker_seq"] or "–", e["kind"], fmt_ts(e["ts"]),
                        e["song_number"] if e["song_number"] is not None else "",
                        f"{e['address'] or ''} {e['args'] or ''}", e["note"] or ""))
            else:
                self._headings(["Take", "Song", "Start", "Length", "Wing markers", "Video files"])
                for t in db.takes(sid):
                    parent = self.detail_tree.insert("", "end", open=True, values=(
                        t.seq, f"{t.song_number} — {cfg.song_name(t.song_number)}", fmt_ts(t.started_at),
                        fmt_duration((t.stopped_at - t.started_at) if t.stopped_at else None),
                        f"{t.start_marker or '–'} → {t.stop_marker or '–'}", ""))
                    for v in db.videos(t.id):
                        self.detail_tree.insert(parent, "end", values=(
                            "", f"   [{v.status}] {v.camera}", "", "", "", v.path))
        finally:
            db.close()

    def export_selected(self) -> None:
        sid = self.selected_session()
        if sid is not None:
            self.app.export_tab.select_session(sid)
            self.app.notebook.select(self.app.export_tab)

    def open_recordings(self) -> None:
        cfg = self.app.config_obj
        if cfg:
            cfg.recordings_dir.mkdir(parents=True, exist_ok=True)
            open_path(cfg.recordings_dir)


# --- Export tab -----------------------------------------------------------------------

class ExportTab(ttk.Frame):
    MODES = {
        "auto": "Auto-detect",
        "subdirs": "One subfolder per song",
        "regex": "Per-song files in one folder (pattern)",
        "split": "Full-session WAVs (split by markers)",
    }

    def __init__(self, master, app: App):
        super().__init__(master, padding=10)
        self.app = app
        self.actions: list[Action] = []
        self._session_ids: list[int] = []

        form = ttk.Frame(self)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        r = 0

        def row(label: str) -> int:
            nonlocal r
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
            r += 1
            return r - 1

        i = row("Session")
        self.session_var = tk.StringVar()
        self.session_combo = ttk.Combobox(form, textvariable=self.session_var, state="readonly")
        self.session_combo.grid(row=i, column=1, columnspan=2, sticky="ew")

        i = row("LiveSessions export folder")
        self.audio_var = tk.StringVar(value=app.settings.get("audio_dir", ""))
        ttk.Entry(form, textvariable=self.audio_var).grid(row=i, column=1, sticky="ew")
        ttk.Button(form, text="Browse…", command=lambda: self._browse(self.audio_var, "audio_dir")).grid(
            row=i, column=2, padx=(6, 0))

        i = row("Destination folder")
        self.out_var = tk.StringVar(value=app.settings.get("out_dir", ""))
        ttk.Entry(form, textvariable=self.out_var).grid(row=i, column=1, sticky="ew")
        ttk.Button(form, text="Browse…", command=lambda: self._browse(self.out_var, "out_dir")).grid(
            row=i, column=2, padx=(6, 0))

        i = row("Audio layout")
        self.mode_var = tk.StringVar(value=self.MODES["auto"])
        mode = ttk.Combobox(form, textvariable=self.mode_var, values=list(self.MODES.values()), state="readonly")
        mode.grid(row=i, column=1, sticky="w")
        mode.bind("<<ComboboxSelected>>", lambda e: self._update_fields())

        # mode-specific options
        self.opt = ttk.Frame(form)
        self.opt.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        r += 1
        self.pattern_var = tk.StringVar(value=app.settings.get("pattern", r"(?P<segment>\d+)_(?P<track>.+)\.wav"))
        self.match_var = tk.StringVar(value="order")
        self.sync_var = tk.StringVar(value="00:00:00.000")
        self.sync_marker_var = tk.IntVar(value=1)
        self.pre_var = tk.DoubleVar(value=0.0)
        self.post_var = tk.DoubleVar(value=0.0)

        self.regex_frame = ttk.Frame(self.opt)
        ttk.Label(self.regex_frame, text="File name pattern").pack(side="left")
        ttk.Entry(self.regex_frame, textvariable=self.pattern_var, width=48).pack(side="left", padx=6)
        self.match_frame = ttk.Frame(self.opt)
        ttk.Label(self.match_frame, text="Pair audio with takes").pack(side="left")
        ttk.Radiobutton(self.match_frame, text="in order", value="order", variable=self.match_var).pack(
            side="left", padx=6)
        ttk.Radiobutton(self.match_frame, text="by Wing start-marker number", value="marker",
                        variable=self.match_var).pack(side="left")
        self.split_frame = ttk.Frame(self.opt)
        ttk.Label(self.split_frame, text="Marker").pack(side="left")
        ttk.Spinbox(self.split_frame, from_=1, to=999, width=5, textvariable=self.sync_marker_var).pack(
            side="left", padx=(4, 8))
        ttk.Label(self.split_frame, text="is at (in LiveSessions)").pack(side="left")
        ttk.Entry(self.split_frame, textvariable=self.sync_var, width=14).pack(side="left", padx=(4, 12))
        ttk.Label(self.split_frame, text="Pre-roll s").pack(side="left")
        ttk.Spinbox(self.split_frame, from_=0, to=30, increment=0.5, width=5, textvariable=self.pre_var).pack(
            side="left", padx=(4, 8))
        ttk.Label(self.split_frame, text="Post-roll s").pack(side="left")
        ttk.Spinbox(self.split_frame, from_=0, to=30, increment=0.5, width=5, textvariable=self.post_var).pack(
            side="left", padx=4)

        flags = ttk.Frame(form)
        flags.grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.video_var = tk.BooleanVar(value=True)
        self.move_var = tk.BooleanVar(value=False)
        self.overwrite_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(flags, text="Include camera video", variable=self.video_var).pack(side="left")
        ttk.Checkbutton(flags, text="Move instead of copy", variable=self.move_var).pack(side="left", padx=12)
        ttk.Checkbutton(flags, text="Overwrite existing files", variable=self.overwrite_var).pack(side="left")

        btns = ttk.Frame(self)
        btns.pack(fill="x", pady=8)
        self.preview_btn = ttk.Button(btns, text="Preview", command=self.preview)
        self.preview_btn.pack(side="left")
        self.export_btn = ttk.Button(btns, text="Export", command=self.export, state="disabled")
        self.export_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(btns, text="Open destination", command=self.open_out)
        self.open_btn.pack(side="left")
        self.summary = ttk.Label(btns, text="")
        self.summary.pack(side="left", padx=12)
        self.progress = ttk.Progressbar(btns, mode="determinate", length=220)
        self.progress.pack(side="right")

        self.plan_tree = make_tree(self, [("kind", "Action", 60), ("src", "From", 380), ("dst", "To", 420)],
                                   height=12)
        self.plan_tree.frame.pack(fill="both", expand=True)
        self._update_fields()

    def _browse(self, var: tk.StringVar, key: str) -> None:
        path = filedialog.askdirectory(parent=self, initialdir=var.get() or None)
        if path:
            var.set(path)
            self.app.settings[key] = path
            save_settings(self.app.settings)
            self._invalidate()

    def _mode(self) -> str:
        return next(k for k, v in self.MODES.items() if v == self.mode_var.get())

    def _update_fields(self) -> None:
        for f in (self.regex_frame, self.match_frame, self.split_frame):
            f.pack_forget()
        mode = self._mode()
        if mode == "regex":
            self.regex_frame.pack(anchor="w", pady=2)
        if mode in ("auto", "subdirs", "regex"):
            self.match_frame.pack(anchor="w", pady=2)
        if mode == "split":
            self.split_frame.pack(anchor="w", pady=2)
        self._invalidate()

    def _invalidate(self) -> None:
        self.actions = []
        self.export_btn.configure(state="disabled")

    def on_config(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        if self.app.config_obj is None:
            return
        db = self.app.db()
        try:
            rows = list(reversed(db.list_sessions()))
        finally:
            db.close()
        current = self.selected_session()
        self._session_ids = [s["id"] for s in rows]
        self.session_combo["values"] = [
            f"#{s['id']}  {fmt_ts(s['started_at'])}  {s['name'] or ''}  ({s['take_count']} takes)" for s in rows]
        if current in self._session_ids:
            self.select_session(current)
        elif rows:
            self.session_combo.current(0)

    def select_session(self, session_id: int) -> None:
        if session_id not in self._session_ids:
            self.refresh()
        if session_id in self._session_ids:
            self.session_combo.current(self._session_ids.index(session_id))
            self._invalidate()

    def selected_session(self) -> int | None:
        idx = self.session_combo.current()
        return self._session_ids[idx] if 0 <= idx < len(self._session_ids) else None

    def _plan(self) -> list[Action]:
        cfg = self.app.config_obj
        if cfg is None:
            raise ExportError("Load a configuration first.")
        sid = self.selected_session()
        if sid is None:
            raise ExportError("Choose a session.")
        if not self.out_var.get().strip():
            raise ExportError("Choose a destination folder.")
        audio = self.audio_var.get().strip()
        mode = self._mode()
        if audio and not Path(audio).is_dir():
            raise ExportError(f"Audio folder not found: {audio}")
        kwargs = dict(mode=mode, match=self.match_var.get(), include_video=self.video_var.get(),
                      move=self.move_var.get())
        if mode == "regex":
            kwargs["pattern"] = self.pattern_var.get()
            self.app.settings["pattern"] = self.pattern_var.get()
        if mode == "split":
            try:
                kwargs.update(sync_time=parse_time(self.sync_var.get()), sync_marker=int(self.sync_marker_var.get()),
                              pre_roll=float(self.pre_var.get()), post_roll=float(self.post_var.get()))
            except (ValueError, tk.TclError) as e:
                raise ExportError(f"Invalid split settings: {e}") from None
        db = self.app.db()
        try:
            return plan_export(cfg, db, sid, Path(audio) if audio else None, Path(self.out_var.get()), **kwargs)
        finally:
            db.close()

    def preview(self) -> None:
        self.plan_tree.delete(*self.plan_tree.get_children())
        try:
            self.actions = self._plan()
        except (ExportError, OSError) as e:
            self._invalidate()
            messagebox.showerror("Export", str(e), parent=self)
            return
        out = Path(self.out_var.get())
        for a in self.actions:
            try:
                dst = a.dst.relative_to(out)
            except ValueError:
                dst = a.dst
            src = a.src.name if a.kind != "cut" else (
                f"{a.src.name}  [{fmt_duration(a.start)} +{fmt_duration(a.duration) or 'end'}]")
            self.plan_tree.insert("", "end", values=(a.kind, src, str(dst)))
        n_takes = len({a.dst.parent for a in self.actions})
        self.summary.configure(text=f"{len(self.actions)} file(s) into {n_takes} song folder(s)")
        self.export_btn.configure(state="normal" if self.actions else "disabled")

    def export(self) -> None:
        if not self.actions:
            return
        cfg = self.app.config_obj
        actions = self.actions
        overwrite = self.overwrite_var.get()  # Tk variables must not be read from the worker thread
        self.export_btn.configure(state="disabled")
        self.preview_btn.configure(state="disabled")
        self.progress.configure(maximum=len(actions), value=0)

        def progress(i, total, action):
            self.app.call_soon(lambda: (self.progress.configure(value=i),
                                       self.summary.configure(text=f"{i + 1}/{total}: {action.dst.name}")))

        def work():
            execute(actions, ffmpeg=cfg.recording.ffmpeg, overwrite=overwrite, progress=progress)

        def done(_):
            self.progress.configure(value=len(actions))
            self.summary.configure(text=f"Exported {len(actions)} file(s)")
            self.preview_btn.configure(state="normal")
            self.actions = []
            log.info("Export finished: %d file(s) to %s", len(actions), self.out_var.get())
            if messagebox.askyesno("Export", "Export finished. Open the destination folder?", parent=self):
                self.open_out()

        def guarded():
            try:
                work()
            finally:
                self.app.call_soon(lambda: self.preview_btn.configure(state="normal"))
        run_in_thread(self.app, guarded, done, error_title="Export failed")

    def open_out(self) -> None:
        out = self.out_var.get().strip()
        if out and Path(out).is_dir():
            open_path(Path(out))


# --- Setup tab -----------------------------------------------------------------------

class SetupTab(ttk.Frame):
    def __init__(self, master, app: App):
        super().__init__(master, padding=10)
        self.app = app

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Create new config…", command=self.create_config).pack(side="left")
        ttk.Button(bar, text="Open config…", command=app.choose_config).pack(side="left", padx=6)
        ttk.Button(bar, text="Edit config", command=app.edit_config).pack(side="left")
        ttk.Button(bar, text="Send test OSC", command=self.test_osc).pack(side="left", padx=6)
        self.osc_info = ttk.Label(self, text="", foreground="#555", justify="left", wraplength=1000)
        self.osc_info.pack(fill="x", pady=(8, 0))

        cols = ttk.Frame(self)
        cols.pack(fill="both", expand=True, pady=(8, 0))

        # songs editor
        sf = ttk.LabelFrame(cols, text="Songs (number → name, matches the program change sent by VSTLive)",
                            padding=6)
        sf.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.song_tree = make_tree(sf, [("num", "No.", 60), ("name", "Name", 300)], height=14,
                                   selectmode="browse")
        self.song_tree.frame.pack(fill="both", expand=True)
        self.song_tree.bind("<<TreeviewSelect>>", self._song_selected)
        ef = ttk.Frame(sf)
        ef.pack(fill="x", pady=(6, 0))
        self.num_var = tk.StringVar()
        self.name_var = tk.StringVar()
        ttk.Entry(ef, textvariable=self.num_var, width=6).pack(side="left")
        name_entry = ttk.Entry(ef, textvariable=self.name_var)
        name_entry.pack(side="left", fill="x", expand=True, padx=6)
        name_entry.bind("<Return>", lambda e: self.set_song())
        ttk.Button(ef, text="Add / update", command=self.set_song).pack(side="left")
        ttk.Button(ef, text="Remove", command=self.remove_song).pack(side="left", padx=(6, 0))
        self.songs_hint = ttk.Label(sf, text="", foreground="#555", wraplength=420)
        self.songs_hint.pack(fill="x", pady=(4, 0))

        # cameras
        cf = ttk.LabelFrame(cols, text="Cameras (edit in config file)", padding=6)
        cf.pack(side="left", fill="both", expand=True)
        self.cam_tree = make_tree(cf, [("name", "Name", 110), ("cmd", "ffmpeg command", 380)], height=8,
                                  selectmode="browse")
        self.cam_tree.frame.pack(fill="both", expand=True)
        self.cam_tree.bind("<Double-1>", self._show_camera_command)
        cb = ttk.Frame(cf)
        cb.pack(fill="x", pady=(6, 0))
        ttk.Label(cf, text="Double-click a camera to see its full command.", foreground="#555").pack(anchor="w")
        ttk.Button(cb, text="List capture devices", command=self.list_devices).pack(side="left")
        ttk.Button(cb, text="Test selected camera (5 s)", command=self.test_camera).pack(side="left", padx=6)
        self.ffmpeg_info = ttk.Label(cf, text="", foreground="#555", wraplength=480)
        self.ffmpeg_info.pack(fill="x", pady=(4, 0))

    # config ------------------------------------------------------------------------
    def create_config(self) -> None:
        path = filedialog.asksaveasfilename(parent=self, title="Create configuration", initialfile="config.toml",
                                            defaultextension=".toml", filetypes=[("TOML config", "*.toml")])
        if not path:
            return
        write_example_config(path)
        if self.app.load_config(path):
            messagebox.showinfo(APP_TITLE, "An example configuration was created. Adjust the cameras "
                                "(and OSC settings) in the config file, then press Reload.", parent=self)
            self.app.edit_config()

    def on_config(self) -> None:
        cfg = self.app.config_obj
        osc = cfg.osc
        relay = f"relay to mixer {osc.forward_host}:{osc.forward_port}" if osc.forward_host else "no mixer relay"
        self.osc_info.configure(
            text=f"OSC: listening on {osc.listen_host}:{osc.listen_port}   start = {osc.start_address}   "
                 f"stop = {osc.stop_address}   ({relay})\n"
                 f"Database: {cfg.database}     Recordings: {cfg.recordings_dir}")
        self._fill_songs()
        self.cam_tree.delete(*self.cam_tree.get_children())
        for cam in cfg.cameras:
            rec = CameraRecorder(cam, cfg.recording)
            self.cam_tree.insert("", "end", iid=cam.name, values=(
                cam.name, subprocess.list2cmdline(rec.command(Path(f"OUTPUT.{rec.extension}"))[1:])))
        self.ffmpeg_info.configure(text=f"ffmpeg: {cfg.recording.ffmpeg}")
        target = cfg.songs_file or (cfg.base_dir / "songs.csv")
        self.songs_hint.configure(text=f"Changes are saved to {target}")

    def _show_camera_command(self, _e=None) -> None:
        sel = self.cam_tree.selection()
        cfg = self.app.config_obj
        if not sel or cfg is None:
            return
        cam = next(c for c in cfg.cameras if c.name == sel[0])
        rec = CameraRecorder(cam, cfg.recording)
        TextWindow(self.app, f"Camera '{cam.name}'",
                   subprocess.list2cmdline(rec.command(Path(f"OUTPUT.{rec.extension}"))))

    def _fill_songs(self) -> None:
        self.song_tree.delete(*self.song_tree.get_children())
        for n, name in sorted(self.app.config_obj.songs.items()):
            self.song_tree.insert("", "end", iid=str(n), values=(n, name))

    def _song_selected(self, _e=None) -> None:
        sel = self.song_tree.selection()
        if sel:
            n, name = self.song_tree.item(sel[0], "values")
            self.num_var.set(n)
            self.name_var.set(name)

    def _save_songs(self) -> None:
        cfg = self.app.config_obj
        target = cfg.songs_file
        if target is None:
            # point the config at a CSV next to it (top-level key must precede all tables)
            target = cfg.base_dir / "songs.csv"
            text = cfg.source.read_text(encoding="utf-8")
            cfg.source.write_text('songs_file = "songs.csv"\n' + text, encoding="utf-8")
            cfg.songs_file = target
        save_songs_csv(target, cfg.songs)
        self.app.record_tab.on_config()
        log.info("Saved %d song(s) to %s", len(cfg.songs), target)

    def set_song(self) -> None:
        if self.app.config_obj is None:
            return
        try:
            n = int(self.num_var.get())
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Song number must be an integer.", parent=self)
            return
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning(APP_TITLE, "Enter a song name.", parent=self)
            return
        self.app.config_obj.songs[n] = name
        self._save_songs()
        self._fill_songs()
        self.song_tree.selection_set(str(n))
        self.song_tree.see(str(n))

    def remove_song(self) -> None:
        sel = self.song_tree.selection()
        if not sel or self.app.config_obj is None:
            return
        self.app.config_obj.songs.pop(int(sel[0]), None)
        self._save_songs()
        self._fill_songs()

    # tools -------------------------------------------------------------------------
    def list_devices(self) -> None:
        cfg = self.app.config_obj
        ffmpeg = cfg.recording.ffmpeg if cfg else "ffmpeg"
        run_in_thread(self.app, lambda: list_devices(ffmpeg), lambda text: TextWindow(self.app, "Capture devices", text))

    def test_camera(self) -> None:
        cfg = self.app.config_obj
        sel = self.cam_tree.selection()
        if cfg is None or not sel:
            messagebox.showinfo(APP_TITLE, "Select a camera first.", parent=self)
            return
        if self.app.listening and self.app.service.controller.recording:
            messagebox.showwarning(APP_TITLE, "A take is being recorded right now.", parent=self)
            return
        cam = next(c for c in cfg.cameras if c.name == sel[0])
        rec = CameraRecorder(cam, cfg.recording)
        path = Path(tempfile.gettempdir()) / f"session-record-test-{int(time.time())}.{rec.extension}"
        log.info("Testing camera %s for 5 seconds → %s", cam.name, path)

        def work():
            rec.start(path)
            time.sleep(5)
            rec.request_stop()
            if not rec.wait():
                log_file = path.with_suffix(path.suffix + ".log")
                detail = log_file.read_text(errors="replace")[-2000:] if log_file.exists() else ""
                raise RuntimeError(f"Camera '{cam.name}' did not record.\n\n{detail}")
            return path

        def done(p):
            log.info("Camera %s OK", cam.name)
            open_path(p)
        run_in_thread(self.app, work, done, error_title="Camera test failed")

    def test_osc(self) -> None:
        cfg = self.app.config_obj
        if cfg is None:
            return
        from pythonosc.udp_client import SimpleUDPClient
        port = self.app.service.port if self.app.listening else cfg.osc.listen_port
        host = "127.0.0.1" if cfg.osc.listen_host in ("0.0.0.0", "") else cfg.osc.listen_host
        if not messagebox.askyesno(APP_TITLE, f"Send a start (song 0) and, 3 s later, a stop message to "
                                   f"{host}:{port}? This creates a short test take if listening.", parent=self):
            return
        client = SimpleUDPClient(host, port)
        client.send_message(cfg.osc.start_address, [0])
        self.after(3000, lambda: client.send_message(cfg.osc.stop_address, [0]))
        log.info("Sent test start/stop to %s:%d", host, port)


class TextWindow(tk.Toplevel):
    def __init__(self, master, title: str, text: str):
        super().__init__(master)
        self.title(title)
        self.geometry("760x480")
        box = ScrolledText(self, wrap="none")
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        box.configure(state="disabled")


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="session-record-gui")
    parser.add_argument("-c", "--config", help="config file to open")
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        try:  # crisp text on high-DPI displays
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    app = App(args.config)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
