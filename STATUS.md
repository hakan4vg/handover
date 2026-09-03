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

## 2026-09-03 — Segmented media e2e (Modes B + C proven)

- HLS TS VOD: manifest → 6 concurrent fragments → ordered assembly → `vod.ts`
  (container-aware rename from `vod.m3u8`), committed, SHA256-IDENTICAL to
  concatenated source segments. No FFmpeg involved (correct per SPEC §9.2).
- DASH A+V: first attempt failed honestly (my fixture 404'd the init
  segments — regex only allowed `.m4s`). Engine preserved parts and reported
  clearly. Fixed fixture to serve REAL fragmented MP4 (ffmpeg testsrc+sine,
  split into init+frags via a box parser, committed under `fixtures/media/`;
  template.mpd routes reuse the video bytes).
- Stale-segment poisoning found by the retry: old positional part-files from
  a differently-shaped manifest were trusted by existence alone, poisoning
  the mux. Fixed with `segment_identity` (FNV over track kinds + segment
  URLs, SPEC §8.8): mismatch or unknown identity wipes the segment dir and
  refetches. `SegmentState.identity` is `#[serde(default)]` so old DBs load.
- `complete_job()` helper collapsed 4 duplicated completion blocks; bakes
  in total:=downloaded when the source never advertised a size (fixes
  "Unknown size"/"0 B" on completed manifest jobs; also fixes the Rust-side
  `formatBytes(None)` display via frontend null guard).
- DASH retry on clean bytes: muxed `manifest.mp4`, ffprobe shows valid
  h264+AAC, 6s. File size displays (85 KB).
- Mode C (no-range server): single-stream fallback, byte-identical 2MB.
- Transfer matrix now fully proven live: A (ranges) + B single-track +
  B multi-track/mux + C (fallback).
- Dialog follow fix: engine renames (mpd→mp4) now propagate into the open
  Add window unless the user already edited the field (dirty flags).

## 2026-09-03 — Range pause/resume + limiter correctness slice

- Root cause found in the first shared-limiter implementation: range workers
  incremented `downloaded` as response chunks arrived, but persisted a completed
  range only after the whole range had been written. A pause could therefore
  display bytes that were still only in memory and could be counted again on
  retry. The running DB reproduced it (`downloaded` greater than the covered
  `completedRanges`).
- Fixed `range_bytes()` to pace each body chunk but leave accounting to the
  worker after its complete range is written. Added a state gate before media
  finalization, truthful range-job speed/ETA, and explicit paused ETA cleanup.
- The limiter is one token bucket shared across all workers and jobs, so a
  global limit is not multiplied by connection count or simultaneous downloads.
- Fixed settings number inputs to select their current value on click. Verified
  in a rebuilt native Linux binary: clicking the limited field and typing `100`
  persisted exactly `100` rather than appending to the default.
- Real native UI proof on Xvfb with `/tmp/dm-ui2`: at 50–100 KB/s a committed
  range job paused with `downloaded=2,097,153` and exactly
  `completedRanges: 0..2,097,152`; after killing and relaunching the app, the
  paused state and range survived. Resuming through the UI completed the file.
  Output SHA256 matched the live fixture: `70204857af3fc5eaeec3102843f9a0b58a2b8c4d8336a127db9c2aedef5c29bb`.
- Regression matrix rerun on the rebuilt binary `/tmp/dm-ui3`:
  - HLS VOD → `vod.ts`, 18,048 bytes, completed.
  - DASH audio+video → `manifest.mp4`, 87,235 bytes; ffprobe reports H.264,
    AAC, duration 6.037188 seconds.
  - no-range fallback → `no-range.bin`, 2,097,152 bytes, SHA256
    `014bd78c2370e7d3528e2439df2afb5d0d15b612e57c4140105a728b7caf26f7`,
    identical to the fixture response.
- The fixture generator now caches seeded bytes instead of recomputing from
  offset zero for every range request. Historical SHA values in earlier log
  entries refer to the previous generator; current fixtures are deterministic
  and the current values above are the authoritative proof.

## 2026-09-03 — Transfer lifecycle ownership and cancellation races

- Audit finding: detached acquisition tasks were not represented separately
  from persisted job state. Resume could launch a duplicate task, and a stale
  task could overwrite pause/cancel while inside probing, network waits,
  filesystem work, or finalization.
