# session-record

Utilities for distributed rehearsal recording: the Behringer Wing records the multitrack audio, a camera PC records video, and VSTLive Pro marks every song on both with one OSC command. After the rehearsal, one command collects audio and video into a folder per song, named from your song list.

```
 VSTLive Pro 3 (keys laptop)
   song MIDI track ── program change / start / stop
        │
        ▼  Steinberg Midi-to-OSC
   ┌────┴─────────────────────────────┐
   ▼                                  ▼
 Wing Rack (OSC :2223)          Camera PC: `session-record run` (OSC :9000)
  automation → place marker       ├─ start/stop ffmpeg per camera
  (record started manually)       └─ SQLite: song, timestamps, marker #

 After the session:
 LiveSessions → export multitrack WAVs ─┐
                                         ├─► session-record export → "01 - Song name/…"
 camera files + database ───────────────┘
```

## How it works

* **`session-record run`** listens for OSC on the camera PC.
  * `/rehearsal/song/start <song number>` starts one ffmpeg recording per configured camera and creates a *take* in the database.
  * `/rehearsal/song/stop [song number]` stops the cameras and closes the take. ffmpeg finalizes files in the background, so the next song can start straight away.
  * Every start or stop message increments a **marker counter**. The Wing places one marker per message too, so take *n* stores the Wing marker numbers it began and ended on. `session-record show -e` lists them next to the Wing's marker list.
  * Edge cases are recorded but never break numbering:
    * A duplicate start for the song that's already recording is ignored.
    * A start for a different song stops the current take implicitly.
    * A stop while idle is only logged.
* **`session-record export`** pairs the LiveSessions WAV export with the takes, then copies (or moves) the audio and camera files into `<out>/<NN> - <Song name>/`. Takes are named from the songs dictionary. A song played twice gets `(take 2)`.

All times come from the camera PC's clock. Only differences between timestamps are used, so it doesn't need to be synchronized with the Wing.

## Installation (camera PC)

1. Install Python ≥ 3.11 and [ffmpeg](https://ffmpeg.org/download.html), with both `ffmpeg` and `ffprobe` on `PATH` (or set `recording.ffmpeg` to the full path).
2. Install the tool:
   ```
   pip install .
   ```
3. Copy `config.example.toml` to `config.toml` and edit it:
   * **Cameras.** Each `[[cameras]]` block is passed directly to ffmpeg. `session-record cameras` prints the command used for each camera and how to list devices:
     * Windows: `ffmpeg -list_devices true -f dshow -i dummy`
     * Linux: `v4l2-ctl --list-devices`
     * IP cameras: an RTSP URL with `-c copy`.
   * **Songs.** Add them under `[songs]`, or point `songs_file` at a CSV with `number,name` rows.
4. Allow the OSC UDP port (default 9000) through the PC's firewall.

Test it without VSTLive:

```
session-record run --name "test"          # terminal 1
session-record send start 2               # terminal 2
session-record send stop 2
session-record show -e
```

## VSTLive / Wing setup

* **VSTLive.** On each song's MIDI control track, put the start command (for example a program change carrying the song number) at the beginning and the stop command at the end. In the Midi-to-OSC plugin, map them to:
  * `/rehearsal/song/start` with the song number as the first argument (int, float and numeric strings are all accepted);
  * `/rehearsal/song/stop`, where the song number is optional.

  Send them to the camera PC (`<pc-ip>:9000`) and to the Wing (`<wing-ip>:2223`). You can change the addresses in `[osc]`.
* **Only one OSC target possible?** Send everything to the camera PC and set `osc.forward_host` to the Wing's IP. The tool relays every start/stop message to the Wing, rewriting the address if `forward_*_address` is set. Don't also send the same messages directly to the Wing, or you'll get double markers.
* **Wing.** Configure the automation so that the incoming messages place a marker in the running recording.
* **Camera warm-up.** Cameras need around 0.5–2 s to open, so put the start command a bar before the song really begins.

## Rehearsal workflow

1. Start recording on the Wing manually.
2. On the camera PC, run `session-record run --name "2026-10-01 rehearsal"`, and keep it running. Ctrl+C (or closing the window) finalizes any running recordings and closes the session.
   * If the tool was restarted mid-rehearsal, use `session-record run --resume` so marker numbering continues.
3. Play. Each song start/stop is logged and recorded on video.
4. Stop the Wing recording, then export the multitrack WAVs with LiveSessions.
5. Check what was captured: `session-record show -e` (latest session) or `session-record sessions`.
6. Export, with a dry run first:
   ```
   session-record export --audio-dir "D:\LiveSessions\Export" --out "D:\Rehearsals\2026-10-01" --dry-run
   session-record export --audio-dir "D:\LiveSessions\Export" --out "D:\Rehearsals\2026-10-01"
   ```

## Export modes (how the LiveSessions export is organized)

| Your export looks like | Use |
|---|---|
| One subfolder per marker range / song, WAVs inside | default (`--mode subdirs`, picked automatically). Subfolders are sorted naturally and paired with takes in order. |
| All per-song files in one folder, with the range/marker number in the file name | `--pattern "(?P<segment>\d+)_(?P<track>.+)\.wav"` (the regex must have a `segment` group; `track` is optional). Add `--match marker` when `segment` is the Wing marker number rather than a running index. |
| One full-length WAV per channel for the whole session | `--sync-time 00:03:12.500`, the position of marker 1 as shown in LiveSessions. Each take's start/stop is converted to audio positions, and every channel is cut losslessly with ffmpeg in its original sample format. Optional: `--sync-marker N`, `--pre-roll 2 --post-roll 2`. |

Other options:

* `--move`: move files instead of copying them.
* `--no-video`: skip the camera files.
* `--overwrite`: replace files that already exist.
* `session` (positional): the session to export. Defaults to the latest one.

Folder and file names come from the `[export]` templates (`{seq}`, `{song}`, `{song_number}`, `{take}`, `{take_suffix}`, `{track}`, `{camera}`). Characters that aren't allowed in Windows file names are replaced.

## Files and data

* **Video:** `recordings/sessionNNNN_<date>/takeNNN_songNNN_<camera>.mkv`, with an ffmpeg `.log` next to each file. Matroska is the default because it survives crashes and power loss.
* **Database:** `rehearsals.db` (SQLite) with these tables:
  * `sessions`;
  * `events`: every received command, with its marker number;
  * `takes`;
  * `videos`.

## Development

```
pip install -e .[test]
pytest
```

The end-to-end tests send real OSC over UDP and run real ffmpeg capture and WAV splitting. They're skipped if ffmpeg isn't installed.
