"""Command line interface: `session-record <command>`."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .config import load_config


def _fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"


def cmd_run(args, config) -> int:
    from .daemon import run
    if not config.cameras:
        logging.warning("no cameras configured: only song events will be logged")
    run(config, session_name=args.name, resume=args.resume)
    return 0


def cmd_sessions(args, config) -> int:
    from .db import Database
    db = Database(config.database)
    for s in db.list_sessions():
        print(f"#{s['id']:<4} {_fmt_ts(s['started_at'])}  -> {_fmt_ts(s['ended_at'])}  "
              f"takes={s['take_count']:<3} markers={s['marker_count']:<3} {s['name'] or ''}")
    return 0


def cmd_show(args, config) -> int:
    from .db import Database
    db = Database(config.database)
    session_id = args.session or db.latest_session_id()
    s = db.get_session(session_id) if session_id else None
    if s is None:
        print("session not found", file=sys.stderr)
        return 1
    print(f"Session #{s['id']} {s['name'] or ''}  {_fmt_ts(s['started_at'])} -> {_fmt_ts(s['ended_at'])}")
    print("\nTakes:")
    for t in db.takes(session_id):
        dur = f"{t.stopped_at - t.started_at:7.1f}s" if t.stopped_at else "   open "
        print(f"  {t.seq:>3}. song {str(t.song_number):>4} {config.song_name(t.song_number):<30} "
              f"{_fmt_ts(t.started_at)} {dur}  markers {t.start_marker}-{t.stop_marker or '?'}")
        for v in db.videos(t.id):
            print(f"         [{v.status}] {v.camera}: {v.path}")
    if args.events:
        print("\nEvents (marker_seq should match the Wing marker list):")
        for e in db.events(session_id):
            print(f"  marker {e['marker_seq']:>3}  {_fmt_ts(e['ts'])}  {e['kind']:<5} song {e['song_number']}"
                  f"  {e['address'] or ''} {e['args'] or ''}  {e['note'] or ''}")
    return 0


def cmd_export(args, config) -> int:
    from .db import Database
    from .exporter import ExportError, execute, parse_time, plan_export
    db = Database(config.database)
    session_id = args.session or db.latest_session_id()
    try:
        actions = plan_export(
            config, db, session_id,
            audio_dir=Path(args.audio_dir) if args.audio_dir else None,
            out_dir=Path(args.out),
            mode=args.mode, pattern=args.pattern, match=args.match,
            sync_time=parse_time(args.sync_time) if args.sync_time else None,
            sync_marker=args.sync_marker, pre_roll=args.pre_roll, post_roll=args.post_roll,
            move=args.move, include_video=not args.no_video,
        )
        if args.dry_run:
            for a in actions:
                print(a.describe())
            print(f"\n{len(actions)} file(s) planned (dry run, nothing written)")
            return 0
        execute(actions, ffmpeg=config.recording.ffmpeg, overwrite=args.overwrite)
    except ExportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"exported {len(actions)} file(s) to {args.out}")
    return 0


def cmd_send(args, config) -> int:
    from pythonosc.udp_client import SimpleUDPClient
    host = args.host or ("127.0.0.1" if config.osc.listen_host in ("0.0.0.0", "") else config.osc.listen_host)
    port = args.port or config.osc.listen_port
    address = config.osc.start_address if args.action == "start" else config.osc.stop_address
    values = [args.song] if args.song is not None else []
    SimpleUDPClient(host, port).send_message(address, values)
    print(f"sent {address} {values} to {host}:{port}")
    return 0


def cmd_cameras(args, config) -> int:
    from .cameras import CameraRecorder, list_devices_hint
    print(f"List capture devices with:\n  {list_devices_hint()}\n")
    for cam in config.cameras:
        rec = CameraRecorder(cam, config.recording)
        print(f"[{cam.name}]\n  {subprocess.list2cmdline(rec.command(Path('OUTPUT.' + rec.extension)))}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="session-record", description=__doc__)
    p.add_argument("-c", "--config", default="config.toml", help="config file (default: config.toml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="listen for OSC song start/stop and record cameras")
    r.add_argument("--name", help="session name, e.g. 'Rehearsal before gig'")
    r.add_argument("--resume", action="store_true",
                   help="continue the latest session (keeps marker numbering after a restart)")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("sessions", help="list recorded sessions")
    s.set_defaults(func=cmd_sessions)

    sh = sub.add_parser("show", help="show takes of a session")
    sh.add_argument("session", nargs="?", type=int, help="session id (default: latest)")
    sh.add_argument("-e", "--events", action="store_true", help="also list raw events / markers")
    sh.set_defaults(func=cmd_show)

    e = sub.add_parser("export", help="copy audio + video of a session into per-song folders")
    e.add_argument("session", nargs="?", type=int, help="session id (default: latest)")
    e.add_argument("--audio-dir", "-a", help="folder with the LiveSessions multitrack WAV export")
    e.add_argument("--out", "-o", required=True, help="destination folder")
    e.add_argument("--mode", choices=["auto", "subdirs", "regex", "split"], default="auto",
                   help="how the audio export is organised (default: auto)")
    e.add_argument("--pattern", help="regex with (?P<segment>\\d+) [and (?P<track>...)] for per-song files")
    e.add_argument("--match", choices=["order", "marker"], default="order",
                   help="pair audio segments with takes by position or by Wing start-marker number")
    e.add_argument("--sync-time", help="split mode: position of the sync marker in the WAVs (HH:MM:SS.fff)")
    e.add_argument("--sync-marker", type=int, default=1, help="split mode: marker number used for sync (default 1)")
    e.add_argument("--pre-roll", type=float, default=0.0, help="split mode: seconds to include before start")
    e.add_argument("--post-roll", type=float, default=0.0, help="split mode: seconds to include after stop")
    e.add_argument("--move", action="store_true", help="move instead of copy")
    e.add_argument("--no-video", action="store_true", help="skip camera files")
    e.add_argument("--overwrite", action="store_true")
    e.add_argument("-n", "--dry-run", action="store_true", help="only print what would be done")
    e.set_defaults(func=cmd_export)

    t = sub.add_parser("send", help="send a test start/stop OSC message")
    t.add_argument("action", choices=["start", "stop"])
    t.add_argument("song", nargs="?", type=int)
    t.add_argument("--host")
    t.add_argument("--port", type=int)
    t.set_defaults(func=cmd_send)

    c = sub.add_parser("cameras", help="print the ffmpeg command used for each camera")
    c.set_defaults(func=cmd_cameras)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"config file not found: {args.config} (copy config.example.toml to start)", file=sys.stderr)
        return 2
    return args.func(args, config)


if __name__ == "__main__":
    sys.exit(main())
