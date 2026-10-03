# Download Manager

A Windows desktop download manager with a native Rust transfer engine, a Tauri + React
manager UI, and a Chromium browser extension that hands downloads from the browser to a
resident native application.

The core idea is simple: a browser action should immediately become a real native
acquisition. When you start a download, the extension notifies the resident app, a small
standalone **Add Download** window appears to confirm that the resource was actually
captured, and the desktop application owns the transfer from that point on. The manager
window is a secondary surface — it does not need to be open or focused for a download to
start.

> **Status:** v0.2.0, Windows-only, under active development. Not yet production-ready.

## Features

**Browser integration**
- Intercepts ordinary browser downloads and takes them over before they enter Chromium's
  own download machinery.
- Generic media acquisition: associates a Download control with finite media currently
  being viewed, covering progressive files and segmented VOD (HLS/DASH).
- Works with Chromium-family browsers, with Google Chrome as the reference target.
- Paired once: on first use the app shows a code and asks you to allow the browser. After
  that every message between the two is sealed with the pairing key, and the app answers
  only the extension with the pinned ID.
- A download that needs your login carries the browser's cookies for its own URLs, and
  nothing else from the browser's cookie jar.

**Transfer engine**
- **Mode A — whole-object multi-connection.** Splits a file of known size across
  concurrent workers. Range support is tested with a one-byte request, whatever the
  server's `Accept-Ranges` says, and the first response still supplies the start of the
  file. Workers write to disk as data arrives; a dropped connection continues from the
  last byte received. A range counts as done only once it is flushed to disk, so pause,
  resume and a restart after a crash pick up from what is really there.
- **Mode B — ordered-segment multi-connection.** Fetches independent segments of an
  ordered sequence (HLS/DASH) concurrently and finishes them as a regular MP4 in correct
  media order. Separate video and audio tracks are joined into one file. A first-class
  mode, not a fallback for failed byte ranges.
- **Mode C — single-stream fallback.** One healthy stream with honest reporting of the
  effective connection count. A server that does not serve byte ranges cannot be resumed
  from the middle, so a paused single-stream download starts over.
- Mode selection is automatic and degrades gracefully from the observed source behavior.
  A configured connection count is always a maximum, never a demand to force concurrency
  the source does not support.

**Application**
- Portable release: one folder containing the executable plus an `extension` folder. No
  installer, no service, no registry entries, no native-messaging host, no child
  processes.
- State and the WebView2 user-data directory live under `data` beside the executable, so
  moving or renaming the folder does not move the product's state.
- A download's partial file sits next to where the file will be saved
  (`name.<id>.part`), unless Settings › Downloads › Temporary folder names another
  place. Choosing another folder at Save moves the partial file there.
- Finished downloads keep their internet origin (the Mark of the Web), as browser
  downloads do.
- Single resident instance owning all transfer state, with a loopback bridge for the
  browser extension.
- Manager window with download list, detail inspector, manual Add URL, settings, and
  notifications.

## Not in scope

This is a focused download manager, not a media ripper. The following are deliberate
non-goals: live-stream recording, BitTorrent, scheduled queues, clipboard monitoring,
cookie or password vaults, proxy/SOCKS configuration UI, a format/quality browser,
automatic "best quality" selection, site-specific downloader plugins, and bundling
`yt-dlp` / `youtube-dl` / JDownloader. Media capture is a generic mechanism driven by the
media the browser is actually playing, never per-site rules.

## Technology

| Layer | Choice |
| --- | --- |
| Transfer engine and native core | Rust |
| Desktop shell | Tauri 2 |
| UI | React + TypeScript |
| Dev server / bundler | Vite |
| Browser extension | Chromium extension (`extension/`), TypeScript |
| Persistence | Local database beside the executable |

Rust owns the transfer engine, persistence, media finalization, and Windows integration.
The UI renders in the system WebView2 runtime, which keeps the frontend inspectable in an
ordinary Chromium browser with fast HMR instead of requiring a native rebuild for every
visual change.

## Architecture

One resident process does everything: the Tauri shell hosts the React windows and runs the
Rust core, which owns the transfer engine, the database and the loopback bridge. There are no
helper processes; media is assembled natively, without FFmpeg.

```
Chromium extension ──sealed messages──▶ loopback bridge (127.0.0.1:38217)
                                              │
React windows ◀── state-delta events ─── Rust core ──▶ transfer engine ──▶ name.<id>.part
(manager, Add, Pair)  ──commands──▶          │                                  │
                                         SQLite (WAL)                     final file + MotW
```

