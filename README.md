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

> **Status:** v0.1.0, Windows-only, under active development. Not yet production-ready.

## Features

**Browser integration**
- Intercepts ordinary browser downloads and takes them over before they enter Chromium's
  own download machinery.
- Generic media acquisition: associates a Download control with finite media currently
  being viewed, covering progressive files and segmented VOD (HLS/DASH).
- Works with Chromium-family browsers, with Google Chrome as the reference target.

**Transfer engine**
- **Mode A — whole-object multi-connection.** Splits a byte-addressable object across
  concurrent workers, validates `206` / `Content-Range` semantics rather than trusting
  `Accept-Ranges`, and preserves completed-range state for pause, resume, and restart
  recovery.
- **Mode B — ordered-segment multi-connection.** Fetches independent segments of an
  ordered sequence concurrently and finalizes them in correct media order. A first-class
  mode, not a fallback for failed byte ranges.
- **Mode C — single-stream fallback.** One healthy stream with honest reporting of the
  effective connection count.
- Mode selection is automatic and degrades gracefully from the observed source behavior.
  A configured connection count is always a maximum, never a demand to force concurrency
  the source does not support.

**Application**
- Portable release: one folder containing the executable plus an `extension` folder. No
  installer, no service, no registry entries, no native-messaging host, no child
  processes.
- State, temporary data, and the WebView2 user-data directory live under `data` beside
  the executable, so moving or renaming the folder does not move the product's state.
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

## Layout

```
src/              React UI (manager window, Add Download window)
src-tauri/        Rust core: transfer engine, media finalization, IPC, persistence
extension/        Chromium extension (background worker, popup)
fixtures/         Probe scripts and media payloads for manual verification
scripts/          Packaging
```

## Building

Prerequisites: Node.js 20+, a stable Rust toolchain with the MSVC target, and the
Windows WebView2 runtime (or a colocated fixed runtime for the portable package).

```bash
npm install
```

Vite mode selects the data adapter through `VITE_DATA_MODE`. The env files that carry it
are per-checkout and are not committed, so create them once:

```bash
printf 'VITE_DATA_MODE=mock\n'   > .env.mock        # npm run dev
printf 'VITE_DATA_MODE=native\n' > .env.native      # npm run dev:native
printf 'VITE_DATA_MODE=native\n' > .env.production  # build + preview
```

```bash
npm run dev            # UI in mock mode, browser only — no native core
npm run dev:native     # UI against the native core
npm run build          # type-check + production frontend build
npm run build:extension
npm test               # vitest
```

Build the desktop application and the portable release folder:

```bash
npm run tauri build
npm run package:portable
```

## License

MIT — see [LICENSE](LICENSE).