- Added `src-tauri/src/lifecycle.rs`: one per-job transfer owner, atomic claim
  rejection for duplicate starts, explicit abort, release-on-unwind, and the
  persisted-state gate (`connecting`/`downloading`/`finalizing` only).
- Wired `spawn_transfer()` and all start paths (provisional, retry, resume,
  bulk resume, tray resume, and startup recovery) through that owner. Pause,
  cancel, remove, and bulk pause abort before changing state. Existing slow-HLS
  streaming and aggregate limiter work is preserved.
- Added deterministic lifecycle coverage: exclusive claim/reclaim, eight-way
  concurrent claim race (one winner), aborting a pending transfer, and active
  state gating.
- Verified: focused lifecycle tests 4/4, repeated five times; full Rust suite
  11/11; `cargo build`; `npm run build`; `git diff --check`; `server.py`
  compilation; slow manifest HTTP 200/163 bytes; slow fragment HTTP
  1,048,576 bytes.
- The GUI pause/resume proof was not retried through XTEST. The isolated
  display repeatedly returned X11 `XTEST BadValue` for generated keyboard and
  click events. The supported deterministic lifecycle tests and real fixture
  probes were used instead. New `lifecycle.rs` passes rustfmt; the whole
  repository remains non-rustfmt-clean because existing compact source files
  would require an unrelated mass reformat.

## 2026-09-03 — Capture handshake tests, policy trim fix, live native-host proof, tray status

- Added `capture_tests` in `src-tauri/src/main.rs` (9 tests): capture/media
  parsing, `url` alias + trim, rejection of non-HTTP/wrong-type/missing-source,
  `--capture` arg forwarding, policy roundtrip, incomplete-policy rejection,
  reattach compatibility (query ignored, path/scheme strict), tray tooltip text.
- The policy roundtrip test failed first (real bug): `excludedSites`
  normalization lowercased but kept whitespace-only entries (`"  "` survived
  the `is_empty` filter). Fixed to trim before lowercasing; suite went green.
- Proved the extension-to-app handshake against the real compiled binary with
  an isolated HOME over Chrome native-messaging framing
  (`/tmp/dm-native-probe.py`, no GUI, no XTEST): `get-policy` returns defaults,
  unknown types rejected, invalid acquisitions rejected, a valid
  `capture-acquisition` passes validation and reports forward. PASS.
- Commit path audited against SPEC §7.2: `commit_provisional` keeps the same
  job id and temp file and never respawns work; a finished provisional
  finalizes in place, a running one keeps running into the chosen destination.
- Tray now satisfies SPEC §12's live count/speed bullet: `emit_snapshot`
  refreshes the `main-tray` tooltip on every state change
  (`tray_status_text`: idle vs `N active download(s) · speed/s`, reusing the
  real `aggregate_speed`). Menu labels stay static so the menu never rebuilds
  under the cursor. Verified API against vendored tauri 2.11.5 source
  (`set_tooltip`, `tray_by_id`).
- XTEST blocker recorded precisely: on private Xvfb `:101` the server rejects
  synthetic key and button events (`BadValue` for XTEST opcodes); on shared
  `:99` clicks dispatch but `:hover` never updates. No further XTEST loops —
  verification goes through unit tests, live-binary stdio probes, fixture
  HTTP probes, and SQLite inspection.
- Open product decision (not changed unilaterally): the extension's
  `downloads.onCreated` fallback is observe-only — it forwards the capture but
  leaves the browser download running, so a one-use/tokenized URL is consumed
  twice (browser copy succeeds, DM fetch gets 410). Real takeover
  (cancel+erase) risks destroying the one-use transaction when the DM fetch
  then fails. Needs a product call before implementation.
- Verified: full Rust suite 20/20; `cargo build`; `npm run build`
  (255.12 kB / 76.68 kB gzip); `git diff --check`; native-host probe PASS.
  `fixtures/server.py` untouched.

## 2026-09-03 — First frontend test coverage (extension URL gating)