- **Bridge and pairing.** The bridge listens on loopback only, where any local program could
  pose as either side, so the extension pairs once: the app shows a code, you allow it, and
  the extension receives a 256-bit key. Every later message is AES-256-GCM sealed, carries a
  timestamp and a fresh nonce (replays are refused), and each answer is bound to its request.
  The app answers only the pinned extension ID.
- **Cookies.** A download that needs your login carries the browser's cookies for its own
  URLs (each range worker too) and nothing else. They are stored DPAPI-protected while the
  job needs them and erased afterwards; source URLs, referrers and request bodies in the
  database are DPAPI-protected as well.
- **State.** The core keeps the authoritative state and saves only what changed (jobs,
  settings, cookies) in one transaction, with the encryption and disk writes done outside the
  state lock. SQLite runs in WAL mode with `synchronous=FULL`. Windows receive revisioned
  `state-delta` updates instead of the whole state several times a second, which took update
  traffic during a download from about 1 MiB/s to a few KiB/s.
- **Ranged downloads.** A one-byte `Range: bytes=0-0` request decides whether a source can be
  split (only an explicit `Accept-Ranges: none` is taken at its word), and that first response
  also supplies the start of the file. Range workers stream to the part file as data arrives,
  batching writes from a 64 MiB budget per download so a hard drive is not made to seek
  between small interleaved writes. About once a second a flush claims every range written
  before it; a range is recorded as done only after that, so resume always starts from data
  that is really on disk, and a dropped connection continues from its last byte.
- **Partial files** are created next to the destination without reserving the full size up
  front: on exFAT that made Windows write zeros over the whole file first. Choosing another
  folder at Save moves the part file there.

## Layout

```
src/              React UI (manager window, Add Download window)
src-tauri/        Rust core: transfer engine, media finalization, IPC, persistence
extension/        Chromium extension (background worker, popup)
e2e/              End-to-end harness against the real app (results in e2e/results/)
fixtures/         Fixture server and media payloads used by the harness
scripts/          Packaging
```

## Building

Prerequisites: Node.js 20+, a stable Rust toolchain with the MSVC target, and the
Windows WebView2 runtime (or a colocated fixed runtime for the portable package).

```bash
npm install
```

The Vite mode picks the data source: `mock` runs the UI on sample data with no native
core; every other mode talks to the native core.

```bash
npm run dev            # UI in mock mode, browser only — no native core
npm run dev:native     # UI against the native core
npm run build          # type-check + production frontend build
npm run build:extension
```

## Verifying

There are no unit tests. Behaviour is checked end to end against the real
resident executable and a local fixture server; each run writes a JSON artifact
to `e2e/results/` with the evidence for every check and when it ran. Quit any
running Download Manager first (the bridge port must be free).

```bash
python e2e/native.py --exe src-tauri/target/release/download-manager.exe
python e2e/browser_session.py   # real Chrome session with a test page
```

Without `--exe`, `native.py` builds and runs the debug build from Cargo's
target directory. A full run takes about a minute and a half; `--only` runs
just some areas (`engine`, `bandwidth`, `bridge`, `pairing`, `cookies`, `save`,
`extension`, `temp`, `ui`, `restart`; areas that need a paired browser bring
`pairing` along), and `DM_TEST_ROOT` sets where its throwaway runtime folder
goes. The app under test runs with `DM_TEST_INSTANCE=1`: its windows open
transparent and never take the focus, so runs don't get in the way. Two things on the machine can disturb it: a
browser with the extension installed may reach the test app on the bridge port
and start its own pairing, and the Save checks use UI Automation, which hangs
while the Windows session is locked.

Build the desktop application and the portable release folder:

```bash
npm run tauri build
npm run package:portable
```

## Running a release

Load the `extension` folder as an unpacked extension (chrome://extensions, Developer mode),
then click Pair in its popup and allow the code shown by the app.

## Windows security warning

Release builds are not code-signed yet. Microsoft Defender and SmartScreen judge new
executables partly by signature and download reputation, and an unsigned app that intercepts
browser downloads, listens on a local port and writes files elsewhere on disk looks a lot like
the things they are meant to stop. So Defender may quarantine `Download Manager.exe` as a
false positive (new unsigned Tauri apps hit this often), and SmartScreen may show "Windows
protected your PC" on first launch.

If that happens and you trust the build, either click More info › Run anyway, restore the file
from Windows Security › Protection history, or add the release folder as an exclusion. Only do
this for a build you downloaded from this repository's Releases page or built yourself from
source. The binary is not altered to slip past antivirus heuristics; the intended fix is code
signing, which is planned.

## License

MIT — see [LICENSE](LICENSE).
