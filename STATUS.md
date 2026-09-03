# Download Manager — DEVLOG

Working log for the continuous v1 push. Newest entries at the bottom.
SPEC.md is the contract. Reference PNGs are the visual target.
Where they conflict, SPEC wins (owner directive).

## 2026-09-03 — Baseline audit (codex greenfield drop)

Unzipped `downloadmanager.zip` (38 files) to `/srv/repos/downloadmanager/`.
Old `download-manager/`, `download-manager-redux/`, `download-manager.zip` untouched.

What exists:
- Frontend (`src/`): manager, rows, inspector (Overview/Network/Media/Files/Log),
  Settings tab (6 pages), Add Download window, extension popup surface,
  tray surface, notifications surface. Mock adapter with 14 sample jobs
  (`VITE_DATA_MODE=mock`). Real adapter via Tauri commands. Custom SVG icons.
- Rust core (`src-tauri/src/main.rs`, ~1100 lines): single instance, SQLite,
  provisional acquisition, whole-object multi-connection with range validation,
  single-stream fallback, HLS/DASH segments (`media.rs` + unit tests),
  FFmpeg finalize/mux, bandwidth throttle, retry, pause/resume, reattach,
  tray, notifications, native-host stdio protocol (--native-host, --capture,
  --policy), Windows registry registration + Run key.
- Missing: `extension/` source entirely (package.json + tauri.conf expect it).
  No git repo, no .gitignore. No STATUS file.

Reference-vs-SPEC conflicts found (SPEC wins, code already follows SPEC):
- Reference Add Download has "Add to List" → SPEC §7.2: no backlog action. Code omits it. Correct.
- Reference General has "Silent downloads" + "Default add-download behavior",
  Browser Integration has "When takeover succeeds" → SPEC §11.3: Add window
  always shows, no silent takeover. Code omits them. Correct.
- Reference sidebar has "Pending" → SPEC §10.1: Paused, no Pending. Code uses Paused. Correct.
- Reference Notifications has 3rd toggle (silent-download toasts) → SPEC §11.5: 2 toggles. Code has 2. Correct.
- Reference Network has bandwidth usage chart → SPEC §11.4: only if backed by
  real data. Code shows honest "not recorded" state. Acceptable; real session
  counters are a later improvement, never a fake chart.