- `npm test` (`vitest run`) previously exited 1 with "No test files found".
  Added `extension/src/shared.test.ts`: 5 tests locking `isHttp` (accepts
  http/https incl. fixture shapes; rejects blob/ftp/file/data/empty/garbage/
  relative) and `siteOf` (strips www, lowercases, keeps subdomains/ports as
  hostname only, `''` for hostname-less input) plus the `DEFAULT_POLICY`
  shape. These are the exact predicates `background.ts` uses to decide what
  gets remembered, forwarded to the native host, or blob-resolved.
- Verified: `npx vitest run` 5/5; `npx tsc -b` clean (test file typechecks
  under `tsconfig.node.json`, explicit `vitest` import resolves despite the
  restricted `types` list); `npm run build:all` clean and `extension/dist/`
  contains no test artifact (explicit rollup inputs only).

## 2026-09-03 — Aggregate limiter proven end to end (headless, no XTEST)

- New `fixtures/limiter_probe.py`: isolated HOME, boots the real binary under
  Xvfb `:99`, seeds `bandwidthLimit=1 MB/s` in its SQLite settings, starts a
  `--capture` of the 8 MiB deterministic `/file/range.bin` fixture, and polls
  the jobs table for the effective rate. No GUI input of any kind.
- Result: `1 -> 7,340,032 bytes over 7.5s = 0.98 MB/s` (RATE-ASSERT PASS —
  loopback otherwise moves >50 MB/s, so the plateau is the token bucket, not
  the network). Job reached `finalizing`; the provisional temp file is
  8,388,608 bytes byte-identical to the fixture (BYTE-IDENTITY PASS).
- This also proves the headless `--capture` → provisional → paced
  multi-connection → finalizing path works with zero interaction.
- Next SPEC gap queued: §8.6 sentence 2 — per-job limits constraining a job
  inside the global limit are still unimplemented (no per-job limit field
  anywhere in `DownloadJob`, inputs, or UI).

## 2026-09-03 — Per-job bandwidth caps (SPEC §8.6 sentence 2)

- TDD: wrote the RED tests first (`effective_rate` minimum rule, capture
  parsing of `bandwidthLimit` bps). They failed on the missing field/function
  as required, then went green with the implementation.
- Engine (`src-tauri/src/main.rs`): `DownloadJob.bandwidth_limit: Option<u64>`
  (bytes/sec, `#[serde(default)]` so pre-cap databases load as uncapped);
  `ProvisionalInput`/`CommitInput` carry it; `effective_rate()` resolves the
  binding rate as the minimum, ignoring non-positive values so zero can never
  wedge pacing. `throttle()` paces against the global bucket at the binding
  rate when a global limit exists (both constraints hold), otherwise against
  a per-job bucket shared by that job's workers (no worker-count
  multiplication). Buckets are cleaned on transfer unwind, cancel, remove,
  and cap-clear. Capture messages accept `bandwidthLimit` (positive ints only).
- Frontend: new pure `src/bandwidth.ts` (`bandwidthToBps`/`bpsToParts`/
  `sanitizeCapBps`) with 6 vitest tests; `types.ts` + both adapters pass the
  cap through; Add window Advanced section has Global/Limited + value + unit
  (mirrors the global Network settings, gated by `perDownloadOverrides`,
  prefills from the captured job); inspector Network tab shows the cap or
  "Global setting".
- Headless rate matrix against the real binary (all 8 MiB byte-identical):
  global 1 MB/s only → 0.98 MB/s (no regression from the throttle rework);
  job 0.5 + global 10 → 0.47 MB/s; job 0.5 with no global → 0.47 MB/s.
- Incidents: the parametrized probe first wrote the limit as JSON float
  `1.0`; Rust `Option<u64>` rejects floats, so settings silently fell back to
  defaults (unlimited) and the run downloaded unpaced. Real finding: any
  non-integer in settings resets the WHOLE settings to defaults. Probe fixed
  to write integers (as the real settings path always does); the silent-reset
  behavior is recorded here as a hardening candidate, not changed in this
  slice. The frontend sends integers (`Math.floor`), so the product path is
  unaffected.
- Known limitation (same convention as `maxConnections`): omitting the cap at
  commit keeps the existing value; there is no explicit "clear cap" except
  recreating the provisional. Clearing via null is a follow-up if wanted.
- Verified: Rust 22/22; vitest 11/11; `tsc -b`; `build:all`; `diff --check`.
  `fixtures/server.py` untouched.