Cross-platform directive (owner, 2026-09-03): product must work on Linux too,
not Windows-only. Windows-first, Linux must not be broken or stubbed.
Consequences: XDG paths, Linux native-host manifests (~/.config/*NativeMessagingHosts),
Linux autostart (.desktop), no Windows-only types in core logic.

## 2026-09-03 — Baseline commit

- Added `.gitignore`, this file. `git init`, baseline commit.
- Toolchains on infra-vps: node v22, npm 10, rustc/cargo 1.98. No git repo before this.

## 2026-09-03 — Visual dev loop repair (.env files)

- Defect: `npm run dev` (`vite --mode mock`) never selected the mock adapter —
  no `.env.mock` existed, so `VITE_DATA_MODE` was undefined and the dev loop
  opened the "core unavailable" screen. The SPEC §3.2 iteration loop was dead
  on arrival.
- Fix: `.env.mock` (mock), `.env.native` (native), `.env.production` (native).
  Production can never silently fall back to mock either.
- Verified: `npx tsc -b` clean, vite serves 200 on :4173.

## 2026-09-03 — Extension scaffold (was entirely missing)

- `build:extension`, `build:all`, tauri `resources: ../extension/dist`, and
  `tsconfig.node.json` all referenced `extension/`, which did not exist.
- Added minimal MV3 scaffold, no framework, matching the native-host protocol
  already in main.rs (`get-policy`, `update-policy`, `capture-acquisition`,
  `media-capture`, `open-manager`):
  - `manifest.json` (storage, downloads, webRequest, `<all_urls>`).
  - `background.ts`: policy cache in chrome.storage, observe-only
    `downloads.onCreated` fallback (forwards intent, NEVER cancels the browser
    download — destroying one-use transactions is worse than a duplicate;
    interim until the M0 pre-browser proof), bounded media-traffic ring buffer
    for blob:/MSE resolution, message router.
  - `content.ts`: hover/playing player picker, single anchored Download button
    following layout via rAF, stale-button cleanup, exclusion respect.
    Fixed my own tangled rAF loop before first run (loop/track split).
  - `popup.html/ts`: two toggles, site state, exclude/enable, open manager.
  - `vite.config.ts`: multi-entry build into `extension/dist/` + manifest copy
    plugin. Fixed wrong outDir (first build landed in `dist/extension/`).
- `DownloadItem.tabId` does not exist in @types/chrome — removed.
- Verified: `tsc -b` clean, `build:extension` outputs 6 files incl. manifest.

## 2026-09-03 — UI fidelity pass (browser screenshots vs references)

- Screenshots via headless Chromium (Hermes browser driver is down: 500s from
  its localhost:9377 backend — using chrome binary directly).
- Fixed: sidebar footer clipped ("Browser inte…") — dropped shield icon,
  tightened to 9px. Now shows full "Browser integration on".
- Fixed: settings nav "Browser Integration" wrapped to two lines — nowrap.
- Added: file-type labels under row icons (ISO/MKV/ZIP/TAR/MP4), as in reference.
- False alarm: thought settings nav/content were mirrored; DOM order + fresh
  screenshot confirm nav-left/content-right. Correct per reference.
- All surfaces render: main, settings, add window, extension popup, tray,
  notifications. No blank/clipped/overlapping defects seen.

## 2026-09-03 — Cross-platform residency (Linux must work)

- `app_data_root()` is now the single resolver for the per-user data dir on
  all OSes (mirrors Tauri's own convention for the bundle id). DB, browser
  policy, and native-host registration all use it — previously the policy
  path fell back to `"."` off-Windows and setup used a different resolver.
- `default_settings()`: XDG paths on Linux (~/Downloads,
  $XDG_CACHE_HOME/download-manager/tmp). Windows paths unchanged.
- `sync_startup()`: Linux writes `~/.config/autostart/download-manager.desktop`.
- `register_native_host()`: Linux writes wrapper `.sh` + manifests for
  Chrome/Chromium/Edge/Vivaldi/Brave. Windows manifest now points at a `.cmd`
  wrapper adding `--native-host` — the bare exe never spoke the framed stdio
  protocol, so the old registration could never complete a handshake.
- `DM_EXTENSION_ID` env adds dev extension IDs to allowed_origins (unpacked
  builds get random IDs).
- Pending: `cargo test` re-run with these changes (previous run failed only on
  the then-missing extension/dist; re-running in background).

## 2026-09-03 — Fixture server + green Rust run

- `cargo test`: 7/7 pass, cross-platform edits compile clean on Linux.
  (Two earlier failures were environmental: missing extension/dist, then
  missing frontend dist — both fixed by building, not by code changes.)
- `fixtures/server.py` (stdlib only, :8901): range/no-range/slow files,
  redirects, token auth, mint-once one-use URLs (200 then 410), ETag variants,
  finite + live HLS, static/template/dynamic DASH, progressive MP4, 503/404.
  Curl-verified every shape. Deterministic seeded bytes for resume/identity.
- `npm run build:all` passes (frontend + extension).
- Full `cargo build` (Linux link against system webkit/gtk) running.

## 2026-09-03 — Extension proven in a real browser (Xvfb + CDP rig)

- Rig: `fixtures/cdp_shot.py` (CDP screenshot + evaluate via websocket-client),
  `fixtures/real.mp4` (ffmpeg testsrc, committed), `/page/video.html`.
- Fixed along the way (each reproduced, then verified):
  - MV3 content scripts cannot `import` ESM chunks (SyntaxError, extension
    disabled). Helpers inlined into content.ts; background/popup still share.
  - Policy fetch raced SW wakeup and failed silently once. Content now retries
    every 1s until the first answer, then backs off.
  - Button vanished under the cursor: hovering it un-hovers the video.
    `pick()` keeps the current player while its button is hovered.
  - rAF can be throttled to zero (observed r0). `track()` positions directly
    too; rAF is smooth-follow only, never load-bearing.
- Wedge hunt: page renderer fully wedged (frozen frames, CDP evaluate timeout,
  no input) with the full script, alive with a title-only script. Bisected
  V1→V4: neither the whole-document MutationObserver nor per-tick
  `document.title` writes wedge alone — TOGETHER they self-drive
  (write → childList mutation → observer → write …) and starve the main
  thread. Removed all debug DOM writes, kept the observer, and documented the
  idempotence discipline in a comment on `track()`.
- Test-env lesson: scrot/X captures under Xvfb do NOT show compositor
  overlays (button + marker invisible though hit-test-perfect). Chrome's own
  `Page.captureScreenshot` is authoritative — and it shows the blue Download
  button painted over the playing video. Proof: `artifacts/cdp-final.png`.
- Full app links on Linux: `src-tauri/target/debug/download-manager` (297M).

## 2026-09-03 — Real app running on Linux (native backend, fresh DB)

- Ran the actual binary under Xvfb with sandboxed HOME (`/tmp/dm-home`):
  manager renders, "Connected", empty DB, tray icon up. Screenshots in
  `artifacts/app-*.png` (X captures are fine for the app UI itself).
- Residency proof: app wrote `~/.config/autostart/download-manager.desktop`
  plus NativeMessagingHosts manifests for Chrome/Chromium/Edge/Vivaldi/Brave.
  The cross-platform registration code works for real.
- Fixed from these screenshots: empty-state "No all downloads" → "No
  downloads"; footer spacing tightened again (DejaVu is wider than Segoe).
- Tauri lesson: frontend dist is EMBEDDED at compile time (`cargo build`),
  not read from disk at runtime. Rebuild binary after every frontend change
  before re-testing the app (only `tauri dev` uses the live dev server).

## 2026-09-03 — First full end-to-end download (M1 proof on Linux)

- Manual Add URL → 8MB range file → provisional (7 live connections, real
  bytes/progress/resumable) → Download → Completed, dialog closed.
- Output `/tmp/dm-home2/Downloads/range.bin` is SHA256-IDENTICAL to the
  fixture source (`b061c26e…`). Mode A multi-connection engine verified live.
- Fixed en route:
  - Windows `\\` path joins in `start_provisional` created literal-backslash
    filenames on Linux (proven by a committed `Downloads\range.bin`). Now
    `Path::join` everywhere in core logic.
  - Add-dialog "Connections" metric said "Connecting…" for finished
    provisionals → state-aware copy.
- Test note: xdotool mouse CLICKS dispatch fine in webkit, but :hover never
  updates (same XTest quirk as Chrome). Keyboard (Tab/Space/typing) works
  everywhere — commit went through Tab×4+Space after clicks mysteriously
  missed one specific button 4 times (cause undetermined; possibly focus).
  Clicks otherwise reliable (Add URL, Start Download).
