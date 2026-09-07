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

## 2026-09-03 — Settings hardening + notification-action verdict

- Root cause from the limiter-probe incident: settings load was all-or-nothing
  (`serde_json::from_str` straight into `AppSettings`), so one mistyped value
  anywhere reset folders, toggles, and limits to defaults silently.
- Fix: `settings_from_stored()` overlays stored keys onto defaults one at a
  time and keeps a key only if the whole struct still parses; corrupt keys
  fall back individually. Wired into startup in place of the all-or-nothing
  parse. TDD with 2 new tests (float-limit survival, garbage/round-trip
  sanity).
- New `fixtures/settings_probe.py` proves the wiring against the live binary:
  seeded a corrupt row (float limit + custom folder + custom connections),
  booted under Xvfb, read back `folder=/tmp/custom-downloads limit=None
  maxConn=4` with the app alive throughout. PASS.
- SPEC §14 notification actions (Open / Show in folder / View details):
  checked the pinned `tauri-plugin-notification 2.4.0` source in the local
  registry — the desktop builder is title/body/icon/sound only; `Action` /
  `ActionType` models are mobile-only. Buttons are not implementable on this
  plugin version. NOT building a custom WinRT/notify-rust action layer
  unilaterally; the honest interim is the current title/body notifications
  (completion names file+size, failure names file+reason), which already work
  resident. Options when this is wanted: upgrade the plugin past 2.4.0 if a
  later 2.x adds desktop actions, or add per-OS action code. Recorded, not
  started.
- Verified: Rust 24/24; `cargo build`; settings probe PASS; `diff --check`.
  `fixtures/server.py` untouched.

## 2026-09-03 — Aggregate proven shared across simultaneous jobs

- Extended `fixtures/limiter_probe.py` with `DM_JOBS`: extra captures launch
  as separate processes and reach the resident app through single-instance
  forwarding, exactly like rapid browser captures. The poll loop measures the
  COMBINED downloaded-bytes rate plus a per-job no-starvation check.
- Result with 2 concurrent 8 MiB captures under one 1 MB/s global:
  `combined 2 -> 14,680,065 bytes over 14.5s = 1.01 MB/s`, both jobs
  progressed, first job byte-identical. The global limit paces the sum, not
  each job independently (SPEC §8.6: "must not become 50 MB/s independently
  for every job"). Single-job default re-verified at 0.98 MB/s after the
  probe rework.
- Verified: probe PASS (2-job) + PASS (1-job regression); `diff --check`.
  `fixtures/server.py` untouched.

## 2026-09-03 — Range-resume math unit coverage

- `merge_range` / `covered_bytes` / `missing_ranges` previously had zero unit
  coverage despite carrying pause/resume persistence. Added 3 tests: overlap
  + adjacency coalescing, disjoint ordering, bridge collapse; inclusive-end
  byte counts; 1 MiB chunking of a 5 MiB gap set, full-coverage emptiness,
  middle-gap complement, zero-length no-op.
- Verified: Rust 27/27. `fixtures/server.py` untouched.

## 2026-09-03 — Per-job cap clearing (keep/clear/set contract)

- Follow-up to the per-job slice: omitting the cap at commit kept the old
  value with no way to clear it. `CommitInput.bandwidth_limit` is now
  `Option<Option<u64>>` — absent keeps, explicit null clears to the global
  setting, number sets. Frontend passes null straight through (Global radio
  clears); mock adapter implements the same three states.
- The new contract test caught a real serde gotcha first run: plain
  `Option<Option<u64>>` collapses explicit null into None, making clear ==
  keep. Fixed with a three-state `opt_opt_u64` deserializer (missing → None,
  null → Some(None), n → Some(Some(n))).
- Clear-path is unit-covered, not e2e-covered (commit needs the Tauri
  runtime); the set-path matrix from the previous slice stands.
- Verified: Rust 28/28; vitest 11/11; `tsc -b`; `build:all`; `diff --check`;
  headless default probe on the rebuilt binary 0.98 MB/s + byte identity.
  `fixtures/server.py` untouched.

## 2026-09-03 — Segment/fragment path paced on the reworked binary

- All limiter proofs so far exercised `range_bytes`. Extended the probe with
  `DM_MANIFEST_URL` mode for the slow-HLS manifest (4 x 1 MiB fragments via
  `fragment_bytes`, a different throttle call site): 4,194,304 bytes over
  4.0s = 1.05 MB/s under a 1 MB/s global, 4/4 segments, `finalizing`. PASS.
- Measurement lesson: parallel same-size fragments complete together, so
  segmented progress jumps 0 -> total at the end instead of ramping. The
  probe measures wall-clock across the downloading phase in manifest mode
  rather than progress samples. The coarseness is honest (real completed
  fragments only) but worth knowing for progress UX with large fragments.
- Range default re-verified after the probe rework (0.98 MB/s + identity).
- Verified: probe PASS (manifest) + PASS (range regression); `diff --check`.
  `fixtures/server.py` untouched.

## 2026-09-03 — Single-stream fallback paced; all throttle sites proven

- The last unverified throttle call site was the single-stream fallback loop.
  Generalized the probe (`DM_CAPTURE_URL`, `DM_EXPECT_BYTES`,
  `DM_MIN_SAMPLES`): `no-range.bin` (2 MiB, server ignores Range) under a
  1 MB/s global → 1.18 MB/s over a 1.5s window, byte-identical. The rate sits
  high in the band because the window is short (burst + polling granularity
  dominate); unpaced loopback would finish in ~0.05s, so the pacing signal is
  unambiguous.
- Complete call-site matrix on the reworked binary: `range_bytes` 0.98,
  `fragment_bytes` 1.05, single-stream 1.18 — all byte-identical.
- Range default re-run after the probe generalization: 0.84 MB/s (PASS, in
  band; lower than the earlier 0.98 — box-load variance, same binary).
- Verified: probe PASS (no-range) + PASS (range regression); `diff --check`.
  `fixtures/server.py` untouched.

## 2026-09-03 — Acquire paths proven: redirect, one-use, bounded 503 retry

- New `fixtures/acquire_probe.py` (`DM_MODE` = redirect | one-use |
  retry-503) drives the real binary headless (Xvfb :99, isolated HOME,
  `--capture` stdio) and asserts on the jobs SQLite table only — no XTEST.
- redirect: `/redirect` (302 -> range.bin) completes byte-identical 8 MiB.
  PASS.
- one-use: minted single-fetch URL completes as sole consumer (64 KiB,
  byte-identical across mints). Documents the double-consume hazard: a
  parallel browser fetch would 410 the app's copy — still an open product
  call, not changed. PASS.
- retry-503: `/status/503` fails honestly after 5 bounded automatic retries;
  job events show the retry attempts, error names the 503. PASS.
- Verified: probe PASS x3 modes; `diff --check`. `fixtures/server.py`
  untouched.

## 2026-09-03 — DASH SegmentList path on the current binary

- The manifest probe only covered HLS. Generalized its end-state knobs
  (`DM_CAPTURE_NAME`, `DM_EXPECT_SEGMENTS`, `DM_EXPECT_DOWNLOADED`,
  `DM_SKIP_RATE` for small fixtures with no meaningful rate window) and ran
  `/dash/manifest.mpd`: 6/6 segments, 87,235 bytes, downloading →
  finalizing — byte count matches the historical DASH e2e exactly. PASS.
- First run with guessed expectations failed on the byte count (actual
  87,235) while segments already read 6/6; pinned the observed deterministic
  values and re-ran green. HLS slow re-verified after the shared-code edits
  (1.05 MB/s, 4/4).
- Verified: probe PASS (DASH) + PASS (HLS regression); `diff --check`.
  `fixtures/server.py` untouched.

## 2026-09-03 — DASH template + HLS master paths proven

- `/dash/template.mpd` (SegmentTemplate + timeline): 4/4 segments, 32,058
  bytes, downloading → finalizing. PASS.
- `/hls/master.m3u8` (master → variant resolution): 6/6 segments, 18,048
  bytes — matches the historical HLS VOD size exactly. PASS.
- Both ran completion-only (`DM_SKIP_RATE=1`); files are too small for rate
  windows. With these, every fixture manifest shape on the current binary is
  covered: HLS VOD, HLS master, HLS slow, DASH SegmentList, DASH template
  (live HLS/DASH correctly rejected at the parser unit level).
- Verified: probe PASS x2; `diff --check`. `fixtures/server.py` untouched.

## 2026-09-03 — Crash recovery: provisional sweep, committed survival

- New `fixtures/recovery_probe.py`: seeds a committed paused range job (real
  first MiB + matching completed range) in the DB, boots the app, starts a
  slow-HLS capture, SIGTERMs at 1 MiB mid-download, reboots and asserts.
- Result: no provisional rows survive, no provisional temp/segment leftovers
  survive, the seeded job is intact and still paused with its verified range,
  and the relaunched app stays alive. SPEC §7.3 + §8.7 restart behavior,
  proven headless. PASS.
- Verified: probe PASS; `diff --check`. `fixtures/server.py` untouched.

## 2026-09-03 — UI verification via headless Chromium + CDP (no XTEST)

- New `/tmp/dm-cdp.py`: dependency-free (stdlib only) CDP driver — navigate,
  evaluate JS, screenshot. The repo's `cdp_shot.py` needs `websocket-client`
  (absent) and is hardcoded to port 9222 + video.html pages.
- Verified today's frontend in mock mode against a real browser engine, all
  visually inspected: `?settings=network` renders the global-limit controls
  intact (`/tmp/dm-ui-network.png`); `?window=add` + Advanced toggle shows
  Connections 8 + Bandwidth cap Global/Limited-to 50 MB/s
  (`/tmp/dm-ui-add.png`); the inspector Network tab shows the new
  Bandwidth-cap row as "Global setting" on an uncapped job
  (`/tmp/dm-ui-inspector.png`).
- Full create flow driven through CDP (URL set via native input setter,
  Advanced expanded, Limited-to radio clicked, Start Download): the window
  transitioned to captured state — provisional panel 42.0 MB / 1.25 GB,
  Downloading, 1 active, Resumable Yes, action button flipped to Download
  (`/tmp/dm-ui-created.png`). The cap radio/form state machine works in the
  real component, not just in unit tests.
- Caveat: the driver's JS-result readout returns null even for `1+1`
  (screenshots are authoritative; clicks demonstrably land). The mock
  progress numbers are simulated — wiring and transitions are the real
  product code; engine-side set-path is proven by the limiter probes.
- UI rig (vite mock :4317, headless Chromium :9223) stopped after the run;
  the pre-existing `:4173` native-mode vite was left untouched.
- Verified: 4/4 surfaces visually correct. `fixtures/server.py` untouched.

## 2026-09-03 — CDP driver preserved + HEAD sweep green

- `fixtures/cdp_drive.py`: the stdlib-only CDP driver used for the UI
  verification above, preserved in-repo (navigate + evaluate + screenshot,
  no third-party deps). The older `cdp_shot.py` stays for its video-page
  workflow; this one is the general-purpose equivalent.
- Full sweep on HEAD: Rust 28/28, vitest 11/11, `tsc -b` clean.
  `fixtures/server.py` untouched.

## 2026-09-03 — Native-host capture proven byte-identical; cache leak fixed

- Direct native-host probe (framed `capture-acquisition` into the real
  `com.downloadmanager.host.sh`, isolated HOME): host replied `{"ok":true}`,
  resident app created the provisional and drove it to `finalizing` with
  dl=2097152/2097152. Temp file `cmp` against the served fixture:
  BYTE-IDENTITY PASS (2,097,152 bytes).
- Root cause found on the way: the first attempt (host env without DISPLAY)
  replied ok but produced no job — the spawned `--capture` second instance
  needs a display to forward via single-instance. With `DISPLAY=:99` the
  row appears. On desktop Linux the browser session always provides one, so
  this is harness environment, not an app bug.
- Real finding from the same runs: every headless probe leaked provisional
  temps into the REAL `~/.cache` (~38 orphan files, ~115 MB) because the
  probes overrode HOME without XDG_CACHE_HOME (the app resolves temp via
  the cache dir). Cleaned all orphans (no live app, no real-HOME DB
  references them). All four app-launching probes now set
  `XDG_CACHE_HOME=$HOME/.cache`; re-ran settings recovery green and
  confirmed the real cache tmp stays empty while the isolated HOME absorbs
  cache writes.
- This commit also carries the `cdp_drive.py` target-selection (`DM_CDP_TARGET`)
  and skip-navigate (`-` URL) improvements built for the extension e2e work.
- Verified: BYTE-IDENTITY PASS; settings-probe PASS after fix; `diff --check`.
  `fixtures/server.py` untouched.

## 2026-09-04 — Pre-browser ordinary-download takeover proven

- Root cause of the first browser probe: the MV3 manifest lacked the required
  `nativeMessaging` permission. Chromium reported `Specified native messaging
  host not found` from the extension service worker; direct framed stdio still
  worked. Chrome's official native-messaging documentation confirms both the
  permission requirement and the rule that content scripts must pass messages
  through the service worker.
- Implemented the smallest canonical path:
  - `extension/src/content.ts` captures only unmodified left-clicks on explicit
    HTTP(S) `<a download>` actions in the document capture phase, prevents the
    browser default, and forwards source/name/page URL to the worker.
  - `extension/src/background.ts` forwards that intent to the native host.
    If native messaging fails, it uses `chrome.downloads.download()` as a
    non-destructive fallback; the existing `downloads.onCreated` listener
    ignores extension-owned fallback downloads.
  - `extension/manifest.json` declares `nativeMessaging`.
- Real fresh Chromium + real native-host + fixture proof (`/tmp/dm-confirm2.Tpp5VR`):
  explicit click returned `dispatch=false, defaultPrevented=true`; isolated DB
  contained exactly one job; it acquired 2,097,152/2,097,152 bytes; the Add
  Download window was inspected over WebKit's remote inspector and its real
  Download button was activated without XTEST; the row became
  `provisional=false, state=completed`; final output was
  `/tmp/dm-confirm2.Tpp5VR/Downloads/confirm.bin` (2,097,152 bytes); the temp
  `.part` was removed; `cmp` against a fresh `/file/no-range.bin` fetch passed.
  Chromium's Downloads directory stayed empty, proving no duplicate browser
  copy.
- Policy-off audit (`/tmp/dm-policy-off.o4kPVD`): after a real worker policy
  update to `interceptDownloads=false`, the same explicit click returned
  `defaultPrevented=false`; Chromium saved its own 2,097,152-byte copy; no
  Download Manager DB was created.
- Native-failure audit (`/tmp/dm-native-fail.LSNOOI`): with no native-host
  manifest, the click was prevented, the worker's browser API fallback saved
  `native-fail.bin` at 2,097,152 bytes, and `cmp` passed; no app DB was created.
- Verification: `npm run build:extension`, `npx vitest run`, `npx tsc -b`, and
  `git diff --check` all passed during the implementation. No XTEST input was
  used. `fixtures/server.py` remains untouched.

## 2026-09-04 — Blob/MSE capture selects the manifest, not a fragment

- The capture-spec audit covered SPEC §6.2, §6.3, and §7.1–§7.2. The old
  blob-player fallback chose the newest request in the tab/frame ring. A real
  MSE run demonstrated the defect: it created a job for `v-2.m4s` (10,194
  bytes), which is only one fragment and is not the media the player showed.
- The fix keeps the generic browser-network observation path, records response
  content types when available, classifies manifest/segment traffic, prefers
  an observed HLS/DASH manifest, and refuses to guess a fragment when no
  complete source is known. There are no site-specific resolver rules.
- Unit coverage now checks HLS/DASH classification, manifest preference, and
  the manifestless-fragment rejection.
- Fresh bounded E2E proof used fixture server `:18902`, a clean app home
  `/tmp/dm-mse-proof.XCCqAG`, a clean Chromium profile, and the real WebKit
  Add Download window. The page fetched `/dash/manifest.mpd`, fed the video
  `v-init.mp4` and three `v-*.m4s` requests through a real `MediaSource`, and
  displayed the media Download control.
- Before confirmation the isolated database contained exactly one job:
  source `/dash/manifest.mpd`, `downloaded=87235`, state `finalizing`.
- After the real Add Download confirmation the same row became
  `provisional=false`, `state=completed`, with `downloaded=87235` and the
  destination `/tmp/dm-mse-proof.XCCqAG/Downloads/MSE manifest proof.mp4`.
  No fragment job was created in the clean database.
- `ffprobe` validated the final 86,836-byte output as an MP4 containing H.264
  video and AAC audio with duration 6.037188 seconds. The output is the local
  FFmpeg mux/remux result, so it is not expected to be byte-identical to the
  concatenated source fragments; the job's downloaded-byte total covers all
  manifest tracks.
- The implementation intentionally does not guess a complete download from a
  segment-only blob/MSE traffic set. Candidate scope remains tab/frame based;
  associating several simultaneous players in one tab is a later refinement.

## 2026-09-04 — Committed range job survives restart and resumes

- A committed 8 MiB range job was paused after its first verified byte had
  been written. The isolated row was `provisional=false`, `state=paused`,
  `downloaded=1`, with `completedRanges=[{"start":0,"end":0}]`; the temporary
  file remained owned by the job.
- The resident process was terminated and relaunched from the same isolated
  home `/tmp/dm-resume-proof.AZkkDT`. Startup preserved the committed row,
  paused state, resource identity, and verified range.
- The real main-window Tauri command `resume_job` was invoked through the
  WebKit inspector. The existing temporary file was reused and the transfer
  completed without losing the persisted job.
- Final state was `provisional=false`, `state=completed`, with
  `downloaded=8388608` and `total=8388608`. The destination
  `/tmp/dm-resume-proof.AZkkDT/Downloads/resume.bin` was 8,388,608 bytes and
  `cmp` passed against a fresh `/file/range.bin` fixture fetch.
- This closes a real restart/resume acceptance path under SPEC §8.7. The
  earlier recovery probe still covers crash cleanup and committed-state
  preservation; this run additionally exercised the post-restart Resume
  command through the live application.

## 2026-09-04 — Cold-start native capture launches the resident app

- A fresh Chromium profile used a native-host wrapper while no
  `download-manager` process was running. The ordinary link click was stopped
  before Chromium's normal download path (`dispatch=false`,
  `defaultPrevented=true`).
- The native host launched the resident application, which created the
  independent Add Download window and began a real capture without opening the
  main manager window.
- Before confirmation the clean DB contained exactly one provisional row for
  `/file/no-range.bin`, at `2,097,152/2,097,152` bytes and `finalizing`.
- The real Add Download Download action was activated through the WebKit
  inspector. The same row became `provisional=false`, `state=completed`, and
  the destination `/tmp/dm-coldstart.uQZNoC/Downloads/cold.bin` was
  2,097,152 bytes. `cmp` passed against a fresh fixture response, and no
  duplicate browser Downloads copy existed.
- This closes the application-not-running flow in SPEC §7.6 using the real
  native-host launch path.

## 2026-09-04 — Provisional cancellation aborts and cleans active media

- A fresh resident app received a slow finite HLS capture with a real
  provisional Add Download window. The job created its `.segments` workspace
  and remained uncommitted while fragment work was in flight.
- The live `cancel_job` command was invoked through the main manager's WebKit
  inspector. After the abort settled, the isolated database contained zero
  jobs and the cache contained only its root directory: no provisional `.part`
  file and no `.segments` directory remained.
- This verifies the destructive side of SPEC §7.3: cancellation removes the
  uncommitted acquisition instead of leaving an orphaned transfer or durable
  row. The same lifecycle abort path is shared by the Add Download Cancel
  action, titlebar close, and bulk cancellation paths.

## 2026-09-04 — Explicit reattach resumes the chosen partial job

- Seeded one committed paused range job (`reattach-1`) with a verified first
  MiB and a persisted resource identity. Relaunching the app preserved the
  selected job and range.
- Invoked `reattach_job` for that exact ID through the live manager WebKit
  inspector. A renewed browser capture used the same path with a changed query
  (`old=expired` → `renewed=1`); the source compatibility check accepted the
  renewed URL without binding any other job.
- The existing job ID and temporary file were reused. It completed at
  `8388608/8388608` bytes, updated its source to the renewed URL, and produced
  `/tmp/dm-reattach-proof.ABC8ry/Downloads/reattach.bin`.
- `cmp` passed against a fresh renewed-URL fixture response. This verifies the
  targeted reattach path in SPEC §8.9 rather than a global URL heuristic.

## 2026-09-04 — Real browser media-button capture proven

- Started the fixture server from its required `fixtures/` working directory
  on `:18902`; this matters because `/media/real.mp4` is intentionally loaded
  relative to that directory. The endpoint returned a playable 34,524-byte MP4
  (`readyState=4`, `paused=false`).
- Fresh Chromium loaded `/page/video.html` with the built extension. The real
  content script injected `#dm-media-download-button`; DOM inspection confirmed
  the playing video and button. Clicking that button sent `media-capture` over
  native messaging and created a second job in the resident app DB.
- The media Add Download window was inspected and its real Download button was
  activated through the WebKit remote inspector, not XTEST. The row became
  `provisional=false, state=completed`, with 34,524/34,524 bytes. Final output
  `/tmp/dm-confirm2.Tpp5VR/Downloads/real.mp4` is 34,524 bytes and `cmp` passes
  against a fresh `:18902/media/real.mp4` fetch.
- The existing completed ordinary pre-browser job remained intact. Total DB
  rows after the media confirmation: exactly 2, both completed and byte-
  verified. The media temp `.part` was removed.
- This closes the real progressive-media browser flow in addition to the
  explicit ordinary-download flow. The fixture server source remains
  untouched.

## 2026-09-04 — Simultaneous-player and document isolation proven

- The implementation audit covered SPEC §§6.2–6.4, §7.1–§7.2, §7.6, and
  §19.3. The remaining weakness was that a tab/frame-wide candidate ring could
  associate one MSE player's manifest with another player's blob control, or
  reuse traffic after navigation.
- Implemented the bounded evidence path in
  `extension/src/background.ts`, `extension/src/content.ts`, and
  `extension/src/media-candidates.ts`: each media element receives a stable
  `playerKey`; active, hovered, playing, visible, and fresh evidence is ranked;
  candidates and player evidence are scoped by `tabId`, `frameId`, and
  `documentId`; a player with no owned evidence refuses another player's
  identified traffic; manifests win over segments; and initialization/segment
  traffic alone is rejected.
- Added focused regression coverage in `extension/src/shared.test.ts` for two
  players in one frame, cross-player refusal, document isolation, active-player
  ranking, and MSE initialization-file rejection.
- Fresh Chromium profile + real native host + real WebKit Add Download flow
  proved two simultaneous MSE players in one tab/document. The test loaded the
  player-A and player-B traffic while switching active/playing evidence, then
  clicked each media control. The isolated DB contained exactly two rows:
  `source=.../dash/manifest.mpd?player=a` and
  `source=.../dash/manifest.mpd?player=b`; there was no `.m4s`, init, or
  cross-player source. Both rows completed at `87235/87235` downloaded bytes,
  and both final outputs were 86,836-byte MP4 files with H.264 video and AAC
  audio. The Add Download actions were invoked through WebKit inspection, not
  XTEST. Artifacts are under `/tmp/dm-mp-e2e-20260904/`, including
  `two-players-fresh.png`, `navigation-data.png`, and the final DB readback.
- Same-tab navigation was rerun with a corrected CDP driver that selects one
  exact page target ID, enables the Runtime domain, parses the Runtime result
  envelope correctly, and waits for the requested document URL rather than
  merely waiting for any `complete` document. The same target
  `A6703CCB07DE05DFFB851E1ABC6AA158` stayed selected while navigating from
  `http://127.0.0.1:18902/page/video.html?same-tab-old` to the fresh 200 HTML
  document at `http://127.0.0.1:18905/?same-tab-fresh`.
- The fresh page's one visible control belonged to its current in-memory
  `data:` video, not to the previous manifest. Clicking it produced no new
  native job. The saved readback shows SQLite `1 -> 1`, Downloads `2 -> 2`,
  no `stale-guard` row/file, and unchanged 86,836-byte `player-a.mp4` and
  `player-b.mp4` hashes. This proves no stale managed candidate was reused;
  it does not claim a managed capture for the data URL.
- The original disposable HTTP server on `:18904` was accepting connections
  but returning empty replies because its inherited output pipe had closed.
  The proof replaced it with a redirected server on `:18905`; the application
  and fixture source were not changed.
- Verification: `npx vitest run extension/src/shared.test.ts`,
  `npm run build:extension`, `npx tsc -b`, `npm test`, `npm run build:all`,
  `cargo test --manifest-path src-tauri/Cargo.toml`, `cargo build
  --manifest-path src-tauri/Cargo.toml`, and `git diff --check` all passed.
  `fixtures/server.py` remains untouched.

## 2026-09-04 — Media track artifact cleanup and bounded orphan sweep

- Added `cleanup_media_track_files(temp_path)` to remove only files matching
  the temporary primary file's `<name>.track-*` pattern. The primary `.part`
  remains intact. Provisional cleanup, cancellation, removal, segment-identity
  replacement, and successful media finalization now call it.
- A startup sweep now removes only direct files named
  `provisional-*.part.track-<digits>` in the configured temp folder. It keeps
  track files whose primary `.part` belongs to an active committed job and
  leaves unrelated, malformed, nested, or non-track files untouched.
- Deterministic Rust coverage passes for both direct cleanup and the bounded
  orphan sweep, including preservation of the primary `.part` and active-job
  tracks.
- Fresh real Tauri/WebKit cancellation proof used a unique multi-track DASH
  provisional. The row reached `finalizing` with all 6 segments downloaded and
  both `.part.track-00` and `.part.track-01` present. The real `cancel_job`
  command then removed the DB row, all job temp artifacts, and the destination
  was never created. Artifact script: `/tmp/dm-mp-e2e-20260904/run_segmented_cancel.py`.

## 2026-09-04 — Generation-aware transfer ownership verified

- Lifecycle audit found that a canceled worker could finish after a replacement
  transfer claimed the same job id. Persisted state alone could not distinguish
  the stale worker from the replacement.
- `TransferRegistry` now assigns a monotonic generation to every owner. The
  async acquisition chain carries that generation through probe, range,
  fragment, manifest, throttle, retry, file-write, mux, finalization, and
  publication boundaries. Stale workers no-op and cannot release a replacement.
  `abort()` removes the current owner before aborting its future, so a replacement
  can claim immediately without being cleared by the old worker.
- Removed obsolete non-generation `claim()`/`release()` wrappers. Lifecycle tests
  now exercise the generation-aware API directly; the previous dead-code warning
  is gone.
- Fresh disposable fixture server on `127.0.0.1:18906` was verified with the
  range object and DASH manifest. The first range/retry attempt was rejected as
  evidence because a pre-existing single-instance disposable app forwarded both
  requests into another HOME. After terminating only that disposable process,
  isolated reruns passed:
  - `/tmp/dm-acquire-probe-18906.py`, `DM_MODE=redirect`: `REDIRECT: PASS`,
    8,388,608 bytes byte-identical through the 302 redirect.
  - `/tmp/dm-acquire-probe-18906.py`, `DM_MODE=retry-503`: `RETRY-503: PASS`,
    honest 503 failure after bounded retries with retry events recorded.
  - `/tmp/run_segmented_cancel-18906.py`: `SEGMENTED_CANCEL: PASS`; the
    generation-specific DASH job reached `finalizing` at 6/6 segments and
    87,235 downloaded bytes, then its DB row, `.part.segments`, both track files,
    and destination were absent after cancellation.
- Verification after the final lifecycle edits: Rust 31/31; `cargo build
  --manifest-path src-tauri/Cargo.toml`; JavaScript 17/17; `npm run build:all`;
  `npx tsc -b`; direct `rustfmt --edition 2021 --check
  src-tauri/src/lifecycle.rs`; and `git diff --check` all passed. The repository
  `cargo fmt --manifest-path src-tauri/Cargo.toml --all -- --check` remains
  non-zero only because existing compact formatting in unrelated `main.rs` and
  `media.rs` would require a mass reformat; those files were not reformatted.
  `fixtures/server.py` remains untouched.

## 2026-09-04 — Post-await ownership guard hardening

- A follow-up lifecycle audit found filesystem await boundaries that could still
  perform another write, destination move, or cleanup before checking whether the
  worker's generation remained current. Added generation gates after range-file
  setup/initialization, segmented identity reset and directory discovery, media
  assembly, and single-stream destination preparation and handoff.
- The first rerun of range/retry while the fresh WebKit instance was alive was
  rejected as evidence because the application's single-instance forwarding left
  those probe HOMEs empty. After stopping only that disposable instance, both
  isolated reruns passed against the rebuilt binary:
  - `DM_MODE=redirect`: 8,388,608 bytes byte-identical through the 302 redirect.
  - `DM_MODE=retry-503`: honest 503 failure after five bounded retries, with
    retry events persisted in the job record.
- A fresh isolated WebKit cancellation run passed again with job
  `provisional-e58a2345-a59d-445a-835c-f6c27830fecb`: `finalizing`, 6/6 segments,
  87,235 downloaded bytes, then no DB row, no `.part.segments`, no track files,
  and no destination output.
- Verification before this follow-up commit: `cargo test
  --manifest-path src-tauri/Cargo.toml` 31/31; `cargo build
  --manifest-path src-tauri/Cargo.toml`; `git diff --check`; direct rustfmt check
  for `src-tauri/src/lifecycle.rs`. `fixtures/server.py` remains untouched.

## 2026-09-04 — Limiter + redirect regression on the generation-aware engine

- Rebuilt the binary at HEAD (includes the uncommitted
  `wait_for_transfer_idle` commit-path guard) and re-ran two committed
  probes against the reworked transfer ownership, isolated HOMEs:
  - limiter default (global 1 MB/s, 8 MiB range): 0.79 MB/s, byte-identical.
    In band; on the low side with the box under load (prior runs on the older
    engine: 0.98/0.84), signal unchanged (paced vs >50 unpaced).
  - acquire redirect (302 -> range.bin): 8,388,608 bytes byte-identical.
- Full suite on the same worktree: Rust 31/31, vitest 17/17, `tsc -b`
  clean. No repo edits in this check; the `main.rs` working change was left
  untouched. `fixtures/server.py` untouched.

## 2026-09-04 — Per-key live settings patches (no more all-or-nothing)

- `update_settings` had the same flaw boot recovery once had: it merged the
  whole patch, then a single `from_value` decided everything. One mistyped
  value in a multi-key patch (e.g. a float `bandwidthLimit` next to a valid
  `maxConnections`) silently discarded the good keys too.
- Fix: extracted `apply_settings_patch` (per-key keep-if-parses, unknown keys
  ignored, non-object patch = no-op). `settings_from_stored` now delegates to
  it, so boot recovery and live patches share one routine. TDD: new test
  `settings_patch_keeps_good_keys_when_one_key_is_bad` failed first (E0432,
  fn did not exist), then passed after the refactor.
- Suite: Rust 32/32 (incl. pre-existing `settings_fallbacks_stay_sane`,
  which now covers the shared routine), build clean, no new warnings.
- Commit hygiene: the file also holds the sibling session's uncommitted
  `wait_for_transfer_idle` slice (16 lines, commit path). Split-staged only
  my hunks (`git apply --cached` on a filtered patch); their 16 lines stay
  uncommitted in the working tree for their session to land.
- Note: `cargo fmt --check` flags the whole file tree-wide (200+ hunks incl.
  `media.rs`) — pre-existing style drift from recent sessions, not mine; left
  alone rather than reformatting under someone else's in-flight change.
  `fixtures/server.py` untouched.

## 2026-09-04 — Settings deep-link validation (?settings= route guard)

- The tray "Set Bandwidth Limit" item navigates the manager to
  `?settings=network`, and `Manager` read that param with an unchecked cast:
  any unknown value (`?settings=bogus`, `?settings=` , wrong case) fell
  through every page branch and rendered a blank Settings content area.
- Fix: new `src/settings-route.ts` with `settingsPageFromSearch` (known-page
  allowlist, fallback `'general'`), wired into `Manager`'s initial state.
  TDD: `src/settings-route.test.ts` (4 tests) failed first (module missing),
  then green. Suite: vitest 21/21 (3 files), `tsc -b` clean, `npm run build`
  clean (256.60 kB / 77.18 kB gzip).
- Browser proof (CDP rig, no XTEST): mock UI at `?settings=bogus` renders the
  General page with full content (`/tmp/dm-settings-bogus.png`);
  `?settings=network` renders Network with bandwidth controls
  (`/tmp/dm-settings-network.png`). Vision-verified 2/2. Rig torn down after.
- Housekeeping: the teardown also reaped a stale `:4317` vite mock
  (PIDs 1706871/91/92, `--host 127.0.0.1`) predating this session — leftover
  from an earlier rig, not mine, ports confirmed down after.
  `fixtures/server.py` untouched.

## 2026-09-04 — Tray checkmarks follow Settings both directions (SPEC §12)

- SPEC §12 demands one coherent policy between tray and Settings. The two
  tray check items (Browser Integration, Media Buttons) were built with the
  right initial state but never updated after: tray toggles flipped the
  setting without touching the item's visual check, and Settings-panel
  toggles never reached the tray at all. Verified against the vendored
  tauri-2.11.5/muda-0.19.3 sources: the wrapper exposes `set_checked` /
  `is_checked` and performs no automatic flip on click, so explicit sync is
  required in both directions.
- Fix: `CoreState.tray_checks` stores the two `CheckMenuItem<Wry>` handles at
  install time; new `sync_tray_checks` sets both visuals, called from all
  four writers (both tray arms, `update_settings`, `apply_browser_policy`).
  Startup path already passes persisted policy into `install_tray`, so the
  initial state was and stays correct. Lock discipline: sync touches only
  `tray_checks`, never `snapshot` — no new lock ordering, no cycle.
- Verification, stated honestly: no headless unit test exists for this —
  native tray menus need a running app (no AppHandle in `cargo test`, CDP
  cannot see native menus, XTEST out of bounds). Verified instead by
  source-level API confirmation + `cargo test` 32/32 + `cargo build` clean
  with no new warnings. Two compile fights on the way, both real lessons:
  `CheckMenuItem` needs its `<tauri::Wry>` generic, and `if let … lock()`
  in tail position extends the scrutinee temporary past the `State` binding
  (E0597) — trailing `;` fixes it.
- Commit hygiene as before: sibling session's 16-line `wait_for_transfer_idle`
  slice still uncommitted in the same file; split-staged only my hunks
  (`@@ -11/111/349/1482/1577/1592/1622`), theirs stay in the working tree.
  `fixtures/server.py` untouched.

## 2026-09-04 — Native toast actions: investigated, closed as platform-limited (SPEC §14)

- SPEC §14 wants Open / Show in folder / View details actions on native
  toasts. Our native toasts carry title/body only. Checked whether that is
  an implementation gap or a platform ceiling, from two sides:
  - Vendored `tauri-plugin-notification-2.4.0` source (the version this tree
    builds): `register_action_types` exists only in `mobile.rs`;
    `desktop.rs` `show()` forwards title/body/icon to the OS notifier and
    never consumes `action_type_id`. No action buttons or action-click
    events exist on the desktop path.
  - Official docs (v2.tauri.app/plugin/notification) describe Actions
    generically with JS `registerActionTypes`/listener APIs, but the Rust
    desktop backend drops them — so wiring action types from our side
    would be dead code on Windows/Linux.
- Verdict: no code change. Native toasts stay notify-only (they already
  identify the file, and failures carry the concise reason). All three
  SPEC actions exist and work in the in-app notification center
  (`NotificationCard`: Open / Show in folder for completed, View details /
  Open Manager for failed — verified in `App.tsx`). This closes the old
  "notification action labels" blocker note: there is no label/command
  mismatch to fix, only a plugin ceiling. Revisit only if the plugin's
  desktop backend gains action support (would need a dependency upgrade,
  not a local patch).
- Final regression on the closing tree (my three slices + sibling's
  uncommitted wait-guard, all present in the worktree binary): Rust 32/32,
  vitest 21/21 (3 files), `tsc -b` clean, `cargo build` clean, headless
  limiter probe PASS (8 MiB byte-identical, paced). No test rigs left
  running. `fixtures/server.py` untouched.

## 2026-09-04 — Settings patch→restart round-trip test (persistence seam)

- Audited the settings write path for a real bug first: `update_settings`
  mutates the snapshot but never touches the DB directly — however every
  settings writer ends in `emit_snapshot`, which calls `save_snapshot`
  (settings row + jobs table), so patches do persist. No bug; but the seam
  had zero direct coverage (only the corrupt-row boot probe touched it).
- New `settings_patch_survives_database_round_trip` test: in-memory
  `CoreState` → `apply_settings_patch` → `save_snapshot` → raw SQL read →
  `settings_from_stored` → asserts values survive. Stated honestly, this is
  a characterization test, not RED-first: it passed on the first run and
  exists to lock the patch→DB→boot chain against future schema drift.
  Suite now Rust 33/33.
- Also verified while here: both adapters send partial patches (mock merges,
  native forwards `{patch}`), consistent with the per-key backend; the
  `?job=` default (`'job-2'`) self-heals via the existing fallback
  (`snapshot.jobs[0]` + ID check), so no bug there either; `open_path`
  backend command exists per-OS, so all in-app notification actions are
  wired end to end.
  `fixtures/server.py` untouched.

## 2026-09-04 — Acceptance top-up on the final tree (no new code)

- Acquire matrix re-run against the closing worktree binary (all slices +
  sibling's wait-guard): redirect PASS (8 MiB via 302, identical),
  one-use PASS (sole consumer), retry-503 PASS (bounded, honest fail).
- `cargo clippy`: 18 warnings tree-wide, none in this run's added code
  (checked locations: pre-existing items in older regions + `media.rs` +
  capture-area helpers). Left alone — cleaning others' lines is churn.
- `npm run build:all` green (app 1.35s + extension 215ms).
- `/tmp` still ~91%: my probe HOMEs and screenshots are the only things I
  added and the HOMEs are removed; the remainder is the sibling's active
  footprint plus another project's files — not mine to delete.
  `fixtures/server.py` untouched.

## 2026-09-04 — Real-app boot smoke for the tray-handle code (§21.8/§21.10)

- The tray-sync slice was compile- + source-verified only, and SPEC §21.10
  says that is never sufficient. So: booted the real worktree binary on the
  shared `:99` Xvfb rig with a fresh isolated HOME (no XTEST — boot, observe,
  screenshot, kill). Result: clean start, no panic, no error in the log;
  `libayatana-appindicator` tray backend initialized; main window
  `Download Manager 1180x760` plus the tray host window both present;
  rendered UI vision-verified (sidebar, empty state, Add URL, status bar
  "Connected · Browser integration on · 0 active").
- Precisely what this proves: `install_tray` with the new handle-store lines
  executes at startup without panic, and the app runs normally with the
  tray installed. What it does not prove: checkmark visuals on click (needs
  GUI event injection, out of bounds) — that direction stays
  source-verified. Screenshot `/tmp/dm-tray-boot-window.png`; rig torn down
  (process + boot HOME removed), `:99` Xvfb left running as found.
  `fixtures/server.py` untouched.

## 2026-09-04 — Blank folder paths rejected (live patch + boot)

- Real, reachable bug: the folder fields are free-text inputs, so clearing
  one sends a blank `defaultFolder`/`tempFolder` straight through the
  per-key patch (empty strings parse fine). Every later destination then
  joins onto an empty path and lands relative to the process working
  directory, with no feedback at the Settings surface. Zero-limit and
  zero-connection cases were audited first and are already safe
  (`effective_rate` filters non-positive rates; `clamp_connections` +
  per-site `.clamp(1, …)` at every spawn site) — folders were the open hole.
- Fix in the owned routine: `apply_settings_patch` skips blank folder
  values (stored folder kept, good keys in the same patch still apply);
  `settings_from_stored` falls back to the platform default when a stored
  folder is blank. TDD: `settings_patch_rejects_blank_folders` failed first
  (assertion, blank applied), green after — suite Rust 34/34, build clean.
- Process note (earned the hard way): never backslash-escape quotes inside
  patch/write parameters — they are written verbatim, so typed backslashes
  land in the file as literal backslashes and break string literals. Two
  mangled attempts were repaired by excising the broken hunk via
  `git diff` → hunk-filter script (`/tmp/dm-filter.py`) → `git apply -R`
  full + `git apply` filtered, which restored the sibling's uncommitted
  slice byte-identically (re-verified: 16 insertions, 33/33 green before
  re-adding). Type plain quotes, always.
- Commit hygiene as before: split-staged only my hunks (`@@ -322`,
  `@@ -1896`); the sibling's 16-line `wait_for_transfer_idle` slice stays
  uncommitted in the working tree. `fixtures/server.py` untouched.

## 2026-09-04 — Frontend settings patch parity guard

- Tight red repro: focused Vitest for the new settings patch seam failed
  with `TypeError: sanitizeSettingsPatch is not a function` in all three
  tests because `src/adapters.ts` exported no such helper.
- Implemented the smallest seam: exported `sanitizeSettingsPatch`, which
  removes only blank string `defaultFolder`/`tempFolder` fields and preserves
  every other key. The mock adapter now uses it before merging settings, and
  the native adapter uses it before sending the patch. Rust
  `apply_settings_patch` remains authoritative for schema validation,
  persistence, and all malformed non-folder values.
- Verification: focused Vitest 3/3, full Vitest 24/24, `tsc -b` clean,
  `npm run build:all` green, Rust tests 34/34, and the redirect acquisition
  plus aggregate limiter probes both passed. `src-tauri/src/main.rs` still
  contains only the sibling's 16-line uncommitted `wait_for_transfer_idle`
  slice; it was not staged.
- `fixtures/server.py` untouched.

## 2026-09-04 — Browser interception worker failure restores default download

- Reachability audit found `content.ts` prevented an explicit `<a download>`
  click before calling `chrome.runtime.sendMessage`, then swallowed a rejected
  message promise. If the MV3 worker could not be reached, that user download
  disappeared instead of returning to browser handling.
- Added `restoreBrowserDownload` with a `data-dm-browser-fallback` recursion
  marker. The capture listener ignores only that marked synthetic anchor; the
  rejected-worker path replays it with the original URL and cleaned filename.
  The worker-response `{ok:false}` path remains unchanged because background.ts
  already owns its browser-download fallback.
- Clean jsdom regression: 1/1. Full frontend/extension suite: 25/25 across 5
  files. `npx tsc -b` clean. `npm run build:all` produced application and
  classic extension bundles, including `dist/content.js` 4.76 kB.
- No Rust files were staged for this slice. The sibling's 16-line
  `wait_for_transfer_idle` change remains unstaged in `src-tauri/src/main.rs`.
- `fixtures/server.py` untouched.

## 2026-09-04 — Filename path escape closed

- Fresh audit found `start_provisional` joining the caller-provided filename
  directly onto the configured download folder. A manual Add URL name or a
  native capture payload containing `../../outside.bin` could therefore write
  outside the configured folder; a Windows-style `C:\\...` name was also not
  treated as a leaf on the cross-platform test host. This violated the
  filename/save-folder split in the Add Download UI.
- TDD red test covered slash and backslash traversal, whitespace, dot-only
  names, NUL, and the resulting destination. Added `safe_filename` and
  `destination_for_filename`: only the trimmed final path component is kept;
  empty, dot, dot-dot, and NUL-containing names become `download.bin`.
  Provisional destination creation and committed display-name updates use the
  helper. The explicit user-selected destination remains unchanged.
- Real binary probe: `--capture` against local `/file/range.bin` with name
  `../../outside.bin` produced `NAME: outside.bin` and a destination inside
  the isolated `Downloads/` folder. Rust tests 35/35 and `cargo build` passed.
- Sibling safety: only my filename hunks will be staged; the sibling's
  16-line `wait_for_transfer_idle` change in `src-tauri/src/main.rs` remains
  unstaged. `fixtures/server.py` untouched.

## 2026-09-04 — Resume All preserves ready provisional acquisitions

- Audit found an inconsistency in the reachable lifecycle commands. `resume_job`
  already restored a completed provisional to `finalizing` / “Ready to save”,
  while `resume_all` changed every paused job to `downloading` and respawned it.
  Pause All followed by Resume All could therefore reacquire a one-use source
  instead of preserving the pending user decision.
- Added the tested `resume_all_plan` decision in Rust and used it from both
  resume commands. A ready provisional (`provisional=true`, progress >= 100)
  returns to `finalizing`, has zero connections, and is not spawned; all other
  paused/pending jobs retain the existing resume path. The mock adapter uses the
  matching `resumePlan` seam so mock UI behavior does not diverge from native.
- Rust suite: 36/36. Frontend/extension suite: 27/27 across 6 files.
  `npx tsc -b` clean. `cargo build --manifest-path src-tauri/Cargo.toml`
  completed successfully. `git diff --check` clean.
- The sibling's `wait_for_transfer_idle` changes remain unstaged in
  `src-tauri/src/main.rs`; no sibling hunks are included in this slice.

## 2026-09-04 — Reattach compatibility includes the service port

- Audit found `source_compatible` compared scheme, host, and path but not the
  port. Reattach could therefore accept a renewed source from the same host and
  path on a different service port. Query changes remain allowed for expiring
  token renewal.
- Added `port_or_known_default()` to the compatibility check. Explicit
  `https:443` remains equivalent to omitted HTTPS port, while `8443` and `9443`
  are rejected as different endpoints. The regression covers both cases.
- Rust suite: 36/36. `git diff --check` clean. Only this comparison and its
  test are mine; the sibling's two `wait_for_transfer_idle` hunks remain
  unstaged in `src-tauri/src/main.rs`.

## 2026-09-04 — Ranged manifest probe now fetches the full manifest

- A real fixture probe reproduced a valid finite HLS playlist failing when the
  server honored the initial `Range: bytes=0-0` request. The parser was given
  that one-byte `206` response instead of the complete manifest.
- `acquire_once` now performs one un-ranged GET for manifest sources when the
  probe response is partial, checks that recovery response is successful, and
  parses the full body. Non-manifest range probing remains unchanged.
- Added `/hls/ranged-vod.m3u8` to the isolated fixture and a
  `ranged-manifest` acquire-probe mode. Real binary evidence after rebuild:
  `RANGED-MANIFEST: PASS (18048 bytes after full-manifest recovery)` and byte
  equality against all six fixture segments. Existing redirect, one-use,
  bounded-503, and limiter/byte-identity probes also passed.
- Rust suite: 36/36; frontend/extension suite: 27/27 across 6 files;
  `npx tsc -b` clean; cargo build and `git diff --check` passed. The sibling's
  two `wait_for_transfer_idle` hunks remain unstaged in `src-tauri/src/main.rs`.

## 2026-09-04 — No-range fallback reports non-resumable state

- Fresh audit found Mode C marking every known-length single-stream transfer
  `resumable=true`, even though restart currently recreates the stream from
  byte zero. `commit_provisional` also overwrote the mode-specific flag with
  `true`, so the Add Download UI could promise resume the source cannot provide.
- Single-stream acquisition now records `resumable=false`, and commit preserves
  that verified mode flag. Range and segmented paths still set it true after
  their completed-work state is established.
- Added a no-range fixture probe requiring both the flag and exact bytes. Real
  rebuilt-binary evidence: `NO-RANGE: PASS (2097152 bytes, restart-safe flag is
  false)`. Redirect, one-use, bounded-503, ranged-manifest, limiter, and byte
  identity probes remained passing. Rust 36/36; frontend/extension 27/27;
  TypeScript clean; cargo build and diff check passed.
- Final post-staging rerun: Rust `36/36`; no-range and ranged-manifest probes
  passed again; redirect, one-use, bounded-503, limiter, and byte-identity
  probes passed again. Temporary ranged fixture server was isolated on :8902
  and stopped afterward.
- The sibling's two `wait_for_transfer_idle` hunks remain unstaged in
  `src-tauri/src/main.rs`.

## 2026-09-04 — Excluded media sites normalize to hostnames

- Settings previously stripped only a leading scheme and one trailing slash.
  Input such as `https://www.Example.com/watch/` was stored as a path-bearing
  value, while the extension matches only `example.com`; exclusion therefore
  silently failed.
- Added `normalizeSite`, using URL hostname parsing, lowercase, and the same
  `www.` removal as the extension. Browser Integration now stores the exact
  representation used for matching and deduplicates reliably.
- Regression evidence: `src/site.test.ts` covers URL/path/port input and the
  full frontend/extension suite passes `28/28`; TypeScript and `npm run
  build:all` are clean. The sibling's two `wait_for_transfer_idle` hunks remain
  unstaged in `src-tauri/src/main.rs`.

## 2026-09-04 — Native policy reload migrates legacy site values

- Audit found the Rust policy-file parser only lowercased entries. Legacy values
  containing schemes, paths, ports, or `www.` therefore could not match the
  extension's hostname-only comparison.
- Added native normalization through URL hostname parsing, matching the
  extension and Settings behavior. The existing round-trip and incomplete
  payload tests remain intact; a regression covers URL/path/port migration.
- Evidence: Rust suite `37/37` and cargo build passed; `git diff --check`
  passed. The sibling's two `wait_for_transfer_idle` hunks remain unstaged in
  `src-tauri/src/main.rs`.

## 2026-09-04 — Resume identity requires a validator

- Audit found `identities_match` treating equal length plus equal absence of
  ETag and Last-Modified as proof that partial bytes belonged to the same
  resource. The first-byte check was not enough to safely stitch same-length
  replacements.
- Resume now requires at least one validator and rejects any validator that is
  missing or different on either side. Equal length alone restarts safely.
- Regression evidence: Rust suite `38/38`; cargo build and `git diff --check`
  passed. Redirect, one-use, bounded-503, limiter, and byte-identity probes
  passed again. The sibling's two `wait_for_transfer_idle` hunks remain
  unstaged in `src-tauri/src/main.rs`.

## 2026-09-04 — DASH adaptation-level SegmentList support

- Audit found `parse_dash_tracks` only captured `Initialization` and `SegmentURL`
  elements while a `Representation` was open. Valid MPDs may inherit a
  `SegmentList` from `AdaptationSet`.
- The parser now collects those refs before the first representation and treats
  self-closing `Representation` elements as the selected representation.
- Added a deterministic `/dash/adaptation-list.mpd` fixture and real-binary
  `dash-adaptation-list` probe. The assembled init plus three media fragments
  matched byte-for-byte: `DASH-ADAPTATION-LIST: PASS (32058 bytes assembled)`.
- Regression evidence: Rust suite `39/39`; cargo build and `git diff --check`
  passed. No-range, redirect, one-use, bounded-503, ranged-manifest, limiter,
  and byte-identity probes passed again.

## 2026-09-04 — Settings reject invalid enum and numeric values

- Native per-key settings recovery now rejects unknown close/collision/theme/
  density/unit values, zero or fractional bandwidth limits, and connection or
  retry counts outside their UI bounds. Valid sibling keys still apply.
- The frontend sanitizer mirrors those bounds for MockAdapter and NativeAdapter
  calls; Rust remains the source of truth.
- Regression evidence: native Rust suite `40/40`; focused sanitizer Vitest
  `5/5`; full frontend/extension suite `30/30`; TypeScript clean; application
  and extension production builds passed; `git diff --check` passed.

## 2026-09-04 — Provisional commit waits without stealing cancellation

- Race audit of `commit_provisional` found that it changed `provisional` to
  `false` before waiting for an active transfer owner. A cancellation during
  that wait could then be treated as a managed failure, preserving a temporary
  part instead of removing the provisional acquisition. A timeout also returned
  an error after silently changing ownership. The old post-wait path could
  overwrite a pause/cancel transition during media or file finalization.
- Commit now makes no ownership change before the idle wait. It rechecks the
  row, provisional flag, `finalizing` state, 100% progress, and transfer owner
  under the lifecycle lock before accepting a ready acquisition. Timeout,
  cancellation, and non-finalizing transitions return errors without accepting
  the row. Finalization and completion use ownership checkpoints and the final
  state update is guarded, so pause/cancel is not overwritten.
- Added state-machine regressions for active-owner wait, post-wait cancellation
  and finalizing transitions, plus a deterministic async owner-release test.
  Focused test passed; full native Rust suite is now `43/43`.
- Real native Tauri probe used `create_provisional` followed immediately by
  `commit_provisional` for fixture `slow.bin` while acquisition was active.
  The isolated row completed as `state=completed, provisional=false`; output
  `/tmp/dm-commit-probe-home-tAe0rJ/Downloads/active.bin` is `524288` bytes and
  the provisional part was removed. The modal commit path was also verified
  with `slow.bin` and produced the same completed state.
- Full frontend/extension suite: `30/30`; `npx tsc -b` clean; `cargo build`
  passed; `npm run build:all` passed; `git diff --check` passed. Cargo reports
  one dead-code warning only for the sibling's separate uncommitted
  `wait_for_transfer_idle` helper.
- The sibling's 16-line `wait_for_transfer_idle` change remains unstaged and
  unmodified in `src-tauri/src/main.rs`; this slice stages no sibling hunk.

## 2026-09-04 — Ordinary capture restores the browser download on ok:false

- Audit found `content.ts` preventDefaulted the explicit `<a download>` click,
  then only restored the browser download when `sendMessage` rejected. A
  background reply of `{ok:false}` (native unreachable and its own
  downloads-API fallback failed) resolved successfully, so the user download
  silently disappeared after interception.
- Added pure `captureNeedsBrowserRestore` in `download-fallback.ts`: any answer
  other than explicit `{ok:true}` requires the synthetic-anchor replay. The
  click path now checks the reply in `.then` as well as `.catch`; background
  answers `ok:false` only after its own fallback failed, so the anchor replay
  is a last resort via a different mechanism, not a duplicate. The fallback
  marker still prevents recursion.
- Regression evidence: focused `download-fallback` Vitest `2/2`; full
  frontend/extension suite `31/31`; `npx tsc -b` clean; native Rust suite
  `43/43`; `npm run build:all` passed (app + extension, incl. `content.js`
  4.82 kB); `git diff --check` passed.
- The sibling's `wait_for_transfer_idle` helper remains unstaged and unmodified
  in `src-tauri/src/main.rs`; this slice stages no sibling hunk.

## 2026-09-04 — Real mid-transfer cancel probe (SPEC §7.3)

- Used the live isolated Tauri app + fixture `:8904` via the WebKit inspector
  bridge. Created `slow.bin?capped=1` with a 10 KB/s per-job cap, observed
  `downloading 4.66% (24436 bytes), provisional=true` mid-transfer, then
  issued `cancel_job` for that exact id.
- After cancel: the row is absent from the isolated SQLite `jobs` table, its
  temp `.part` is gone, and no destination file was created. The unrelated
  ready `slow-hls` provisional (`finalizing 100%`, awaiting Download/Cancel)
  was correctly preserved — cancel is selective, not a sweep.
- An earlier immediate create→cancel of `slow.bin?cancel=1` also left no row,
  no temp, and no destination.
- No code change; probe evidence only. Fixture `:8904` and the isolated app
  were left running for further probes.

## 2026-09-04 — Real pause/resume/commit chain is byte-identical

- Same live app + fixture `:8904`. Created `slow.bin?pauseresume=1` at 50 KB/s:
  observed `downloading 23.4% (122880 bytes)`, then `pause_job` → `paused
  48.8% (256000 bytes)` with the 256000-byte temp part preserved on disk.
- `resume_job` continued the same acquisition to `finalizing 100% (524288
  bytes)` — no restart from zero. `commit_provisional` then completed it as a
  managed download; the temp part was removed.
- SHA256 of `/tmp/dm-commit-probe-home-tAe0rJ/Downloads/pauseresume.bin` is
  `d76394c6…ccdc33f`, identical to a fresh GET of `/file/slow.bin`.
- No code change; probe evidence only.

## 2026-09-04 — Failed media remux now names its cause (real HLS probe)

- Committing the ready HLS `slow-hls` provisional as `slow-hls.bin` failed in
  `finalize_media`: the fixture's `/hls/slow.m3u8` segments are random bytes,
  and destination extension `.bin` routes through `ffmpeg -c copy`, which
  rejected the input (`Invalid data found`). The product failed honestly with
  parts preserved — but `job.error` carried only the generic string, discarding
  ffmpeg's cause. Reproduced byte-for-byte with the installed ffmpeg 7.1.5.
- `finalize_media` now returns `ffmpeg_remux_failure(&status)`, e.g. `Media
  remux failed (ffmpeg exit 183); downloaded parts were preserved`, which flows
  into `job.error` and the invoke result. The timeline event text is unchanged.
  Pure regression `failed_remux_names_ffmpeg_exit_status`; Rust suite `44/44`.
- Live proof on the rebuilt binary: a fresh HLS commit as `.bin` failed with
  `error: Media remux failed (ffmpeg exit 183); downloaded parts were
  preserved` and kept its 4194304-byte part; a fresh HLS commit as `.ts`
  (remux correctly skipped) completed with `state=completed, error=None`,
  output `tsok.ts` at 4194304 bytes, temp part removed.
- Frontend/extension suite `31/31`, `tsc` clean, cargo build clean (only the
  sibling's unused-helper warning), `git diff --check` clean.
- The sibling's `wait_for_transfer_idle` helper remains unstaged and unmodified
  in `src-tauri/src/main.rs`; this slice stages no sibling hunk.

## 2026-09-04 — One-use URL consumed exactly once via native path

- Minted `http://127.0.0.1:8904/one-use/t1`, created the provisional through the
  live app's real `create_provisional` command: reached `finalizing 100%
  (65536 bytes)`. `commit_provisional` completed it (`state=completed,
  error=None`), output `oneuse.bin` (sha256 `96ba4f57…45f71`).
- Re-fetching the same token returns `410` — the native acquisition was the
  sole consumer; nothing re-fetched or duplicated the one-shot transaction.
- No code change; probe evidence only.

## 2026-09-04 — Registered unpacked extension native bridge and fallback e2e

- Rebuilt the app and extension, then loaded the unpacked extension from
  `extension/dist` in a disposable Chromium profile. Its exact runtime ID was
  `mogdhelapdlmkfgeeaogeehnlclhcnkn`.
- Started the rebuilt app under the same isolated HOME with
  `DM_EXTENSION_ID=mogdhelapdlmkfgeeaogeehnlclhcnkn`. The generated Chromium
  host manifest contained both the published origin and this runtime ID, and
  pointed at the executable wrapper in that isolated HOME.
- Direct service-worker evidence: `chrome.runtime.sendNativeMessage` for
  `get-policy` returned `{ok:true, interceptDownloads:true,
  showMediaButtons:true}` with `lastError=null`. A direct
  `capture-acquisition` message also returned `{ok:true}` with no error.
- Ordinary browser capture evidence: a real `<a download>` click on the
  fixture page returned `dispatchReturned=false` and
  `defaultPrevented=true`; the browser created no fallback file. The app DB
  gained the named provisional job at `finalizing 100% (65536 bytes)`, with a
  real `.part` whose SHA256 was
  `96ba4f57e721a5b66c8cc934a9df430390c0fb371df4bce778cdd5c845846f71`.
  A second request for the one-use source returned `410`.
- Fallback evidence was run in a separate disposable Chromium profile with no
  host manifest. The service-worker callback returned the exact Chromium error
  `Specified native messaging host not found.` The ordinary click was still
  intercepted (`dispatchReturned=false`, `defaultPrevented=true`), then
  `chrome.downloads.search` reported a complete browser download at
  `65536/65536` bytes with `error=null`. The app DB stayed at exactly the
  three native jobs, and a second request for the fallback one-use source
  returned `410`.
- Initial host-not-found results came from placing `--user-data-dir` outside
  the isolated HOME; Chromium searched that profile's own
  `NativeMessagingHosts` directory. Relaunching with the disposable data
  directory at `$HOME/.config/chromium` matched the app's registered path and
  made the native handshake succeed. No product code change was needed.
- Verification after the probe: frontend/extension suite `31/31`, TypeScript
  clean through `npm run build:all`, Rust suite `44/44`, cargo build passed,
  and `git diff --check` passed. The sibling's `wait_for_transfer_idle` helper
  remains the only unstaged worktree change and is unmodified; this evidence
  slice stages no sibling hunk.

## 2026-09-04 — Media button capture reaches the native engine

- Started the valid fixture page from `fixtures/` so its real H.264 MP4 served
  successfully (`34,524` bytes, 5 seconds, 640x360). The native-enabled browser
  rendered exactly one `Download` media button while the video was playing
  (`readyState=4`, `paused=false`).
- Clicking that rendered button sent `media-capture` through the extension's
  native bridge. The app created `real.mp4` as `media=true`,
  `mode=single-stream`, `mime=video/mp4`, and reached `finalizing 100%`
  (`34,524` bytes). Its temporary part SHA256 was
  `c5dba3fa0bbbf13a6c8981b1941f9713dc85bd5fb9522bee98945ba0592932c9`,
  identical to the served MP4. No code change; probe evidence only.
- The sibling's `wait_for_transfer_idle` helper remains the only unstaged and
  unmodified worktree change; this evidence slice stages no sibling hunk.

## 2026-09-04 — Policy-off ordinary download stays in Chromium

- Used the real extension popup sender to update the persisted policy to
  `interceptDownloads=false`, `showMediaButtons=true`, with no exclusions. The
  popup reply was `ok=true` and the worker/content-script state followed it.
- A fresh ordinary one-use link on the fixture page was not cancelled:
  `dispatchReturned=true`, `defaultPrevented=false`. Chromium's download
  history reported one complete item at `65536/65536` bytes with
  `state=complete` and `error=null`; the saved file was present in the
  isolated HOME. The app SQLite job count stayed at exactly four, so the
  native app did not consume the source. A second source request returned
  `410`.
- No code change; probe evidence only. The sibling's
  `wait_for_transfer_idle` helper remains the only unstaged and unmodified
  worktree change; this evidence slice stages no sibling hunk.

## 2026-09-04 — Interception and media buttons remain independent

- Audited SPEC §§5.3, 6.4, and 19.2 after the policy-off ordinary-download
  probe. The missing real-browser case was the combination of
  `interceptDownloads=false` with `showMediaButtons=true`.
- Through the real extension popup sender, set that combination in the live
  disposable Chromium profile. On the still-playing fixture MP4 page, the
  content script rendered exactly one `Download` control. An ordinary anchor
  click returned `dispatchReturned=true` and `defaultPrevented=false`; Chromium
  saved `policy-independent-browser.bin` at `34524/34524` bytes with
  `state=complete` and `error=null`.
- Clicking the media control separately reached the native bridge and added a
  new isolated app row for `real.mp4`, `media=true`, `34524/34524`,
  `state=finalizing`, `provisional=true`. No browser download was attributed
  to the media-control click; the ordinary browser copy was the separate
  expected artifact.
- Live-page policy propagation was then exercised without navigation or app
  restart: setting `showMediaButtons=false` produced zero controls while the
  video remained `readyState=4, paused=false`; setting it back to true restored
  one control with text `Download` and aria-label `Download this media`.
  `interceptDownloads` stayed false in both states.
- No code change; this closes the independent-control acceptance gap. The
  sibling's `wait_for_transfer_idle` helper remains the only unstaged and
  unmodified worktree change; this slice stages no sibling hunk.

## 2026-09-04 — Live HLS and dynamic DASH are rejected by the real app

- The fixture audit found live rejection recorded only at parser-unit level,
  despite SPEC §§6.3, 9, 18, and 19.3 requiring a clear failure instead of an
  indefinite recorder. After stopping the known disposable single-instance app,
  ran fresh `--capture` probes against the live fixture server on `:8904`.
- `/hls/live.m3u8` produced exactly one isolated provisional row with
  `state=failed`, `downloaded=0`, no temp output, and the exact error
  `Live media is not supported; a finite VOD playlist is required`.
- `/dash/live.mpd` produced exactly one isolated provisional row with
  `state=failed`, `downloaded=0`, no temp output, and the exact error
  `Live media is not supported; a static MPD is required`.
- Both app processes remained alive until the harness terminated them; neither
  entered an indefinite recording or retry loop. The fixture endpoints returned
  HTTP 200 before the app probes, so these are application rejection results,
  not missing-route errors.
- No code change; real binary failure-path evidence only. The sibling's
  `wait_for_transfer_idle` helper remains the only unstaged and unmodified
  worktree change; this slice stages no hunk.

## 2026-09-04 — Tray Resume All preserves ready provisional acquisitions

- Audit found the tray `resume-all` closure still unconditionally changed every
  paused/pending job to `downloading`, set one connection, and respawned it.
  The main `resume_all` command already avoided that for a ready provisional,
  so tray use could refetch a consumed one-use source after Pause All.
- Added `resume_plan_for_job`, returning the next state, spawn decision, and
  connection count. `resume_job`, the main `resume_all` command, and the tray
  `resume-all` handler now use the same plan. Ready provisionals return to
  `finalizing` / `Ready to save`, with zero connections and no respawn; ordinary
  paused jobs retain the downloading/respawn path.
- TDD evidence: the new tray-plan test was first run RED because the helper was
  absent, then GREEN after the shared implementation. Focused test passed.
- Real rebuilt-app IPC evidence used a fresh one-use fixture: the row reached
  `finalizing`, `100%`, and `65536` bytes; `pause_all` changed it to `paused`
  with zero connections; `resume_all` restored `finalizing` with zero
  connections and preserved the bytes. The second request returned HTTP `410`,
  and deterministic fixture-byte identity passed. Job ID:
  `provisional-ea4b7a73-cbe2-499a-84bc-9ceeb7a3bea0`.
- Rust suite: `45/45`; cargo build passed. Frontend/extension suite: `31/31`;
  `npx tsc -b` and `npm run build:all` passed. The only Rust warning is the
  sibling's untouched unused `wait_for_transfer_idle` helper.
- The tray itself was not driven through XTEST; the shared handler decision is
  covered by the focused Rust regression and the real command path by IPC.
  XTEST remains blocked by the previously recorded `BadValue` failure.
- Only the owned resume-plan code/test hunks belong in this commit. The sibling
  `wait_for_transfer_idle` hunk remains unstaged.

## 2026-09-04 — Per-site media exclusion is honored in a real browser

- Audited SPEC §6.4 after the global media-button toggle proof. The remaining
  unverified policy case was a hostname exclusion while media buttons stayed
  enabled.
- In disposable Chromium 151 with the unpacked extension, the popup writer set
  `{interceptDownloads:true, showMediaButtons:true, excludedSites:["127.0.0.1"]}`.
  The real fixture page had a playing, ready video (`readyState=4`,
  `paused=false`) and rendered zero `#dm-media-download-button` controls.
- A second disposable popup tab cleared `excludedSites` through the supported
  `update-policy` message while the media page stayed open. The page's real
  `HTMLMediaElement.play()` resolved successfully; the same video then had
  `readyState=4`, `paused=false`, and exactly one control with aria-label
  `Download this media`.
- This verifies hostname exclusion and policy recovery without changing the
  resident browser or application. The temporary storage-only write was not
  treated as a product path because the worker's in-memory policy correctly
  remained authoritative until the popup update message.
- No source change; the disposable profile and Chromium process were cleaned
  after the probe. The sibling `wait_for_transfer_idle` helper remains the only
  unstaged worktree change.

## 2026-09-04 — Multi-track DASH segmented restart and recovery proof

- Re-read SPEC §8.5 and §8.7. The relevant contract is that committed jobs
  survive restart with completed segmented fragment state intact, retries are
  bounded and cancellation-aware, and pause/resume must preserve valid work.
- Added `fixtures/segmented_restart_probe.py`. It launches a fresh
  `fixtures/server.py` finite `/dash/manifest.mpd`, fronts it with a local
  counting/delaying proxy, and runs the real debug binary in an isolated HOME
  and dedicated Xvfb. It seeds only three real fragments of a committed,
  non-provisional two-track job and computes the same manifest identity as the
  Rust engine.
- Phase 1 passed: the running app recorded `5/6` fragments before the probe
  SIGKILLed only that disposable app. The three seeded paths were not fetched;
  only the three missing paths were requested once. The SQLite row and five
  non-empty fragment files remained after termination.
- Phase 2 passed: relaunching the binary reused all five persisted fragments.
  The delayed sixth request was cancelled through direct WebKit Inspector IPC;
  the committed row became `failed`, `connections=0`, and its `a-0` request
  count stayed at `1` for one second after cancellation. The normal `retry_job`
  command then fetched only `a-0` and completed the job.
- The recovered output was `86836` bytes with SHA-256
  `b69a17e4dad7e0b7e664b8e31dffbdd92268c3ec57532c87584e500678bbcbf0`. It was
  byte-identical to an independent FFmpeg `-c copy` assembly of the four video
  and two audio fixture fragments.
- Phase 3 passed the retry bound: with `maxRetries=2`, an injected HTTP 503 on
  the missing audio fragment produced exactly `3` attempts, then a failed row
  at `5/6` with no retry storm. Clearing the injected failure and invoking the
  normal retry command reused five fragments, fetched only `a-0`, and produced
  the same final SHA-256.
- Clean command evidence: `python3 -m py_compile
  fixtures/segmented_restart_probe.py && python3
  fixtures/segmented_restart_probe.py` exited `0` and printed
  `PHASE-1-PERSIST: PASS`, `CANCEL-BOUND: PASS`, `RESTART-REUSE: PASS`,
  `RETRY-BOUND: PASS`, `RETRY-RECOVERY: PASS`, and
  `SEGMENTED-RESTART-PROBE: PASS`. The retained probe root was
  `/tmp/dm-segmented-restart-k22e1jke`.
- The probe required three harness corrections before the clean pass: product
  identity grouping was mirrored correctly, WebKitGTK target discovery used
  its emitted page target instead of unsupported `Target.getTargets`, and
  Tauri bridge readiness was awaited. None changed product code.
- Only this probe and this STATUS entry belong in the next commit; the sibling
  `wait_for_transfer_idle` hunk remains untouched.
- Verification after the probe: native `media::tests` passed `8/8`, native
  `lifecycle::tests` passed `5/5`, full Rust passed `45/45`, and `cargo build`
  passed. `npm test` passed `31/31`, `npx tsc -b` passed, and
  `npm run build:all` passed. Cargo reported only the sibling's existing
  `wait_for_transfer_idle` warning.

## 2026-09-04 — Progressive MP4 remains byte-identical

- Audited SPEC §9.1 against `acquire_once`: progressive single-stream media
  moves the completed `.part` directly to the committed destination; segmented
  media alone uses `finalize_media`. A real-binary red probe was added at
  `fixtures/progressive_mp4_probe.py` to verify this branch.
- The probe launched a disposable app under a dedicated Xvfb and served the
  existing `fixtures/real.mp4` through a local counting proxy. It invoked the
  real `create_provisional` and `commit_provisional` commands.
- The provisional temp file was `34524` bytes and had the source SHA-256
  `c5dba3fa0bbbf13a6c8981b1941f9713dc85bd5fb9522bee98945ba0592932c9`.
- The committed destination was also `34524` bytes with the identical SHA-256.
  The proxy recorded exactly one `/media/real.mp4` request. No remux or second
  fetch occurred. This closes the progressive MP4 byte-preservation gap with
  no production-code change.
- The probe's first two launches hit the known disposable WebKit/SIGSEGV race;
  after owned probe-root cleanup, the same scenario passed. The final run
  exited `0` and retained only its small E2E root for inspection.
- The sibling `wait_for_transfer_idle` helper remains the only unstaged change.

## 2026-09-04 — HLS fragmented MP4 with alternate audio

- The DASH restart proof closed the multi-track fMP4 path, but no real binary
  evidence covered HLS `EXT-X-MAP` or HLS alternate audio. Added
  `fixtures/hls_fmp4_probe.py` as a disposable real-app probe.
- The probe serves a finite HLS master plus separate video and audio playlists
  through a local proxy. Every media byte is forwarded from the existing real
  `fixtures/media/{v,a}-*` fMP4 files; no external site or synthetic media is
  involved. It drives `create_provisional` and `commit_provisional` through
  WebKit Inspector IPC in a dedicated Xvfb and isolated HOME.
- Real output passed: the provisional reached `finalizing` at `6/6` fragments
  across `video + audio`; commit produced `86836` bytes with SHA-256
  `b69a17e4dad7e0b7e664b8e31dffbdd92268c3ec57532c87584e500678bbcbf0`, exactly
  matching an independent FFmpeg `-c copy` assembly.
- Request accounting passed: `/hls/master.m3u8`, `/hls/video.m3u8`,
  `/hls/audio.m3u8`, and all six `/dash/*` fMP4 fragments were each requested
  exactly once. This proves HLS map parsing, alternate audio discovery,
  manifest order, and native two-track muxing on the real binary.
- Command evidence: `python3 -m py_compile fixtures/hls_fmp4_probe.py &&
  python3 fixtures/hls_fmp4_probe.py` exited `0` with
  `HLS-FMP4-PROBE: PASS`. No product source changed; the sibling's
  `wait_for_transfer_idle` helper remains the only unstaged worktree change.

## 2026-09-04 — Targeted reattach reuses compatible partial data safely

- Audited SPEC §§8.8–8.9 against `source_compatible` and `acquire_ranges`.
  Reattach is one-shot and targeted: the selected job ID is stored, the next
  visible normal/media capture is accepted only when scheme, host, effective
  service port, and path match; query renewal is allowed.
- Added `fixtures/reattach_probe.py`, using the real rebuilt binary, isolated
  HOME/database, disposable Xvfb/WebKit IPC, the existing deterministic
  `changed.bin` fixture, and a local counting proxy. The seeded committed
  partials use the same full-length sparse `.part` shape as production: the
  first `65536` bytes are present and the file is preallocated to `262144`.
- The compatible renewal URL changed only its query. It produced exactly two
  requests, not a duplicate full acquisition: `Range: bytes=0-0` returned
  `206`, `Content-Range: bytes 0-0/262144`, `1` byte, ETag `"v1"`; then
  `Range: bytes=65536-262143` returned `206`, `196608` bytes, the same ETag.
  The existing first `65536` bytes were reused, one selected job completed,
  and its output SHA-256 was
  `3f1703cb2b1a99b9b700d46a1d2bdfbec74fd50a2fcee3df1070fa6e53e81f87`.
- The changed-resource URL kept the path but returned ETag `"v2"` and
  different bytes. It produced `Range: bytes=0-0` followed by
  `Range: bytes=1-262143`, both `206`; the second response was `262143`
  bytes. The final SHA-256 was
  `437838d6112dca73f4bd8d08c2e792c43207e999336d1422faed6a28744af307`,
  different from the compatible output. This proves the old partial was not
  silently stitched into a resource with a changed validator.
- The first probe run reported two `/changed.bin` requests because its coarse
  counter treated the validator probe and the range fetch alike. Instrumenting
  Range, status, Content-Range, ETag, and byte count resolved that as expected
  protocol behavior. The same run also exposed and corrected a harness-only
  mismatch: its partial file was not preallocated to the persisted total.
- Extended the native regression `reattach_compatibility_ignores_query_but_not_path`
  to reject a changed host as well as changed path, scheme, and service port.
  Focused command: `cargo test --manifest-path src-tauri/Cargo.toml
  reattach_compatibility_ignores_query_but_not_path -- --nocapture` passed
  `1/1` (44 filtered out). The only warning was the protected sibling helper.
- Real probe command `python3 -m py_compile fixtures/reattach_probe.py &&
  python3 fixtures/reattach_probe.py` exited `0` and printed
  `REATTACH-COMPATIBLE: PASS`, `REATTACH-IDENTITY-CHANGE: PASS`, and the
  exact request records above. No production implementation change was needed.
- Only the probe, this status entry, and the host-identity test hunk belong in
  this slice. The sibling `wait_for_transfer_idle` hunk remains untouched and
  must not be staged.

## 2026-09-04 — Committed range jobs recover automatically at startup

- Continued the §8.9 audit into the boot path. `setup` preserves committed
  `connecting`, `downloading`, and `finalizing` rows and respawns their normal
  acquisition engine without UI or browser input.
- Added `fixtures/startup_recovery_probe.py`. It starts the real rebuilt binary
  with an isolated HOME and no command/IPC, after seeding one committed
  `downloading` row with a full-length sparse `.part`, completed range
  `0-65535`, and validator `"v1"`.
- The automatic startup recovery passed with exactly two HTTP requests:
  `GET Range: bytes=0-0` → `206`, `Content-Range: bytes 0-0/262144`, `1`
  byte, ETag `"v1"`; then `GET Range: bytes=65536-262143` → `206`,
  `196608` bytes, the same ETag. No duplicate full fetch occurred.
- The single recovered row completed, its temporary file was removed, and the
  output SHA-256 was
  `3f1703cb2b1a99b9b700d46a1d2bdfbec74fd50a2fcee3df1070fa6e53e81f87`.
  The probe exited `0` with `STARTUP-AUTO-RECOVERY: PASS`.
- Parameterized the shared probe seeder so startup tests can select an active
  persisted state while targeted reattach keeps its paused default. No native
  production source changed in this slice.
- This probe closes automatic startup recovery for ordinary committed range
  jobs. The next related check is a persisted `finalizing` media job, where
  recovery must reuse assembled fragment state rather than refetching or
  overwriting a completed artifact.
- Only `fixtures/reattach_probe.py`, `fixtures/startup_recovery_probe.py`, and
  this status entry belong in this slice. The sibling
  `wait_for_transfer_idle` hunk remains untouched and must not be staged.

## 2026-09-04 — Persisted finalizing media resumes from local fragments

- Continued the §8.9 startup audit with a committed two-track DASH job already
  in `finalizing`, containing all six persisted fragments and the matching
  segment identity.
- Added `fixtures/startup_finalizing_probe.py`. It launches the real rebuilt
  binary in an isolated HOME with no UI command or browser capture. It serves
  the finite DASH manifest through the existing local proxy and counts every
  manifest/fragment request.
- Startup recovery fetched `/dash/manifest.mpd` exactly once and fetched zero
  of the six media fragments. The application rebuilt the video and audio
  tracks from the persisted `.segments` files, moved the muxed output, removed
  the temporary files, and left exactly one completed job.
- The output was byte-identical to the independent FFmpeg reference:
  `86836` bytes, SHA-256
  `b69a17e4dad7e0b7e664b8e31dffbdd92268c3ec57532c87584e500678bbcbf0`.
  Command `python3 -m py_compile fixtures/startup_finalizing_probe.py &&
  python3 fixtures/startup_finalizing_probe.py` exited `0` with
  `STARTUP-FINALIZING-RECOVERY: PASS`.
- This closes persisted `finalizing` recovery for a complete finite multi-track
  job. The next weakness to audit is identity invalidation during startup: a
  changed manifest/segment identity must discard stale local fragments before
  assembling the new resource, rather than silently combining generations.
- Only `fixtures/startup_finalizing_probe.py` and this status entry belong in
  this slice. The sibling `wait_for_transfer_idle` hunk remains untouched and
  must not be staged.

## 2026-09-04 — Startup invalidates stale segmented identities

- Continued the §8.8–§8.9 audit with a persisted `finalizing` job whose six
  local segment files were deliberately replaced by stale-generation bytes.
  Its stored identity described generation 1, while startup received a
  generation-2 manifest with a query-bearing URL for every segment.
- Added `fixtures/startup_identity_probe.py`. The real rebuilt binary starts
  with no UI command or browser input. The proxy records manifest and fragment
  requests; its generation-2 manifest forwards valid fixture fragments while
  the on-disk positional files remain stale.
- Startup fetched the manifest once and then fetched each of the six new
  generation-2 fragments exactly once: `v-init`, `v-0`, `v-1`, `v-2`,
  `a-init`, and `a-0`. It did not reuse the stale files.
- The job completed once, the stale `.segments` directory and `.part` were
  removed, and output matched the independent FFmpeg reference: `86836` bytes
  with SHA-256
  `b69a17e4dad7e0b7e664b8e31dffbdd92268c3ec57532c87584e500678bbcbf0`.
  Command `python3 -m py_compile fixtures/startup_identity_probe.py &&
  python3 fixtures/startup_identity_probe.py` exited `0` with
  `STARTUP-IDENTITY-INVALIDATION: PASS`.
- This closes startup invalidation when the persisted segmented identity no
  longer matches the manifest. The next targeted-reattach check is that an
  incompatible capture leaves the selected target armed and creates its own
  provisional job instead of binding the wrong source.
- Only `fixtures/startup_identity_probe.py` and this status entry belong in
  this slice. The sibling `wait_for_transfer_idle` hunk remains untouched and
  must not be staged.

## 2026-09-04 — Incompatible capture does not consume a reattach target

- Completed the remaining targeted §8.9 bridge safety check with
  `fixtures/reattach_scope_probe.py`. A committed target was explicitly armed
  through real WebKit/Tauri IPC, then an unrelated path was sent through the
  real single-instance `--capture` path.
- The unrelated capture created its own provisional row and failed independently
  after exactly two bounded requests (`Range: bytes=0-0` and an unbounded
  fallback), both HTTP 404. The selected target stayed `pending`, retained its
  original `/changed.bin` source, and remained the only row with its target ID.
- A subsequent renewed query for the target path reattached that same job. It
  completed with the expected validator probe plus missing-range fetch, output
  SHA-256 `3f1703cb2b1a99b9b700d46a1d2bdfbec74fd50a2fcee3df1070fa6e53e81f87`,
  and no duplicate target row.
- Command `python3 -m py_compile fixtures/reattach_scope_probe.py &&
  python3 fixtures/reattach_scope_probe.py` exited `0` with
  `REATTACH-INCOMPATIBLE-ISOLATED: PASS` and
  `REATTACH-COMPATIBLE-AFTERWARD: PASS`. No production source change was
  needed; the existing one-shot target restoration behaves as intended.
- This closes the targeted reattach lifecycle cases covered by §8.9: explicit
  one-job selection, compatible query renewal, validator-change restart, and
  isolation of incompatible captures. The next audit should move to adjacent
  resource-identity/duplicate-transfer behavior rather than reattach itself.
- Only `fixtures/reattach_scope_probe.py` and this status entry belong in this
  slice. The sibling `wait_for_transfer_idle` hunk remains untouched and must
  not be staged.

## 2026-09-04 — Rapid captures keep independent provisional jobs

- Audited SPEC §7.4 against the real single-instance capture path. Added
  `fixtures/simultaneous_capture_probe.py`, which launches the resident app in
  an isolated HOME, waits for WebKit/Tauri readiness, and sends two rapid
  captures for different deterministic resource variants.
- The real application created two distinct provisional IDs. Each row retained
  its own source URL and filename, and each temporary output matched its own
  independently fetched variant. No row was retargeted to the other capture.
- The proxy recorded four range requests total: the expected `0-0` probe and
  one missing-range request for each of the two resources. Both provisional
  rows reached `finalizing` with truthful state and remained independently
  addressable.
- Command `python3 -m py_compile fixtures/simultaneous_capture_probe.py &&
  python3 fixtures/simultaneous_capture_probe.py` exited `0` with
  `SIMULTANEOUS-CAPTURES: PASS` and `CAPTURE-SOURCES: PASS`. No production
  source change was needed.
- A first attempt launched two native-instance processes at exactly the same
  instant and hit the disposable single-instance launcher's startup race
  before row inspection. The durable probe uses two rapid sequential launches,
  which isolates the §7.4 capture-addressing contract from that launcher race.
- This closes the ordinary repeated-capture isolation case. The next audit can
  move to adjacent collision/overwrite and duplicate-destination semantics.
- Only `fixtures/simultaneous_capture_probe.py` and this status entry belong in
  this slice. The sibling `wait_for_transfer_idle` hunk remains untouched and
  must not be staged.

## 2026-09-04 — Reserve collision destinations at the final move boundary

- Audited SPEC §11.2 and the commit path. The previous `collision_destination`
  check ran before asynchronous finalization, so two ready provisional jobs
  could both select the same free path. On Unix, the later rename could replace
  the earlier completed file.
- Added `reserve_collision_destination` in `src-tauri/src/main.rs`. Rename
  behavior now claims the selected path with exclusive `create_new` semantics
  immediately before moving the completed file. A competing commit advances to
  the next `(n)` name. If the move fails, the reservation is removed; replace
  behavior remains explicit and unchanged.
- Added native regression
  `collision_reservation_allocates_distinct_paths_before_moves`. It passed with
  `1 passed; 45 filtered out`.
- Added `fixtures/collision_commit_probe.py`. The real rebuilt application
  created two provisional captures, waited until both were `finalizing`, and
  issued concurrent `commit_provisional` calls through WebKit/Tauri IPC. It
  completed both rows at distinct destinations:
  `/tmp/.../Downloads/same.bin` and `/tmp/.../Downloads/same (1).bin`.
- The two output hashes were, in job order,
  `3f1703cb2b1a99b9b700d46a1d2bdfbec74fd50a2fcee3df1070fa6e53e81f87` and
  `437838d6112dca73f4bd8d08c2e792c43207e999336d1422faed6a28744af307`,
  matching the two independent fixture variants. No output was overwritten.
- The first probe versions exposed harness errors rather than product behavior:
  exact-concurrent launcher startup raced, WebKit void-return serialization
  was `{}`, and pre-seeded `finalizing`/provisional rows mixed startup recovery
  into the test. The final probe uses the already-proven bootstrap, one paused
  managed setup row, real single-instance captures, SQLite row discovery, and
  concurrent commit IPC. The successful run exited `0` with
  `COMMIT-RESULT: {}` and `COLLISION-RESERVATION: PASS`.
- After the reservation cleanup repair, the affected and full checks passed:
  native `46/46`, Cargo build, frontend/extension `31/31`, TypeScript build,
  and `npm run build:all`. The only warning is the protected sibling
  `wait_for_transfer_idle` helper being unused.
- This closes the verified concurrent rename-collision case.
- Follow-up verification added `fixtures/replace_commit_probe.py`. With an
  existing `replace.bin` and `collisionBehavior=replace`, the real provisional
  job completed at the same path and replaced the old bytes. Result:
  `REPLACE-COLLISION: PASS`, output SHA-256
  `3f1703cb2b1a99b9b700d46a1d2bdfbec74fd50a2fcee3df1070fa6e53e81f87`.
- The next audit should cover replace-mode failure cleanup under a deliberately
  unwritable destination, without staging the sibling hunk.

## 2026-09-04 — Preserve replace targets when final move fails

- Follow-up audit found that replace mode removed the existing destination before
  moving the completed temp file. If the temp disappeared between finalization
  and commit, the old file was lost. The fallback copy path also removed its
  destination after a copy error, which could destroy an existing replacement
  target.
- Product repair: `move_completed_file` now receives explicit
  `replace_existing` and `reserved` flags. Replace mode keeps the old target
  until the new file is ready; cross-device/Windows fallback copies to a fresh
  staging path, temporarily backs up the old file, restores it if the final
  swap fails, and cleans staging artifacts. Rename reservations use direct copy
  into their owned empty reservation on fallback and remove it on failure.
- The three automatic finalization call sites no longer pre-delete their
  destinations. They pass the collision mode to the move helper. Automatic
  rename-mode reservation remains the next adjacent audit; this slice fixes
  failure safety without claiming that broader path is closed.
- Added `fixtures/replace_failure_probe.py`. It created a real provisional job,
  waited for `finalizing`, wrote an existing destination, deleted only the
  persisted temp file, and issued replace-mode commit through WebKit/Tauri IPC.
  The real rebuilt binary returned the expected failure and preserved the old
  destination. Result: `REPLACE-FAILURE-CLEANUP: PASS`; error was
  `No such file or directory (os error 2)` and the old bytes remained.
- Native tests passed `46/46` and Cargo build passed. The only warning remains
  the protected sibling `wait_for_transfer_idle` being unused.

## 2026-09-04 — Automatic finalization reserves managed destinations

- Re-audited the collision fix at all three automatic final-move boundaries:
  ranged objects, segmented media, and single-stream objects. Rename mode now
  reserves the destination with exclusive `create_new` semantics immediately
  before the move, while explicit `replace` mode keeps its replacement path.
  When a reservation changes the path, the persisted job name/destination and
  warning event are updated before completion.
- The reservation cleanup path now removes its owned empty destination on an
  ordinary move error as well as on cancellation. The existing sibling
  `wait_for_transfer_idle` helper was not staged or changed.
- Real managed-download probe `python3 fixtures/managed_collision_probe.py`
  exited `0` with:
  `MANAGED-RENAME-COLLISION: PASS (job=managed-collision,
  destination=/tmp/dm-managed-collision-.../home/Downloads/managed (1).bin,
  sha256=3f1703cb2b1a99b9b700d46a1d2bdfbec74fd50a2fcee3df1070fa6e53e81f87)`.
  The pre-existing `managed.bin` retained `OLD-MANAGED-DOWNLOAD`, and the
  completed output at `managed (1).bin` matched the fixture bytes.
- The concurrent provisional collision probe was rerun after this change and
  exited `0`: `same.bin` and `same (1).bin` were distinct, with hashes
  `3f1703cb2b1a99b9b700d46a1d2bdfbec74fd50a2fcee3df1070fa6e53e81f87` and
  `437838d6112dca73f4bd8d08c2e792c43207e999336d1422faed6a28744af307`.
  The initial inspector failure was harness-only; the corrected probe uses
  propagated Xvfb display state and retained application logs.
- The current full verification passed: Rust `46/46`; Cargo build; Vitest
  frontend/extension `7` files and `31/31` tests; `npx tsc -b`; and
  `npm run build:all` (frontend plus extension). The only warning is the
  protected sibling `wait_for_transfer_idle` helper being unused.
- The index contains only the owned automatic-finalization changes,
  `fixtures/collision_commit_probe.py`, `fixtures/managed_collision_probe.py`,
  and this STATUS entry. `fixtures/__pycache__` remains untracked and
  unstaged. The next audit should test automatic-finalization failure and
  cancellation after a destination reservation, especially whether a
  persisted renamed destination can be safely retried without stale ownership.

## 2026-09-04 — Recover persisted destination reservations safely

- The next ownership audit found a crash window after `create_new` reserved a
  final path but before the completed bytes replaced the placeholder. An empty
  placeholder had no ownership marker, so startup could leave it behind and
  retry at a suffixed path. This was unsafe for cleanup and could create a
  duplicate destination on recovery.
- Product repair: rename-mode reservations now write a random
  `download-manager-reservation-v1:` marker and persist it as the optional
  `destinationReservation` job field before the move. Startup reconciles that
  field before spawning recovery: an exact marker is removed and retried at the
  original path; any different file is preserved as a completed move and the
  active job is completed; unreadable paths are left untouched. Normal success,
  cancellation-before-move, and move-error paths clear the persisted marker.
  Explicit replace mode does not create a reservation marker.
- Added the Rust regression
  `destination_reservation_recovery_removes_only_its_marker`, which proves an
  exact marker is reclaimed while completed output bytes survive. The old
  collision unit now exercises the marker-producing reservation function
  directly; the protected sibling `wait_for_transfer_idle` hunk remains
  unstaged.
- Added `fixtures/startup_reservation_probe.py`. It seeds a real SQLite job
  with a complete persisted temp file, a matching destination marker, and a
  `destinationReservation` field, then relaunches the real binary. The probe
  exited `0` with:
  `STARTUP-RESERVATION-RECOVERY: PASS
  (job=startup-reservation-recovery, destination=managed.bin,
  sha256=3f1703cb2b1a99b9b700d46a1d2bdfbec74fd50a2fcee3df1070fa6e53e81f87)`.
  The final database field was cleared, the original destination was reused,
  the marker was replaced by the expected bytes, and the only network request
  was `GET /changed.bin?variant=1` with `Range: bytes=0-0` and HTTP `206`.
- Current complete verification passed after this slice: Rust `47/47`; Cargo
  build; Vitest frontend/extension `7` files and `31/31` tests; `npx tsc -b`;
  and `npm run build:all` (frontend plus extension). The only native warning
  is the protected sibling `wait_for_transfer_idle` helper being unused.

## 2026-09-04 — Reclaim interrupted reservation writes

- A follow-up audit found that a crash while writing the reservation token could
  leave an empty or prefix-only destination. The reconciler previously treated
  an empty file as completed output. It now reclaims empty and prefix-only
  reservation bytes, while preserving an empty file only when the persisted job
  total is exactly zero. A marker mismatch with ordinary non-empty bytes still
  means the move completed and those bytes are preserved.
- Extended the Rust regression with exact, prefix-only, empty/non-zero, and
  empty/zero-length cases. The protected sibling `wait_for_transfer_idle`
  hunk remains unstaged.
- Extended `fixtures/startup_reservation_probe.py` to seed a half-written
  marker in SQLite and the destination. The rebuilt real binary exited `0`:
  `STARTUP-INCOMPLETE-RESERVATION-RECOVERY: PASS
  (job=startup-reservation-recovery, destination=managed.bin,
  sha256=3f1703cb2b1a99b9b700d46a1d2bdfbec74fd50a2fcee3df1070fa6e53e81f87)`.
  It reused the original destination, cleared `destinationReservation`,
  removed the interrupted marker, reused the complete temp file, and made only
  the one-byte `Range: bytes=0-0` request.

## 2026-09-04 — Final move wins a pause/cancel race

- The next finalization audit found that a pause or cancel arriving after the
  durable move began could leave a completed destination paired with a paused
  job. A later resume could fetch the source again and create a suffixed
  duplicate. The post-move ownership check is now applied only before the move;
  once the move succeeds, completion wins, temporary cleanup runs, and the
  destination reservation is cleared. Provisional commit uses the same rule
  even if the job state changed from `finalizing` during the move.
- Added `fixtures/cancel_during_move_probe.py`. It used a real `/tmp` to `/srv`
  cross-filesystem move so the product entered its fallback copy path, paused
  the job after the destination exceeded 1 MiB, and waited for the real Tauri
  commit to finish. The probe exited `0` with:
  `MOVE-CANCEL-RACE: PASS
  (job=provisional-ab676467-85c0-41ff-a0ce-262b22521f5e, state=completed,
  bytes=268435456,
  sha256=a292ece20ee4810922263532e87657a77cc95e190389cc2b197a5db9114f7b8b)`.
  The requested destination remained the only output, the temp source was
  removed, `destinationReservation` was cleared, and exactly one job row
  remained. Tauri IPC returned `{}` for the void command.

## 2026-09-04 — Recognize Windows replacement errors

- The move-helper audit found that fallback selection matched raw Linux
  `EEXIST`/`EXDEV` values `17`/`18` only. Windows reports an existing target as
  `ERROR_ALREADY_EXISTS` (`183`), so reserved rename moves and explicit replace
  moves could fail instead of entering the tested fallback path.
- Added `move_needs_fallback`, which accepts the portable Rust
  `ErrorKind::AlreadyExists` plus raw `17`, `18`, and `183`, and routed both
  reserved and replace fallback branches through it. The protected sibling
  `wait_for_transfer_idle` remains unstaged.
- Added the Rust regression
  `move_fallback_accepts_existing_and_cross_device_errors`: all three platform
  codes pass and an unrelated error is rejected. The real cross-filesystem
  move probe was rerun against the rebuilt binary and passed with one durable
  268435456-byte output and SHA-256
  `a292ece20ee4810922263532e87657a77cc95e190389cc2b197a5db9114f7b8b`.

## 2026-09-04 — Stage cross-filesystem reserved moves atomically

- The move-helper audit found that reserved cross-filesystem fallback copied
  directly into the final destination. A reader could observe partial bytes,
  and cleanup removed the destination without checking that it still belonged
  to the manager.
- `move_completed_file` now copies reserved moves to a same-filesystem
  `.download-manager-staging-*` path, renames the complete staging file into
  place, and removes a reservation only when its marker still matches. The
  reservation token is passed from all four finalization callers. Windows
  replacement of the marker is handled by an ownership check before removing
  the marker; explicit replace fallback keeps its existing backup path.
- The corrected `fixtures/cancel_during_move_probe.py` retains application logs
  on startup failure and watches the staging path. Against the rebuilt binary it
  exited `0` with:
  `MOVE-CANCEL-RACE: PASS
  (job=provisional-e3e324f6-02fc-4192-a49e-c0d00690a33c, state=completed,
  bytes=268435456,
  sha256=a292ece20ee4810922263532e87657a77cc95e190389cc2b197a5db9114f7b8b)`.
  During the copy the final destination stayed below 1 KiB; pause then arrived
  after staging exceeded 1 MiB. The final output was the only output, and both
  temp and staging files were removed.

## 2026-09-04 — Restore reservations after a failed staging handoff

- The remaining handoff audit found a Windows-style failure window: after the
  marker was removed to permit installation, a failed final staging rename
  could leave neither the destination nor an owned reservation. A caller would
  then clear the persisted reservation field, making the incomplete move
  unrecoverable.
- `install_reserved_staging` now verifies the exact marker before removal. If
  the final install fails, it recreates the marker with exclusive `create_new`
  semantics only when the destination is absent. If another actor has created a
  destination, it preserves that path and reports that ownership changed. The
  unique staging file is cleaned on every failed handoff. `clear_destination_reservation`
  now removes a restored marker only after checking its exact bytes, keeping the
  persisted field and filesystem ownership aligned during normal cleanup while
  leaving both available for startup recovery if the process crashes first.
- Added the deterministic Rust regression
  `reserved_staging_install_restores_marker_when_final_rename_fails`. A
  test-only fault is injected immediately after marker removal; the helper
  returns the expected error, restores the exact marker, and removes staging.
  The fault hook is compiled out of production binaries.
- Fresh real-binary probe run used `set -euo pipefail`; every command exited
  successfully:
  `MOVE-CANCEL-RACE: PASS`
  (job=provisional-c67cadcd-0d5b-4d73-b6ee-da062ada114e,
  state=completed, bytes=268435456,
  sha256=a292ece20ee4810922263532e87657a77cc95e190389cc2b197a5db9114f7b8b);
  `MANAGED-RENAME-COLLISION: PASS` with output `managed (1).bin` and the
  pre-existing target preserved;
  `REPLACE-COLLISION: PASS`;
  `REPLACE-FAILURE-CLEANUP: PASS` with the old destination preserved after a
  missing-source error;
  `STARTUP-INCOMPLETE-RESERVATION-RECOVERY: PASS` with one `bytes=0-0`
  request and the expected output hash; and
  `COLLISION-RESERVATION: PASS` with distinct `same.bin`/`same (1).bin`
  outputs and distinct hashes.
- Native verification at this checkpoint passed `49/49` tests and Cargo build.
  The only warning remains the protected sibling `wait_for_transfer_idle`
  helper being unused. The replacement-failure probe also received display
  propagation and retained app-log diagnostics after one harness-only
  inspector-startup failure; its isolated rerun exited `0`.

## 2026-09-04 — Preserve durable output when source cleanup fails

- The next finalization audit found that both cross-filesystem fallback branches
  deleted the final destination if removing the temporary source failed after a
  successful install. That could destroy the only durable output and turn a
  cleanup error into data loss.
- Added `remove_completed_source` and routed reserved and explicit-replace
  fallback cleanup through it. A source-removal error is now reported without
  deleting the already-installed destination. Test-only controls force the
  fallback path and source-cleanup failure; production builds compile those
  controls out.
- Added the deterministic Rust regression
  `durable_reserved_destination_survives_source_cleanup_failure`. It forces a
  reserved fallback, injects failure at source cleanup, and verifies the
  complete destination bytes remain while the source remains available for
  diagnosis or retry.
- Fresh native verification passed `50/50` tests and Cargo build. The only
  warning remains the protected sibling `wait_for_transfer_idle` helper being
  unused.
- Fresh fail-fast real-binary probes all exited successfully:
  `MOVE-CANCEL-RACE: PASS` with one 268435456-byte output and SHA-256
  `a292ece20ee4810922263532e87657a77cc95e190389cc2b197a5db9114f7b8b`;
  `MANAGED-RENAME-COLLISION: PASS` with `managed (1).bin`;
  `REPLACE-COLLISION: PASS`;
  `REPLACE-FAILURE-CLEANUP: PASS` with the old destination preserved;
  `STARTUP-INCOMPLETE-RESERVATION-RECOVERY: PASS` with one `bytes=0-0`
  request; and `COLLISION-RESERVATION: PASS` with distinct output names and
  hashes.

## 2026-09-04 — Preserve changed destinations on reserved move errors

- The next ownership audit found that the generic initial-move error branch
  removed every reserved destination without checking its marker. A destination
  replaced by another actor could therefore be deleted after an unrelated move
  error.
- Reserved cleanup now uses the exact-marker ownership check already used by
  reservation rollback. Matching markers are reclaimable; changed or unreadable
  destinations are left untouched. A test-only initial-error injection makes
  this race deterministic without affecting production builds.
- Added the Rust regression
  `reserved_nonfallback_error_preserves_changed_destination`. It forces a
  non-fallback move error after the reservation path points at a foreign file,
  verifies the error is returned, and verifies the foreign bytes survive.
- Fresh native verification passed `51/51` tests and Cargo build. The only
  warning remains the protected sibling `wait_for_transfer_idle` helper being
  unused.
- Fresh fail-fast real-binary probes all exited successfully:
  `MOVE-CANCEL-RACE: PASS` with one 268435456-byte output and SHA-256
  `a292ece20ee4810922263532e87657a77cc95e190389cc2b197a5db9114f7b8b`;
  `MANAGED-RENAME-COLLISION: PASS`; `REPLACE-COLLISION: PASS`;
  `REPLACE-FAILURE-CLEANUP: PASS`; `STARTUP-INCOMPLETE-RESERVATION-RECOVERY: PASS`
  with one `bytes=0-0` request; and `COLLISION-RESERVATION: PASS` with
  distinct output names and hashes.

## 2026-09-04 — Keep foreign files safe during reservation abort cleanup

- The next audit found five cancellation/commit cleanup branches that still
  called unconditional `remove_file(destination)` whenever a reservation flag
  was set. If a foreign file replaced the marker before cancellation or move
  failure, cleanup could delete that file.
- Added `cleanup_reserved_destination`, which delegates to exact-marker
  ownership verification, and routed all five branches through it. Matching
  markers are removed; changed, unreadable, or missing destinations are not
  touched.
- Added the deterministic Rust regression
  `abort_cleanup_preserves_changed_reserved_destination`. It presents a
  foreign destination under a reservation token, runs the abort cleanup seam,
  and verifies the foreign bytes remain.
- Fresh native verification passed `52/52` tests and Cargo build. The only
  warning remains the protected sibling `wait_for_transfer_idle` helper being
  unused.
- Fresh fail-fast real-binary probes all exited successfully:
  `MOVE-CANCEL-RACE: PASS` with one 268435456-byte output and SHA-256
  `a292ece20ee4810922263532e87657a77cc95e190389cc2b197a5db9114f7b8b`;
  `MANAGED-RENAME-COLLISION: PASS`; `REPLACE-COLLISION: PASS`;
  `REPLACE-FAILURE-CLEANUP: PASS`; `STARTUP-INCOMPLETE-RESERVATION-RECOVERY: PASS`
  with one `bytes=0-0` request; and `COLLISION-RESERVATION: PASS` with
  distinct output names and hashes.

## 2026-09-04 — Defer foreign reservation tokens instead of false completion

- Auditing the previous mismatch-token fix found a second semantic risk: returning
  `Completed` for a different job's reservation token would mark the persisted
  job complete and discard its resumable temp state.
- Recovery now returns `Unknown` for a different full reservation token. The
  marker remains untouched, and startup leaves the job's state and temp data
  available for safe retry or inspection. Exact markers and strict own-token
  prefixes still return `Retry` and are reclaimed.
- The recovery regression now asserts `Unknown` plus preserved bytes for the
  mismatched token. Before this correction it failed with `left: Completed,
  right: Unknown`; it now passes.
- Fresh native verification passed `52/52` tests and Cargo build. The only
  warning remains the protected sibling `wait_for_transfer_idle` helper being
  unused.
- Fresh fail-fast real-binary probes all exited successfully:
  `MOVE-CANCEL-RACE: PASS` with one 268435456-byte output and SHA-256
  `a292ece20ee4810922263532e87657a77cc95e190389cc2b197a5db9114f7b8b`;
  `MANAGED-RENAME-COLLISION: PASS`; `REPLACE-COLLISION: PASS`;
  `REPLACE-FAILURE-CLEANUP: PASS`; `STARTUP-INCOMPLETE-RESERVATION-RECOVERY: PASS`
  with one `bytes=0-0` request; and `COLLISION-RESERVATION: PASS` with
  distinct output names and hashes.

## 2026-09-04 — Report replace rollback failures and retain the backup

- The explicit-replace fallback previously ignored errors while restoring the
  old destination after a failed staged install. The resulting failure could
  hide the only remaining copy under a generated backup path.
- Added `restore_replacement_backup` and report its error, including the exact
  retained backup path, while leaving the backup untouched for recovery.
- Added the deterministic Rust regression
  `replacement_rollback_reports_preserved_backup_on_restore_failure`. It
  injects a restoration failure and verifies the backup remains and no false
  destination is created.
- Fresh native verification passed `53/53` tests and Cargo build. The only
  warning remains the protected sibling `wait_for_transfer_idle` helper being
  unused.
- Fresh fail-fast real-binary probes all exited successfully:
  `MOVE-CANCEL-RACE: PASS` with one 268435456-byte output and SHA-256
  `a292ece20ee4810922263532e87657a77cc95e190389cc2b197a5db9114f7b8b`;
  `MANAGED-RENAME-COLLISION: PASS`; `REPLACE-COLLISION: PASS`;
  `REPLACE-FAILURE-CLEANUP: PASS`; `STARTUP-INCOMPLETE-RESERVATION-RECOVERY: PASS`
  with one `bytes=0-0` request; and `COLLISION-RESERVATION: PASS` with
  distinct output names and hashes.

## 2026-09-04 — Verify HLS fMP4 alternate-audio finalization

- Reran the previously unconfirmed `fixtures/hls_fmp4_probe.py` against the
  rebuilt binary. It uses only local finite fixture bytes and exercises HLS
  master/video/audio manifests, `EXT-X-MAP` fMP4 initialization fragments,
  parallel acquisition, and native FFmpeg muxing.
- The real probe passed with 6/6 segments, video plus alternate audio, each
  manifest and segment fetched exactly once, and an 86836-byte output with
  SHA-256 `b69a17e4dad7e0b7e664b8e31dffbdd92268c3ec57532c87584e500678bbcbf0`.

## 2026-09-04 — Close startup and media recovery probe gaps

- `startup_finalizing_probe.py` passed against the rebuilt binary. It restored
  the persisted finalizing HLS media job byte-for-byte, with one manifest
  request and SHA-256 `b69a17e4dad7e0b7e664b8e31dffbdd92268c3ec57532c87584e500678bbcbf0`.
- The first combined specialized batch reached segmented persistence,
  cancellation, and restart reuse, then hit a transient third-app `-11`
  before Inspector readiness. No product log was produced. The isolated rerun
  passed every segmented phase: 5/6 fragment persistence, bounded cancel,
  restart reuse, bounded three-attempt retry, and manual retry recovery, with
  the same 86836-byte reference hash.
- The subsequent fail-fast batch passed targeted reattach (query renewal and
  validator identity change), incompatible reattach isolation, startup
  segmented identity invalidation, simultaneous independent captures, and
  progressive MP4 byte preservation. The progressive result was 34524 bytes
  with SHA-256 `c5dba3fa0bbbf13a6c8981b1941f9713dc85bd5fb9522bee98945a0592932c9`.

## 2026-09-04 — Preserve segmented parts when FFmpeg is unavailable

- `finalize_media` previously treated an absent `ffmpeg` executable as success.
  That could allow unremuxed segmented bytes to move as a completed output.
- Missing FFmpeg now returns an explicit finalization failure:
  `FFmpeg is required to finalize segmented media; downloaded parts were preserved`.
  The existing temporary media file and downloaded parts remain available for
  retry or diagnosis.
- Added focused coverage to keep the missing-tool message distinct from a
  non-zero FFmpeg exit while asserting the parts-preserved contract.
- Focused remux regression passed. The complete native suite passed `53/53`,
  Cargo build passed, Vitest passed with `7` files and `31/31` tests, TypeScript
  compilation passed, and both frontend and extension builds passed.
- Fresh real-binary probes passed: HLS fMP4 alternate-audio finalization
  (`ready=6/6`, video plus audio, 86836 bytes, SHA-256
  `b69a17e4dad7e0b7e664b8e31dffbdd92268c3ec57532c87584e500678bbcbf0`) and
  startup finalizing recovery with one manifest request and the same hash.
- Two later isolated HLS retries exited `-11` before Inspector readiness. Their
  application logs contained only the existing libayatana-appindicator
  deprecation warning and no product error. The independent startup-finalizing
  rerun passed; the earlier retained HLS pass remains the media-path evidence.

## 2026-09-04 — Prove retry after missing FFmpeg preserves media parts

- Added `fixtures/missing_ffmpeg_recovery_probe.py` for the single-track HLS
  fMP4 path that calls `finalize_media` rather than the separate-track muxer.
- The real application ran with an isolated PATH containing no FFmpeg. Commit
  reached `failed` with the explicit error `FFmpeg is required to finalize
  segmented media; downloaded parts were preserved`; all four fragments stayed
  on disk and no destination file was created.
- Adding the real FFmpeg executable to that same PATH and invoking the normal
  Retry action completed the job at 32057 bytes with SHA-256
  `6b6875bc6d4c6233f362624c1d5e919993e49f0e7530fc007cfb2d6d17167861`.
- The retry refetched the two HLS playlist manifests but fetched zero media
  fragments again. The four preserved parts were removed after successful
  finalization.

## 2026-09-04 — Support HLS byte-range media segments

- Audited finite HLS handling against RFC 8216 `EXT-X-BYTERANGE` and found
  that each `Segment` stored only a URL, so a playlist that sliced one resource
  would download the entire resource for every media entry.
- Added optional `(start, length)` ranges to HLS segments, including
  `EXT-X-MAP:BYTERANGE`, strict offset/overflow validation, range-aware
  segmented identity, and HTTP `Range`/`206 Content-Range` validation.
- Added parser coverage for explicit offsets, implicit continuation on the same
  resource, ranged initialization maps, and rejection of an unsafe first
  implicit range.
- Full native verification passed: Rust `54/54`; Cargo build passed. The only
  warning remains the protected unused `wait_for_transfer_idle` helper.
- Added and ran `fixtures/hls_byterange_probe.py` against the rebuilt binary.
  It completed `3/3` segments and produced the expected 18-byte concatenation,
  SHA-256 `0d819259a1693bddf406312b22fc8572f168064422e992ccf35ac952be3b115c`.
  The server observed exactly `bytes=0-4`, `bytes=5-10`, and `bytes=20-26`
  (concurrent arrival order was intentionally not assumed).

## 2026-09-04 — Support DASH SegmentList byte ranges

- Audited static DASH `SegmentList` handling and found that `mediaRange` and
  `Initialization range` attributes were discarded, causing whole shared
  resources to be fetched for each logical segment.
- Extended the shared segment model and DASH parser to preserve explicit byte
  ranges, validate `start-end` arithmetic, include ranges in segmented identity,
  and use the existing strict HTTP range-response path.
- Unit coverage passed for ranged initialization/media entries and malformed
  reversed ranges.
- Full native verification passed: Rust `54/54`; Cargo build passed. The only
  warning remains the protected unused `wait_for_transfer_idle` helper.
- Added and ran `fixtures/dash_byterange_probe.py` against the rebuilt binary.
  It completed `3/3` segments and produced the expected 15-byte concatenation,
  SHA-256 `a9342f061651954a0223ba72bfa54ff6629ecfe72d97c975e5effe289d740f53`.
  The server observed exactly `bytes=0-4`, `bytes=20-25`, and `bytes=30-33`.

## 2026-09-04 — Prove resident single-instance capture forwarding

- Added `fixtures/single_instance_probe.py` to launch the rebuilt application
  twice in one isolated HOME: first as the resident process, then with a real
  `--capture` payload.
- The real two-process probe passed twice. The second process exited `0`, the
  resident first PID stayed alive, `/proc` reported exactly one product
  process, and the resident SQLite database contained one provisional job.
- The local source server saw exactly one GET. The forwarded job reached
  `finalizing` with all 32768 bytes downloaded, proving the second launch was
  forwarded rather than starting an independent database/job owner.
- Probe output was:
  `SINGLE-INSTANCE: PASS (second_exit=0, resident_pid=2392036, jobs=1, state=finalizing, bytes=32768, source_requests=1)`.
- The resident application log contained only the known
  `libayatana-appindicator is deprecated` warning. The forwarded process log
  was empty. No product error was observed.
- The probe disables Python bytecode generation so shared fixture imports do
  not leave disposable `fixtures/__pycache__` files in the worktree.

## 2026-09-04 — Prove close-to-tray keeps the resident transfer alive

- Added `fixtures/close_to_tray_probe.py` for SPEC §19.1. It starts the real
  application with a fresh HOME, forwards a capture through a second process,
  and uses the actual manager Close button while the transfer is active.
- The probe first observed a non-zero `downloading` job, then clicked
  `button[aria-label="Close"]`. The native window was subsequently reported
  by `xwininfo` as `Map State: IsUnMapped` while the resident PID remained
  alive and `/proc` still reported exactly one product process.
- The same provisional job continued to `finalizing` with all 67108864 bytes
  downloaded. The local slow source observed exactly one request, so closing
  the manager did not cancel or duplicate the transfer.
- Real probe output:
  `CLOSE-TO-TRAY-DIAGNOSTIC: plugin_is_visible={}; manager_id=0x200003; Map State: IsUnMapped`
  and
  `CLOSE-TO-TRAY: PASS (main_visible={}, resident_pid=2395598, processes=1, state=finalizing, bytes=67108864, source_requests=1)`.
- The raw Inspector `is_visible` getter returned `{}` through this direct
  bridge, so the probe uses the native X11 map state as the visibility proof.
  The resident log contained only the known libayatana-appindicator
  deprecation warning and no product error.

## 2026-09-04 — Prove three independent Add Download windows

- Added `fixtures/multiple_add_windows_probe.py` for SPEC §19.1. It starts one
  resident application and sends three real `--capture` launches for distinct
  local sources through the single-instance forwarding path.
- The rebuilt binary created three distinct SQLite jobs and three native
  `Add Download` windows. The resident process stayed alive and `/proc`
  reported exactly one product process.
- Each source was requested exactly once: `one.bin`, `two.bin`, and
  `three.bin`. The windows therefore remained independently addressable rather
  than retargeting one capture or creating duplicate product owners.
- Real probe output:
  `MULTIPLE-ADD-WINDOWS: PASS (windows=3, jobs=3, processes=1, source_requests={'three.bin': 1, 'two.bin': 1, 'one.bin': 1})`.
- The resident application log contained only the known
  `libayatana-appindicator is deprecated` warning and no product error.

## 2026-09-04 — Degrade segmented media to sequential acquisition

- Added `fixtures/sequential_only_probe.py` for SPEC §19.4. Its local finite
  HLS source returns HTTP `429 Too Many Requests` whenever a second segment
  overlaps the first. The initial real-binary run failed at one of four
  fragments with `source returned 429 Too Many Requests`, proving that the
  previous concurrent-only path did not meet the sequential-source acceptance.
- The acquisition path now preserves successful fragment files, retries only
  missing fragments with one active connection, reports the transition as
  `Retrying sequentially`, and keeps bounded 100 ms spacing on fragment
  retries. Persistent errors still fail honestly with the original and
  fallback causes.
- The corrected real probe passed with four segments and 131072 bytes. The
  server rejected five overlapping requests, then served all segments through
  the serial fallback: `SEQUENTIAL-ONLY: PASS (state=finalizing, segments=4,
  bytes=131072, rejected_overlaps=5, requests={'seg2.ts': 1, 'seg1.ts': 3,
  'seg0.ts': 2, 'seg3.ts': 3})`.
- Added `fixtures/sequential_pause_probe.py`. It invokes the real `pause_job`
  command as soon as the sequential fallback begins. The probe passed with
  `state=paused`, `completed=1/4`, and `downloaded=262144` after four overlap
  rejections; valid work was retained and no error was recorded.
- Cargo verification after the product change passed Rust `54/54` and the
  native build. The only Rust warning remains the protected sibling
  `wait_for_transfer_idle` helper. The successful pause probe log contained
  only the shared-Xvfb accessibility-bus warning and the known
  libayatana-appindicator deprecation warning.

## 2026-09-04 — Verify exit behavior through native window state

- The direct Inspector Tauri getter could not be used: object and primitive
  command results were serialized as `{}` by this WebKit bridge. The probe
  therefore seeds the isolated settings row with `closeBehavior=exit` and
  verifies the rendered Settings `<select>` value instead.
- JavaScript `current.close()` did not produce a native `CloseRequested` event
  in this headless runtime. The final probe asserts the manager window is
  `IsViewable` and sends a real X11 `WM_DELETE_WINDOW` with `xdotool`.
- The rebuilt product lets the native close complete, then schedules the
  documented Tauri `AppHandle::exit(0)` request. The resident process
  terminated and did not exit by `SIGSEGV`:
  `EXIT-BEHAVIOR: PASS (closeBehavior=exit, resident_exit=1,
  native_close=WM_DELETE_WINDOW)`.
- Headless Xvfb/GTK reported `BadDrawable` after the window-close message, so
  this environment returns exit code `1` despite the process termination. The
  retained log contained only that GTK warning and the known
  libayatana-appindicator deprecation warning. This is recorded as a harness
  limitation, not hidden as a clean exit code.
- Full native verification after the change passed Rust `54/54`; Cargo build
  passed. The only Rust warning remains the protected unused sibling helper.

## 2026-09-04 — Prove real Chromium public-site capture paths

- Headless Chrome for Testing 149 uses a custom-profile native-host lookup that
  was not covered by the old fixture manifest placement. The probe initially
  reported `Specified native messaging host not found`; writing the manifest
  into the profile's `NativeMessagingHosts` directory and isolating
  `XDG_CONFIG_HOME` fixed the test boundary. Product registration now also
  includes `google-chrome-for-testing`, matching Chrome's documented user-level
  directory.
- The service-worker diagnostic proved the intended MV3 worker was loaded:
  `chrome-extension://mogdhelapdlmkfgeeaogeehnlclhcnkn/background.js`,
  `dm-policy={interceptDownloads:true,showMediaButtons:true,excludedSites:[]}`,
  and native `lastError=null` with `{ok:true}`. The first `button=false` gate
  was the fixture: it played the first W3Schools video after moving the page to
  a non-scrollable/off-viewport position. The fixture now drives the visible
  top-frame player without changing the product visibility rule. MDN's compact
  example was rejected as a coverage target because its player is inside an
  iframe and the manifest intentionally does not inject into all frames.
- The WebKit Inspector could not provide app control in this environment:
  `Target.getTargets` returned `-32601`, `"'Target.getTargets' was not found"`.
  The probe records this limitation once and uses a different path: the
  resident application's single-instance `--commit` control, which calls the
  same `commit_provisional` implementation. Added parser coverage for that
  control; the focused test passed.
- Added and ran `fixtures/public_chromium_probe.py` with a fresh Chrome
  profile/HOME, the built extension, the real native host, the real binary, and
  the public W3Schools player. The real media-button capture and explicit
  generated `<a download>` capture each created one job, were committed through
  the resident CLI, reached `state=completed` with `provisional=false`, and
  matched browser-side byte/hash references: `media_bytes=788493`,
  `anchor_bytes=788493`. Final output:
  `PUBLIC-CHROMIUM: PASS (page=https://www.w3schools.com/html/html5_video.asp,
  media_source=https://www.w3schools.com/html/mov_bbb.mp4, media_bytes=788493,
  anchor_source=https://www.w3schools.com/html/mov_bbb.mp4?dm_public_anchor=1,
  anchor_bytes=788493, jobs=2, browser_downloads=[])`.
- Plain Python reference fetching was rejected by W3Schools with HTTP 403; the
  fixture now uses same-page `fetch()` plus Web Crypto SHA-256 so the reference
  uses browser-equivalent headers and creates no download.
- Follow-up real run exercised browser ownership after both captures. A trusted
  CDP right-click (`button=2`) and trusted Ctrl-click (`button=0`, `ctrlKey=true`)
  on a normal HTTP link reached the page with `defaultPrevented=false` and
  `isTrusted=true`; the page canceled navigation afterward. The native job count
  remained exactly two and Chromium's Downloads directory stayed empty. Output:
  `BROWSER-OWNERSHIP: PASS ({"click":{"button":0,"ctrlKey":true,
  "defaultPrevented":false,"isTrusted":true},"context":{"button":2,
  "defaultPrevented":false,"isTrusted":true}})`.

## 2026-09-04 — Prove public blob/MSE HLS capture

- Added `fixtures/public_hls_chromium_probe.py` against the official hls.js demo
  at `https://hlsjs.video-dev.org/demo/`, using its fixed finite 480p Big Buck
  Bunny stream:
  `https://test-streams.mux.dev/x36xhzz/url_6/193039199_mp4_h264_aac_hq_7.m3u8`.
  The real player reported `blob=true`, `readyState=4`, `paused=false`, and
  `duration=634.6`; the extension button was visible on the playing player.
- First real attempt failed at the probe boundary, not the product: the fixture
  fetched all 64 reference segments before clicking, which filled the bounded
  traffic ring and evicted the manifest. The native job was not created. The
  fixture was reordered to click and commit first, then fetch the browser
  reference, so it no longer tests its own reference traffic.
- This exposed the real candidate-ordering edge: the manifest can be observed
  before player evidence, while later segments carry the player key. The old
  selector then considered only owned segments and returned no source. Added a
  regression and a narrow fix: when no competing player's traffic is known,
  owned traffic may be considered with unassigned candidates, still preferring
  a manifest; cross-player refusal remains unchanged.
- The clean, non-instrumented real run completed one native `.ts` job from the
  exact manifest. Browser-context fetch plus Web Crypto assembled 64 ordered
  segments and produced `71878228` bytes with SHA-256
  `6e830be99296a8452aa3294f25ca64193bee2b58bf4d559a7cc5ffbb1cda0a42`.
  Native output matched both size and hash:
  `PUBLIC-HLS-CHROMIUM: PASS (segments=64, output_bytes=71878228,
  sha256=6e830be99296a8452aa3294f25ca64193bee2b58bf4d559a7cc5ffbb1cda0a42,
  jobs=1, browser_downloads=[])`.
- Trusted Ctrl-click and right-click on a normal HTTP link remained
  browser-owned (`isTrusted=true`, `defaultPrevented=false`); no extra native
  job or browser download appeared. The run ended with
  `PUBLIC-HLS-CHROMIUM-PROBE: PASS`.
- Frontend/extension verification after the fix passed Vitest `32/32`,
  `tsc -b`, `npm run build:all`, and the focused candidate regression `12/12`.

## 2026-09-04 — Prove public blob/MSE DASH capture

- Added `fixtures/public_dash_chromium_probe.py` against the official DASH-IF
  reference client. The fixture uses the visible `stream-url` field and `Load`
  button to load the static 30-second MPD
  `https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps_shortened.mpd`.
  The real player reported `blob=true`, `readyState=4`, `paused=false`, and
  `duration=30`; the extension button was visible on the playing MSE player.
- The first run reached native completion and found both tracks, but the probe
  incorrectly required a 29–31 second output. The MPD's 4-second segment grid
  produces a valid `32.085333` second remux. The probe now checks that observed
  range instead of rejecting correct output.
- The clean real run created one native job from the exact MPD, committed it,
  and completed a playable MP4. Browser-context fetch plus Web Crypto recorded
  the exact first-representation source tracks: video `9` segments,
  `9755337` bytes, SHA-256
  `44ddc05092974595726f81181796611d257ac55927244b36d8b60129bf8a839f`; audio
  `9` segments, `267561` bytes, SHA-256
  `26195401fbff20512d36a9b08c9fd8e9c45b62fe0ad8e9a212a7b6ea24be9273`.
  Native FFmpeg output was `10017817` bytes with SHA-256
  `a1cb574d31bbb18c8b0e54918177ad3ad2d4dc8e00d509c5dee6fd59f911b833`; FFprobe
  found one audio and one video stream. Final output:
  `PUBLIC-DASH-CHROMIUM: PASS (tracks=video+audio, output_bytes=10017817,
  duration=32.085333, jobs=1, browser_downloads=[])`.
- Trusted Ctrl-click and right-click on a normal HTTP link remained
  browser-owned (`isTrusted=true`, `defaultPrevented=false`), with no extra job
  or browser download. The run ended with
  `PUBLIC-DASH-CHROMIUM-PROBE: PASS`.
- This closes a real static DASH/MSE plus separate audio/video mux path. The
  adaptive sample starts at 480p and switches to 768p while the parser selects
  the first representation, so this evidence does not claim the separate
  quality-switch acceptance case; that remains a distinct gap.

## 2026-09-04 — Reverify sequential fallback pause with owned Xvfb

- The existing pause probe's first current run was not product evidence: it used
  a fixed `DISPLAY=:99` without starting Xvfb, and the resident died during
  Inspector startup with `app_poll=-11`. The probe now starts the same disposable
  Xvfb helper used by the passing sequential-only probe and owns its cleanup.
- The corrected real-binary run passed:
  `SEQUENTIAL-PAUSE: PASS (state=paused, completed=1/4,
  downloaded=262144, rejected_overlaps=4,
  requests={'seg0.ts': 1, 'seg1.ts': 2, 'seg2.ts': 1, 'seg3.ts': 1})`.
- The resident remained alive, one valid fragment was retained, and no error was
  recorded. This closes the real pause-during-sequential-fallback case.

## 2026-09-04 — Prove adaptive HLS representation selection

- Added `fixtures/adaptive_hls_quality_probe.py` against the official hls.js demo
  and its public multi-level Big Buck Bunny HLS master
  `https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8`. The real page is
  blob/MSE-backed and exposes the hls.js manual-level API. The probe changed
  `window.hls.currentLevel` from `3` to level `1` (`512x288`, `460560` bitrate)
  and waited for the selected `url_4` variant playlist and segment traffic.
- The first real quality run exposed a product defect: the selected variant was
  observed, but candidate selection used insertion order and forwarded an older
  re-observed `url_0` manifest. The captured job therefore targeted the wrong
  representation (`177374616` bytes). A deterministic regression now requires
  the newest `at` timestamp to win, and the selector sorts by observation time
  before preferring manifests.
- The final real run created one native job from the selected `url_4` manifest,
  committed it, and completed raw MPEG-TS output. Browser-context fetch plus
  Web Crypto recorded `64` ordered segments, `39392016` bytes, SHA-256
  `cb796a641c60e28e47bbbf09e9a260c3e16d2fe29bfdadb50298907f42d9a8c1`. Native
  output was exactly `39392016` bytes with the same SHA-256.
- Trusted right-click and Ctrl-click on the ordinary browser-owned link remained
  `isTrusted=true` and `defaultPrevented=false`; there was one native job and no
  Chromium Downloads artifact. The run ended with
  `ADAPTIVE-HLS-QUALITY-PROBE: PASS`.
- This closes the v1 quality-switch acceptance case for a real adaptive HLS
  player. It does not add a quality browser or site-specific resolver. The
  already-proven static DASH and HLS paths remain intact.

## 2026-09-04 — Prove cold-start Chromium capture

- Added `fixtures/cold_start_chromium_probe.py` with a fresh HOME/profile and
  no resident application process before the browser action. The real W3Schools
  player exposed the media button and was playing the direct MP4.
- The first run read the newly-created SQLite file before its `jobs` table was
  initialized (`no such table: jobs`). The probe now waits for schema readiness;
  that was harness synchronization, not product evidence.
- The corrected run launched the resident through the native messaging host,
  created and committed one native job, and completed `788493` bytes. Browser
  fetch plus Web Crypto produced SHA-256
  `3bb938fb70049e3e45f533b37ccae995ae96516e04c2f35b0c1142e47b2a39c1`, matching
  the native output exactly.
- Trusted right-click and Ctrl-click remained browser-owned
  (`isTrusted=true`, `defaultPrevented=false`), with one native job and no
  Chromium Downloads artifact. The run ended with
  `COLD-START-CHROMIUM-PROBE: PASS`.

## 2026-09-04 — Prove adaptive DASH representation selection

- Added `fixtures/adaptive_dash_quality_probe.py` against the official DASH-IF
  reference player and the static Akamai Big Buck Bunny MPD
  `https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps_shortened.mpd`.
  The player exposed ten video representations and one audio representation.
  The probe disabled dash.js video ABR only for the deterministic test, changed
  `window.player` from `bbb_30fps_3840x2160_12000k` to
  `bbb_30fps_320x180_200k`, and recorded exact `.m4v`/`.m4a` URLs from CDP
  `Network.requestWillBeSent` events.
- The first real run exposed a product defect: the browser switched to the
  320x180 representation, but native DASH capture always selected the first
  1024x576 representation from the shared MPD. The final real run also exposed
  that the extension classified `.m4s` but not the actual `.m4v`/`.m4a` traffic,
  so no selector hints were reaching native capture.
- The fix carries up to eight recent, ownership-scoped HTTP(S) segment hints in
  the native capture message. Rust resolves those URLs against every
  representation's expanded DASH template and persists the hints in the job so
  retry/resume keeps the selected representation. Existing callers still use
  `parse_dash_tracks()` with the first representation when no hint exists.
  `.m4a`, `.m4s`, and `.m4v` are now classified as media segments.
- The corrected real run recorded six selected video requests and four selected
  audio requests. Browser-context fetch plus Web Crypto recorded selected video
  bytes `505512` with SHA-256
  `44af089c298d873c8991226a0e22c635e2e55b8be57dee175204c4fcf6177f22`, and
  selected audio bytes `133555` with SHA-256
  `b8499fa98450954c841d9ec7933d6394c0a8cb4c8eb594556ca56a2f436f5638`.
  The native job completed `1065647` assembled bytes and produced
  `1060566` output bytes. FFprobe reported video `320x180` plus audio and
  duration `32.085333` seconds.
- The run retained the eight browser segment hints in SQLite, created one native
  job, and left Chromium Downloads empty. Trusted right-click and Ctrl-click on
  the ordinary browser-owned link remained `isTrusted=true` and
  `defaultPrevented=false`. Final output:
  `ADAPTIVE-DASH-QUALITY: PASS (selected=bbb_30fps_320x180_200k,
  video_requests=6, audio_requests=4, output_bytes=1060566,
  output_sha256=9e92c69c7143d6662ef7fe9b98778bf133272aa1cd349aa7d0ce4addece6c24c,
  browser_downloads=[])` and `ADAPTIVE-DASH-QUALITY-PROBE: PASS`.
- Rust verification passed `57/57`; focused extension verification passed
  `14/14`; `npm run build:extension` and `npx tsc -b` passed. The only Rust
  warning remains the protected sibling `wait_for_transfer_idle` helper.

## 2026-09-04 — Prove ordinary plain-link download fallback

- Added `fixtures/ordinary_download_chromium_probe.py` against the official
  GitHub `octocat/Hello-World` repository archive
  `https://github.com/octocat/Hello-World/archive/refs/heads/master.zip`.
  Header validation confirmed the redirect to `codeload.github.com` and
  `Content-Disposition: attachment; filename=Hello-World-master.zip`.
- In a fresh Chromium profile, the probe injected a plain `<a>` with no
  `download` attribute and clicked it through CDP input. The event was not
  pre-browser intercepted; Chromium completed download id `1` with
  `bytesReceived=351`, `error=null`, final URL
  `https://codeload.github.com/octocat/Hello-World/zip/refs/heads/master`,
  and SHA-256
  `acd2fd3563d8de4b46dae1edeb96607b3d105a0a3be894e90aa5472353a93233`.
- The observe-only `downloads.onCreated` fallback created exactly one native
  provisional job from the final URL. After resident `--commit`, it completed
  one managed output with the same `351` bytes and the same SHA-256. Final
  output:
  `ORDINARY-DOWNLOAD: PASS (browser_bytes=351,
  sha256=acd2fd3563d8de4b46dae1edeb96607b3d105a0a3be894e90aa5472353a93233,
  native_bytes=351,
  native_sha256=acd2fd3563d8de4b46dae1edeb96607b3d105a0a3be894e90aa5472353a93233,
  browser_state=complete, jobs=1)` and `ORDINARY-DOWNLOAD-PROBE: PASS`.
- This proves the least-destructive fallback: the browser download is never
  cancelled, the native copy is independently managed, and both copies are
  byte-identical. The current event timing exposes the browser's interim URL
  basename (`master`) before its final Content-Disposition filename is known;
  the probe deliberately chooses `ordinary-fallback.zip` at commit and records
  this filename handoff as a remaining UX refinement, not a data-loss defect.

## 2026-09-05 — HTML5 source children and range-rate-limit recovery

- The real public-player audit reached a Commons/Video.js class of HTML5 player
  that keeps its URL on a child `<source src>` while `currentSrc` and the video
  `src` property are empty during lazy initialization. The old media button
  could therefore fall back to the page URL and capture HTML. The content path
  now resolves `currentSrc`, element `src`, then a child source attribute against
  `document.baseURI`, and refuses disabled/no-source placeholder videos. The
  classic MV3 content bundle keeps this helper local; importing the shared
  module caused a real classic-script load failure and was removed.
- The native ranged whole-object path previously returned the first worker
  error. A host that advertises ranges but returns HTTP 429 for parallel/nonzero
  ranges now retries missing ranges with one connection, then waits for the
  observed rate-limit reset and retries as a fresh no-Range stream. The fallback
  clears ranged metadata and marks the job `single-stream` and non-resumable
  before reusing the existing safe commit path.
- Deterministic real-binary fixture: `fixtures/range_fallback_probe.py`. Its
  server accepted `bytes=0-0`, rejected later ranges with 429, and served one
  plain GET. Output:
  `RANGE-FALLBACK: PASS (ready=3145851 bytes, output=3145851 bytes,
  sha256=c1dfec107e18567f102212e5cecae4a1675353d55bee9b9cedbc5025e9700162,
  ranged_requests=31, plain_gets=1, mode=single-stream, resumable=False)` and
  `RANGE-FALLBACK-PROBE: PASS`.
- Public-site exploration is not claimed as acceptance in this entry. The
  YouTube embed returned the documented player Error 153. The Commons player
  exposed disabled placeholder videos before its real source was instantiated;
  later native requests reached HTTP 429 on the public edge. The Video.js probe
  then hit `ConnectionRefusedError: [Errno 111]` while querying Chromium
  `/json/list` during startup, before page navigation. The next pass must repair
  that CDP lifetime and obtain a retained public initiation result.
- Verification on the exact worktree passed Rust `58/58`, Vitest `37/37`,
  `npx tsc -b`, `npm run build:all`, Python compilation, and the real range
  fallback probe. The only Rust warning is the pre-existing protected sibling
  `wait_for_transfer_idle` helper. No public Chromium PASS is claimed here.

## 2026-09-05 — Prove real Video.js public-player initiation

- The public Chromium harness now waits for both `/json/version` and `/json/list`
  to expose a page target before opening the service-worker target. Its
  `/json/list` polling tolerates transient connection refusal and reports the
  final error if the browser really dies. This fixes the prior startup race;
  no product behavior was changed by the CDP repair.
- Added `fixtures/public_videojs_chromium_probe.py` against the official
  Video.js demo page `https://videojs.github.io/video.js/`. A fresh Chromium
  profile loaded the real player, which reported `readyState=4`, `paused=false`,
  duration `46.613333`, and source `https://vjs.zencdn.net/v/oceans.mp4`.
  The extension's real media button was visible and activated through trusted
  CDP mouse input.
- The native host created exactly one media job from the browser-observed source.
  Resident `--commit` returned exit `0`; the job became `completed` with
  `provisional=false`. Browser-context fetch plus Web Crypto recorded
  `23014356` bytes and SHA-256
  `9f8f979374969429263495ffcaac832cd603b1efe4c66a05fafd6729dffef9af`.
  The managed native output had the same size and hash. Chromium's Downloads
  directory remained empty.
- Retained output:
  `VIDEOJS-CHROMIUM: PASS (page=https://videojs.github.io/video.js/,
  source=https://vjs.zencdn.net/v/oceans.mp4, output_bytes=23014356,
  output_sha256=9f8f979374969429263495ffcaac832cd603b1efe4c66a05fafd6729dffef9af,
  traffic=1, jobs=1, browser_downloads=[])` and
  `VIDEOJS-CHROMIUM-PROBE: PASS`.
- Verification for this slice passed Python compilation, the repaired fresh
  Chromium E2E, and `git diff --check`. The known Inspector limitation remains
  `Target.getTargets -> -32601`; the probe uses the resident `--commit` control.

## 2026-09-05 — Prove public top-level audio initiation

- W3Schools' HTML Audio article and MDN's interactive audio example were
  inspected as possible targets. Both put their live audio element inside an
  iframe; the top-level CDP document exposed zero media elements, so neither
  could produce a native initiation with the current all-frame/document policy.
  A separate top-level GitHub Pages audio demo exposed a playing source but hid
  the underlying element at `0x0`, so the visibility rule correctly refused it.
- Added `fixtures/public_paciello_audio_chromium_probe.py` against The Paciello
  Group's public accessibility test page
  `https://thepaciellogroup.github.io/AT-browser-tests/acc-name-test/audio.html`.
  The fresh Chromium profile reported one visible native audio player at
  `300x54`, `readyState=4`, `paused=false`, duration `437.565306`, and browser
  source `https://thepaciellogroup.github.io/AT-browser-tests/audio/jeffbob.mp3`.
  The real extension media button was visible and activated through trusted CDP
  mouse input.
- The native host created exactly one media job from that browser-observed MP3.
  Resident `--commit` returned exit `0`; the row became `completed` with
  `provisional=false`. Browser-context fetch plus Web Crypto recorded
  `4104320` bytes and SHA-256
  `a60b3d66d9bb9cacbe697b7bfc18ce31dd53d1fadec33e5d8f71bb780e6f551a`.
  The managed native output matched both size and hash. Chromium's Downloads
  directory remained empty.
- Retained output:
  `AUDIO-CHROMIUM: PASS (page=https://thepaciellogroup.github.io/AT-browser-tests/acc-name-test/audio.html,
  source=https://thepaciellogroup.github.io/AT-browser-tests/audio/jeffbob.mp3,
  output_bytes=4104320,
  output_sha256=a60b3d66d9bb9cacbe697b7bfc18ce31dd53d1fadec33e5d8f71bb780e6f551a,
  traffic=1, jobs=1, browser_downloads=[])` and `AUDIO-CHROMIUM-PROBE: PASS`.
- Verification for this public slice passed Python compilation and the real
  fresh-profile Chromium/native E2E. The known appindicator deprecation warning
  was the only resident log warning; no product error was recorded.

## 2026-09-05 — About:blank frame policy fallback (W3Schools blocker)

- Chrome's content-script documentation confirms that `all_frames` covers matching
  frames, while `match_about_blank`/`match_origin_as_fallback` are needed for
  `about:`/opaque child documents. MDN documents that an iframe's
  `Document.referrer` is initially the parent window URL (full URL for
  same-origin frames, parent origin by default cross-origin).
- Added the pure `siteOfDocument(url, referrer)` helper and a focused regression:
  an ordinary child URL wins, `about:blank` falls back to the parent referrer,
  and `www` normalization remains unchanged for policy exclusions. Mirrored the
  helper in the classic content script and changed only `active()` to use it.
- The focused extension regression passed `18/18`; `npm run build:extension`
  rebuilt `extension/dist` successfully. The manifest worktree change retains
  HTTP/HTTPS matching, `all_frames`, `match_about_blank`, and
  `match_origin_as_fallback` for the opaque-frame path.
- Fresh-profile command:
  `timeout 360s python3 fixtures/public_w3_iframe_audio_chromium_probe.py`
  exited `1`. Chromium reached
  `https://www.w3schools.com/html/tryit.asp?filename=tryhtml5_audio_all` and
  observed the real nested player source
  `https://www.w3schools.com/html/horse.ogg`, `readyState=4`, `paused=false`,
  duration `1.515102`, and child media geometry `300x54`. The result frame's
  top-document geometry was `top=62`, `bottom=62`, `height=0`; the probe saw
  `button=false`, `buttonRect=null`, and no trusted button click/native job.
  The second `about:blank` frame was an unrelated `data:` MP4 ad. This is a
  concrete public-page layout/injection limitation, not evidence of a completed
  browser/native capture, so no size/hash/job PASS is claimed.
- The protected Rust `wait_for_transfer_idle` sibling hunk remains untouched.

## 2026-09-05 — MDN iframe audio probe (no child frame exposed)

- Ran the existing fresh-profile probe unchanged:
  `timeout 360s python3 fixtures/public_mdn_iframe_audio_chromium_probe.py`.
- The real native app started and the extension worker was present at
  `chrome-extension://mogdhelapdlmkfgeeaogeehnlclhcnkn/background.js`. The probe
  recorded the known Inspector limitation: `Target.getTargets` returned
  `-32601`, so it used the resident `--commit` CLI path.
- The MDN page was
  `https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/audio`.
  `Page.getFrameTree` exposed only the top document
  `https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/audio`.
  Two default execution contexts had origin `https://developer.mozilla.org`,
  but no child frame, child media element, or injected Download button was
  exposed. The candidate wait ended with `last=None`, `error=None`, no trusted
  click, no native job, and the process exited `1`.
- This is a concrete MDN/browser frame-layout or page-content blocker. It does
  not establish native completion, browser/native size/hash equality, or empty
  browser Downloads. No product policy change or candidate fixture rewrite was
  made for this failure.

## 2026-09-05 — Iframe Tester/Paciello audio probe (wrapper boundary)

- Ran the existing fresh-profile probe unchanged:
  `timeout 360s python3 fixtures/public_iframe_paciello_audio_chromium_probe.py`.
- The real native app started and the extension worker was present at
  `chrome-extension://mogdhelapdlmkfgeeaogeehnlclhcnkn/background.js`. The probe
  recorded the known Inspector limitation: `Target.getTargets` returned
  `-32601`, so it used the resident `--commit` CLI path.
- The only frame exposed by `Page.getFrameTree` was the top-level
  `iframe.verekia.com` wrapper, with URL
  `https://iframe.verekia.com/?allow=accelerometer;%20autoplay;%20clipboard-write;%20encrypted-media;%20fullscreen;%20gyroscope;%20picture-in-picture;%20web-share&w=1280&h=720&url=https://thepaciellogroup.github.io/AT-browser-tests/acc-name-test/audio.html`.
  One default execution context had origin `https://iframe.verekia.com`; no
  Paciello child frame, child media element, injected Download button, trusted
  click, or native job was exposed. The candidate wait ended with `last=None`,
  `error=None`, and the process exited `1`.
- This is a concrete public wrapper/frame-boundary blocker. It does not
  establish native completion, browser/native size/hash equality, or empty
  browser Downloads. No product policy change or candidate fixture rewrite
  was made for this failure.
- Fresh re-run on 2026-09-07 (`/tmp/dm-public-iframe-paciello-audio-fresh-20260907.log`,
  probe exit `1`) reproduced the boundary. `Page.getFrameTree` exposed only the
  top-level `iframe.verekia.com` wrapper, with one default context at
  `https://iframe.verekia.com`; the expected Paciello child URL was absent.
  The wait ended with `last=None`, `error=None`, no trusted click, no native job,
  and no browser/native comparison. No product code changed.

## 2026-09-05 — Sa11y SoundCloud iframe probe (embed-player boundary)

- Ran the existing fresh-profile probe unchanged:
  `timeout 360s python3 fixtures/public_sa11y_soundcloud_iframe_chromium_probe.py`.
- The real native app started and the extension worker was present at
  `chrome-extension://mogdhelapdlmkfgeeaogeehnlclhcnkn/background.js`. The probe
  recorded the known Inspector limitation: `Target.getTargets` returned
  `-32601`, so it used the resident `--commit` CLI path.
- The only frame exposed by `Page.getFrameTree` was the top-level
  `https://panzi.github.io/embedplayer/` document. One default execution
  context had origin `https://panzi.github.io`; no SoundCloud child frame,
  child media element, injected Download button, trusted click, or native job
  was exposed. The candidate wait ended with `last=None`, `error=None`, and the
  process exited `1`.
- This is a concrete public embed-player/frame-boundary blocker. It does not
  establish native completion, browser/native size/hash equality, or empty
  browser Downloads. No product policy change or candidate fixture rewrite was
  made for this failure.

## 2026-09-05 — Public iframe candidates reconciled (repeatable boundary)

- Re-ran all three existing candidates with the built extension, fresh profiles,
  fresh native-app HOME directories, and the resident `--commit` path. Every run
  reached the extension worker but exited `1` before player activation:
  - MDN: contexts `2` and `4`, both origin `https://developer.mozilla.org`;
    `Page.getFrameTree` contained only
    `https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/audio`.
  - Iframe Tester/Paciello: one context, origin `https://iframe.verekia.com`;
    only the wrapper URL was in `Page.getFrameTree`.
  - Sa11y/SoundCloud: one context, origin `https://panzi.github.io`;
    only `https://panzi.github.io/embedplayer/` was in `Page.getFrameTree`.
- None exposed the expected child frame, child media element, injected Download
  button, trusted click, or native job. The extension manifest already requests
  `all_frames`, `match_about_blank`, and `match_origin_as_fallback`; the worker
  was present in all runs. This is therefore a public-page loading/embedding or
  Chromium CDP frame-boundary limitation, not a reachable extension policy or
  injection defect. No product hardening or policy change was made.

## 2026-09-05 — Prove native-failure browser download fallback

- Added `fixtures/browser_fallback_chromium_probe.py` for SPEC §5.1.1. The
  probe uses a fresh Chromium profile and HOME, deliberately does not register
  the native messaging host, and serves a safe 64 KiB local fixture. This
  isolates the browser-restoration path from native acquisition.
- The real extension worker was present at
  `chrome-extension://mogdhelapdlmkfgeeaogeehnlclhcnkn/background.js`. Its
  diagnostic returned `Specified native messaging host not found.`. A trusted
  CDP left-click targeted a visible explicit `<a download>` with filename
  `browser-fallback.bin`.
- The content script consumed the first click, the worker's native attempt
  failed, and `chrome.downloads.download({saveAs:false})` restored the intended
  browser transaction. Chromium created exactly one completed download (id
  `1`) at the isolated HOME Downloads directory, with `bytesReceived=65536`,
  `error=null`, and one local server request. The downloaded bytes matched the
  fixture: size `65536`, SHA-256
  `d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a`.
- No native application database or native binary process was created, so the
  expected native-job count for this failure-mode acceptance is `0`; one
  browser download is the intended result. The exact probe output ended with
  `BROWSER-FALLBACK: PASS (browser_bytes=65536,
  sha256=d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a,
  downloads=1, source_requests=1, native_jobs=0, native_processes=0)` and
  `BROWSER-FALLBACK-CHROMIUM-PROBE: PASS`.
- The one-download/one-request assertion also verifies that the synthetic
  fallback anchor did not recursively re-enter ordinary interception. No
  product code was changed; the existing fallback unit tests remain the
  relevant code-path regression.

## 2026-09-05 — Prove integration-off leaves Chromium alone

- Added `fixtures/integration_off_chromium_probe.py` for SPEC §§17.1 and 19.2.
  It uses a fresh Chromium profile and HOME, loads the real extension, changes
  only the extension's persisted policy through an extension-origin page, and
  then navigates to a safe local fixture.
- The worker reported `{interceptDownloads:false, showMediaButtons:true,
  excludedSites:[]}`. A trusted CDP left-click targeted a visible explicit
  `<a download>` link. Chromium completed exactly one browser-owned download
  (id `1`) with `byExtensionId=null`, `bytesReceived=49152`, `error=null`, and
  one local server request. The downloaded bytes matched the fixture at size
  `49152`, SHA-256
  `bb33c3eac7269e51cea14832f2cc1fdeb718bb5f85cd22fafb15b3aae81ce8a0`.
- No Download Manager database or native binary process was created. The exact
  output ended with `INTEGRATION-OFF: PASS (browser_bytes=49152,
  sha256=bb33c3eac7269e51cea14832f2cc1fdeb718bb5f85cd22fafb15b3aae81ce8a0,
  downloads=1, source_requests=1, browser_initiator=page, native_jobs=0,
  native_processes=0)` and `INTEGRATION-OFF-CHROMIUM-PROBE: PASS`.
- The expected native-job count is `0` for this contract case: with
  interception disabled, Chromium owns the download and the manager does
  nothing. The existing protected Rust sibling hunk remains untouched.

## 2026-09-05 — MDN `about:srcdoc` audio capture

- The first direct MDN interactive-page run exposed a reachable policy-site
  defect. The media was in execution context `5`, frame ID
  `7CC287E809731ACC307CCF9D0A8567D2`, with URL `about:srcdoc`, empty referrer,
  source `https://interactive-examples.mdn.mozilla.net/media/cc0-audio/t-rex-roar.mp3`,
  `readyState=4`, `paused=false`, visible geometry, and owner iframe geometry
  `510.1875x369` at `(768.796875,50)`, but `button=false`. The old URL/referrer-only
  policy derivation could not identify the parent site.
- MDN documents `about:srcdoc` as the location of `srcdoc` documents, and
  `Location.ancestorOrigins` exposes ancestor browsing-context origins. Added
  the smallest third-priority fallback: document URL, then referrer, then the
  first ancestor origin. Existing document/referrer precedence and exclusion
  matching remain unchanged.
- Focused extension tests passed `18/18`; `npx tsc -b`, `npm run
  build:extension`, and `git diff --check` passed. The fresh-profile rerun used
  the rebuilt extension and the resident `--commit` path. The worker was
  present, and the child context remained `5` with frame ID
  `37AA06C5DC5ECA4DEBB85B858821AF9F`.
- The injected child button was present at child rectangle
  `left=213, top=63.609375, width=90.890625, height=28.796875`; the owner was
  `510.1875x369` at `(768.796875,50)`. The trusted CDP click point was
  `(1027.2421875,128.0078125)`. The child reported `readyState=4`,
  `paused=false`, `duration=2.074218`, and `visible=true`.
- Exactly one native media job was created and committed. The completed output
  was `39868` bytes with SHA-256
  `41191d0727073bf848bcc8f0bd851d71a0b0058e901abb1c1b236ad327bda52e`. The
  browser-side reference for the same source was `39868` bytes with the same
  SHA-256. The job was `completed`, `provisional=false`, and Chromium Downloads
  was empty. Exact output ended with
  `MDN-INTERACTIVE-AUDIO: PASS (... jobs=1, browser_downloads=[])` and
  `MDN-INTERACTIVE-AUDIO-CHROMIUM-PROBE: PASS`.

## 2026-09-05 — Explicit `<a download>` native interception

- Added `fixtures/explicit_anchor_chromium_probe.py` for the successful branch
  paired with the native-failure fallback proof. It used the built extension,
  a fresh Chromium profile, a fresh native-app HOME, the registered native host,
  and a safe local 64 KiB fixture.
- The first attempt stopped before the click because the probe treated the
  known service-worker CDP diagnostic (`runtimeType=undefined`, while the
  worker target was present) as native-host failure. That was a probe bug, not
  product evidence; the gate was removed without changing production code.
- The corrected run performed a trusted CDP left-click on an explicit
  `<a download>` with source `http://127.0.0.1:[DISPOSABLE-PORT]/browser-fallback.bin`
  and filename `browser-fallback.bin`. It created exactly one native job with
  `media=false`, then the resident `--commit` path returned exit `0`.
- The completed managed output was `65536` bytes with SHA-256
  `d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a`. The
  browser-side reference was `65536` bytes with the same SHA-256. The source
  server saw two requests: one native acquisition and one explicit browser
  reference fetch. Chromium Downloads remained empty.
- Exact output ended with
  `EXPLICIT-ANCHOR-CHROMIUM: PASS (... output_bytes=65536,
  output_sha256=d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a,
  jobs=1, source_requests=2, browser_downloads=[])` and
  `EXPLICIT-ANCHOR-CHROMIUM-PROBE: PASS`. The protected Rust sibling remains
  untouched.

## 2026-09-05 — Add Download confirmation surface

- Added `fixtures/add_window_chromium_probe.py` to verify the user-visible
  confirmation surface on a real Chromium/native capture. It used a fresh
  Chromium profile and native-app HOME, the built extension, the registered
  native host, and the safe local 64 KiB fixture.
- A trusted CDP click on the explicit `<a download>` created exactly one
  provisional native job. Before commit, `xwininfo -root -tree` observed exactly
  one visible `Add Download` window: `410x560+435+170`, title `Add Download`.
- The Inspector endpoint returns `404` and its `Target.getTargets` method is
  unsupported (`-32601`), so this probe uses the already-established resident
  `--commit` CLI control after proving the window is mapped. It does not claim a
  child-window Commit-button click.
- The CLI commit returned exit `0`. The completed managed output was `65536`
  bytes with SHA-256
  `d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a`. The
  browser-side reference matched size and hash. The job was completed and
  non-provisional; Chromium Downloads was empty. The source server saw two
  requests: native acquisition and browser reference fetch.
- Exact output ended with
  `ADD-WINDOW-CHROMIUM: PASS (... output_bytes=65536,
  output_sha256=d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a,
  jobs=1, source_requests=2, browser_downloads=[], ui_matches=1)` and
  `ADD-WINDOW-CHROMIUM-PROBE: PASS`. No XTEST automation was used and the
-  protected Rust sibling remains untouched.

## 2026-09-05 — Completion notification and in-app Notifications surface

- Added `fixtures/notification_surface_probe.py`. It used the built native
  binary, a fresh native-app HOME/Xvfb, and the safe local 64 KiB fixture. The
  native `--capture` forwarding path created one provisional job; resident
  `--commit` returned exit `0`; the main window was brought back with the
  normal single-instance launch.
- The real completed job produced `65536` bytes with SHA-256
  `d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a` and one
  source request. The rendered route was
  `tauri://localhost/?view=notifications`, with one completion card containing
  `Download completed` and `notification-surface.bin · 64 KB`.
- The card exposed exactly the action labels `Open` and `Show in folder`.
  Exact output ended with
  `NOTIFICATION-SURFACE-PROBE: PASS (job=provisional-319bc2e0-5793-4721-bf35-0fab59bac845,
  output_bytes=65536, output_sha256=d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a,
  notification_type=completed, cards=1, buttons=['Open', 'Show in folder'], source_requests=1)`.
- The first retry hit the known intermittent Inspector connection refusal
  before any job; the fresh retry passed. This verifies in-app notification
  state and action rendering. OS-level toast was not independently asserted.
  The protected Rust sibling remains untouched.

## 2026-09-05 — Failure notification surface

- Added `fixtures/failure_notification_probe.py`. It used a safe local missing
  URL, the built extension/native host, a fresh Chromium profile and native-app
  HOME, and the normal native `--capture` forwarding path. The resulting native
  job was exactly one failed provisional job:
  `provisional-c75206c2-974a-4150-bbcf-2a75c6d8b8f4`, with error
  `Source returned 404 Not Found`.
- After bringing the main window back and opening
  `tauri://localhost/?view=notifications`, the UI rendered exactly one failure
  card. Its text contained `Download failed` and
  `failure.bin · Source returned 404 Not Found`; its exact actions were
  `View details` and `Open Manager`.
- No managed destination file was created and the local missing URL saw zero
  successful source requests. Exact output ended with
  `FAILURE-NOTIFICATION-PROBE: PASS (job=provisional-c75206c2-974a-4150-bbcf-2a75c6d8b8f4,
  state=failed, cards=1, buttons=['View details', 'Open Manager'], source_requests=0,
  managed_files=0)`. A non-fatal local 404-handler `BrokenPipeError` was printed
  when the native client closed after the error response; the probe exited `0`.
  OS-level toast delivery remains unasserted. The protected Rust sibling remains
  untouched.

## 2026-09-05 — Excluded-site browser ownership

- The first exclusion probe run showed a reachable defect: the worker policy
  correctly reported `excludedSites=["127.0.0.1"]`, but a trusted explicit
  anchor click still produced an extension-owned browser record
  (`byExtensionId=mogdhelapdlmkfgeeaogeehnlclhcnkn`). The probe’s first failure
  also had cleanup/path assumptions, which were corrected without product
  changes.
- The product fix adds one `siteAllowed()` decision in `content.ts`, using the
  existing document/referrer/ancestor-origin derivation. Both media-button
  activation and ordinary `<a download>` interception now require a derived
  site that is not in `excludedSites`; global interception remains enabled.
- Full extension tests passed `38/38`; `npx tsc -b`, `npm run build:extension`,
  and `git diff --check` passed. The rebuilt extension was tested with a fresh
  Chromium profile and no native-host registration. The trusted excluded click
  produced exactly one browser-owned artifact: `65536` bytes,
  SHA-256 `d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a`,
  `browser_initiator=page`, one source request, and `native_jobs=0`.
- Exact output ended with
  `EXCLUDED-SITE-CHROMIUM-PROBE: PASS (site=127.0.0.1, browser_bytes=65536,
  sha256=d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a,
  browser_initiator=page, downloads=['[DISPOSABLE-PATH]/browser-fallback.bin'],
  source_requests=1, native_jobs=0)`. This proves excluded sites bypass both
  native capture and extension fallback. The protected Rust sibling remains
  untouched.

## 2026-09-05 — Public media buttons disabled

- Added `fixtures/public_paciello_media_off_probe.py` to exercise the real
  Paciello public audio page with a fresh Chromium profile and the built
  extension. The probe first updates policy through the extension worker to
  `showMediaButtons=false`, then uses one trusted native-control click to
  activate the page's audio element. The trusted click is needed because
  Chromium blocks script-only autoplay; it does not invoke Download Manager.
- The corrected probe passed after fixing its harness-only assertion: it had
  assigned `app_db` but still referenced the removed `db_files` variable. The
  final check targets only the application database path, not Chromium's NSS
  `key4.db`/`cert9.db` files.
- Exact result:
  `showMediaButtons=false`, source
  `https://thepaciellogroup.github.io/AT-browser-tests/audio/jeffbob.mp3`,
  `readyState=4`, `paused=false`, `button=false`, `browser_downloads=0`, and
  `native_jobs=0`. The application database did not exist.
- Exact output ended with
  `PACIello-MEDIA-OFF-CHROMIUM-PROBE: PASS (page=https://thepaciellogroup.github.io/AT-browser-tests/acc-name-test/audio.html,
  source=https://thepaciellogroup.github.io/AT-browser-tests/audio/jeffbob.mp3,
  readyState=4, paused=False, button=False, browser_downloads=0, native_jobs=0)`.
  This closes the media-button-off control behavior without changing product
  code. The protected Rust sibling remains untouched.

## 2026-09-05 — Public media button keyboard activation

- Added `fixtures/public_paciello_keyboard_chromium_probe.py` by reusing the
  real Paciello public audio setup and changing only the initiation stimulus.
  The fresh Chromium profile focused the injected native `<button>` and sent
  trusted CDP Space key input. An earlier Enter-only sequence generated no
  activation in this headless Chromium build; it produced no native job and
  was not treated as product evidence. The passing probe uses Space, the
  standard native-button activation path observed by Chromium here.
- The focused button reported `aria-label=Download this media` and text
  `Download`. The trusted keyboard activation created exactly one media job:
  `provisional-58447b95-59f9-4b73-a5ac-599e5f66dedd`, source
  `https://thepaciellogroup.github.io/AT-browser-tests/audio/jeffbob.mp3`.
- Resident `--commit` returned exit `0`; the job completed with
  `provisional=false`. Browser-context fetch recorded `4104320` bytes and
  SHA-256 `a60b3d66d9bb9cacbe697b7bfc18ce31dd53d1fadec33e5d8f71bb780e6f551a`.
  The managed native output matched both values, and Chromium Downloads was
  empty.
- Exact output ended with
  `KEYBOARD-AUDIO-CHROMIUM: PASS (page=https://thepaciellogroup.github.io/AT-browser-tests/acc-name-test/audio.html,
  source=https://thepaciellogroup.github.io/AT-browser-tests/audio/jeffbob.mp3,
  output_bytes=4104320,
  output_sha256=a60b3d66d9bb9cacbe697b7bfc18ce31dd53d1fadec33e5d8f71bb780e6f551a,
  traffic=1, jobs=1, browser_downloads=[])` and
  `KEYBOARD-AUDIO-CHROMIUM-PROBE: PASS`. This adds a real public keyboard
  initiation path without changing product code. The protected Rust sibling
  remains untouched.

## 2026-09-05 — Public Plyr player initiation

- Added `fixtures/public_plyr_chromium_probe.py` against the official Plyr
  demo page `https://plyr.io/`. The page exposed two video elements; the
  extension selected the visible, playing `800x450` player rather than the
  hidden `0x0` placeholder. Its source was the finite direct MP4
  `https://cdn.plyr.io/static/demo/View_From_A_Blue_Moon_Trailer-576p.mp4`.
- The player reported `readyState=4`, `paused=false`, duration `183.125333`,
  and the injected Download control was visible. CDP recorded five browser
  range requests to the selected source. The trusted button activation created
  exactly one native media job.
- Resident `--commit` returned exit `0`; the job completed with
  `provisional=false`. Browser-context fetch recorded `49900386` bytes and
  SHA-256 `2f4bd69d9bfc928a399493eaec2ba0e5a6a4f7e326d6e74e0d1d415f63be86f8`.
  The managed native output matched both values, and Chromium Downloads was
  empty.
- Exact output ended with
  `PLYR-CHROMIUM: PASS (page=https://plyr.io/,
  source=https://cdn.plyr.io/static/demo/View_From_A_Blue_Moon_Trailer-576p.mp4,
  output_bytes=49900386,
  output_sha256=2f4bd69d9bfc928a399493eaec2ba0e5a6a4f7e326d6e74e0d1d415f63be86f8,
  traffic=5, jobs=1, browser_downloads=[])` and
  `PLYR-CHROMIUM-PROBE: PASS`. This adds a distinct public custom-player path
  without site-specific resolver code or product changes. The protected Rust
  sibling remains untouched.

## 2026-09-05 — Completion notification actions execute

- Added `fixtures/notification_actions_probe.py` on top of the real completion
  notification flow. It launched the real binary under Xvfb, captured and
  committed the safe local `65536`-byte fixture, navigated the real Tauri UI to
  `?view=notifications`, and clicked both rendered action buttons.
- The surface rendered one completion card with exactly `Open` and
  `Show in folder`. A disposable `xdg-open` recorder then observed the exact
  arguments in order: the managed file path, followed by its parent folder.
  No real desktop opener was launched.
- The managed output was `65536` bytes with SHA-256
  `d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a`, and
  the source server saw exactly one request. Exact output ended with
  `NOTIFICATION-ACTIONS-PROBE: PASS (job=provisional-7b14c8f0-8dc2-49c6-bd81-39f637a994e5,
  output_bytes=65536,
  output_sha256=d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a,
  buttons=['Open', 'Show in folder'],
  open_calls=['[DISPOSABLE-PATH]/Managed/notification-surface.bin',
  '[DISPOSABLE-PATH]/Managed'], source_requests=1)`.
- This closes the in-app completion action wiring on Linux's equivalent shell
  opener. Windows Explorer behavior remains a separate platform check; the
  installed notification plugin's OS-toast action API remains unasserted. The
  protected Rust sibling remains untouched.

## 2026-09-05 — Shaka public-player boundary

- Added `fixtures/public_shaka_chromium_probe.py` to inspect the official
  Shaka Player demo at `https://shaka-project.github.io/shaka-player/demo/`.
- The real page rendered its `PLAY` labels in `document.body.innerText`, but
  the Inspector found no actionable `button` or `[role=button]` node, and
  traversing open shadow roots still found no visible target. The bounded
  diagnostic ended with `buttons=[]`; this is a public demo/custom-shadow
  boundary, not evidence that the extension failed to inject.
- No trusted demo click, media capture, native job, managed output, or browser
  download was produced. The candidate is retained as a diagnostic artifact;
  no product change is justified by this unreachable public control.

## 2026-09-05 — Form POST browser initiation

- Added `fixtures/form_download_chromium_probe.py` with a local, safe
  `POST /download` form returning `Content-Disposition: attachment` and a
  deterministic `65536`-byte payload. The real Chromium probe clicked the
  visible submit button with CDP mouse input; it did not synthesize the form
  submission in page JavaScript.
- Chromium recorded one ordinary browser download from `POST /download` with
  a 17-byte form body. The extension's browser-download fallback created one
  native provisional job for the same URL; the native replay used `GET
  /download` and completed successfully.
- Browser and native outputs were both `65536` bytes with SHA-256
  `7daca2095d0438260fa849183dfc67faa459fdf4936e1bc91eec6b281b27e4c2`.
  Chromium's record was `state=complete`, `error=null`; the native job was
  `state=completed`, `provisional=false`; Chromium produced no second record.
  Exact output included `FORM-SERVER-METHODS: [{"body_bytes": 17,
  "method": "POST", "path": "/download"}, {"body_bytes": 0,
  "method": "GET", "path": "/download"}]` and
  `FORM-DOWNLOAD-PROBE: PASS`.
- This proves the browser remains intact for a form-originated attachment
  while the native fallback replays a safe GET. It does not claim to preserve
  arbitrary POST bodies for native replay; no product change was needed.

## 2026-09-05 — Mozilla top-level HTML5 player capture

- Added `fixtures/public_mozilla_chromium_probe.py` against the official
  cross-browser HTML5 player at `https://iandevlin.github.io/mdn/video-player/`.
  The page exposed one visible top-level `<video>` with three `<source>`
  children (MP4, WebM, and Ogg). Chromium selected the MP4 source
  `https://iandevlin.github.io/mdn/video-player/video/tears-of-steel-battle-clip-medium.mp4`.
- The fresh real Chromium profile reported `readyState=4`, `paused=false`,
  duration `70.542222`, and a visible player-bound Download button. Trusted
  CDP mouse activation created exactly one native media job. Resident `--commit`
  returned exit `0`; the job completed with `provisional=false`.
- Browser-context fetch recorded `15256787` bytes and SHA-256
  `8f8b69ed443be171cb505c75fab22f3af25375b09817e713fe0fd7c88c78f451`.
  The managed native output matched both values. Chromium Downloads was empty.
- Exact output ended with
  `MOZILLA-HTML5-CHROMIUM: PASS (page=https://iandevlin.github.io/mdn/video-player/,
  source=https://iandevlin.github.io/mdn/video-player/video/tears-of-steel-battle-clip-medium.mp4,
  output_bytes=15256787,
  output_sha256=8f8b69ed443be171cb505c75fab22f3af25375b09817e713fe0fd7c88c78f451,
  traffic=1, jobs=1, browser_downloads=[])` and
  `MOZILLA-HTML5-CHROMIUM-PROBE: PASS`. This adds an independent top-level
  HTML5 custom-controls path without product changes. The protected Rust
  sibling remains untouched.

## 2026-09-05 — VidPly public accessibility-player capture

- Added `fixtures/public_vidply_chromium_probe.py` against the VidPly project’s
  main demo at `https://matthiaspeltzer.github.io/vidply/demo/demo.html`.
  The fresh real Chromium profile exposed six top-level media elements. The
  first visible player reported `readyState=4`, `paused=false`, duration
  `76.81161`, and used `https://matthiaspeltzer.github.io/vidply/demo/media/deadline.mp4`
  from a multi-source, WebVTT-enabled player.
- The page produced nine observed public media requests. Trusted CDP mouse
  activation of the injected player-bound Download button created exactly one
  native media job. Resident `--commit` returned exit `0`; the job completed
  with `provisional=false`.
- Browser-context fetch recorded `12769545` bytes and SHA-256
  `249d6ec925db1ed34b66f66882030a25800138da8d9ed6af89987e0ff612d74f`.
  The managed native output matched both values. Chromium Downloads was empty.
- Exact output ended with
  `VIDPLY-CHROMIUM: PASS (page=https://matthiaspeltzer.github.io/vidply/demo/demo.html,
  source=https://matthiaspeltzer.github.io/vidply/demo/media/deadline.mp4,
  output_bytes=12769545,
  output_sha256=249d6ec925db1ed34b66f66882030a25800138da8d9ed6af89987e0ff612d74f,
  traffic=9, jobs=1, browser_downloads=[])` and
  `VIDPLY-CHROMIUM-PROBE: PASS`. This adds an independent public accessibility
  player path without product changes. The protected Rust sibling remains
  untouched.

## 2026-09-05 — MDN top-level WebM player capture

- Added `fixtures/public_mdn_webm_chromium_probe.py` against the official MDN
  learning-area example at
  `https://mdn.github.io/learning-area/html/multimedia-and-embedding/video-and-audio-content/simple-video.html`.
  The page exposed one visible top-level HTML5 `<video>` whose source was the
  finite WebM resource
  `https://mdn.github.io/learning-area/html/multimedia-and-embedding/video-and-audio-content/rabbit320.webm`.
- The fresh real Chromium profile reported `readyState=4`, `paused=false`,
  duration `7.8`, and a visible player-bound Download button. Trusted CDP
  mouse activation created exactly one native media job. Resident `--commit`
  returned exit `0`; the job completed with `provisional=false`.
- Browser-context fetch recorded `330618` bytes and SHA-256
  `074b046f0832c1c262a7a3e015b042092fa226b1550b83a7d14cca9025d34e1e`.
  The managed native output matched both values. Chromium Downloads was empty.
- Exact output ended with
  `MDN-WEBM-CHROMIUM: PASS (page=https://mdn.github.io/learning-area/html/multimedia-and-embedding/video-and-audio-content/simple-video.html,
  source=https://mdn.github.io/learning-area/html/multimedia-and-embedding/video-and-audio-content/rabbit320.webm,
  output_bytes=330618,
  output_sha256=074b046f0832c1c262a7a3e015b042092fa226b1550b83a7d14cca9025d34e1e,
  traffic=1, jobs=1, browser_downloads=[])` and
  `MDN-WEBM-CHROMIUM-PROBE: PASS`. This closes a finite WebM top-level player
  path without product changes. The protected Rust sibling remains untouched.

## 2026-09-05 — HTML5Demo Ogg player boundary

- Added `fixtures/public_html5demo_ogg_chromium_probe.py` against the live
  page `https://html5demo.yo.fr/demo/video.php`, whose fetched HTML exposes
  three top-level videos; the first is
  `https://html5demo.yo.fr/demo/media/windowsill.ogv` with `video/ogg`.
- The stale copied `#mwe_player_0` selector was replaced with a source-based
  selector for the page's actual Ogg `<video>`, and the helper scrolls that
  element into view before a trusted CDP click. The runtime DOM confirmed the
  Ogg element has `readyState=4`, `paused=false`, duration `20`, and geometry
  `213x53`.
- The corrected real Chromium probe remained blocked at the product-control
  boundary: after the full wait, `#dm-media-download-button` was absent. The
  page's Ogg is decoded and playing, but no native job was created, so there
  was no valid commit, native output, or browser-reference comparison to
  claim. Exact probe exit was `1` with
  `HTML5 demo Ogg player did not become playable/injected ... button=False`.
- The extension source's actual visibility rule is `width >= 120` and
  `height >= 40`; this element meets it. The page nevertheless did not expose
  a reachable Download control in this fresh profile. This is recorded as a
  page/extension reachability boundary, not as an Ogg decode failure or a
  product fix. No product code was changed; the protected Rust sibling remains
  untouched.
- Fresh re-run on 2026-09-07 (`/tmp/dm-public-html5demo-ogg-fresh-20260907.log`,
  probe exit `1`) reproduced the same state: all three page videos reported
  `readyState=4`, `paused=false`, and the selected Ogg player reported duration
  `20` with `button=false`. The loader diagnostic
  (`/tmp/dm-public-html5demo-ogg-response.log`) saw the actual browser document
  response as HTTP `200`, `text/html`, with no CSP header, and no final-frame
  redirect; Chromium created no extension content-script context for that public
  navigation. This rules out an Ogg decode or native acquisition failure but
  does not establish a generalized product defect, so no product code changed.

## 2026-09-05 — W3C recycled-player source switch

- Added `fixtures/public_w3c_source_switch_chromium_probe.py` against the
  official W3C HTML5 Video Events page at
  `https://www.w3.org/2010/05/video/mediaevents.html`.
- The probe first clicked the page's trusted `Test movie` control. The control
  reused the existing top-level `<video>`, changed its child source to the
  alternate `http://media.w3.org/2010/05/video/movie_300.mp4`, and called
  `load()`. Chromium automatically upgraded the media request to HTTPS and
  reported three range requests. The player then reported `readyState=4`,
  `paused=false`, duration `300.141134`, and the injected Download button.
- Exactly one native media job was created from the switched source. Resident
  `--commit` returned exit `0`; the job completed with `provisional=false`.
- The first run exposed two probe assumptions, both corrected before the PASS:
  the page keeps MP4 before its WebM fallback, and its media server does not
  allow CORS from the W3C page. The final probe asserts the observed MP4
  source and navigates the same Chromium target to the media server origin for
  a same-origin byte reference.
- The browser reference recorded `2757913` bytes and SHA-256
  `80c548058688a577ce9ca501cf9807311b95cc526cc82d292ec7e138e42257de`.
  The managed native output matched both values. Chromium Downloads was empty.
- Exact output ended with
  `W3C-SOURCE-SWITCH-CHROMIUM: PASS (page=https://www.w3.org/2010/05/video/mediaevents.html,
  source=http://media.w3.org/2010/05/video/movie_300.mp4,
  output_bytes=2757913,
  output_sha256=80c548058688a577ce9ca501cf9807311b95cc526cc82d292ec7e138e42257de,
  traffic=3, jobs=1, browser_downloads=[])` and
  `W3C-SOURCE-SWITCH-CHROMIUM-PROBE: PASS`. This closes a reachable recycled
  player/source-update path without product changes. The protected Rust sibling
  remains untouched.

## 2026-09-05 — MediaElement.js custom player capture

- Added `fixtures/public_mediaelement_chromium_probe.py` against the official
  MediaElement.js homepage at `https://mediaelement.github.io/mediaelement/`.
  Its live HTML exposes top-level `#player1` with the library's custom player
  and GitHub-hosted Big Buck Bunny MP4 source.
- The probe used a generic source-based player selector and trusted CDP click,
  avoiding the stale page-specific selector from the earlier copied harness.
  Chromium reported `readyState=4`, `paused=false`, duration `60.095011`, and
  the injected Download button. Network evidence captured the GitHub source,
  raw redirect, and final `raw.githubusercontent.com` asset.
- Exactly one native media job was created. Resident `--commit` returned exit
  `0`; the job completed with `provisional=false`.
- The browser same-origin reference recorded `5510872` bytes and SHA-256
  `543a4ad9fef4c9e0004ec9482cb7225c2574b0f889291e8270b1c4d61dbc1ab8`.
  The managed native output matched both values. Chromium Downloads was empty.
- The first run exposed and corrected a probe-only query-string selector bug:
  the real GitHub source ends in `big_buck_bunny.mp4?raw=true`, so the helper
  now checks the URL path before its query. The final output ended with
  `MEDIAELEMENT-CHROMIUM: PASS (page=https://mediaelement.github.io/mediaelement/,
  output_bytes=5510872,
  output_sha256=543a4ad9fef4c9e0004ec9482cb7225c2574b0f889291e8270b1c4d61dbc1ab8,
  traffic=5, jobs=1, browser_downloads=[])` and
  `MEDIAELEMENT-CHROMIUM-PROBE: PASS`. No product code changed; the protected
  Rust sibling remains untouched.

## 2026-09-05 — MediaElement HLS boundary and DASH capture

- The official MediaElement source selector exposes an HLS option, but its
  exact live playlist
  `https://bitdash-a.akamaihd.net/content/MI201109210084_1/m3u8s/f08e80da-bf1d-4e3d-8899-f0f6155f6efa.m3u8`
  returned HTTP `403` with `content-type: text/html` and `486` bytes. This is
  an external CDN boundary; no product defect was inferred and no HLS probe
  was claimed.
- Added `fixtures/public_mediaelement_dash_chromium_probe.py` for the
  canonical MediaElement page `https://www.mediaelementjs.com/`. The page's
  real `#player1-sources` change handler selected `M(PEG)-DASH` and resolved
  the protocol-relative option to
  `https://www.bok.net/dash/tears_of_steel/cleartext/stream.mpd`.
- Browser CDP observed the MPD, video/audio initialization segments, and
  `.m4f` media segments from `www.bok.net`. Runtime player evidence was
  `readyState=4`, `paused=false`, duration `734`, with the product Download
  button present. The native job source was the MPD; the database contained
  one media job.
- Resident `--commit` exited `0`. Final job state was `completed` with
  `provisional=false`. The managed output was `59,936,285` bytes with SHA-256
  `f1e5f73a9ae02e8eee29c16c55c86ae2a9336d8e94d3f6a6ba06353d5a796b12`.
  `ffprobe` reported AAC audio plus H.264 video at `512x214`, duration
  `734.166667`. Browser-context fetch of the selected MPD was `1,913` bytes
  with SHA-256
  `408a09285a7b2fda93b835998c1273902a1e03e0ed827eacedccf542b787c620`.
  Chromium Downloads was empty.
- The final output ended with
  `MEDIAELEMENT-DASH-CHROMIUM: PASS (traffic=10, jobs=1,
  browser_downloads=[])` and
  `MEDIAELEMENT-DASH-CHROMIUM-PROBE: PASS`. No product code changed; the
  protected Rust sibling remains untouched.

## 2026-09-05 — Able Player accessibility-player capture

- Added `fixtures/public_ableplayer_chromium_probe.py` against the official
  Able Player demo at
  `https://ableplayer.github.io/ableplayer/demos/video1.html`. Its live DOM
  contains one top-level `#video1` with Able Player's custom accessibility
  controls and `video/mp4` source
  `https://ableplayer.github.io/ableplayer/media/wwa.mp4`.
- Runtime Chromium evidence was `readyState=4`, `paused=false`, duration
  `52.406826`; the extension Download button was present. Chromium made three
  range requests for the MP4.
- The native database contained one media job. Resident `--commit` exited `0`.
  Final job state was `completed` with `provisional=false`. The managed output
  was `5,613,210` bytes, matching the browser-context reference size.
  Both native and browser SHA-256 values were
  `87716917cfefa444ecc3ae9e4a05a779dbf6621f62ad0c61bc24211283cd6e38`.
  Chromium Downloads was empty.
- The final output ended with
  `ABLEPLAYER-CHROMIUM: PASS (traffic=3, jobs=1, browser_downloads=[])` and
  `ABLEPLAYER-CHROMIUM-PROBE: PASS`. No product code changed; the protected
  Rust sibling remains untouched.

## 2026-09-05 — Able Player transcript initiation

- Added `fixtures/public_ableplayer_transcript_chromium_probe.py` against the
  official transcript demo at
  `https://ableplayer.github.io/ableplayer/demos/video3.html`. The probe used
  the page's generated `Show transcript` control (`aria="Show transcript"`,
  title `Show transcript`) and then the generated
  `.able-transcript-seekpoint` `Intro` cue (`data-start=0`, `data-end=37`).
  Both were activated by trusted Chromium CDP mouse clicks.
- After the transcript cue click, the real video reported `readyState=4`,
  `paused=false`, duration `52.406826`, and the Download button was present.
  Chromium made three range requests for
  `https://ableplayer.github.io/ableplayer/media/wwa.mp4`.
- The native database contained one media job. Resident `--commit` exited `0`.
  Final job state was `completed` with `provisional=false`. The managed output
  and browser-context reference were both `5,613,210` bytes with matching
  SHA-256
  `87716917cfefa444ecc3ae9e4a05a779dbf6621f62ad0c61bc24211283cd6e38`.
  Chromium Downloads was empty.
- The final output ended with
  `ABLEPLAYER-CHROMIUM: PASS (page=https://ableplayer.github.io/ableplayer/demos/video3.html,
  traffic=3, jobs=1, browser_downloads=[])` and
  `ABLEPLAYER-CHROMIUM-PROBE: PASS`. No product code changed; the protected
  Rust sibling remains untouched.

## 2026-09-05 — Able Player dynamic video creation

- Added `fixtures/public_ableplayer_dynamic_chromium_probe.py` against the
  official dynamic-player demo at
  `https://ableplayer.github.io/ableplayer/demos/external6.html`. The page
  initially had no media element. A trusted CDP click on `#addVideo` created
  the page's duplicate-ID wrapper plus `video#video1`; the generated source
  was `https://ableplayer.github.io/ableplayer/media/wwa.mp4`.
- The generated video was `640x480`, `readyState=4`, and the player-bound
  Download button was present after activation. Chromium made three range
  requests for the MP4.
- The native database contained one media job. Resident `--commit` exited `0`.
  Final job state was `completed` with `provisional=false`. The managed output
  and browser-context reference were both `5,613,210` bytes with matching
  SHA-256
  `87716917cfefa444ecc3ae9e4a05a779dbf6621f62ad0c61bc24211283cd6e38`.
  Chromium Downloads was empty.
- The final output ended with
  `ABLEPLAYER-CHROMIUM: PASS (page=https://ableplayer.github.io/ableplayer/demos/external6.html,
  traffic=3, jobs=1, browser_downloads=[])` and
  `ABLEPLAYER-CHROMIUM-PROBE: PASS`. No product code changed; the protected
  Rust sibling remains untouched.

## 2026-09-05 — retained public iframe candidate boundaries

- Ran `fixtures/public_iframe_paciello_audio_chromium_probe.py` with fresh
  real Chromium/native-host state. It exited `1` before native capture:
  `Iframe Tester Paciello audio player did not become playable/injected`.
  The only exposed page execution context was origin
  `https://iframe.verekia.com`, with frame URL
  `https://iframe.verekia.com/?allow=accelerometer;%20autoplay;%20clipboard-write;%20encrypted-media;%20fullscreen;%20gyroscope;%20picture-in-picture;%20web-share&w=1280&h=720&url=https://thepaciellogroup.github.io/AT-browser-tests/acc-name-test/audio.html`.
  No player context or Download button was observable.
- Ran `fixtures/public_mdn_iframe_audio_chromium_probe.py` with fresh real
  Chromium/native-host state. It exited `1` before native capture:
  `MDN iframe audio player did not become playable/injected`.
  Chromium exposed the MDN top-level frame and a second MDN-origin context,
  but no playable child media context or Download button was observable.
- Ran `fixtures/public_sa11y_soundcloud_iframe_chromium_probe.py` with fresh
  real Chromium/native-host state. It exited `1` before native capture:
  `Sa11y SoundCloud iframe player did not become playable/injected`.
  The only exposed page context was origin `https://panzi.github.io`, frame
  URL `https://panzi.github.io/embedplayer/`; no player context or Download
  button was observable.
- These are retained external iframe/frame-boundary results. No native job,
  product defect, or policy change was inferred. The three candidate probes
  are now durable artifacts for the audit. The protected Rust sibling remains
  untouched.

## 2026-09-05 — close-to-tray transfer continuity

- Ran `fixtures/close_to_tray_probe.py` against a fresh resident application,
  a real 64 MiB slow transfer, and one forwarded capture. The main window was
  closed through the live WebKit window API while the transfer was active.
- X11 reported `Map State: IsUnMapped`; the Tauri visibility result was `{}`
  from the installed inspector endpoint. The resident remained alive with
  exactly one application process.
- The transfer reached `finalizing` at `67,108,864` bytes and the fixture
  server saw exactly one source request. The final output ended with
  `CLOSE-TO-TRAY: PASS (main_visible={}, processes=1, state=finalizing,
  bytes=67108864, source_requests=1)` and
  `CLOSE-TO-TRAY-PROBE: PASS`. No product code changed; the protected Rust
  sibling remains untouched.

## 2026-09-05 — independent simultaneous Add Download windows

- Ran `fixtures/multiple_add_windows_probe.py` against a fresh resident and
  three rapid native capture forwards for `one.bin`, `two.bin`, and `three.bin`.
- The real application opened three Add Download windows and created three
  distinct jobs while keeping exactly one resident application process.
  Each source server counter was exactly one request:
  `{'one.bin': 1, 'three.bin': 1, 'two.bin': 1}`.
- The run ended with
  `MULTIPLE-ADD-WINDOWS: PASS (windows=3, jobs=3, processes=1,
  source_requests={'one.bin': 1, 'three.bin': 1, 'two.bin': 1})` and
  `MULTIPLE-ADD-WINDOWS-PROBE: PASS`. No product code changed; the protected
  Rust sibling remains untouched.

## 2026-09-05 — Able Player hidden-player exposure

- Added `fixtures/public_ableplayer_hidden_chromium_probe.py` against the
  official demo at
  `https://ableplayer.github.io/ableplayer/demos/external5.html`. Both media
  elements began inside `div.able { display:none }`. A trusted CDP click on
  `#showPlayers` changed both containers to `display:block`; the runtime then
  exposed one audio and one video player.
- The video source was
  `https://ableplayer.github.io/ableplayer/media/wwa.mp4`. Chromium reported
  `readyState=4`, `paused=false`, duration `52.406826`, and observed seven
  range requests across the exposed players. The Download button was present.
- The native database contained one media job. Resident `--commit` exited `0`.
  Final job state was `completed` with `provisional=false`. The managed output
  and browser-context reference were both `5,613,210` bytes with matching
  SHA-256
  `87716917cfefa444ecc3ae9e4a05a779dbf6621f62ad0c61bc24211283cd6e38`.
  Chromium Downloads was empty.
- The final output ended with
  `ABLEPLAYER-CHROMIUM: PASS (page=https://ableplayer.github.io/ableplayer/demos/external5.html,
  traffic=7, jobs=1, browser_downloads=[])` and
  `ABLEPLAYER-CHROMIUM-PROBE: PASS`. No product code changed; the protected
  Rust sibling remains untouched.

## 2026-09-05 — Able Player external-control initiation

- Added `fixtures/public_ableplayer_external_chromium_probe.py` against the
  official external-controls demo at
  `https://ableplayer.github.io/ableplayer/demos/external4.html`. The page's
  separate `#play` control was discovered at runtime and activated with a
  trusted Chromium click. Its `#status-play` changed to `true`.
- The underlying `video#video1` reported `readyState=4`, `paused=false`,
  duration `52.406826`, and the product Download button was present. Chromium
  observed two range requests for
  `https://ableplayer.github.io/ableplayer/media/wwa.mp4`.
- The native database contained one media job. Resident `--commit` exited `0`.
  Final job state was `completed` with `provisional=false`. The managed output
  and browser-context reference were both `5,613,210` bytes with matching
  SHA-256
  `87716917cfefa444ecc3ae9e4a05a779dbf6621f62ad0c61bc24211283cd6e38`.
  Chromium Downloads was empty.
- The final output ended with
  `ABLEPLAYER-CHROMIUM: PASS (page=https://ableplayer.github.io/ableplayer/demos/external4.html,
  traffic=2, jobs=1, browser_downloads=[])` and
  `ABLEPLAYER-CHROMIUM-PROBE: PASS`. No product code changed; the protected
  Rust sibling remains untouched.

## 2026-09-05 — dynamic Able Player audio and zero-native-box fallback

- Added `fixtures/public_ableplayer_dynamic_audio_chromium_probe.py` for the
  official dynamic-player page
  `https://ableplayer.github.io/ableplayer/demos/external6.html`. The real
  `#addAudio` control created `audio#audio1` with the verified
  `https://ableplayer.github.io/ableplayer/media/smallf.mp3` source.
- The first real Chromium/native run exposed a concrete gap: the playing audio
  reached `readyState=4`, `paused=false`, duration `232.0521`, and its Able
  wrapper was visible at `640x105`, but the native `<audio>` box was `0x0` and
  no Download button or native job appeared. The fixture exited `1`.
- The focused fix in `extension/src/content.ts` keeps normal video and visible
  audio geometry unchanged. For a playing, enabled `<audio>` with a zero-sized
  own box, visibility, ranking, and button placement may use a visible,
  non-root ancestor within five levels. Hidden or paused media remains
  ineligible.
- Rebuilt the extension with `npm run build:extension` and ran `npx tsc -b`;
  both exited `0`. The rerun passed the same real scenario: one native media
  job, resident `--commit` exit `0`, final `completed` and `provisional=false`,
  `4,690,721` native/browser bytes, matching SHA-256
  `60c777096e72ae34ceb250d66f071ba6b612f445aefba8438d12e8536288a2de`, and no
  Chromium Downloads.
- The final output ended with
  `ABLEPLAYER-CHROMIUM: PASS (page=https://ableplayer.github.io/ableplayer/demos/external6.html,
  source=https://ableplayer.github.io/ableplayer/media/smallf.mp3,
  output_bytes=4690721, output_sha256=60c777096e72ae34ceb250d66f071ba6b612f445aefba8438d12e8536288a2de,
  traffic=2, jobs=1, browser_downloads=[])` and
  `ABLEPLAYER-CHROMIUM-PROBE: PASS`.

## 2026-09-05 — Able Player playlist audio source selection

- Added `fixtures/public_ableplayer_playlist_audio_chromium_probe.py` for the
  official playlist page
  `https://ableplayer.github.io/ableplayer/demos/playlist1-audio.html`. The
  probe trusted-clicked the second generated playlist item, `PHP 7.0 Alpha`,
  and verified that `audio#audio1` switched to
  `https://ableplayer.github.io/ableplayer/media/php70alpha.mp3`.
- The selected audio reached `readyState=4`, `paused=false`, duration
  `278.256286`; its rendered Able wrapper was `640x260`, and the product
  Download button was present. The page made two initial `paulallen.mp3`
  requests, but the single native job source was only the selected
  `php70alpha.mp3` URL.
- Resident `--commit` exited `0`. Final job state was `completed` with
  `provisional=false`. The managed output and browser-context reference were
  both `3,895,621` bytes with matching SHA-256
  `e89bbffc3889ee44f63de9960b715ddab12301c008fe9fe988053814181d0a4b`.
  Chromium Downloads was empty.
- The final output ended with
  `ABLEPLAYER-CHROMIUM: PASS (page=https://ableplayer.github.io/ableplayer/demos/playlist1-audio.html,
  source=https://ableplayer.github.io/ableplayer/media/php70alpha.mp3,
  output_bytes=3895621, output_sha256=e89bbffc3889ee44f63de9960b715ddab12301c008fe9fe988053814181d0a4b,
  traffic=3, jobs=1, browser_downloads=[])` and
  `ABLEPLAYER-CHROMIUM-PROBE: PASS`.

## 2026-09-05 — Able Player autoplay at a non-zero start time

- Added `fixtures/public_ableplayer_autoplay_audio_chromium_probe.py` for the
  official page
  `https://ableplayer.github.io/ableplayer/demos/audio3.html`. The real
  `audio#audio1` element has `data-start-time="140"` and `autoplay`; the probe
  required the page itself to reach `currentTime=140` while playing before
  capture.
- Chromium reported `readyState=4`, `paused=false`, duration `232.0521`, source
  `https://ableplayer.github.io/ableplayer/media/smallf.mp3`, and the product
  Download button. Two range requests were observed.
- Resident `--commit` exited `0`. Final job state was `completed` with
  `provisional=false`. The managed output and browser-context reference were
  both `4,690,721` bytes with matching SHA-256
  `60c777096e72ae34ceb250d66f071ba6b612f445aefba8438d12e8536288a2de`.
  Chromium Downloads was empty.
- The final output ended with
  `ABLEPLAYER-CHROMIUM: PASS (page=https://ableplayer.github.io/ableplayer/demos/audio3.html,
  source=https://ableplayer.github.io/ableplayer/media/smallf.mp3,
  output_bytes=4690721, output_sha256=60c777096e72ae34ceb250d66f071ba6b612f445aefba8438d12e8536288a2de,
  traffic=2, jobs=1, browser_downloads=[])` and
  `ABLEPLAYER-CHROMIUM-PROBE: PASS`.

## 2026-09-05 — DASH-IF trusted quality-menu selection and native capture

- Added `fixtures/public_dash_quality_chromium_probe.py` against the official
  DASH-IF reference player at
  `https://reference.dashif.org/dash.js/latest/samples/dash-if-reference-player/index.html`.
  The fixture used a fresh Chromium profile and the public static MPD
  `https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps_shortened.mpd`.
- Chromium reached a playable blob/MSE player with `duration=30`,
  `readyState=4`, and `paused=false`. A trusted CDP mouse click discovered the
  visible button with `title="Quality"`. The rendered menu exposed ten
  non-Auto video entries plus one audio entry. A second trusted click selected
  `254 kbps (320x180)`, and the menu's selected state confirmed
  `254 kbps (320x180)`.
- The product Download button was then activated through trusted Chromium
  input. The native database contained one media job. Resident `--commit`
  forwarded that job and exited `0`; the final state was `completed` with
  `provisional=false`.
- The browser-context reference fetched the first video and audio
  representations from the MPD: video `9,755,337` bytes with SHA-256
  `44ddc05092974595726f81181796611d257ac55927244b36d8b60129bf8a839f`, and
  audio `267,561` bytes with SHA-256
  `26195401fbff20512d36a9b08c9fd8e9c45b62fe0ad8e9a212a7b6ea24be9273`.
  The completed native MP4 was `15,894,760` bytes with SHA-256
  `365d01c1ebad60049d8e17a20fa081a4c64fdc8fc21dbd110356b31afd053eed`.
  `ffprobe` reported H.264 video and AAC audio with duration `32.085333`.
- Browser ownership remained intact after capture: trusted right-click and
  Ctrl-click on a browser-owned link were both not prevented. Chromium
  Downloads was empty and the database contained exactly one job.
- The exact terminal result ended with
  `DASH-QUALITY: {"control": {"button": true, "title": "Quality", "x": 726.015625, "y": 669.5234375}, "items": ["254 kbps (320x180)", "507 kbps (320x180)", "760 kbps (480x270)", "1013 kbps (640x360)", "1255 kbps (640x360)", "1884 kbps (768x432)", "3134 kbps (1024x576)", "4953 kbps (1280x720)", "9915 kbps (1920x1080)", "14932 kbps (3840x2160)", "67 kbps"], "selected": ["254 kbps (320x180)", "Auto"], "target": "254 kbps (320x180)"}` and
  `PUBLIC-DASH-CHROMIUM-PROBE: PASS`.
- The extension diagnostic also returned the native policy with
  `interceptDownloads=true`. No product code changed; the protected Rust
  sibling remains untouched.

## 2026-09-05 — WET audio player with Archive redirect and zero native box

- Added `fixtures/public_wet_audio_chromium_probe.py` against the current Web
  Experience Toolkit audio-only demo:
  `https://wet-boew.github.io/wet-boew/demos/multimedia/multimedia-audio-en.html`.
  The page exposes one top-level HTML5 `<audio>` with MP3 and OGG sources. The
  selected MP3 source was
  `https://www.archive.org/download/RideOfTheValkyries/ride_of_the_valkyries_2.mp3`.
- Fresh real Chromium reported `readyState=4`, `paused=false`, duration
  `246.386938`, and three browser requests across the Archive redirect. The
  native audio element's own rectangle was `0x0`, but the extension's real
  player-bound Download button was visible and activated by trusted CDP input.
  This exercises the wrapper-geometry path proven by the earlier zero-box audio
  fix on a different public player implementation.
- The first run reached native capture and resident commit but its browser-side
  reference fetch failed with the exact CDP error `TypeError: Failed to fetch`.
  This was a fixture CORS boundary: the player could consume the Archive media,
  while a cross-origin page fetch could not read it. The probe was changed only
  to navigate to the exact source, wait for the redirect-resolved media origin,
  and fetch `location.href` from that same origin.
- The corrected fresh-profile rerun created exactly one native media job.
  Resident `--commit` forwarded it and exited `0`; final state was `completed`
  with `provisional=false`. The browser-context reference resolved to
  `https://dn711106.ca.archive.org/0/items/RideOfTheValkyries/ride_of_the_valkyries_2.mp3`
  and recorded `3,942,417` bytes with SHA-256
  `d712ccb817a8a205292283eec45d814f3d3805a68ecfde1d4c7e59792fd8fc3d`.
  The native output matched exactly. Chromium Downloads was empty and the
  database contained one job.
- Exact output ended with
  `WET-AUDIO-CHROMIUM: PASS (page=https://wet-boew.github.io/wet-boew/demos/multimedia/multimedia-audio-en.html, source=https://www.archive.org/download/RideOfTheValkyries/ride_of_the_valkyries_2.mp3, output_bytes=3942417, output_sha256=d712ccb817a8a205292283eec45d814f3d3805a68ecfde1d4c7e59792fd8fc3d, traffic=3, jobs=1, browser_downloads=[])` and
  `WET-AUDIO-CHROMIUM-PROBE: PASS`. No product code changed; the protected
  Rust sibling remains untouched.

## 2026-09-05 — Custom Video Player trusted play control

- Added `fixtures/public_custom_video_player_chromium_probe.py` against the
  safe GitHub Pages demo
  `https://chrisnajman.github.io/custom-video-player/`. Its live page exposes
  one top-level `<video>` with three source formats, captions, a transcript,
  and a JavaScript custom control bar.
- The fresh Chromium profile loaded the paused video at `readyState=4`,
  duration `70.542222`, and source
  `https://iandevlin.github.io/mdn/video-player/video/tears-of-steel-battle-clip-medium.mp4`.
  A trusted CDP click on the page's actual `#playpause` control changed the
  state from `paused=true`, `text="Play"` to `paused=false`, `text="Pause"`.
  The product's player-bound Download button then appeared. This proves a
  custom page control can initiate playback before media capture without
  script-only `video.play()` being used by the probe.
- The product Download button was activated by trusted CDP input. Exactly one
  native media job was created. Resident `--commit` forwarded it and exited
  `0`; final state was `completed` with `provisional=false`.
- Browser-context fetch recorded `15,256,787` bytes and SHA-256
  `8f8b69ed443be171cb505c75fab22f3af25375b09817e713fe0fd7c88c78f451`.
  The managed native output matched both values. Chromium Downloads was empty
  and the database contained exactly one job.
- The first probe attempt had one harness-only assertion typo: its control
  diagnostic omitted the already-observed `video=true` field and rejected a
  valid paused surface. After adding that field, `py_compile` and the real
  fresh-profile Chromium/native rerun passed without product changes. Exact
  output ended with
  `CUSTOM-PLAYER-HTML5-CHROMIUM: PASS (page=https://chrisnajman.github.io/custom-video-player/, source=https://iandevlin.github.io/mdn/video-player/video/tears-of-steel-battle-clip-medium.mp4, output_bytes=15256787, output_sha256=8f8b69ed443be171cb505c75fab22f3af25375b09817e713fe0fd7c88c78f451, traffic=1, jobs=1, browser_downloads=[])` and
  `CUSTOM-PLAYER-HTML5-CHROMIUM-PROBE: PASS`. No product code changed; the
  protected Rust sibling remains untouched.

## 2026-09-05 — Custom Video Player captions and transcript controls

- Re-ran `fixtures/public_custom_video_player_chromium_probe.py` after adding
  trusted interaction coverage for the page's own accessibility controls. The
  fresh profile again loaded the paused top-level video at `readyState=4`,
  duration `70.542222`, and the same MP4 source.
- A trusted click on `#playpause` changed `paused=true`, `text="Play"` to
  `paused=false`, `text="Pause"`. Trusted clicks then enabled captions with
  `textTracks[0].mode="showing"` and label `"Captions on"`, and opened the
  transcript with `details.open=true`.
- The product Download button was activated after those page interactions.
  Exactly one native media job was created; resident `--commit` exited `0`; the
  final state was `completed` with `provisional=false`. Native and
  browser-context output were both `15,256,787` bytes with SHA-256
  `8f8b69ed443be171cb505c75fab22f3af25375b09817e713fe0fd7c88c78f451`.
  Chromium Downloads was empty.
- Exact output included
  `CUSTOM-PLAYER-CONTROL: {"after": {"button": true, "currentTime": 0, "paused": false, "readyState": 4, "text": "Pause"}, "before": {"button": true, "paused": true, "readyState": 4, "text": "Play", "video": true, "x": 262.5, "y": 630.96875}, "captions": {"label": "Captions on", "mode": "showing"}, "captionsClick": {"selector": "#captions", "x": 753.53125, "y": 624.46875}, "transcript": {"open": true, "status": "Open"}, "transcriptClick": {"selector": "#summary", "x": 632.5, "y": 676.46875}, "trustedClick": true}` and
  `CUSTOM-PLAYER-HTML5-CHROMIUM-PROBE: PASS`. No product code changed; the
  protected Rust sibling remains untouched.

## 2026-09-05 — MDN styled-player mute-control boundary

- Added `fixtures/public_mdn_styled_player_chromium_probe.py` for the live MDN
  styled player
  `https://iandevlin.github.io/mdn/video-player-styled/`. The page exposes one
  top-level `<video>` (`readyState=4`, duration `70.542222`) and an external
  custom control bar with `#playpause`, `#stop`, `#mute`, volume, and fullscreen
  buttons. The player uses three fallback sources and the MP4 source was
  `http://iandevlin.github.io/mdn/video-player/video/tears-of-steel-battle-clip-medium.mp4`.
- Fresh Chromium reached the paused media surface and the trusted `#playpause`
  path started playback; the Download Manager button appeared. The next trusted
  click targeted a visible, enabled 40x40 `BUTTON` at `x=1006,y=523.671875`;
  `document.elementFromPoint` returned `id="mute"`, and a capture listener saw
  `{"trusted":true,"target":"mute"}`.
- Despite that delivered trusted event, the official page listener left
  `video.muted=false`, label `Mute/Unmute`, and `data-state=mute` after 15
  seconds. The same page's programmatic `#mute.click()` immediately produced
  `{"muted":true,"dataState":"unmute","label":"Mute/Unmute"}`. The
  extension has no listener that interferes with this button: its only
  capture-phase click interception requires an anchor with `download`.
- Result: `MDN-STYLED-HTML5-CHROMIUM-PROBE: FAIL` at the external page-control
  assertion. This is a retained external browser/page trusted-input boundary,
  not a Download Manager capture failure; no native job or PASS is claimed.
- The probe's compile passed. The retained disposable profile was used only for
  diagnosis and is cleaned after this run.
- A fresh rerun on 2026-09-06 reproduced the same concrete boundary in
  `/tmp/dm-public-mdn-styled.log`: the page was reachable, the extension button
  was injected, and the trusted mute event was observed, but `video.muted`
  remained false until a programmatic click. No native capture assertion ran;
  this remains an external page-control blocker, not a product defect.

## 2026-09-05 — Native failure restores one browser download without recursion

- Added `fixtures/browser_native_failure_fallback_chromium_probe.py` for the
  host-present failure branch of SPEC §5.1.1. It registers a disposable stdio
  native host that answers `get-policy` successfully but returns
  `{"ok":false,"error":"simulated native capture failure"}` for
  `capture-acquisition`. The fixture uses a fresh Chromium profile/HOME and a
  safe local 64 KiB object.
- The first fresh-profile run exposed a real extension defect. The prevented
  explicit-download click sent one native capture, then the fallback
  `chrome.downloads.download()` caused `downloads.onCreated` to send a second
  native capture. Chromium's second event had the same source but
  `pageUrl=null` and an empty filename; `byExtensionId` was not populated in
  this headless run. The run was rejected rather than claimed as PASS.
- The focused fix is in `extension/src/background.ts`. Before invoking the
  Downloads API, the worker records a bounded, 30-second source/name marker.
  The `onCreated` listener consumes a matching marker before its observe-only
  native forwarding path. It still keeps the documented `byExtensionId` guard,
  and does not cancel browser downloads or alter native acquisition policy.
- Rebuilt with `npm run build:extension` and ran `npx tsc -b`; both exited `0`.
  The corrected real Chromium/native rerun produced exactly one
  `capture-acquisition` message for the user click, exactly one browser
  download (id `1`), one local source request, and no native DB, native job, or
  native binary process. The browser item was `complete`, `error=null`,
  `bytesReceived=65536`, and the saved artifact was `65536` bytes with SHA-256
  `d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a`.
- Exact terminal output ended with
  `BROWSER-NATIVE-FAILURE-FALLBACK: PASS (browser_bytes=65536,
  sha256=d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a,
  downloads=1, source_requests=1, native_jobs=0, native_processes=0)` and
  `BROWSER-NATIVE-FAILURE-FALLBACK-CHROMIUM-PROBE: PASS`.
- The protected `src-tauri/src/main.rs` sibling remains unchanged and is not
  included in this slice.

## 2026-09-05 — Ordinary browser download preserved after fallback guard fix

- Re-ran `fixtures/ordinary_download_chromium_probe.py` in a fresh Chromium
  profile/HOME after commit `eb1d1f6`. The safe public source was GitHub's
  `octocat/Hello-World` archive, which redirected to the codeload URL.
- The plain link had no `download` attribute. Chromium created one browser
  item, `id=1`, `state=complete`, `error=null`, with `351` bytes. The
  observe-only `downloads.onCreated` path created one native provisional job;
  resident `--commit` exited `0` and the job ended `completed`,
  `provisional=false`.
- Browser and native artifacts were both `351` bytes with the identical
  SHA-256 `acd2fd3563d8de4b46dae1edeb96607b3d105a0a3be894e90aa5472353a93233`.
  The database contained exactly one job. Exact output ended with
  `ORDINARY-DOWNLOAD: PASS (browser_bytes=351,
  sha256=acd2fd3563d8de4b46dae1edeb96607b3d105a0a3be894e90aa5472353a93233,
  native_bytes=351,
  native_sha256=acd2fd3563d8de4b46dae1edeb96607b3d105a0a3be894e90aa5472353a93233,
  browser_state=complete, jobs=1)` and
  `ORDINARY-DOWNLOAD-PROBE: PASS`.
- This is a regression check of the normal browser-owned path, not a new
  interception policy: the browser copy remains intact while the resident app
  receives one observe-only capture. The protected Rust sibling remains
  untouched.

## 2026-09-05 — Explicit Save-As gesture remains browser-owned

- Added `fixtures/save_as_ownership_chromium_probe.py` for SPEC §§5.2 and
  19.2. The fresh Chromium profile registered a disposable native host, built a
  visible explicit `<a download>` on a local page, and sent a trusted right
  click (`button=2`) to represent Save Link As. The probe's own listener then
  prevented the headless context menu from opening, but recorded the event
  before doing so.
- Chromium delivered `isTrusted=true`, `button=2`, and
  `defaultPrevented=false` to the page listener. The extension's capture-phase
  ordinary-download handler did not intercept it: the fake host received only
  `get-policy` messages and zero `capture-acquisition` messages.
- Chromium's Downloads API returned an empty list, no application database was
  created, and no native job existed. Exact output ended with
  `SAVE-AS-OWNERSHIP-CHROMIUM: PASS (trusted_contextmenu=true,
  default_prevented=false, native_captures=0, browser_downloads=0,
  native_jobs=0)` and
  `SAVE-AS-OWNERSHIP-CHROMIUM-PROBE: PASS`.
- This proves the reachable Save-Link-As gesture boundary. The probe does not
  claim that headless Chromium selected an operating-system context-menu item;
  the menu itself is deliberately suppressed after the event is observed. The
  protected Rust sibling remains untouched.

## 2026-09-05 — Manual Add URL commits through the real UI

- Added `fixtures/manual_add_ui_probe.py` to close the manual Add URL acceptance
  in SPEC §§10.6 and 19.3. A fresh disposable HOME/Xvfb booted the compiled
  resident binary; the main WebKit inspector drove the rendered Add URL overlay,
  not a direct Tauri command.
- The overlay initially rendered `Start Download` disabled. After entering the
  local HTTP fixture URL and `manual-ui.bin`, the button became enabled. The
  server request count stayed `0` until that actual button was activated.
- The UI-created job was `provisional=true` and reached `finalizing` after one
  source request. The probe set the Save-to field through the same captured
  overlay and activated its real `Download` button. The resident job then ended
  `state=completed`, `provisional=false`, with `262144` bytes at the requested
  destination. The output matched the fixture payload byte-for-byte and had
  SHA-256 `ac6533c30d2d4fcc01be82be68bd63a592d37c49fe769b05aebfb4504fa146b3`.
- Exact output ended with `MANUAL-ADD-UI-PROBE: PASS` and one source request.
  The first attempt stopped before UI action on the known intermittent
  `WebKit inspector did not become ready: [Errno 111] Connection refused`.
  The next run exposed a fixture-only label mismatch (`Start Download`), which
  was corrected and then passed. No product source changed; the protected Rust
  sibling remains untouched.

## 2026-09-05 — Appearance settings persist through a real UI restart

- Added `fixtures/settings_ui_persistence_probe.py` for the still-uncovered
  user-facing persistence path. A fresh disposable HOME/Xvfb booted the real
  resident binary, and the main WebKit inspector clicked the Settings and
  Appearance controls rather than invoking `update_settings` directly.
- The rendered UI changed from `theme=system, accent=#0878ed` to
  `theme=dark, accent=#d3138c`; the native SQLite settings row held both values
  immediately. After terminating and restarting only that disposable app, the
  Appearance surface restored both values, with `sqlite_match=true` and
  `restart=true`.
- Exact output ended with
  `SETTINGS-UI-PERSISTENCE: PASS (theme=dark, accent=#d3138c,
  sqlite_match=true, restart=true)` and `SETTINGS-UI-PROBE: PASS`.
- The original probe intentionally left App density unclaimed because WebKit
  Inspector has no `Input` domain (`-32601`). The extended probe now uses the
  native HTML `select.value` setter plus a bubbled `change` event: the first
  real UI phase changed density from `comfortable` to `compact` and SQLite
  immediately saved `density=compact`. The second disposable boot hit the known
  Inspector startup race (`[Errno 111] Connection refused`) before restored
  density could be inspected; a fresh retry hit the same race before the first
  UI phase. Evidence: `/tmp/dm-settings-ui-density.log` and
  `/tmp/dm-settings-ui-density-retry.log`. Restart restoration remains
  unclaimed, and no product source changed.

## 2026-09-05 — Captured Add Download child target remains inspector-blocked

- Added `fixtures/child_window_inspector_probe.py` for a bounded protocol attempt
  against the real captured Add Download window. A disposable resident app,
  native capture, Xvfb display, and one visible `Add Download` X11 window were
  created successfully (`windows=1`).
- The WebKit inspector root responded, but `/json`, `/json/list`, and
  `/json/version` all returned HTTP 404. Candidate WebPage sockets did not expose
  a usable child target: socket `1/1` emitted no target event and
  `Target.getTargets` returned `-32601` (`'Target.getTargets' was not found`);
  socket `1/3` was only `about:blank`; sockets `1/2`, `1/4`, `1/5`, and `1/6`
  emitted unusable non-UTF-8 frames. No child Download click was claimed.
- Two fresh retries stopped earlier at the known inspector startup races
  (`[Errno 111] Connection refused` and no page-target event). The successful
  endpoint run is the retained blocker evidence. This is an instrumentation
  limit, not a product failure; the existing resident `--commit` path remains
  the verified fallback for child-window lifecycle tests. No product source
  changed; the protected Rust sibling remains untouched.

## 2026-09-05 — Public MDN progressive WebM capture

- Ran `fixtures/public_mdn_webm_chromium_probe.py` with a fresh disposable
  Chromium profile, the unpacked extension, a disposable native-host manifest,
  and the real resident binary. The page was the safe MDN simple-video example:
  `https://mdn.github.io/learning-area/html/multimedia-and-embedding/video-and-audio-content/simple-video.html`.
- Chromium reported one visible top-level video, source
  `https://mdn.github.io/learning-area/html/multimedia-and-embedding/video-and-audio-content/rabbit320.webm`,
  `readyState=4`, `paused=false`, duration `7.8`, and a visible 320x240 player.
  The Download button was at `x=270.4453125, y=104.2734375`, inside that player;
  the trusted CDP click created one media job for the exact current source.
- The native job began `downloading`, then the real resident `--commit` CLI
  forwarded it with exit `0`. The final job was `completed` and
  `provisional=false`. The output was `330618` bytes. A browser-context fetch
  of the exact source was also `330618` bytes, with matching SHA-256
  `074b046f0832c1c262a7a3e015b042092fa226b1550b83a7d14cca9025d34e1e`.
- Exactly one media request was observed, Chromium Downloads remained empty,
  and the probe ended with `MDN-WEBM-CHROMIUM-PROBE: PASS`.
- No product source changed. The dead uncommitted `wait_for_transfer_idle`
  duplicate was removed separately; `src-tauri/src/main.rs` is now clean.

## 2026-09-05 — Public hls.js segmented VOD capture

- Ran `fixtures/public_hls_chromium_probe.py` with a fresh disposable Chromium
  profile, the unpacked extension, a disposable native-host manifest, and the
  real resident binary. The safe public page was the hls.js demo, using the
  finite VOD manifest
  `https://test-streams.mux.dev/x36xhzz/url_6/193039199_mp4_h264_aac_hq_7.m3u8`.
- Chromium reported a playing blob/MSE video with `duration=634.6`,
  `readyState=4`, and a visible 1012x572.828 player. The product button was
  visible inside it at `x=1080.9453125, y=649.7578125`; the trusted click
  created one media job for the observed manifest rather than the blob URL.
- The browser reference independently fetched and concatenated 64 manifest
  segments: `71878228` bytes, SHA-256
  `6e830be99296a8452aa3294f25ca64193bee2b58bf4d559a7cc5ffbb1cda0a42`.
  Resident `--commit` exited `0`; the final native job was `completed` and
  `provisional=false`, with the same byte count and hash. Chromium Downloads
  was empty.
- Trusted browser-owned context-click and Ctrl-click remained unprevented.
  The exact run ended with `PUBLIC-HLS-CHROMIUM-PROBE: PASS`.
- No product source changed; `src-tauri/src/main.rs` remains clean.

## 2026-09-05 — Public live HLS is rejected cleanly

- Added `fixtures/public_live_rejection_chromium_probe.py` and ran it with a
  fresh disposable Chromium profile, the unpacked extension, a disposable
  native-host manifest, and the real resident binary. The safe public page was
  the hls.js demo with iReplay's continuously generated Blender channel:
  `https://ireplay.tv/test/blender.m3u8`.
- The live master returned HTTP 200 with content type
  `application/vnd.apple.mpegurl`, no `#EXT-X-ENDLIST`, and four `_live.m3u8`
  variants. Chromium loaded a playing blob/MSE video at `readyState=4`; its
  current `HTMLMediaElement.duration` was a finite DVR-window value (`1535`),
  so the manifest's sliding-window evidence—not duration infinity—was used to
  identify the live source.
- The player-bound Download button was visible inside the current 1012x569.25
  player (`attached=true`, button center `x=1080.9453125, y=439.7578125`). A
  trusted CDP click created one media job for the observed live variant
  `https://ireplay.tv/test/rate_3_28.m3u8`.
- The resident acquisition rejected the source before any commit with the
  generic core error `Live media is not supported; a finite VOD playlist is
  required`. The job ended `state=failed`; there was one job, no output, and
  Chromium Downloads was empty. The exact run ended with
  `PUBLIC-LIVE-REJECTION-CHROMIUM-PROBE: PASS`.
- No product source changed; `src-tauri/src/main.rs` remains clean.

## 2026-09-05 — Manager stale-context guard fixed; end-to-end evidence recorded

- Added `fixtures/manager_filters_context_chromium_probe.py` for SPEC
  §§10.1, 10.5, and 19.6. It runs the explicit `npm run dev` mock adapter in
  a fresh Chromium profile and drives the rendered manager with trusted CDP
  mouse input.
- The first real run exposed a product race rather than a probe-only failure:
  clicking `Remove from list` emitted a new snapshot before the context menu
  closed. `contextJobId` still named the removed row, so the render passed
  `undefined` through the non-null assertion and the React surface became
  blank. The failure was reproduced with the target button hit by a trusted
  event and the post-action DOM at zero rows.
- The minimal fix in `src/App.tsx` derives `contextJob` from the current
  snapshot and renders `JobContextMenu` only while that job exists. No native
  state or action semantics changed.
- A later fresh-profile command returned exit `0` with its complete stdout
  retained at `/tmp/dm-manager-filters-context-final.log`. The exact filter row
  sets, heading counts, context-menu labels, row removal, and inspector-selection
  assertions are recorded in the later `2026-09-05` manager-filters entry below.
- No native application or external service was touched during the mock probe.
  The protected Rust sibling remains clean.
- Verification after the repair passed `npm run build:all`, `npx tsc -b`,
  Vitest `38/38`, Rust `58/58`, fixture compilation, and `git diff --check`.

## 2026-09-05 — Redirected browser downloads retain the final filename

- The existing `fixtures/ordinary_download_chromium_probe.py` was tightened to
  compare the browser's final filename with the native provisional job name.
  The red real-Chromium baseline reproduced the reachable mismatch: browser
  `Hello-World-master.zip` versus native `master`, even though the source
  redirected from GitHub to codeload.
- Chrome's documented downloads lifecycle exposes a tentative basename at
  `onCreated`, then resolves the header/MIME filename during
  `onDeterminingFilename`. The extension's observe-only native forward now runs
  at that later event and always calls `suggest()`. Extension-initiated browser
  fallback downloads are still consumed by the existing marker and skipped.
- The corrected fresh-profile run recorded native name
  `Hello-World-master.zip`, browser filename
  `Hello-World-master.zip`, browser/native `351` bytes, and identical SHA-256
  `acd2fd3563d8de4b46dae1edeb96607b3d105a0a3be894e90aa5472353a93233`.
  Chromium finished one browser item and the resident finished one native job;
  no duplicate was created.
- Exact output ended with
  `ORDINARY-DOWNLOAD: PASS (browser_bytes=351,
  sha256=acd2fd3563d8de4b46dae1edeb96607b3d105a0a3be894e90aa5472353a93233,
  native_bytes=351,
  native_sha256=acd2fd3563d8de4b46dae1edeb96607b3d105a0a3be894e90aa5472353a93233,
  browser_state=complete, jobs=1)` and
  `ORDINARY-DOWNLOAD-PROBE: PASS`; the complete log is
  `/tmp/dm-ordinary-filename-green.log`.
- Neighboring real-browser regressions also passed: explicit-anchor native
  capture; native-failure browser fallback with one download/one request and
  zero native jobs; form POST fallback with browser POST/native GET and matching
  bytes; and integration-off page-owned download with zero native jobs. Logs:
  `/tmp/dm-reg-explicit-anchor.log`, `/tmp/dm-reg-native-fallback.log`,
  `/tmp/dm-reg-form.log`, and `/tmp/dm-reg-integration-off.log`.
- Full gates passed after the change: `npm run build:all`, `npx tsc -b`, Vitest
  `7` files/`38` tests, Rust `58/58`, fixture compilation, and `git diff --check`.
  The protected `src-tauri/src/main.rs` sibling remains clean.

## 2026-09-05 — Extension popup and manager handoff pass

- Ran `fixtures/extension_popup_chromium_probe.py` against the explicit Vite
  mock surface with a fresh disposable Chromium profile. The probe's complete
  captured log is `/tmp/dm-extension-popup-final.log`.
- The popup rendered `CURRENT SITE` for `example.com`, trusted CDP clicks
  changed `Exclude this site` to `Enable on this site` and back, and the
  persisted `download-manager.settings` excluded-sites list reflected both
  transitions. `Open Manager` then reached the manager without opening a new
  tab. The manager showed `All Downloads`, count `14`, and `14` rows.
- Exact output: `EXTENSION-POPUP-PROBE: PASS {"excluded_site":
  "example.com", "excluded_then_enabled": true, "manager_heading": "All
  Downloads\\n14", "manager_rows": 14}`.

- Reran `fixtures/manager_filters_context_chromium_probe.py` with a fresh
  disposable Chromium profile and captured the complete log at
  `/tmp/dm-manager-filters-context-final.log`.
- The real rendered manager reported the expected sets and heading counts:
  All `14`, Active `3`, Paused `1`, Completed `8`, Failed `2`, and Media `7`.
  The trusted row-specific menu for `backup-manifest.json` contained `Retry`,
  `Open containing folder`, `Copy source URL`, `Reattach download`, and
  `Remove from list`. Removing that row left `13` rows and preserved the
  selected `Big Buck Bunny (1080p).mkv` row and inspector.
- Exact output ended with `MANAGER-FILTERS-CONTEXT-PROBE: PASS`; the probe
  returned `0`. These are mock-adapter UI checks; no native application or
  external service was touched. The protected Rust sibling remains clean.

## 2026-09-05 — Multiple-player page selects the interacted audio player

- Added `fixtures/public_ableplayer_audio_selection_chromium_probe.py` for the
  current-media selection case on a page exposing both audio and video. It uses
  the official Able Player external5 demo, where a trusted `Show players` click
  exposes `audio#audio1` and `video#video1` side by side. The audio's declared
  sources are `smallf.ogg` first then `smallf.mp3`, so `currentSrc` is the Ogg.
- The probe trusted-clicked the audio wrapper's real `Play` control
  (`aria-label=Play`, wrapper `640x105` while the native `<audio>` box is `0x0`,
  so this also exercises the zero-box wrapper geometry path). The audio reached
  `readyState=4`, `paused=false`, duration `231.893333`, with the product
  Download button anchored inside the audio wrapper at
  `x=894.9453125, y=571.4765625`.
- The trusted button activation created exactly one native media job for
  `https://ableplayer.github.io/ableplayer/media/smallf.ogg` — the interacted
  audio player, not the larger `640x480` video. Resident `--commit` exited `0`;
  the job completed with `provisional=false`.
- Browser-context fetch of the same source recorded `4600399` bytes and
  SHA-256
  `09e3151c902c2fef6f98075a6ee23067ca8e1faca249932dc1ce530f379b2159`; the
  managed native output matched both values. Chromium Downloads was empty and
  the database contained exactly one job.
- Exact output ended with
  `ABLEPLAYER-AUDIO-SELECTION: PASS (page=https://ableplayer.github.io/ableplayer/demos/external5.html,
  source=https://ableplayer.github.io/ableplayer/media/smallf.ogg,
  output_bytes=4600399,
  output_sha256=09e3151c902c2fef6f98075a6ee23067ca8e1faca249932dc1ce530f379b2159,
  jobs=1, browser_downloads=[], trusted_audio_click=true)` and
  `ABLEPLAYER-AUDIO-SELECTION-CHROMIUM-PROBE: PASS`; the complete log is
  `/tmp/dm-ableplayer-audio-selection.log`.
- Recorded observation only, no product change: the media capture message
  names every non-manifest capture after the page title plus `.mp4`, so this
  Ogg provisional was named `… | Able Player Demos.mp4` until commit renamed
  it. That is a separate naming-polish item, not a selection or data defect.
- No product source changed; the protected `src-tauri/src/main.rs` sibling
  remains clean.

## 2026-09-05 — Custom-element players: open shadow-root media discovery

- Added `fixtures/public_muxvideo_chromium_probe.py` against the Media Chrome
  `<mux-video>` example at
  `https://media-chrome.mux.dev/examples/vanilla/media-elements/mux-video.html`.
  The page exposes zero light-DOM `video`/`audio`; the media is a `<mux-video>`
  custom element whose real `<video>` lives inside its open shadow root, and
  the HLS VOD master streams from `stream.mux.com`.
- The red fresh-profile baseline proved a real generic gap: after a trusted
  click on `media-play-button`, the shadow video was playing
  (`readyState=4`, `paused=false`, duration `653.875`, blob source,
  `640x360`) but `lightMedia=0` and `#dm-media-download-button` was absent.
  The extension only scanned light DOM, so web-component players were
  invisible.
- Fixes in `extension/src/content.ts`:
  - `collectMedia()` scans light DOM every tick and pierces open shadow roots
    at most once per second, caching the full result so throttled ticks cannot
    drop shadow media and flicker the button; closed shadow roots stay
    invisible.
  - `pick()` now calls `collectMedia()` once and reuses the list for the hover
    and best-area passes (the previous two calls let the throttled second call
    return only light DOM).
- Fix in `extension/src/media-candidates.ts`: the first fresh run exposed a
  second reachable selection defect. The newest-observed-manifest rule picked
  the later-fetched **subtitles playlist** (`subtitles.m3u8`) instead of the
  media rendition; the native job then failed in FFmpeg on webvtt-in-mp4.
  Selection now prefers the media manifest over subtitle/caption playlists
  (`isSubtitlePlaylist()` naming check, generic, not site-specific) and falls
  back to a subtitle playlist only when it is the only manifest evidence.
- Three focused unit tests added (41/41 Vitest total): subtitle-name
  recognition, media-manifest-over-subtitle preference, subtitle-only fallback.
- The corrected fresh-profile run: button present on the shadow video
  (`button=true`, `light_media=0`), trusted activation created exactly one
  native media job for the 720p `rendition.m3u8` (not subtitles). Resident
  `--commit` exited `0`; the job completed with `provisional=false`.
- Independent byte reference: the probe fetched the exact same rendition
  playlist (map + every segment) and ran the identical
  `ffmpeg -c copy` finalize. Native output and reference both were
  `172,487,989` bytes with SHA-256
  `9d20a196f4ceb8ecbf9691398a6f6ff0016fd8c2a77f09df0e1fd768f362f063`.
  Chromium Downloads was empty and the database contained exactly one job.
- Exact output ended with
  `MUXVIDEO-CHROMIUM: PASS (page=https://media-chrome.mux.dev/examples/vanilla/media-elements/mux-video.html,
  output_bytes=172487989,
  sha256=9d20a196f4ceb8ecbf9691398a6f6ff0016fd8c2a77f09df0e1fd768f362f063,
  jobs=1, browser_downloads=[], shadow_media=true, light_media=0)` and
  `MUXVIDEO-CHROMIUM-PROBE: PASS`; the complete log is
  `/tmp/dm-muxvideo-green.log`; the red baseline is `/tmp/dm-muxvideo-red.log`.
- Regression gates passed: `npm run build:all`, `npx tsc -b`, Vitest
  `7` files/`41` tests, Rust `58/58`, fixture compilation, and
  `git diff --check`. The light-DOM public WebM probe re-passed after the
  content-script change (`MDN-WEBM-CHROMIUM-PROBE: PASS`, 330,618 bytes,
  SHA-256 `074b046f0832c1c262a7a3e015b042092fa226b1550b83a7d14cca9025d34e1e`).
  The protected `src-tauri/src/main.rs` sibling remains clean.

## 2026-09-05 — Manager inline controls and bulk actions

- Added `fixtures/manager_inline_controls_chromium_probe.py` for the SPEC
  §10.2 inline row controls, §10.3 bulk actions, sorting, and the Add URL
  overlay. It drives the explicit `npm run dev` mock adapter in a fresh
  Chromium profile with trusted CDP input; the complete log is
  `/tmp/dm-manager-inline-controls.log`.
- Inline row controls: a trusted click on the active
  `ubuntu-24.04-desktop-amd64.iso` row's `Pause` action changed it to
  `Paused`/`Resume`; `Resume` returned it to `Downloading`/`Pause`. A trusted
  `Retry` on the failed `old-archive.tar.xz` changed `Failed` to `Connecting`.
- Bulk actions: `Pause All` paused the active rows and switched the toolbar to
  `Resume All`; `Resume All` restored them and switched the toolbar back to
  `Pause All`.
- Sorting: `Sort → Name` produced a DOM order equal to the browser's
  `localeCompare` order over all 14 rows (first `backup-manifest.json`);
  `Sort → File size` placed the largest job first
  (`ubuntu-24.04-desktop-amd64.iso`).
- Add URL: the toolbar `Add URL` opened the `Add Download` overlay (Cancel
  present); `Cancel` closed it.
- Exact output ended with `MANAGER-INLINE-CONTROLS: PASS`; the probe returned
  `0`. These are mock-adapter UI checks; no native application or external
  service was touched. The protected Rust sibling remains clean.

## 2026-09-05 — Real GitHub release asset download through the real page

- Added `fixtures/public_github_release_chromium_probe.py`. It navigates the
  real Chromium/native setup to the actual flask 3.1.3 release page
  `https://github.com/pallets/flask/releases/tag/3.1.3` and trusted-clicks the
  real "Source code (zip)" asset link
  (`https://github.com/pallets/flask/archive/refs/tags/3.1.3.zip`). The asset
  is a plain link (no `download` attribute) that redirects to codeload with
  `Content-Disposition: attachment; filename=flask-3.1.3.zip`.
- Chromium created one browser download (`id=1`, `state=complete`,
  `error=null`, `filename=…/flask-3.1.3.zip`, `857854` bytes, SHA-256
  `4c8bfa9d33ac64436875f3f7d213b4fd3bbd855e0a4c4e4afaf7acc51f6b64ab`).
- The observe-only native fallback created exactly one provisional job whose
  name was already the header-resolved `flask-3.1.3.zip` — the
  `onDeterminingFilename` change proves correct through a real public page.
  The source was the codeload final URL.
- Resident `--commit` exited `0`; the job completed with `provisional=false`.
  Native output matched the browser exactly: `857854` bytes and the same
  SHA-256 `4c8bfa9d33ac64436875f3f7d213b4fd3bbd855e0a4c4e4afaf7acc51f6b64ab`.
  The database contained exactly one job.
- Exact output ended with
  `GITHUB-RELEASE: PASS (page=https://github.com/pallets/flask/releases/tag/3.1.3,
  name=flask-3.1.3.zip, browser_bytes=857854,
  sha256=4c8bfa9d33ac64436875f3f7d213b4fd3bbd855e0a4c4e4afaf7acc51f6b64ab,
  native_bytes=857854,
  native_sha256=4c8bfa9d33ac64436875f3f7d213b4fd3bbd855e0a4c4e4afaf7acc51f6b64ab,
  jobs=1)` and `GITHUB-RELEASE-PROBE: PASS`; the complete log is
  `/tmp/dm-github-release.log`.
- No product source changed; the protected `src-tauri/src/main.rs` sibling
  remains clean.

## 2026-09-05 — Real Wikimedia Commons media page capture

- Added `fixtures/public_commons_media_chromium_probe.py` against the real
  Commons page `https://commons.wikimedia.org/wiki/File:Example.ogg`. The page
  uses MediaWiki's TimedMediaHandler player: a `disabled` placeholder `<video>`
  with a `poster` and `preload="none"`, plus an `a.mw-tmh-play` overlay.
- A trusted click on that overlay (`title="Play audio"`) loaded a real playing
  `<audio>` (source
  `https://upload.wikimedia.org/wikipedia/commons/c/c8/Example.ogg?utm_source=...&utm_campaign=index&utm_content=original`,
  `readyState=4`, `paused=false`, duration `6.104036`). The native `<audio>`
  box is `0x0`, yet the product Download button was present at
  `x=823.9453125, y=589.8984375` — the zero-box wrapper geometry path.
- The extension correctly ignored the `disabled` placeholder `<video>` and
  picked the real playing audio. Trusted button activation created exactly one
  native media job for the Ogg URL; the job completed with `provisional=false`.
- Because this is a whole-object progressive media capture (not a manifest),
  the native output is byte-identical to the raw source. The independent
  browser-context reference recorded `104793` bytes and SHA-256
  `f57b56d8aae4c847cf01224fb45293610d801cfdac43d932b5eeab1cd318182a`; the
  managed native output matched both. Chromium Downloads was empty and the
  database contained exactly one job.
- Exact output ended with
  `COMMONS-MEDIA: PASS (page=https://commons.wikimedia.org/wiki/File:Example.ogg,
  output_bytes=104793,
  sha256=f57b56d8aae4c847cf01224fb45293610d801cfdac43d932b5eeab1cd318182a,
  jobs=1, browser_downloads=[])` and `COMMONS-MEDIA-PROBE: PASS`; the complete
  log is `/tmp/dm-commons.log`.
- Recorded observation only, no product change: the media capture message names
  every non-manifest capture after the page title plus `.mp4`, so the Ogg
  provisional was named `File:Example.ogg - Wikimedia Commons.mp4` until commit
  renamed it. No product source changed; the protected Rust sibling remains
  clean.

## 2026-09-06 — Real same-origin download attribute page

- Added `fixtures/public_jcisaacs_download_attr_chromium_probe.py` against the
  real public page
  `https://jcisaacs.com/TEST/HTML5-Test/HTML5%20Download%20Attribute%20Demo.html`.
  The target is a real top-document anchor, not an injected test element or an
  iframe: `href="samp/htmldoc.html"` resolves to
  `https://jcisaacs.com/TEST/HTML5-Test/samp/htmldoc.html` and carries the
  filename attribute `download="sample-file.html"`.
- A clean fresh Chromium profile trusted-clicked that anchor and recorded the
  browser-owned `sample-file.html`: `681` bytes, SHA-256
  `529724dd54dd4966554ed18e61bad2efd48cfad35996351f7fc2150f848b8bb7`.
- A second fresh profile ran the resident binary and unpacked extension. The
  extension's document-capture path created exactly one ordinary native job
  with the attribute filename `sample-file.html`; resident `--commit` exited
  `0`, and the job completed with `provisional=false`.
- Native output was byte/hash-identical to the clean browser download: `681`
  bytes and SHA-256
  `529724dd54dd4966554ed18e61bad2efd48cfad35996351f7fc2150f848b8bb7`.
  The database contained exactly one job and the extension profile's
  Chromium Downloads directory was empty.
- Exact output ended with
  `JCISAACS-DOWNLOAD-ATTR: PASS (page=https://jcisaacs.com/TEST/HTML5-Test/HTML5%20Download%20Attribute%20Demo.html,
  source=https://jcisaacs.com/TEST/HTML5-Test/samp/htmldoc.html,
  filename=sample-file.html, browser_bytes=681,
  browser_sha256=529724dd54dd4966554ed18e61bad2efd48cfad35996351f7fc2150f848b8bb7,
  native_bytes=681,
  native_sha256=529724dd54dd4966554ed18e61bad2efd48cfad35996351f7fc2150f848b8bb7,
  jobs=1, browser_downloads=[])` and
  `JCISAACS-DOWNLOAD-ATTR-PROBE: PASS`; the complete log is
  `/tmp/dm-jcisaacs-download-attr.log`.
- No product source changed; the protected Rust sibling remains clean.

## 2026-09-06 — Real GitHub raw-button download (blob boundary)

- Added `fixtures/public_github_raw_button_chromium_probe.py` against the real
  GitHub blob page `https://github.com/pallets/flask/blob/main/README.md`. The
  target is the real JS control `[data-testid=download-raw-button]` ("Download
  raw file") in the file header; no injected element, no iframe.
- A clean fresh Chromium profile trusted-clicked that button and recorded the
  browser-owned `README.md`: `1639` bytes, SHA-256
  `1f2de14735b1ee9d3a342fa7c5d5e87b95727276c0a56c8a9d77221f37880602`.
- A second fresh profile ran the resident binary and unpacked extension, and
  trusted-clicked the identical button. `chrome.downloads.search` from the
  extension worker proved the download's `url` and `finalUrl` are both
  `blob:https://github.com/<uuid>` — GitHub's button fetches the raw file and
  initiates a client-side `blob:` download (`mime=text/markdown`,
  `state=complete`, `error=null`, `fileSize=1639`, filename `README.md`).
- The ordinary-download fallback is HTTP(S)-only by contract (`isHttp` in
  `shared.ts`), so the extension leaves the blob transaction browser-owned.
  The resident database stayed empty (`native_jobs=0`) while the complete
  browser download (identical `1639` bytes) persisted — the correct boundary
  for client-generated blob downloads, now proven on a real production page
  rather than a fixture.
- Exact output ended with
  `GITHUB-RAW-BUTTON: PASS (page=https://github.com/pallets/flask/blob/main/README.md,
  source=blob:https://github.com/…, filename=README.md, browser_bytes=1639,
  browser_sha256=1f2de14735b1ee9d3a342fa7c5d5e87b95727276c0a56c8a9d77221f37880602,
  native_jobs=0, browser_downloads=1)` and
  `GITHUB-RAW-BUTTON-PROBE: PASS`; the complete log is
  `/tmp/dm-github-raw-button.log`.
- No product source changed; the protected Rust sibling remains clean.

## 2026-09-06 — Real bare download attribute (URL-basename naming)

- Added `fixtures/public_jcisaacs_download_attr_bare_chromium_probe.py` against
  the same real public page
  `https://jcisaacs.com/TEST/HTML5-Test/HTML5%20Download%20Attribute%20Demo.html`.
  The target is the page's real **bare** download anchor: `download` present
  with no value (`getAttribute('download') === ''`), same-origin
  `href="samp/htmldoc.html"` resolving to
  `https://jcisaacs.com/TEST/HTML5-Test/samp/htmldoc.html`.
- A clean fresh Chromium profile trusted-clicked that anchor and recorded the
  browser-owned `htmldoc.html` (named from the URL basename): `681` bytes,
  SHA-256 `529724dd54dd4966554ed18e61bad2efd48cfad35996351f7fc2150f848b8bb7`.
- A second fresh profile ran the resident binary and unpacked extension. The
  extension's document-capture path derived the name from the URL basename
  (`cleanFilename('')` is undefined, so `basenameFromUrl` produced
  `htmldoc.html`) and created exactly one ordinary native job with that name;
  resident `--commit` exited `0`, and the job completed with
  `provisional=false`.
- Native output was byte/hash-identical to the clean browser download: `681`
  bytes and SHA-256
  `529724dd54dd4966554ed18e61bad2efd48cfad35996351f7fc2150f848b8bb7`.
  The database contained exactly one job and the extension profile's
  Chromium Downloads directory was empty.
- Exact output ended with
  `JCISAACS-BARE-DOWNLOAD-ATTR: PASS (page=https://jcisaacs.com/TEST/HTML5-Test/HTML5%20Download%20Attribute%20Demo.html,
  source=https://jcisaacs.com/TEST/HTML5-Test/samp/htmldoc.html,
  filename=htmldoc.html, browser_bytes=681,
  browser_sha256=529724dd54dd4966554ed18e61bad2efd48cfad35996351f7fc2150f848b8bb7,
  native_bytes=681,
  native_sha256=529724dd54dd4966554ed18e61bad2efd48cfad35996351f7fc2150f848b8bb7,
  jobs=1, browser_downloads=[])` and
  `JCISAACS-BARE-DOWNLOAD-ATTR-PROBE: PASS`; the complete log is
  `/tmp/dm-jcisaacs-bare.log`.
- No product source changed; the protected Rust sibling remains clean.

## 2026-09-06 — Real production podcast page (Transistor) media capture

- Added `fixtures/public_transistor_podcast_chromium_probe.py` against a real
  production host — Transistor's public episode share page
  `https://share.transistor.fm/s/e70577fd` ("Forward Thinking Founders"
  episode 197, published via Transistor, Inc.). This is a live production
  player page, not a library/demo page. The page ships the episode's audio in
  the top document: a native `<audio>` with
  `src=https://media.transistor.fm/e70577fd/6bc50f3d.mp3?src=player` and
  `preload="none"`.
- A trusted CDP click on the player's real control
  (`button.play-button[aria-label="Play/Pause"]`) started playback:
  `paused=false`, `readyState=1`, duration `1776.177756` (29:36). The native
  `<audio>` element box is `0x0`, and the generic Download button appeared at
  `x=1059.9453125, y=128.5859375` — the playing-zero-box wrapper geometry
  path applied outside a demo page.
- Trusted button activation created exactly one native media job
  (`media=true`) for
  `https://media.transistor.fm/e70577fd/6bc50f3d.mp3?src=player`. Resident
  `--commit` exited `0`; the job completed with `provisional=false`.
- For whole-object progressive media the native output is byte-identical to
  the raw source. The independent reference fetched the same element source
  (following its `302` to the CDN) and recorded `28,465,955` bytes with
  SHA-256 `4d587525133b8fa812b3cc5150cb12a15c2783421d06b41c5e58edc047325b1a`;
  the managed native output matched both. Chromium Downloads was empty and the
  database contained exactly one job.
- Exact output ended with
  `TRANSISTOR-PODCAST: PASS (page=https://share.transistor.fm/s/e70577fd,
  source=https://media.transistor.fm/e70577fd/6bc50f3d.mp3?src=player,
  output_bytes=28465955,
  sha256=4d587525133b8fa812b3cc5150cb12a15c2783421d06b41c5e58edc047325b1a,
  jobs=1, browser_downloads=[])` and `TRANSISTOR-PODCAST-PROBE: PASS`; the
  complete log is `/tmp/dm-transistor.log`.
- No product source changed; the protected Rust sibling remains clean.

## 2026-09-06 — Real production video page (Streamable) media capture

- Added `fixtures/public_streamable_video_chromium_probe.py` against a real
  production host — Streamable's public watch page
  `https://streamable.com/o6xqa3` (video hosted and distributed by
  Streamable). This is a live production video page, not a library/demo page.
  The media ships in the top document: a native `<video>` whose `src` is the
  server-issued signed CDN URL
  (`https://cdn-cf-east.streamable.com/video/mp4/o6xqa3.mp4?Expires=...&Key-Pair-Id=...&Signature=...`) —
  the SPEC §6.3 signed, opaque, query-heavy URL case on a real host.
- Headless verification first (fresh disposable profile): the video loads
  (`readyState=4`, duration `92.05`) and a trusted click on the player's real
  play control (`div.svp-button-play`, `aria-label="Play (k)"`) starts
  playback (`paused=false`, current time advancing). The earlier long-lived
  browser session's "An error occurred" banner and `networkState=3` did not
  reproduce in the fresh profile, so the page-player state was environmental,
  not a product path.
- In the probe run the trusted play click produced exactly one native media
  job (`media=true`) for that signed URL. Resident `--commit` exited `0`; the
  job completed with `provisional=false`.
- For whole-object progressive media the native output is byte-identical to
  the raw source. The independent reference fetched the exact signed element
  source and recorded `82,608,844` bytes with SHA-256
  `71397e09c2d6c7ab9b54cfb6903db132bab1666a601cf885042b63e391726877`; the
  managed native output matched both. Chromium Downloads was empty and the
  database contained exactly one job.
- Exact output ended with
  `STREAMABLE-VIDEO: PASS (page=https://streamable.com/o6xqa3,
  source=https://cdn-cf-east.streamable.com/video/mp4/o6xqa3.mp4?Expires=…,
  output_bytes=82608844,
  sha256=71397e09c2d6c7ab9b54cfb6903db132bab1666a601cf885042b63e391726877,
  jobs=1, browser_downloads=[])` and `STREAMABLE-VIDEO-PROBE: PASS`; the
  complete log is `/tmp/dm-streamable.log`.
- No product source changed; the protected Rust sibling remains clean.

## 2026-09-06 — Production segmented VOD (blob/MSE) environment-block record

The remaining distinct horizontal pattern — segmented VOD resolved through
the extension's media-traffic ring on a production page — was attempted
against two real production hosts. Both refuse at the transport or file level
in this environment; the evidence is recorded here with no product source
change and no site resolver exception:

- **PeerTube** (peertube.tv): watch pages mount their hls.js/video.js player,
  but the media fails before playback. The "Sliding from a waterfall in Tahiti
  #shorts" clip fails with `HLS.js error: mediaError - fatal: true -
  manifestIncompatibleCodecsError`, and the 40s "Lille Norge" clip's direct
  web-video (`https://media.tube.tchncs.de/videos/…-480.mp4`) reports
  `[object MediaError]` at `readyState=0` — PeerTube's WebM-class web-video
  files are not decodable by this headless Chromium build.
- **Al Jazeera** (aljazeera.com video newsfeed,
  `https://www.aljazeera.com/video/newsfeed/2026/9/5/09-05-sv-india-building-collapse-mp4`):
  the page and its "Play video" control work, but a trusted click mounts five
  `<video>` elements whose `src` attributes are all plain-HTTP
  (`http://ajmn-aje-vod.akamaized.net/media/v1/pmp4/static/clear/…/main.mp4`)
  inside an HTTPS document. Chromium's mixed-content policy kills each one:
  `networkState=3` (NO_SOURCE) and `[object MediaError]` on every element —
  no decodable media can ever exist, so the generic media button has nothing
  to attach to. Server-side probing of those `http://` URLs is additionally
  blocked by the VPS terminal security policy (plain-HTTP fetch scan), so the
  block is recorded from the in-browser evidence alone.
- Conclusion: production MSE/blob segmented capture is environment-blocked
  on the hosts reachable from this box without an HTTPS-served manifest
  player. This is an instrumentation/environment block, not a demonstrated
  product defect; the generic blob resolution path itself remains proven by
  the mux-video probe (`5536ffd`).
- No product source changed; the protected Rust sibling remains clean.

## 2026-09-06 — Reddit dynamic-site boundary (login wall, no product change)

- Attempted the SPEC §17.2 dynamic-site case against
  `https://old.reddit.com/r/funny/comments/9jivgv/she_cut_me_off_so_i_had_a_surprise_for_her/`
  with a fresh disposable Chromium profile (no resident, page-state only).
- The box is redirected before any media can exist:
  `href=https://old.reddit.com/login/?reason=lor2&dest=...`,
  `state=complete`. No `<video>`/`<audio>` is present to attach a button to,
  so there is no reachable capture to attempt.
- This is an external login/bot-wall block, not a product defect. No
  site-specific resolver was added to force a pass. Evidence:
  `/tmp/dm-reddit-diag.log` (`public page did not load`, `probe_rc=1`).
- The §17.2 dynamic-site intent stays covered by the production dynamic
  players already proven (Streamable watch page, Transistor share page,
  Mux custom-element player) while the two named hosts remain: Reddit =
  login-walled here; YouTube embeds already recorded as Error 153.
- No product source changed; the protected `src-tauri/src/main.rs` sibling
  remains clean.

## 2026-09-06 — Direct-MP4 navigation (built-in player document)

- Added `fixtures/public_direct_mp4_chromium_probe.py`. It navigates the
  address bar straight to
  `https://cdn.plyr.io/static/demo/View_From_A_Blue_Moon_Trailer-576p.mp4`
  (same already-proven CDN host as the Plyr page probe, but a distinct
  initiation pattern: top-level media document, browser built-in player, no
  player page or custom controls).
- Fresh-profile run: one `<video>` (`readyState=4`, `paused=false`, duration
  `183.125333`, `1024x576`), product Download button present, trusted click
  created exactly one native media job for that MP4. Resident `--commit`
  exited `0`; the job completed with `provisional=false`.
- Independent browser-context fetch of the same source recorded `49900386`
  bytes and SHA-256
  `2f4bd69d9bfc928a399493eaec2ba0e5a6a4f7e326d6e74e0d1d415f63be86f8`; the
  managed native output matched both. Chromium Downloads was empty and the
  database contained exactly one job.
- Exact output ended with
  `DIRECT-MP4: PASS (page=https://cdn.plyr.io/static/demo/View_From_A_Blue_Moon_Trailer-576p.mp4,
  source=https://cdn.plyr.io/static/demo/View_From_A_Blue_Moon_Trailer-576p.mp4,
  output_bytes=49900386,
  output_sha256=2f4bd69d9bfc928a399493eaec2ba0e5a6a4f7e326d6e74e0d1d415f63be86f8,
  jobs=1, browser_downloads=[])` and `DIRECT-MP4-PROBE: PASS`; the complete
  log is `/tmp/dm-direct-mp4.log` (`probe_rc=0`).
- Re-ran after the `16af69f` content-script rebuild: identical PASS,
  `output_bytes=49900386`,
  `output_sha256=2f4bd69d9bfc928a399493eaec2ba0e5a6a4f7e326d6e74e0d1d415f63be86f8`
  (`/tmp/dm-direct-mp4-postfix.log`, `probe_rc=0`). The media-button path
  is unaffected by the anchor-attribute fix.
- No product source changed; the protected `src-tauri/src/main.rs` sibling
  remains clean.

## 2026-09-06 — Cross-origin download attribute follows Chromium (server basename)

- Real defect in `extension/src/content.ts` (`interceptDownloadClick`): the
  author-supplied `download` attribute value was honored for every
  HTTP(S) target. Chromium itself drops that value for cross-origin
  targets and saves under the server basename instead (verified via web
  search: WHATWG/html#2562 discussion, Chromium issue 714373, MDN
  `download` attribute cross-origin behavior). The native job therefore
  diverged from the browser's name on every cross-origin attributed link.
- Minimal fix: honor the attribute only when
  `new URL(source).origin === new URL(window.location.href).origin`;
  otherwise fall back to `basenameFromUrl(source)`. Same-origin valued and
  bare attributes are untouched (the jcisaacs probes below re-prove them).
- New `fixtures/xorigin_download_attr_chromium_probe.py`: page on
  `127.0.0.1`, file on `localhost` (different host = different origin),
  anchor carries `download="author-supplied-name.bin"` for
  `/file/range.bin`. It asserts the native job is NOT the author name,
  commits, and checks byte/hash equality with an independent fetch.
- Red baseline (fix stashed, extension rebuilt): native job named
  `author-supplied-name.bin`, probe FAIL, `probe_rc=1`
  (`/tmp/dm-xorigin-attr-red.log`).
- Green run: `XORIGIN-ATTR: PASS (author=author-supplied-name.bin,
  native_name=range.bin, output_bytes=8388608,
  output_sha256=70204857af3fc5eaeec3102843f9a0b58a2b8c4d8336a127db9c2aedef5c29bb,
  jobs=1, browser_downloads=[])` and `XORIGIN-ATTR-PROBE: PASS`;
  complete log `/tmp/dm-xorigin-attr.log` (`probe_rc=0`).
- Neighboring regressions with the fix: jcisaacs valued
  (`JCISAACS-DOWNLOAD-ATTR-PROBE: PASS`) and bare
  (`JCISAACS-BARE-DOWNLOAD-ATTR-PROBE: PASS`) real-public probes.
- Post-fix sweep of the ordinary-capture family sharing the touched
  `content.ts` intercept path, all fresh-profile PASS after `16af69f`:
  `EXPLICIT-ANCHOR-CHROMIUM-PROBE: PASS`,
  `FORM-DOWNLOAD-PROBE: PASS`,
  `BROWSER-NATIVE-FAILURE-FALLBACK-CHROMIUM-PROBE: PASS`,
  `INTEGRATION-OFF-CHROMIUM-PROBE: PASS`. Logs
  `/tmp/dm-sweep-<fixture>.log`, all `rc=0`.
- Full gates: `npm run build:all`, `npx tsc -b`, Vitest `7` files/`41`
  tests, Rust `58/58`, fixture compilation, `git diff --check`
  (`/tmp/dm-xorigin-gates.log`). The protected `src-tauri/src/main.rs`
  sibling remains clean.

## 2026-09-06 — Cold browser capture starts the app without opening the manager

- Covers the SPEC §19.1 / M2 launch-from-extension path, previously
  unproven: with no resident process, a browser capture must start the app,
  create the provisional acquisition, show the standalone Add Download
  window, and not open the manager. The chain is Chrome spawning
  `BIN --native-host` on demand, which spawns detached `BIN --capture`,
  whose setup hides main and starts the provisional (`main.rs` setup +
  single-instance callback both verified by reading the source first).
- New `fixtures/cold_launch_capture_chromium_probe.py`: fresh HOME with the
  resident deliberately never started (environ-scanned `app_pids=[]`
  before the click), fixture-server page plus a same-origin valued
  `download` anchor, trusted CDP click. It polls for a new app process
  (environ contains the disposable HOME root), one provisional ordinary
  job, empty Chromium Downloads — then samples X11 visibility
  (`xdotool search --onlyvisible`) 3x1s.
- First-run evidence (`/tmp/dm-cold-launch.log`, `probe_rc=0`):
  `COLD-LAUNCH-JOB` provisional `cold-launch.bin` for the file (same-origin
  attribute honored, consistent with the cross-origin fix above);
  `COLD-LAUNCH-APP: pids=[3604097]`; `COLD-LAUNCH-WINDOWS:
  [{add:1,main:0} x3]`; `COLD-LAUNCH: PASS (...,
 add_window_visible=true, manager_visible=false, jobs=1,
 browser_downloads=[])` and `COLD-LAUNCH-PROBE: PASS`.
 - No product source changed; the protected `src-tauri/src/main.rs` sibling
 remains clean.

 ## 2026-09-06 — Add-window Download button wiring proof (SPEC §19.2)

 - Gap: every E2E probe commits through the resident `--commit` CLI, which
   reaches the same `commit_provisional` backend as the Add window's
   Download button — but no evidence showed the real button invokes it with
   the captured job's id, shown name, and destination. The WebKit inspector
   cannot dispatch input (`Input` `-32601`, already recorded), so the real
   button is not headlessly clickable; the proportionate proof is a
   component test over the exact submit path.
 - Change (`src/App.tsx`, one word): export `AddDownloadWindow` for tests.
   New `src/add-window-commit.test.tsx` (jsdom, already installed): renders
   the captured-mode window with a provisional job, clicks the real
   `Download` button, and asserts `onCommit` fires once with
   `(job.id, job.name, job.destination, maxConnections, null)`; a second
   test routes `Cancel` to the captured job id, never another row.
 - First run: `8` files/`43` tests pass (was `7`/`41`). Full gates after the
   change: `npx tsc -b`, Vitest `8`/`43`, Rust `58/58`, `git diff --check`
   (`/tmp/dm-addbtn-gates.log`). The protected `src-tauri/src/main.rs`
   sibling remains clean.

## 2026-09-06 — Browser-context Referer replay (SPEC §5.1/§3.6/§16)

- Gap, proven red first: the resident fetched every URL bare, so any
  Referer-gated (hotlink-protected) source failed natively with no path to
  success. New fixture route `/file/ref-gated.bin` (403 unless the Referer
  host matches) plus new `fixtures/referrer_replay_chromium_probe.py`, which
  also proves the gate itself (bare fetch 403s, Referer-bearing fetch serves
  `1048576` bytes, `077ce9d8e0b8cad7a2813c8e7d7ce7d8153f50df309c31fd7f1649c18e214d14).
- Red run on the unmodified binary (`/tmp/dm-referrer-red.log`, `probe_rc=1`):
  capture created the job, native fetch got `Source returned 403 Forbidden`,
  job `failed`. The section is earned, not assumed.
- Implementation, minimal and generic (no site rules):
  - Extension (`background.ts`, 3 payload sites): forwards the capture page
    as `referrer` — ordinary pre-browser and media captures use `pageUrl`,
    the downloads-API fallback uses the item referrer. No new permissions;
    cookies explicitly out of scope (would need the cookies permission plus
    a §16 review).
  - Resident (`main.rs`): `ProvisionalInput.referrer` (explicit key, else
    `pageUrl`, HTTP(S)-validated) → persisted `DownloadJob.referrer`
    (serde-defaulted, old DBs load). One `get_with_referrer` builder used at
    all 10 transfer fetch sites (range workers, segment fetcher, manifest
    chain, whole-object probe/fallbacks); no signature changes, lookup
    follows the existing snapshot pattern.
  - Scoping replicates strict-origin-when-cross-origin: full page URL when
    the request targets the same origin, page origin only otherwise, nothing
    for invalid values. Paths and query strings never leak cross-origin.
- Green run (`/tmp/dm-referrer-green.log`, `probe_rc=0`):
  `REFERRER-REPLAY: PASS (output_bytes=1048576,
  output_sha256=077ce9d8e0b8cad7a2813c8e7d7ce7d8153f50df309c31fd7f1649c18e214d14,
  jobs=1, browser_downloads=[])` and `REFERRER-REPLAY-PROBE: PASS`.
- Unit tests: capture-parse precedence/fallback/tolerance plus five
  scoping cases. Suite now Rust `60/60` (was `58`).
- Neighbors on the rebuilt tree: explicit-anchor ordinary capture and
  direct-MP4 media capture both PASS; full gates `build:all`, `tsc -b`,
  Vitest `8`/`44`, Rust `60`/`60`, fixture compile, `diff --check`
  (`/tmp/dm-referrer-gates.log`).

## 2026-09-06 — Form POST-body replay for POST-only endpoints (SPEC §5.1)

- Gap, proven red first: the native fallback replayed a bare GET, so any
  endpoint requiring the form POST failed. New probe
  `fixtures/postonly_replay_chromium_probe.py` serves a POST-only
  attachment (GET answers 405; only the exact `fixture=post-only` body
  succeeds) and asserts SUCCESS with byte/hash equality against the
  browser's own POST download.
- Red run (`/tmp/dm-postonly-red.log`, `probe_rc=1`): browser POST completed
  `65536` bytes (`3473fca7…`); native GET got
  `Source returned 405 Method Not Allowed`, job `failed`.
- Course correction caught by the neighbor probe: the first implementation
  replayed POST unconditionally, and the existing form GET probe failed with
  methods `[POST, POST]` — a re-submission for endpoints where GET already
  answers. Narrowed to GET-first with POST only after a 405 refusal when an
  observed body exists. Unconditional replay would re-submit every form.
- Implementation, generic (no site rules):
  - Extension (`background.ts`): observe-only `webRequest.onBeforeRequest`
    `requestBody` ring — urlencoded form bodies ≤64 KiB, 60 s TTL, one-shot
    consume by URL. Multipart/raw/oversized forms are left out and stay safe
    GET. No new permissions, no blocking. The fallback capture attaches
    `postBody`; pre-browser anchor and media paths are untouched (GET).
  - Resident (`main.rs`): `ProvisionalInput`/`DownloadJob.post_body`
    (serde-defaulted, capped, empty dropped) and a 405-gated POST replay in
    `acquire_once` reusing the Referer scoping. All other fetch sites stay
    GET exactly as before.
- Green run (`/tmp/dm-postonly-green.log`, `probe_rc=0`):
  `POSTONLY-REPLAY: PASS (output_bytes=65536,
  output_sha256=3473fca710f006025d284d4e32ad4fc453a1c522c36bfcde4cb19da38220d8a7,
  jobs=1, browser_downloads=1)` and `POSTONLY-REPLAY-PROBE: PASS`. The
  browser copy stays intact throughout (least-destructive rule holds).
- Neighbors on the rebuilt tree: form GET probe PASS with methods back to
  `[POST, GET]` (no re-submission); explicit-anchor PASS on rerun
  (`source_requests=2`) after one flaky `3` (raw per-connection counter,
  transparent Chromium retry against the toy server — rerun green, no
  product involvement); direct-MP4 PASS earlier on this tree.
- Unit test for body parse caps; suite Rust `61/61` (was `60`). Full gates
  `build:all`, `tsc -b`, Vitest `8`/`44`, Rust `61`/`61`, fixture compile,
  `diff --check` (`/tmp/dm-postonly-gates.log`).

## 2026-09-06 — Cross-origin Referer stripped to page origin, proven live

- The §16 scoping half of the Referer section was unit-only. New fixture
  route `/file/origin-gated.bin` serves only when the Referer is exactly
  the embedding page's origin — a full page URL must 403.
- New `fixtures/xorigin_referrer_scope_chromium_probe.py`: page on
  `127.0.0.1`, file on `localhost` (cross-origin), anchor carrying a
  cross-origin `download` author name. It proves the gate discriminates
  in-probe (`bare=403 full-url=403 origin=200`, `1048576` bytes,
  `077ce9d8…`) and then captures through the real extension path.
- First-run evidence (`/tmp/dm-xorigin-scope.log`, `probe_rc=0`): native job
  named `origin-gated.bin` (author name ignored — the earlier fix composes),
  `completed`, byte/hash-identical output, empty browser Downloads:
  `XORIGIN-SCOPE-PROBE: PASS`.
- No product source changed in this slice.

## 2026-09-06 — Add window shows live provisional metadata (SPEC §19.2)

- Adjacent proof in the same test file: the captured Add window must show
  real progress, size, and resumability as available. A third test renders
  the window with a mid-download provisional (`5 MiB` of `10 MiB`,
  `downloading`, `4` connections, resumable) and asserts the panel reads
  `5.0 MB / 10.0 MB`, `Downloading`, `4 active`, `Yes`.
- First version of the new assertion failed on my wrong expectation
  (`5 MB`, actual format is `5.0 MB` per `formatBytes`); corrected the test,
  not the product. Suite now `8` files/`44` tests green with `npx tsc -b`
  and `git diff --check` (`/tmp/dm-addmeta-gates.log`). No product source
  changed in this slice; the protected `src-tauri/src/main.rs` sibling
  remains clean.

## 2026-09-06 — Post-core engine matrix re-proven (Referer + POST changes)

- Two transfer-core changes landed since the last engine run (Referer replay
  at all ten fetch sites, 405-gated POST replay in `acquire_once`), so the
  deterministic matrix was re-run on the current binary with its documented
  scaffolding (Xvfb `:99`, `fixtures/server.py --port 8901`, isolated
  `DM_HOME`; scaffolding started for the run and killed after).
- `acquire_probe` all green: redirect PASS (`8388608` bytes identical via
  302), one-use PASS (`65536` bytes, sole consumer), retry-503 PASS (fails
  honestly after bounded retries) — `/tmp/dm-postcore-acquire-*.log`.
- `limiter_probe` PASS on the same binary (`0.84 MB/s` in band,
  `8388608` bytes identical) — `/tmp/dm-postcore-limiter.log`. The segmented
  path shares the edited `acquire_once` arm and is unaffected.
- No product source changed in this slice.

## 2026-09-06 — Referer replay composes with segmented media (SPEC §5.1/§16)

- New fixture: `/hls/gated.m3u8` (6-segment VOD) plus `gseg` segments, both
  403 without a same-host Referer, sharing a `_referer_host_ok` helper with
  the ref-gated route (also fixes a stray Pyright possibly-unbound note by
  dropping a try/except `urlparse` cannot trip).
- New `fixtures/gated_media_referrer_probe.py`: two phases through the real
  binary's Inspector `create_provisional`/`commit_provisional`, reference =
  ordered segment concatenation.
- Phase A (red): no page context → `failed`, `Source returned 403
  Forbidden`. Phase B first failed for a REAL product reason: the Inspector
  command path deserializes `ProvisionalInput` directly, so the extension's
  `pageUrl` key was silently dropped (`referrer: None` on the job row) while
  the native-messaging path maps it manually. Fix: one serde
  `alias = "pageUrl"` on `ProvisionalInput.referrer` (the `postBody` alias
  was redundant under `rename_all = "camelCase"` and left out) plus a unit
  test; first attempt put the alias on the `DownloadJob` DB struct by
  mistake, caught by re-reading before commit.
- Green evidence (`/tmp/dm-gated-media.log`, `probe_rc=0`): RED failed 403
  as required; READY `segments.completed=6/6`; output `18048` bytes,
  `sha256=3e826d2e…` identical to the reference:
  `GATED-MEDIA-PROBE: PASS`.
- Bring-up note: three consecutive WebKit SIGSEGVs (`poll=-11`) at app
  start mid-sequence; the same binary passes manual launch and the
  established `hls_fmp4` probe on the same box, and the gated probe goes
  green on rerun — transient Xvfb/WebKit startup flake, no product
  involvement.
- Gates: `build:all`, `tsc -b` clean, Vitest `8`/`44`, Rust `62`/`62`
  (was `61`), fixture compile, `diff --check`
  (`/tmp/dm-gated-gates.log`).

## 2026-09-06 — Referer replay composes with DASH (SPEC §5.1/§16)

- Last transfer family: new fixture `/dash/gated.mpd` (video-only
  SegmentList, init + 3 segments) serving the same real fMP4 bytes as the
  open routes under gated names — manifest, init, and segments all 403
  without a same-host Referer (verified live: bare `403` x3, gated `200`
  x3, out-of-range `404`, open manifest untouched).
- New `fixtures/gated_dash_referrer_probe.py`, mirroring the gated-HLS
  probe: red phase without page context, green phase with `pageUrl`,
  reference = ffmpeg `-c copy` mux of the fetched track (the resident's
  exact `mux_media_tracks` invocation).
- First-run evidence (`/tmp/dm-gated-dash.log`, `probe_rc=0`): RED failed
  403 as required; READY `segments.completed=4/4` (init counted, as
  predicted); output `32057` bytes, `sha256=6b6875bc…` identical to the
  reference: `GATED-DASH-PROBE: PASS`. No bring-up flakes this run.
- Request context now has E2E evidence on every transfer family:
  whole-object, segmented HLS, DASH, cross-origin scoping, POST replay.
- No product source changed in this slice.

## 2026-09-06 — Cookies: decided boundary, not a pending question

- Cookies stay OUT of v1. Rationale, now final: forwarding cookies needs
  the `cookies` host permission (new data-access surface on every site),
  a same-party scoping rule per request, and a §16 privacy review — three
  decisions that belong to the owner, not to an autonomous slice. The
  Referer/POST/body work covers the hotlink/form class without them; login-
  walled sources (Reddit login wall, proven) stay honestly unsupported.
- This closes the parked "cookie question": it is a recorded v1 boundary,
  not an open item. Revisit only on explicit owner direction.

## 2026-09-06 — Full-chain IDM loop: button -> gated media -> commit

- New fixture page `/page/gated-hls.html`: real hls.js 1.5.13 (CDN) plays
  the gated VOD so the page itself exercises the gate like any hotlinked
  embed. First attempt used the synthetic `.ts` playlist and a real demuxer
  correctly refused it (in-page diag: manifest 200, MSE attached, segments
  never appended) — added a gated fMP4 variant (`gated-fmp4.m3u8`,
  `ginit.mp4`, `gN.m4s`) serving the real media bytes; resident EXT-X-MAP
  handling already covered that shape.
- New `fixtures/fullchain_gated_media_chromium_probe.py`: fresh disposable
  profile, real unpacked extension, real native-messaging path, trusted CDP
  click on `#dm-media-download-button`, commit through the real
  provisional-to-completed flow.
- Evidence (`/tmp/dm-fullchain.log`, `probe_rc=0`): player `playing`,
  `readyState 4`; job row `media=true` with `referrer` = the page URL;
  output `32057` bytes, `sha256=6b6875bc…` identical to the ffmpeg
  reference; exactly 1 job; Chromium Downloads empty:
  `FULLCHAIN-PROBE: PASS`.
- No product source changed in this slice.

## 2026-09-06 — Public Plyr player: custom controls -> button -> MP4

- New `fixtures/public_plyr_button_chromium_probe.py`: real plyr.io page
  (Plyr custom controls, no native controls element), trusted play click,
  trusted click on the real `#dm-media-download-button`, commit through the
  real provisional-to-completed flow. Reference = the CDN bytes fetched
  live (cdn.plyr.io 403s Python's bare default UA — diagnosed, probe sends
  the same HeadlessChrome UA it drives).
- Evidence (`/tmp/dm-plyr-green.log`, `probe_rc=0`): button unoccluded
  after playback starts (audit: `hit: dm-media-download-button`); job row
  `media=true kind=video`; output `49900386` bytes, `sha256=2f4bd69d…` —
  identical to the live reference AND to the earlier direct-navigation hash
  for the same CDN bytes; exactly 1 job; Chromium Downloads empty:
  `PLYR-PROBE: PASS`.
- No product source changed in this slice.

## 2026-09-06 — Public Video.js player: blob MSE -> Mux VOD -> muxed MP4

- New `fixtures/public_videojs_button_chromium_probe.py`: real videojs.org
  hero player (Video.js + VHS, `<video>` fed via blob MSE), trusted play
  click, trusted click on the real `#dm-media-download-button`, commit
  through the real provisional-to-completed flow. The captured source is a
  signed Mux `rendition.m3u8` (fMP4, EXT-X-MAP + segments).
- First run failed honestly on the PROBE side: the reference fetched only
  the ~3.4 KB manifest while the resident correctly downloaded 869,306
  bytes of media. Fixed by resolving the manifest (MAP + segment URLs),
  fetching all fragments, and muxing the reference with the resident's
  exact ffmpeg `-c copy` invocation.
- Evidence (`/tmp/dm-videojs-green.log`, `probe_rc=0`): reference
  `fragments=10 bytes=21005303 sha256=da4d8ec2…`; job row `media=true
  kind=video`; output byte/hash-identical; exactly 1 job; Chromium
  Downloads empty: `VIDEOJS-PROBE: PASS`. (Mux serves renditions per run —
  fragment count differs between runs; equality is per-run manifest vs
  output.)
- No product source changed in this slice.

## 2026-09-06 — Vimeo/DRM boundary (honest v1 limit, no acceptance)

- Real Vimeo embed `https://player.vimeo.com/video/76979871`: player
  playable, `dm-media-download-button` present and unoccluded, trusted
  button click captured a real media job (`media=true`, source ending in
  `/drm/cenc/.../playlist.mpd`). Fetching that MPD live proves the boundary:
  `ContentProtection cenc` with Widevine UUID
  `edef8ba9-79d6-4ace-a3c8-27dcd51d21ed` and `cenc:pssh` present — the
  segments are CENC-encrypted and the resident has no CDM, so no playable
  output is producible. Chasing this would mean hoster-specific DRM
  handling, against the v1 contract.
- Recorded as an honest v1 boundary (like the Reddit login wall):
  DRM/CENC-gated sources stay unsupported. The failing experiment probe was
  removed rather than committed; no acceptance claimed.

## 2026-09-06 — Public Dailymotion player: AES-128 HLS -> decrypted muxed MP4

- New `fixtures/public_dailymotion_button_chromium_probe.py`: real
  dailymotion.com embed (`/embed/video/x7svh5p`, geo player), trusted play
  state check (click only when not already playing), trusted click on the
  real `#dm-media-download-button`, commit through the real
  provisional-to-completed flow. The captured source is a Dailymotion VOD
  media playlist (`..._mp4_h264_aac.m3u8#cell=cf2`): 212 MPEG-TS segments
  under a single `EXT-X-KEY:METHOD=AES-128` with explicit IV.
- Product change (smallest honest RFC 8216 4.4.2.4 path, no DRM widening):
  `src-tauri/src/media.rs` parses `EXT-X-KEY` (key transitions incl.
  `METHOD=NONE` clearing, `EXT-X-MEDIA-SEQUENCE` anchoring, explicit or
  sequence-derived IV; any other METHOD/KEYFORMAT fails honestly),
  `hls_key_iv` + `decrypt_aes128_segment` (CBC/PKCS#7, block-alignment and
  padding validated; new deps `aes 0.8`, `cbc 0.1`, `cipher 0.4` with
  `block-padding`); `src-tauri/src/main.rs` fetches each segment's 16-byte
  key through the existing acquisition context (`acquisition_request`, so
  Referer scoping flows) and decrypts before the segment write/mux path.
  Unsupported modes keep failing honestly; CENC/DRM stays out of scope.
- Two probe-side defects were fixed with evidence, not product guesses:
  (a) the reference concatenated raw (still-encrypted) segments so its
  ffmpeg failed with `Invalid data found when processing input` — fixed by
  giving the reference the same RFC 8216 key/IV/decrypt path (key fetch +
  openssl AES-128-CBC per segment); (b) the reference muxed with
  `-map 0:0` while the resident finalizes with `-map 0 -c copy`, yielding
  5,155,297 vs 35,709,183 bytes on identical decrypted input — fixed by
  using the resident's exact map. The resident output hash was stable
  across both runs (`199658ea...`), proving the resident was right and the
  reference was narrow.
- Evidence (`/tmp/dm-dailymotion.log`, `PROBE_EXIT=0`):
  `DAILYMOTION-REFERENCE: fragments=212 bytes=35709183
  sha256=199658ea9995cf15a8cd0cfe2dfea9eec9518c975c347be45140d9b2ce225ad2`;
  `DAILYMOTION: PASS (output_bytes=35709183,
  output_sha256=199658ea9995cf15a8cd0cfe2dfea9eec9518c975c347be45140d9b2ce225ad2,
  jobs=1, browser_downloads=[])`; `DAILYMOTION-PROBE: PASS`.
- Focused unit coverage in `media.rs`: key transitions + sequence IV,
  explicit IV + SAMPLE-AES rejection, openssl-anchored CBC/PKCS#7 decrypt
  roundtrip + misalignment/tamper rejection. Rust 65/65, Vitest 8/44,
  `tsc` clean.

## 2026-09-06 — Public Odysee host: watch page button -> progressive MP4

- New `fixtures/public_odysee_button_chromium_probe.py`: real production
  video host (Odysee, LBRY-based — a new family for this loop, not a
  library/demo page). The probe discovers a real watch page at runtime from
  Discover (state-media channels excluded from selection), starts playback
  with a trusted play click, clicks the real `#dm-media-download-button`
  (audit proved it hit and unoccluded), and commits through the real
  provisional-to-completed flow. The captured source is a plain progressive
  MP4 on `player.odycdn.com/v6/streams/.../*.mp4`.
- Environment notes, not product defects: `demo.jwplayer.com` fails TLS
  from this box (curl exit 35), so JW Player is unreachable here and was
  not pursued; Odysee Discover's first card was a channel page (no video),
  so the probe filters for watch URLs (`/@channel/video`).
- Evidence (`/tmp/dm-odysee.log`, `PROBE_EXIT=0`): page
  `https://odysee.com/@ControNews.org:4/algoritmo-all-go-rhythm:d`; job
  `media=true kind=video`; `ODYSEE-REFERENCE: bytes=30994343
  sha256=76dc5d5dfac071400ecfe87f4ab665348f714ee3f1b70a83d518cf4739a0c7ba`;
  `ODYSEE: PASS (output_bytes=30994343,
  output_sha256=76dc5d5dfac071400ecfe87f4ab665348f714ee3f1b70a83d518cf4739a0c7ba,
  jobs=1, browser_downloads=[])`; `ODYSEE-PROBE: PASS` — first-try green.
- No product source changed in this slice (progressive-MP4 path already
  green); Rust/frontend suites untouched.

## 2026-09-06 — Public Kaltura player: blob MSE -> HLS VOD -> muxed MP4

- New `fixtures/public_kaltura_button_chromium_probe.py`: Kaltura VPaaS
  player on its live docs page (`https://developer.kaltura.com/player/` — a
  new player family for this loop). The demo `<video>` is fed via blob MSE;
  the probe autoplays past the play click when already playing, clicks the
  real `#dm-media-download-button` (audit proved it hit and unoccluded),
  and commits through the real provisional-to-completed flow. The captured
  source is a signed Kaltura HLS VOD manifest
  (`cfvod.kaltura.com/.../entryId/1_23yaxsca/.../index.m3u8`, 16 fMP4
  fragments); the probe reference adapts (manifest resolve + MAP +
  fragments muxed with the resident's exact `-map 0 -c copy`, else direct
  bytes; DASH raises an honest no-reference error).
- Evidence (`/tmp/dm-kaltura.log`, `PROBE_EXIT=0`): job `media=true
  kind=video`; `KALTURA-REFERENCE: manifest fragments=16 bytes=36605194
  sha256=d075ecbc14d2cfc011443e6a64b03a1446a0ec7bb5751d318afd733f1c625f75`;
  `KALTURA: PASS (output_bytes=36605194,
  output_sha256=d075ecbc14d2cfc011443e6a64b03a1446a0ec7bb5751d318afd733f1c625f75,
  jobs=1, browser_downloads=[])`; `KALTURA-PROBE: PASS` — first-try green.
- No product source changed in this slice (clear-HLS path already green);
  Rust/frontend suites untouched.

## 2026-09-06 — Takeover verification: Dailymotion AES-128 path remains green

- Re-ran `fixtures/public_dailymotion_button_chromium_probe.py` from the
  current `c6c4ffa` tree after taking over from Muse, using a fresh disposable
  HOME/profile, real Chromium, the unpacked extension, native messaging, and
  the resident binary.
- The job captured a signed Dailymotion VOD playlist (source URL intentionally
  not retained), resolved `212` AES-128 MPEG-TS fragments, and completed one
  media job without a Chromium download. The independent reference decrypted
  each fragment with its RFC 8216 key/IV and muxed with the resident's exact
  `ffmpeg -map 0 -c copy` path.
- Evidence (`/tmp/dm-takeover-dailymotion.log`, `PROBE_EXIT=0`): resident and
  reference both `35,709,183` bytes, SHA-256
  `199658ea9995cf15a8cd0cfe2dfea9eec9518c975c347be45140d9b2ce225ad2`; one
  job and `browser_downloads=[]`. `DAILYMOTION-PROBE: PASS`.
- No product source changed in this verification slice.

## 2026-09-06 — Referer-gated AES-128 HLS composition proof

- Added `fixtures/gated_encrypted_hls_referrer_probe.py`, a disposable local
  fMP4 VOD fixture. Its playlist, AES-128 key, init fragment, and three
  encrypted media fragments all return `403` without the captured page
  Referer. The encrypted transport uses the existing generic fMP4 bytes and
  sequence-derived RFC 8216 IVs; it is not a site resolver.
- Red phase against the resident binary: no page context produced
  `state=failed`, `Source returned 403 Forbidden`.
- Green phase passed `pageUrl` through the real Inspector command path. The
  resident replayed the Referer for the playlist, key, init, and fragment
  requests, decrypted the segments, and finalized the media.
- Evidence (`/tmp/dm-gated-encrypted-hls-rerun.log`, `PROBE_EXIT=0`):
  `segments.completed=4`; resident output and independent plaintext reference
  both `32,057` bytes, SHA-256
  `6b6875bc6d4c6233f362624c1d5e919993e49f0e7530fc007cfb2d6d17167861`;
  `GATED-ENCRYPTED-HLS-PROBE: PASS`. The key was fetched through the same
  request-context path as the media fragments.
- No product source changed in this slice.

## 2026-09-06 — Full browser chain for Referer-gated AES-128 HLS

- Added `fixtures/fullchain_encrypted_hls_chromium_probe.py`. A fresh
  disposable Chromium profile loads the local hls.js page, plays the gated
  encrypted VOD, and clicks the actual extension media button with a trusted
  CDP click. The real extension/native-messaging path creates the job; this is
  not an Inspector-only shortcut.
- Evidence (`/tmp/dm-fullchain-encrypted-hls.log`, `PROBE_EXIT=0`): hls.js
  player `playing=true`, `readyState=4`, extension/native diagnostic clean;
  job `media=true` with the exact page Referer; commit forwarded with exit 0;
  resident output and independent reference both `32,057` bytes, SHA-256
  `6b6875bc6d4c6233f362624c1d5e919993e49f0e7530fc007cfb2d6d17167861`;
  exactly one job and `browser_downloads=[]`. `FULLCHAIN-ENCRYPTED-HLS-PROBE:
  PASS`.
- No product source changed in this slice.

## 2026-09-06 — Replay the capture-page User-Agent through the media chain

- Added `fixtures/fullchain_user_agent_chromium_probe.py` and extended the
  disposable encrypted-HLS fixture with an optional browser-UA gate. The gate
  requires both the existing same-host Referer and a `Mozilla/5.0` User-Agent
  on the playlist, key, init fragment, and all media fragments.
- Red phase against the unchanged resident (`/tmp/dm-user-agent-red.log`,
  `PROBE_EXIT=0`): real Chromium reported
  `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)
  HeadlessChrome/149.0.0.0 Safari/537.36`, but the captured job had no
  `userAgent`; the resident's fixed User-Agent was rejected with 403.
- The extension now forwards the exact capture-page `navigator.userAgent` for
  ordinary and media captures. The background validates it as a bounded,
  newline-free value. The native job persists it with a serde default for old
  databases and applies it to GET, POST fallback, manifest, key, init,
  fragment, and range requests. Cookies and arbitrary headers remain excluded.
- Evidence (`/tmp/dm-user-agent-green.log`, `PROBE_EXIT=0`): the job carried
  the exact Chromium UA, the real extension/native/Rust chain completed the
  gated encrypted VOD, and the managed output matched the independent
  reference exactly: `32,057` bytes, SHA-256
  `6b6875bc6d4c6233f362624c1d5e919993e49f0e7530fc007cfb2d6d17167861`;
  exactly one job and `browser_downloads=[]`. `UA-PROBE: GREEN PASS`.
- Neighbor proofs remained green: the ordinary full encrypted-HLS browser
  chain passed in `/tmp/dm-user-agent-neighbor-encrypted.log`, and the
  Referer-gated DASH path passed with four fragments and the same exact
  `32,057`-byte hash in `/tmp/dm-user-agent-neighbor-dash.log`.
- Regression gates (`/tmp/dm-user-agent-gates.log`) passed: Vitest 8 files /
  44 tests, Rust 65/65, TypeScript, and diff check. Disposable fixture
  bytecode and processes were removed after the runs.

## 2026-09-06 — Able Player audio-player selection

- Ran `fixtures/public_ableplayer_audio_selection_chromium_probe.py` against
  the official `external5.html` demo. A trusted Chromium click exposed the
  hidden players, then a second trusted click started the audio player while
  the video remained a separate candidate.
- Chromium reported the selected source as
  `https://ableplayer.github.io/ableplayer/media/smallf.ogg`, with
  `readyState=4`, `paused=false`, and the extension button anchored to the
  audio wrapper. The native job selected that exact `.ogg` source.
- Evidence (`/tmp/dm-ableplayer-audio-selection.log`, `PROBE_EXIT=0`): resident
  output and an independent browser `fetch(job.source)` reference both
  measured `4,600,399` bytes and SHA-256
  `09e3151c902c2fef6f98075a6ee23067ca8e1faca249932dc1ce530f379b2159`;
  one completed job, `trusted_audio_click=true`, and
  `browser_downloads=[]`. `ABLEPLAYER-AUDIO-SELECTION-CHROMIUM-PROBE: PASS`.
- No product source changed in this verification slice.

## 2026-09-06 — Resident close-to-tray and exit behavior

- `fixtures/close_to_tray_probe.py` passed against the current resident with a
  live 64 MiB transfer. The manager's real Close action left the X11 window
  `IsUnMapped`, kept exactly one resident process alive, made exactly one
  source request, and allowed the provisional transfer to reach `finalizing`
  at `67,108,864` bytes. Evidence: `/tmp/dm-close-to-tray-current.log`.
- `fixtures/exit_behavior_probe.py` passed with `closeBehavior=exit`. It set
  the real Settings value, sent a real `WM_DELETE_WINDOW`, and observed a
  clean resident exit code `1` without SIGSEGV. Evidence:
  `/tmp/dm-exit-behavior-current.log`.
- `fixtures/single_instance_probe.py` passed with a second resident invocation:
  the forwarder exited `0`, the original resident stayed alive as the only
  process, and one 32,768-byte job finalized after one source request.
  Evidence: `/tmp/dm-single-instance-current.log`.
- No product source changed in this verification slice.

## 2026-09-06 — Three rapid captures and independent Add Download windows

- The first attempt hit the known disposable WebKit Inspector startup race
  (`[Errno 111] Connection refused`) before the app became observable; it made
  no product assertion and left no process. The retained first log is
  `/tmp/dm-multiple-add-windows-current.log`.
- A fresh retry passed
  (`/tmp/dm-multiple-add-windows-retry.log`, `PROBE_EXIT=0`): three rapid
  native capture forwards created three independent jobs and three visible Add
  Download windows, while exactly one resident process remained alive. Each of
  `one.bin`, `two.bin`, and `three.bin` was requested exactly once.
  `MULTIPLE-ADD-WINDOWS-PROBE: PASS`.
- No product source changed in this verification slice.

## 2026-09-06 — Concurrent capture isolation

- `fixtures/simultaneous_capture_probe.py` passed with two concurrent native
  captures through one resident. It produced two distinct provisional job IDs,
  preserved both query-specific source URLs, and completed both jobs while the
  fixture recorded four range requests total. Evidence:
  `/tmp/dm-simultaneous-capture-current.log`.
- No product source changed in this verification slice.

## 2026-09-06 — Full Chromium DASH/MSE capture and two-track mux

- `fixtures/public_dash_chromium_probe.py` passed against the live DASH-IF
  reference player. DASH.js produced a playable 30-second `blob:` MediaSource,
  the extension injected its media button, and native capture created one
  managed job. The browser-selected video and audio references each matched
  their logged segment size/SHA-256 records.
- FFmpeg produced a playable two-track MP4 of `2,619,740` bytes with SHA-256
  `c66a095517273e2cbeb01ba0848a43d5232a36a9969a986484e3f6bdec8fec23` and
  duration `32.085333` seconds. The probe found one completed job and
  `browser_downloads=[]`; real context-menu and Ctrl-click ownership remained
  unprevented. Evidence: `/tmp/dm-public-dash-current.log`.
- No product source changed in this verification slice.

## 2026-09-06 — MDN interactive audio browser-chain proof

- `fixtures/public_mdn_interactive_audio_chromium_probe.py` passed against the
  real MDN page `https://interactive-examples.mdn.mozilla.net/pages/tabbed/audio.html`
  in a fresh Chromium profile. The injected audio button selected the current
  source `https://interactive-examples.mdn.mozilla.net/media/cc0-audio/t-rex-roar.mp3`.
- The real Chromium → extension → native → Add Download → commit path produced
  one completed managed job. The resident output and browser-context reference
  both measured `39,868` bytes and SHA-256
  `41191d0727073bf848bcc8f0bd851d71a0b0058e901abb1c1b236ad327bda52e`.
- Chromium Downloads remained empty. Evidence:
  `/tmp/dm-public-mdn-interactive-audio.log`.
- No product source changed in this verification slice.

## 2026-09-06 — Public hls.js runtime stream-selector capture

- Added `fixtures/public_hls_selection_chromium_probe.py` for a distinct
  initiation path on the official hls.js demo
  `https://hlsjs.video-dev.org/demo/`. Unlike the earlier hls.js proof, the
  fresh Chromium profile opened the page without a `src` query parameter and
  selected the page's live `bigBuckBunny480p` stream option through
  `#streamSelect`.
- The page then produced a real playing blob/MSE video
  (`readyState=4`, duration `634.6`) and the extension's player-bound Download
  button. The native job source was the exact selected finite manifest
  `https://test-streams.mux.dev/x36xhzz/url_6/193039199_mp4_h264_aac_hq_7.m3u8`,
  not the blob URL.
- Resident `--commit` exited `0`; the sole managed job ended
  `state=completed, provisional=false`. The independent browser-context
  reference fetched and concatenated all `64` manifest segments: `71,878,228`
  bytes, SHA-256
  `6e830be99296a8452aa3294f25ca64193bee2b58bf4d559a7cc5ffbb1cda0a42`.
  Native output matched the reference exactly. Chromium Downloads was empty.
- Exact retained output is `/tmp/dm-public-hls-selection-green.log`:
  `PUBLIC-HLS-SELECTION-CHROMIUM: PASS (page=https://hlsjs.video-dev.org/demo/,
  selected_source=https://test-streams.mux.dev/x36xhzz/url_6/193039199_mp4_h264_aac_hq_7.m3u8,
  segments=64, output_bytes=71878228,
  output_sha256=6e830be99296a8452aa3294f25ca64193bee2b58bf4d559a7cc5ffbb1cda0a42,
  jobs=1, browser_downloads=[])` and
  `PUBLIC-HLS-SELECTION-CHROMIUM-PROBE: PASS`.
- The first two attempts exposed only probe assumptions: the page reports
  `selected=null` while options are loading, and its option value is the
  registry key `bigBuckBunny480p` rather than the manifest URL. Both were
  corrected in the fixture; no product code changed.

## 2026-09-06 — Public player continuation: Media Chrome pass and external boundaries

- The official hls.js demo's `MP3 VOD demo` and `MPEG Audio Only demo` were
  checked as adjacent selector paths. Chromium loaded the MP3 manifest
  `https://playertest.longtailvideo.com/adaptive/vod-with-mp3/manifest.m3u8`
  and a TS segment, then reported
  `MediaSource.addSourceBuffer: Can't play type` / `bufferAddCodecError`;
  the player stayed `readyState=0`. The MPEG option loaded
  `https://pl.streamingvideoprovider.com/mp3-playlist/playlist.m3u8` but also
  stayed `readyState=0, paused=true` with no finite playback. These are public
  player codec/environment boundaries; no product change was made.
- The W3Schools Try-it result was reachable and its exact `#iframeResult`
  contained playing `https://www.w3schools.com/html/horse.ogg`
  (`readyState=4`, duration `1.515102`), but the fresh-profile extension
  handshake returned `extensionId=null, runtimeType=undefined` and the result
  iframe had no `#dm-media-download-button`, even after the page's Run control
  was activated. The retained diagnostics are
  `/tmp/dm-public-w3schools-audio-run-retry.log` and
  `/tmp/dm-public-w3schools-audio-standard-cdp.log`. This remains an
  extension/bootstrap or about:blank-frame boundary, not a claimed product
  defect; the disposable probe was not committed.
- Added `fixtures/public_media_chrome_chromium_probe.py` for the direct
  official Media Chrome homepage `https://www.media-chrome.org/`. The real
  page uses Media Chrome shadow controls around a light-DOM `<video>`. A fresh
  Chromium profile reached `readyState=4`, duration `32.419667`, and clicked
  the injected player-bound Download button through trusted CDP input.
- The native job captured the exact current progressive source
  `https://stream.mux.com/ddBx5002F02xe7ftFvTFkYBxEdQ2inQ2o029CMqu9A4IcY/high.mp4`.
  Resident `--commit` exited `0`; one job completed with `provisional=false`.
  The independent browser-context reference and native output both measured
  `9,271,282` bytes and SHA-256
  `d777a2a3d2fc6eb5e5b12c031767a033da0d56380d66e7edfb769ca92d057b44`.
  Chromium Downloads was empty.
- Exact retained output is `/tmp/dm-public-media-chrome.log`:
  `PUBLIC-MEDIA-CHROME-CHROMIUM: PASS (page=https://www.media-chrome.org/,
  source=https://stream.mux.com/ddBx5002F02xe7ftFvTFkYBxEdQ2inQ2o029CMqu9A4IcY/high.mp4,
  output_bytes=9271282,
  output_sha256=d777a2a3d2fc6eb5e5b12c031767a033da0d56380d66e7edfb769ca92d057b44,
  jobs=1, browser_downloads=[])` and
  `PUBLIC-MEDIA-CHROME-CHROMIUM-PROBE: PASS`.
- No product source change was justified by these results.

## 2026-09-06 — Internet Archive nested-shadow track-player capture

- Added `fixtures/public_internet_archive_chromium_probe.py` for a distinct
  public-site/player initiation pattern: the Internet Archive public-domain
  item page `https://archive.org/details/HumpbackWhalesSongsSoundsVocalizations`.
  Its real player is nested through `ia-music-theater` and `play-av` open
  shadow roots into a JW-controlled `<video>`; this is not the already-proven
  Media Chrome light-DOM player or the hls.js selector path.
- A fresh Chromium profile trusted-clicked the real player track button for
  track 2, `Humpback_whale_song_3`, then used the real player control to start
  playback. Chromium reported the selected source
  `https://archive.org/download/HumpbackWhalesSongsSoundsVocalizations/Humpback_whale_song_3_64kb.mp3`,
  `readyState=3`, `paused=false`, and duration `31.215964`. The generic
  extension button appeared over the nested-shadow media and a trusted click
  created exactly one media job for that selected source.
- Resident `--commit` exited `0`; the sole managed job completed with
  `provisional=false`. The independent browser-context fetch of the exact
  captured source measured `250,693` bytes and SHA-256
  `c9e971c425fa831c438253dda8ec9232f697b57b647dbccea35939a040a7e70a`.
  Native output matched the reference exactly, and Chromium Downloads stayed
  empty.
- Exact retained green output is `/tmp/dm-public-internet-archive-retry.log`:
  `INTERNET-ARCHIVE: PASS (page=https://archive.org/details/HumpbackWhalesSongsSoundsVocalizations,
  source=https://archive.org/download/HumpbackWhalesSongsSoundsVocalizations/Humpback_whale_song_3_64kb.mp3,
  output_bytes=250693,
  output_sha256=c9e971c425fa831c438253dda8ec9232f697b57b647dbccea35939a040a7e70a,
  jobs=1, browser_downloads=[])` and
  `INTERNET-ARCHIVE-CHROMIUM-PROBE: PASS`.
- The first run reached the same real chain and created the selected job, but
  the probe wrapped an async browser reference in `JSON.stringify`, serializing
  the unresolved Promise as `{}`. That probe-only error is retained at
  `/tmp/dm-public-internet-archive.log`; the helper was corrected and the
  fresh-profile retry above passed. No product source change was justified.

## 2026-09-06 — TED media-controller HLS VOD capture

- Added `fixtures/public_ted_chromium_probe.py` for a distinct public-player
  family: TED's article page
  `https://www.ted.com/talks/chris_anderson_ted_s_secret_to_great_public_speaking`.
  The page uses a `media-controller` custom element, a light-DOM `video#video`,
  and a finite HLS VOD source behind a blob MediaSource. This is not the
  already-covered Media Chrome direct-MP4, hls.js selector, Kaltura, or
  Internet Archive initiation path.
- A fresh Chromium profile reached the real TED player after a trusted CDP
  play click. Chromium reported `readyState=4`, `paused=false`, duration
  `466.925` seconds, and a blob-backed `video#video`. The real injected
  Download button was hit by trusted input; the probe recorded
  `isTrusted=true`, `pointer-events=auto`, and `elementFromPoint` resolving to
  `#dm-media-download-button`.
- The extension/native path created exactly one provisional media job. The
  captured finite HLS source was TED's `project_masters/4567/index-f14-v1.m3u8`
  variant; query parameters are intentionally redacted from durable evidence.
- The browser-context CDP Network readback observed the exact HLS response:
  HTTP `206`, MIME `application/vnd.apple.mpegurl`, `6,309` bytes, SHA-256
  `565c4eece889cecd83f507d17bee1ab3641c95577a6cb81339d2ee9d9a00f7b1`.
  The independent HLS reference resolved `79` finite fragments and used the
  resident's `ffmpeg -map 0 -c copy` mux path.
- Resident `--commit` exited `0`. The sole managed job completed with
  `provisional=false`; resident output and the independent reference matched
  at `201,847,488` bytes, SHA-256
  `0eaed4b40c70b7dfebf6994186af8d5bbfb7e6088e12f09acf71cfadbb15f8f3`.
  Chromium Downloads stayed empty.
- Exact retained green output is `/tmp/dm-public-ted-retry6.log`:
  `TED: PASS (output_bytes=201847488,
  output_sha256=0eaed4b40c70b7dfebf6994186af8d5bbfb7e6088e12f09acf71cfadbb15f8f3,
  browser_source_bytes=6309,
  browser_source_sha256=565c4eece889cecd83f507d17bee1ab3641c95577a6cb81339d2ee9d9a00f7b1,
  jobs=1, browser_downloads=[])` and
  `PUBLIC-TED-CHROMIUM-PROBE: PASS`.
- The first TED attempts exposed only probe seams: `cdp_drive` is a CLI
  helper with import-time argument parsing; the consent check initially counted
  a hidden retained Osano node; the CDN rejected script CORS fetches and did
  not render an HLS document view; TED's player mount and the injected-button
  capture were timing-sensitive. The final probe fixes use no product change:
  bounded player-mount waiting, visible consent geometry, and CDP
  Network-response-body readback. No product defect was established.

## 2026-09-06 — PeerTube ranged fMP4 HLS capture

- Added `fixtures/public_peertube_chromium_probe.py` for a distinct public
  watch-page/player family: the PeerTube page
  `https://peertube.tv/w/sQDpFLkMpWcm33kjTKHPYy`. Its real player exposed a
  blob-backed `<video>` fed by a finite fMP4 HLS media playlist. This path is
  distinct from the earlier TED `media-controller` path and the other closed
  public-player probes.
- A fresh Chromium profile reached the real player, used trusted pause/play
  gestures, and clicked the injected Download Manager button. Chromium
  reported `readyState=4`, `paused=false`, duration `1325.833331` seconds;
  the button event recorded `isTrusted=true`. The extension/native path made
  exactly one provisional media job for the captured PeerTube HLS source.
- The browser-context CDP Network readback observed the exact finite manifest:
  HTTP `206`, MIME `application/vnd.apple.mpegurl`, `36,262` bytes, SHA-256
  `ec219ce1e1c739106e7e83f54d279625ab9707c6310fefc4c9791ccb23517724`.
- PeerTube stores the initialization map and all media fragments as byte
  ranges in one shared `.mp4` resource. The probe's independent reference
  now preserves those ranges, issues bounded `Range` requests, validates each
  `206` response length, concatenates the `332` ordered pieces, and remuxes
  with the same `ffmpeg -map 0 -c copy` path as the resident. This corrected a
  probe-only reference mismatch; no product source change was justified.
- Resident `--commit` exited `0`; the sole managed job completed with
  `provisional=false`. The resident output and independent reference matched
  exactly at `339,829,878` bytes, SHA-256
  `6887e86862d2e33b7a5d62c7f00cafb64ee70eb92cf9f021c4d60482f4a13fee`.
  Chromium Downloads stayed empty.
- Exact retained green output is `/tmp/dm-public-peertube-retry6.log`:
  `PEERTUBE: PASS (output_bytes=339829878,
  output_sha256=6887e86862d2e33b7a5d62c7f00cafb64ee70eb92cf9f021c4d60482f4a13fee,
  browser_source_bytes=36262,
  browser_source_sha256=ec219ce1e1c739106e7e83f54d279625ab9707c6310fefc4c9791ccb23517724,
  jobs=1, browser_downloads=[])` and
  `PUBLIC-PEERTUBE-CHROMIUM-PROBE: PASS`.
- One intermediate fresh run completed all `332/332` resident fragments but
  its final remux hit `/tmp` exhaustion (`No space left on device`, FFmpeg exit
  `228`). The disposable prior probe roots were moved off the tmpfs; the next
  fresh-profile run above passed with the same external source and no product
  change.

## 2026-09-06 — Bitmovin Cloudflare challenge boundary and NASA+ Video.js HLS capture

- The next distinct candidate was Bitmovin's official Test Your Stream page
  (`https://bitmovin.com/demos/stream-test/`). Its browser-inspection backend
  exposed a real player and Play control, but the actual fresh Chromium profile
  used by the native probe received a Cloudflare challenge instead of the page:
  the live CDP target reported `title="Just a moment..."`, `videos=[]`,
  `buttons=[]`, `#player=null`, and no media resources. Evidence is retained in
  `/tmp/dm-public-bitmovin-cdp.log`; the bounded probe is
  `/tmp/dm-public-bitmovin-retry2.log`. The disposable Bitmovin fixture was
  quarantined and no product or challenge-bypass change was made.

- Added `fixtures/public_nasa_plus_chromium_probe.py` for NASA+'s real
  production page `https://plus.nasa.gov/video/astro-smile/`. This is a distinct
  page-level initiation path: a trusted click on `Open Video Player` opens the
  modal, the page's Video.js/VHS player starts from its finite HLS source, and
  the injected Download Manager button is then activated through trusted
  Chromium input. The video element is blob-backed, while the captured native
  job preserves NASA+'s replayable HLS manifest source.
- The first NASA+ reference was intentionally rejected: its `-map 0:0`
  reference omitted the AAC stream from the TS playlist, producing
  `176,649,660` bytes while the resident correctly produced
  `191,251,672` bytes. A live fragment probe confirmed the playlist contains
  H.264 video plus AAC audio. The reference was corrected to the resident's
  all-stream `ffmpeg -map 0 -c copy` semantics. A following run hit only
  disposable `/tmp` exhaustion; the probe root was moved to `/var/tmp` without
  changing the application path.
- Final fresh-profile evidence is `/tmp/dm-public-nasa-plus-retry3.log`:
  `NASA_PLUS-OPEN-CLICK` and a playing blob video (`t=2.933113`) preceded one
  native media job for NASA's finite HLS source. The independent reference
  resolved `132` fragments. Resident output and reference matched exactly at
  `977,853,824` bytes, SHA-256
  `9d7f816d8d7b297d6cbf87971d63e789ac702fad48f47ef5e258330cad883fc9`.
  The log ends with `NASA_PLUS: PASS (... jobs=1, browser_downloads=[])` and
  `NASA_PLUS-PROBE: PASS`. No product source changed in this slice.

## 2026-09-06 — NFB signed trailer Video.js HLS capture

- Added `fixtures/public_nfb_trailer_chromium_probe.py` for NFB's lawful public
  Wavemakers trailer embed:
  `https://www.nfb.ca/film/wavemakers/trailer/wavemakers_trailer/embed/player/`.
  This is a distinct production-site Video.js/HLS initiation path from NASA+'s
  page-level modal and from the earlier player families.
- A fresh Chromium profile reached the direct Video.js embed, issued a trusted
  play click, and reported a playing blob source at `t=4.496195`. The injected
  Download Manager button created exactly one provisional native media job.
  The captured source was NFB's finite signed CloudFront `800Kbps.m3u8` variant;
  signed query strings are redacted in the retained log and never documented.
- The independent reference resolved `36` finite HLS fragments and used the
  resident's all-stream `ffmpeg -map 0 -c copy` path. Resident `--commit`
  exited `0`; the sole job completed with `provisional=false`. Resident output
  and the independent reference matched exactly at `12,150,305` bytes, SHA-256
  `c07e72aa615ef37162af92d340cf1bdc9c0b312655d23c3ef3e579873bafe260`.
  Chromium Downloads stayed empty.
- Exact retained green output is `/tmp/dm-public-nfb-trailer.log`:
  `NFB: PASS (output_bytes=12150305,
  output_sha256=c07e72aa615ef37162af92d340cf1bdc9c0b312655d23c3ef3e579873bafe260,
  jobs=1, browser_downloads=[])` and `NFB-PROBE: PASS`.
  No product source change was justified.

## 2026-09-06 — Wistia Aurora custom-player HLS capture

- Added `fixtures/public_wistia_chromium_probe.py` against the official Wistia
  demo route `https://wistia.com/demo?wchannelid=rs59uqxakj&wmediaid=rbsg3da4jd`.
  The route renders the selected product video as the open-shadow
  `<wistia-player media-id="9mz11isa6g">` element. Its real Play Video control
  started a blob-backed video with `readyState=4`, `currentTime=0.133334`, and
  `duration=78.667`; the injected Download Manager control was found inside the
  open shadow tree and clicked with trusted CDP mouse input.
- The first disposable run stopped before player readiness because the probe
  incorrectly treated the page query `wmediaid` as the rendered custom-element
  media ID. The retained failure `/tmp/dm-public-wistia.log` shows the page had
  already exposed the real blob video and player controls; the probe-only
  assertion was corrected to the rendered ID.
- Retry 3 reached the full browser -> extension -> native path and completed
  `220/220` resident HLS segments, but its reference helper compared the native
  output with the 19,828-byte manifest text. That invalid reference was rejected
  and retained in `/tmp/dm-public-wistia-retry3.log`; no product change was made.
- The corrected probe independently fetched the finite Wistia VOD playlist
  `https://embed-cloudfront.wistia.com/deliveries/e23d45799c14736f4b2111c6be7922a993124278.m3u8`,
  reconstructed all 220 MPEG-TS fragments concurrently, and remuxed all streams
  with FFmpeg `-map 0`. Retry 4 recorded one provisional job, resident commit
  exit `0`, and exact resident/reference equality:
  `82,491,823` bytes, SHA-256
  `99557485bb84cd39e251203981bb529453e1581224d54ab0cb7f23c76b6c8f79`.
- Retained output `/tmp/dm-public-wistia-retry4.log` ends with:
  `WISTIA: PASS (page=https://wistia.com/demo?wchannelid=rs59uqxakj&wmediaid=rbsg3da4jd,
  source=https://embed-cloudfront.wistia.com/deliveries/e23d45799c14736f4b2111c6be7922a993124278.m3u8,
  output_bytes=82491823,
  output_sha256=99557485bb84cd39e251203981bb529453e1581224d54ab0cb7f23c76b6c8f79,
  jobs=1, browser_downloads=[])` and `WISTIA-CHROMIUM-PROBE: PASS`.
- Python compilation and `git diff --check` passed. This slice changed no
  production source.

- Added `fixtures/public_gcore_chromium_probe.py` for the official Gcore player
  demo `https://g-core.github.io/gcore-videoplayer-js/example/player.html`.
  This covers a distinct custom-player initiation path with a real page-level
  VOD tab selector, external player controls, blob playback, and a finite HLS
  rendition. The probe selected the page's real `Blender, Spring` VOD tab.
- A fresh Chromium profile reached the page and the extension/native diagnostic
  passed. A trusted click on the Spring tab produced blob playback with
  `readyState=4`, `paused=false`, duration `464.083` seconds, and no media
  error. Trusted Pause and Play clicks then produced the playing state used for
  capture. The injected Download Manager button reported `isTrusted=true`,
  `pointer-events=auto`, and `elementFromPoint` resolved to the button.
- The extension/native path created exactly one provisional media job. The
  captured source was the finite `index-s0q3570v1-v1-a1.m3u8` rendition; query
  parameters are redacted from durable evidence. Browser CDP readback observed
  HTTP `206`, MIME `application/vnd.apple.mpegurl`, 3,682 bytes, SHA-256
  `f427266194568ea871f000cd4bb6a17160e163b94c9bb6999044338f83e6a6d8`.
- The independent all-stream HLS reference resolved `79` finite fragments and
  used the resident's `ffmpeg -map 0 -c copy` mux path. Resident `--commit`
  exited `0`; the sole job completed with `provisional=false`. Resident output
  and reference matched exactly at `111,477,259` bytes, SHA-256
  `b0ad16fcaa29b6cec57ec481faaac0e770600de8d57dbe2c223a0f869a288145`.
  Chromium Downloads stayed empty.
- Exact retained green output is `/tmp/dm-public-gcore-retry2.log`:
  `GCORE: PASS (output_bytes=111477259,
  output_sha256=b0ad16fcaa29b6cec57ec481faaac0e770600de8d57dbe2c223a0f869a288145,
  browser_source_bytes=3682,
  browser_source_sha256=f427266194568ea871f000cd4bb6a17160e163b94c9bb6999044338f83e6a6d8,
  jobs=1, browser_downloads=[])` and
  `PUBLIC-GCORE-CHROMIUM-PROBE: PASS`.
- The first probe attempt exposed a fixture-only selector error: the helper
  referenced an undefined browser-side `source_id` while selecting the Spring
  tab. The page itself was healthy and auto-playing Sintel. The helper was
  corrected to compare `dataset` values against JSON-encoded strings; the
  fresh-profile retry above then passed. No product change was justified.

## 2026-09-06 — Brightcove Video.js master HLS capture

- Added `fixtures/public_brightcove_chromium_probe.py` against the reachable
  Brightcove VET page `https://vet.brightcove.com/ve795-Interactivity/`.
  This is a distinct Video.js/Brightcove initiation path. A fresh Chromium
  profile reached the real `video-js` player, trusted-clicked `Play Video`,
  observed blob playback with `readyState=4`, and clicked the injected
  Download Manager control.
- The first retained attempt `/tmp/dm-public-brightcove.log` stopped in the
  disposable reference helper because it required `#EXT-X-ENDLIST` on the
  captured master playlist. Cache inspection showed the real source shape:
  a finite master that points to finite fMP4 video and alternate-audio media
  playlists. The resident parser selects the first video variant and first
  audio rendition; the reference was changed to follow that exact shape and
  include each playlist's `#EXT-X-MAP` initialization segment.
- Retry 1 exposed a second fixture-only race: Brightcove analytics produced a
  `media=true` database row whose source was not an HLS playlist. The probe now
  waits for the real `.m3u8` media job rather than selecting the newest media
  row. No production source change was made.
- Fresh-profile retry 2 completed the real browser → extension → native →
  resident path with exactly one HLS media job. The independent reference
  reconstructed the first video and first audio tracks, including `44` ordered
  init/media parts, and used FFmpeg `-map 0:0 -map 1:0 -c copy`. Resident
  `--commit` exited `0`; the job completed with `provisional=false`.
- Resident output and independent reference matched exactly at `20,063,456`
  bytes, SHA-256
  `bc50767489a7f0ad29846a902c47d021ca70ba58190c27d142e227b0673c6158`.
  Chromium Downloads stayed empty and the database contained one job.
- Exact retained green output is `/tmp/dm-public-brightcove-retry2.log`:
  `BRIGHTCOVE: PASS (output_bytes=20063456,
  output_sha256=bc50767489a7f0ad29846a902c47d021ca70ba58190c27d142e227b0673c6158,
  jobs=1, browser_downloads=[])` and `BRIGHTCOVE-CHROMIUM-PROBE: PASS`.
- Python compilation and `git diff --check` passed. This slice changed no
  production source.

## 2026-09-06 — Cloudinary Video Player DASH VP9 capture

- Added `fixtures/public_cloudinary_dash_chromium_probe.py` against
  Cloudinary's public adaptive-streaming page
  `https://cloudinary.github.io/cloudinary-video-player/adaptive-streaming.html`.
  This is a distinct Cloudinary/Video.js player family. The probe targeted the
  page's real DASH VP9 player, issued a trusted click on its visible video, and
  observed blob-backed playback with `readyState=4`, `paused=false`, and
  duration `15.21500015258789` seconds before clicking the injected Download
  Manager control.
- The browser/native path resolved the MSE session to a finite Cloudinary
  `.mp4dv` representation. The database contained exactly one `media=true`
  video job; resident `--commit` exited `0` and the job completed with
  `provisional=false`. The independent reference fetched the captured object
  directly, without using the managed output.
- Resident output and independent reference matched exactly at `490,593`
  bytes, SHA-256
  `620c251fb3230a01b037b54a27c750578af738fbcbd2d81e0bf53e5256e9a48f`.
  Chromium Downloads stayed empty and the database contained one job.
- Exact retained green output is `/tmp/dm-public-cloudinary-dash.log`:
  `CLOUDINARY: PASS (output_bytes=490593,
  output_sha256=620c251fb3230a01b037b54a27c750578af738fbcbd2d81e0bf53e5256e9a48f,
  jobs=1, browser_downloads=[])` and `CLOUDINARY-PROBE: PASS`.
- Python compilation and `git diff --check` passed. This slice changed no
  production source.

## 2026-09-06 — OpenPlayerJS configurable progressive embed capture

- Added `fixtures/public_openplayerjs_chromium_probe.py` against the official
  OpenPlayerJS page `https://www.openplayerjs.com/`. The page's configurable
  `Try it` player is a same-origin `/embed.html` iframe whose default media URL
  is a direct progressive MP4. The probe selects that child player rather than
  the unrelated page-level example video.
- A fresh Chromium profile loaded the configured embed, reached `readyState=4`
  and active playback after trusted `Input.dispatchMouseEvent` input, and
  exposed the injected player-bound Download control inside the iframe. The
  first attempts found and repaired only fixture defects: hidden top-level
  source selection, a player-control replacement race in optional click
  instrumentation, and global iframe coordinates below the viewport.
- The extension/native path created exactly one provisional media job for the
  configured MP4. Resident `--commit` exited `0`; the job completed with
  `provisional=false`. The independent reference fetched the captured source
  separately and matched the resident output exactly at `2,097,084` bytes,
  SHA-256
  `bad8caf78fbf9ca292db2f170f99ff6251782947c0bfa8bfd391b4da4a459163`.
  Chromium Downloads stayed empty.
- Exact retained green output is `/tmp/dm-public-openplayerjs-final2.log`:
  `OPENPLAYERJS: PASS (output_bytes=2097084,
  output_sha256=bad8caf78fbf9ca292db2f170f99ff6251782947c0bfa8bfd391b4da4a459163,
  jobs=1, browser_downloads=[])` and
  `OPENPLAYERJS-CHROMIUM-PROBE: PASS`. No production source changed.

## 2026-09-06 — IVID progressive-player source boundary

- Added `fixtures/public_ivid_chromium_probe.py` for the official IVID sandbox
  `https://ividjs.github.io/ivid/`. Fresh Chromium upgraded the real top-level
  `<i-video>` custom element, exposed its light-DOM `<video class="ivid__video">`
  and custom Play control, and injected the player-bound Download control.
- The probe issued trusted CDP input to the actual IVID control. The element
  transitioned to `paused=false`, but the selected Internet Archive source
  `https://ia601305.us.archive.org/28/items/arashyekt4_gmail_Cat/Cat.mp4`
  remained at `readyState=0` with no duration, so no native job or commit proof
  was claimed. This is an external source/bootstrap boundary, not a product
  capture result.
- Retained diagnostics are `/tmp/dm-public-ivid.log`,
  `/tmp/dm-public-ivid-diagnostic.log`, and `/tmp/dm-public-ivid-retry1.log`.
  The independent 30-second `curl` fetch timed out, and `web_extract` returned
  an empty/blocked response for the exact source. No production source changed;
  the candidate is not green.
- Fresh re-run on 2026-09-07 (`/tmp/dm-public-ivid-fresh-20260907.log`,
  probe exit `1`) reached the host and the real custom Play control. The
  player-bound Download button then appeared, but the exact Archive source
  remained `readyState=0`, `duration=null`, and no media error was exposed;
  there was no native job, commit, output, or byte/hash comparison. A corrected
  direct fetch of the exact source timed out after `60` seconds with zero
  response headers and zero bytes. This independently reconfirms an upstream
  source/bootstrap blocker; no product code changed.

## 2026-09-06 — ImageKit Video Player playlist capture

- Added `fixtures/public_imagekit_playlist_chromium_probe.py` against the official
  ImageKit playlist example
  `https://imagekit-developer.github.io/imagekit-video-player/pages/playlist.html`.
  The page mounts two real Video.js-backed playlist players; the probe selects
  only the first `#player_html5_api` instance and leaves the second player
  paused.
- Fresh Chromium loaded both finite players at `readyState=4`. A trusted CDP
  click on the first player's real `Play Video` control produced active playback
  and the player-bound Download control. The extension/native path created
  exactly one provisional media job for
  `https://ik.imagekit.io/ikmedia/docs/video-player/playlist/horses.mp4`.
- Resident `--commit` exited `0`; the job completed with `provisional=false`.
  An independent fetch matched the resident output exactly at `1,061,077`
  bytes, SHA-256
  `5b7a87ffd96efeebecc60495613983222750e1a330db374543796b88c12461dd`.
  Chromium Downloads stayed empty and the second player created no job.
- Exact retained green output is `/tmp/dm-public-imagekit-playlist-retry1.log`:
  `IMAGEKIT: PASS (output_bytes=1061077,
  output_sha256=5b7a87ffd96efeebecc60495613983222750e1a330db374543796b88c12461dd,
  jobs=1, browser_downloads=[])` and
  `IMAGEKIT-CHROMIUM-PROBE: PASS`. No production source changed.

## 2026-09-06 — Able Player accessible progressive capture

- Added `fixtures/public_ableplayer_video3_chromium_probe.py` against the
  official Able Player demo
  `https://ableplayer.github.io/ableplayer/demos/video3.html`. This is the
  accessibility-focused custom control path with captions, descriptions,
  chapters, and a transcript. The pre-existing tracked
  `fixtures/public_ableplayer_chromium_probe.py` was preserved unchanged.
- Fresh Chromium selected the page's actual MP4 source
  `https://ableplayer.github.io/ableplayer/media/wwa.mp4` from the page's
  source alternatives, reached `readyState=4`, duration `52.406826`, and
  exposed `14` text tracks. A trusted CDP click on Able Player's real large
  Play control produced active playback and the player-bound Download control.
- The extension/native path created exactly one provisional media job.
  Resident `--commit` exited `0`; the job completed with `provisional=false`.
  An independent fetch matched the resident output exactly at `5,613,210`
  bytes, SHA-256
  `87716917cfefa444ecc3ae9e4a05a779dbf6621f62ad0c61bc24211283cd6e38`.
  Chromium Downloads stayed empty.
- The first probe run retained the source-selection diagnostic at
  `/tmp/dm-public-ableplayer-webm.log`; the final green run is
  `/tmp/dm-public-ableplayer-final.log`:
  `ABLEPLAYER: PASS (output_bytes=5613210,
  output_sha256=87716917cfefa444ecc3ae9e4a05a779dbf6621f62ad0c61bc24211283cd6e38,
  jobs=1, browser_downloads=[])` and
  `ABLEPLAYER-CHROMIUM-PROBE: PASS`. No production source changed.

## 2026-09-06 — VisionPlayer AV1 progressive capture

- Added `fixtures/public_visionplayer_chromium_probe.py` against the official
  VisionPlayer trailer page `https://visionplayer.io/`. The page uses a custom
  overlay/controller player and a finite direct AV1 MP4 source.
- Fresh Chromium loaded the source at `readyState=4`, duration `111.199002`,
  and active playback followed a trusted CDP click on VisionPlayer's real large
  overlay Play control. The player-bound Download control then appeared.
- The extension/native path created exactly one provisional media job for
  `https://visionplayer.io/media/trailer/streams/visionplayer-trailer.en.720.av1.mp4`.
  Resident `--commit` exited `0`; the job completed with `provisional=false`.
  An independent fetch matched the resident output exactly at `35,552,069`
  bytes, SHA-256
  `782148a7387da302fa0858805d51fde0d1765087f5b93f926696a4d319f858bb`.
  Chromium Downloads stayed empty.
- Exact retained green output is `/tmp/dm-public-visionplayer.log`:
  `VISIONPLAYER: PASS (output_bytes=35552069,
  output_sha256=782148a7387da302fa0858805d51fde0d1765087f5b93f926696a4d319f858bb,
  jobs=1, browser_downloads=[])` and
  `VISIONPLAYER-CHROMIUM-PROBE: PASS`. No production source changed.

## 2026-09-06 — player.html direct-hash progressive capture

- Added `fixtures/public_player_html_chromium_probe.py` for the official
  player.html page
  `https://pseudosavant.github.io/player.html/player.html`. The probe uses the
  documented `#url=` launcher with the public WebM
  `https://pseudosavant.github.io/player.html/videos/big-buck-bunny.webm`.
  It targets the main `video.player`; the page also creates a separate paused
  thumbnail video, which is excluded from player and job selection.
- The first bounded run exposed a fixture-only JavaScript syntax error in the
  diagnostic `allVideos` object (`Unexpected token ';'`). The probe now reports
  CDP evaluation errors instead of swallowing them, and the single punctuation
  correction was verified by a fresh rerun. No production source changed.
- Fresh Chromium loaded the hash launcher, reached `readyState=4`, duration
  `32.48`, and active playback. A trusted CDP click paused the real player and
  a second trusted click resumed it. The player-bound Download control was
  present and clickable.
- The extension/native path created exactly one provisional media job for the
  WebM source. Resident `--commit` exited `0`; the job completed with
  `provisional=false`. The independent reference fetched `2,165,175` bytes
  separately. Resident output matched it exactly with SHA-256
  `519ce67113629c8556337602c28e7dd1286bed58d60936da5dde5215f6427d74`.
  Chromium Downloads stayed empty and the database contained one job.
- Exact retained green output is `/tmp/dm-public-player-html-retry1.log`:
  `PLAYER-HTML: PASS (output_bytes=2165175,
  output_sha256=519ce67113629c8556337602c28e7dd1286bed58d60936da5dde5215f6427d74,
  jobs=1, browser_downloads=[])` and
  `PLAYER-HTML-CHROMIUM-PROBE: PASS`. Python compilation and `git diff --check`
  passed.

## 2026-09-06 — Flowplayer standalone HLS capture

- Added `fixtures/public_flowplayer_hls_chromium_probe.py` against Flowplayer's
  official standalone HLS demo
  `https://docs.flowplayer.com/demos/hls-plugin/samplecode.html`. The public
  master playlist exposes four finite VOD variants; the real Flowplayer player
  mounted a custom-controls `<video>` backed by a blob URL.
- Fresh Chromium reached `readyState=4`, active playback, `duration=16.873334`,
  and `currentTime=0.1` after a trusted CDP click on Flowplayer's visible Play
  control. The player-bound Download Manager button was visible. A capture-phase
  listener on the actual button recorded `isTrusted=true`.
- The extension/native path created exactly one media job for the selected
  `playlist_1080.m3u8` child playlist. The independent browser CDP response
  contained the finite 191-byte HLS playlist, and the independent reconstruction
  fetched its two media fragments and remuxed them locally.
- Resident `--commit` exited `0`; the job completed with `state=completed` and
  `provisional=false`. Resident output matched the independent reference exactly
  at `7,615,379` bytes, SHA-256
  `e6b1fa6addda41ee3a93c8c23106ec66424a8900c9dfbb114b08cf1e13082f00`.
  Chromium Downloads stayed empty and the database contained one job.
- Exact retained green output is `/tmp/dm-public-flowplayer-hls-final.log`:
  `FLOWPLAYER-NATIVE-RESULT: {"bytes":7615379,"provisional":false,
  "sha256":"e6b1fa6addda41ee3a93c8c23106ec66424a8900c9dfbb114b08cf1e13082f00",
  "state":"completed"}`, followed by
  `FLOWPLAYER: PASS (output_bytes=7615379,
  output_sha256=e6b1fa6addda41ee3a93c8c23106ec66424a8900c9dfbb114b08cf1e13082f00,
  jobs=1, browser_downloads=[])` and
  `PUBLIC-FLOWPLAYER-CHROMIUM-PROBE: PASS`. Python compilation and
  `git diff --check` passed. No production source changed.

## 2026-09-06 — VidPly accessible progressive MP4 capture

- Added `fixtures/public_vidply_chromium_probe.py` against VidPly's official
  demo `https://matthiaspeltzer.github.io/vidply/demo/demo.html`. The probe
  selects the first real `#deadline-video` player and its
  `https://matthiaspeltzer.github.io/vidply/demo/media/deadline.mp4` source.
- Fresh Chromium reached `readyState=4`, duration `76.81161`, and exposed 12
  text tracks. A trusted CDP click on the real `.vidply-play-pause` control
  started active playback. The injected player-bound Download Manager button
  was visible and clickable.
- The extension/native path created exactly one media job. The independent
  direct fetch returned `12,769,545` bytes with SHA-256
  `249d6ec925db1ed34b66f66882030a25800138da8d9ed6af89987e0ff612d74f`.
  Resident `--commit` exited `0`; the final job result was
  `state=completed` and `provisional=false`. Resident output matched the
  independent reference exactly. Chromium Downloads stayed empty and the
  database contained one job.
- Exact retained green output is `/tmp/dm-public-vidply-final.log`:
  `VIDPLY-NATIVE-RESULT: {"bytes":12769545,"provisional":false,
  "sha256":"249d6ec925db1ed34b66f66882030a25800138da8d9ed6af89987e0ff612d74",
  "state":"completed"}`, followed by
  `VIDPLY: PASS (output_bytes=12769545,
  output_sha256=249d6ec925db1ed34b66f66882030a25800138da8d9ed6af89987e0ff612d74,
  jobs=1, browser_downloads=[])` and
  `VIDPLY-CHROMIUM-PROBE: PASS`. Python compilation and `git diff --check`
  passed. No production source changed.

## 2026-09-06 — Vidstack custom-element progressive MP4 capture

- Added `fixtures/public_vidstack_chromium_probe.py` against Vidstack's official
  demo `https://vidstack.io/player/demo/`. The probe targets the real
  `media-player`/`media-provider` custom-element stack, its native video source
  `https://files.vidstack.io/sprite-fight/720p.mp4`, and the visible
  `media-play-button` control.
- Fresh Chromium initially exposed `readyState=0` while the Vidstack player
  was still loading. A trusted CDP click on the actual custom-element control
  started playback; the native video then reached `readyState=4`, duration
  `629.84`, and exposed two text tracks. The injected player-bound Download
  Manager button was visible and clickable.
- The extension/native path created exactly one media job. The independent
  direct fetch returned `122,571,618` bytes with SHA-256
  `2bd1b2116f70a2ed61e6d2331370f7fb6774594ea8f847e56739542c4baca9c3`.
  Resident `--commit` exited `0`; the final job result was
  `state=completed` and `provisional=false`. Resident output matched the
  independent reference exactly. Chromium Downloads stayed empty and the
  database contained one job.
- Exact retained green output is `/tmp/dm-public-vidstack.log`:
  `VIDSTACK-NATIVE-RESULT: {"bytes":122571618,"provisional":false,
  "sha256":"2bd1b2116f70a2ed61e6d2331370f7fb6774594ea8f847e56739542c4baca9c3",
  "state":"completed"}`, followed by
  `VIDSTACK: PASS (output_bytes=122571618,
  output_sha256=2bd1b2116f70a2ed61e6d2331370f7fb6774594ea8f847e56739542c4baca9c3,
  jobs=1, browser_downloads=[])` and
  `VIDSTACK-CHROMIUM-PROBE: PASS`. Python compilation and `git diff --check`
  passed. No production source changed.

## 2026-09-06 — Vime shadow-control progressive MP4 capture

- Added `fixtures/public_vime_chromium_probe.py` against Vime's official demo
  `https://vimejs.com/demo/`. The probe targets Vime's shadow-root
  `vm-player`/`vm-default-ui` stack, the real `vm-playback-control` control,
  and its public native video source
  `https://files.vidstack.io/agent-327/720p.mp4`.
- Fresh Chromium reached `readyState=4`, duration `231.6`, and exposed one
  subtitle track. A trusted CDP click on the actual Vime playback control
  started active playback. The injected player-bound Download Manager button
  was visible and clickable through the Vime shadow boundary.
- The extension/native path created exactly one media job. The independent
  direct fetch returned `43,094,196` bytes with SHA-256
  `0715b3f6743218d802b2137cd258315f9464debc4c7e14059bfce89c7c321753`.
  Resident `--commit` exited `0`; the final job result was
  `state=completed` and `provisional=false`. Resident output matched the
  independent reference exactly. Chromium Downloads stayed empty and the
  database contained one job.
- Exact retained green output is `/tmp/dm-public-vime.log`:
  `VIME-NATIVE-RESULT: {"bytes":43094196,"provisional":false,
  "sha256":"0715b3f6743218d802b2137cd258315f9464debc4c7e14059bfce89c7c321753",
  "state":"completed"}`, followed by
  `VIME: PASS (output_bytes=43094196,
  output_sha256=0715b3f6743218d802b2137cd258315f9464debc4c7e14059bfce89c7c321753,
  jobs=1, browser_downloads=[])` and
  `VIME-CHROMIUM-PROBE: PASS`. Python compilation and `git diff --check`
  passed. No production source changed.

## 2026-09-06 — Vime official-demo HLS/fMP4 capture

- Added untracked fixture `fixtures/public_vime_hls_chromium_probe.py` for the
  provider selector on Vime's official demo `https://vimejs.com/demo/`. The
  probe selects the real `hls` option, waits for `<vm-hls>` to become ready,
  traverses Vime's shadow-root `vm-playback-control`, and keeps the acceptance
  checks for one native job, browser-observed HLS, resident completion,
  independent reconstruction equality, and an empty Chromium Downloads tree.
- The first bounded run correctly exposed that the copied fixture had not
  selected HLS and loaded Vime's default progressive `/agent-327/720p.mp4`.
  The second run selected HLS but clicked before the provider was ready
  (`readyState=4`, `paused=true`, `currentTime=0`). Waiting for Vime's `ready`
  state, finite duration, and native `readyState >= 2` fixed that control-boundary
  failure without weakening the trusted-input assertion.
- The next real run reached active HLS playback and a trusted injected-button
  click, but created no native job. Source tracing found the concrete seam:
  Vime's native element exposes an opaque `blob:` `currentSrc` while its child
  `<source src>` contains the real HTTP HLS manifest. The duplicated MV3
  content-script source resolver chose the opaque value first, so the capture
  could not forward an acquirable source.
- Added a regression for opaque-current/HTTP-child resolution in
  `extension/src/shared.test.ts` and applied the same minimal candidate rule to
  `extension/src/shared.ts` and the intentionally duplicated helper in
  `extension/src/content.ts`. The red baseline failed with the expected
  `blob:https://page.test/player/id`; the focused test then passed.
- Final fresh-profile Chromium/native execution is retained at
  `/tmp/dm-public-vime-hls-retry4.log`. It selected HLS with source
  `https://files.vidstack.io/agent-327/hls/stream.m3u8`; Vime's real playback
  control produced active playback (`readyState=4`, `currentTime=0.317532`,
  `paused=false`), and the injected button event recorded `isTrusted=true`.
- The database contained exactly one native HLS job for the public manifest.
  Chromium independently observed the manifest response (`431` bytes,
  `application/x-mpegurl`, HTTP `206`, SHA-256
  `9728f3b427132e4af15271d78404381dc92047ac89752bbad58612d515b32d43`). The
  independent finite-HLS reconstruction saw 5 variants, selected the 1080p
  child playlist, and assembled 58 fragments with no byte ranges.
- Resident `--commit` exited `0`; the final job was `state=completed` and
  `provisional=false`. Resident output matched the independent fMP4 reference
  exactly: `113,171,567` bytes, SHA-256
  `b26bee5d89b99e662dafabf8218c7720066b3c42c8e1caf2f9b54f4bf71d8c89`.
  Chromium Downloads stayed empty and the database contained one job.
- Verification gates passed: `VIME-HLS-COMPILE=PASS`, `npm test -- --run`
  (`8` files, `45` tests), `npm run build:extension`, and `npx tsc -b`.

## 2026-09-06 — Shaka Angel One DASH SegmentBase capture and active-variant ownership

- Added `fixtures/public_shaka_dash_chromium_probe.py` for the official Shaka
  demo `https://shaka-project.github.io/shaka-player/demo/` and its static Angel
  One MPD `https://storage.googleapis.com/shaka-demo-assets/angel-one/dash.mpd`.
  The MPD was retained at `/tmp/shaka-angel-one.mpd` (11,431 bytes; SHA-256
  `e885a9db550ffc499709fb2ad501131f72c5b3abdcd3cb03a2c497c2b5d9d5e8`).
- The first real run exposed a reachable ownership defect: the browser job
  payload retained stale and active video representation URLs together, and the
  native parser selected the first matching URL. The post-capture event drain
  had a different order from the payload, so output could be 480p even while a
  later browser observation identified 576p. The independent reference was
  deliberately not changed to follow that order.
- Fixed the generic extension path to record response MIME ownership, classify
  audio/video candidates, and retain only the newest observed representation for
  each media kind when a manifest-backed player exposes whole representation
  files. Fragment lists keep their existing bounded behavior. Added regression
  coverage for MIME classification and stale-quality exclusion.
- Completed native static DASH `SegmentBase` support. The parser preserves the
  selected representation URL and initialization/index ranges; the resident
  probes resource length, reads the exact ranges, parses MP4 `sidx` references or
  WebM Cues, downloads ordered ranged fragments, and muxes the separate current
  audio/video tracks without re-encoding.
- Fresh active-variant proof is retained at
  `/tmp/dm-public-shaka-dash-active-selection2.log` with Chromium artifacts at
  `/tmp/dm-public-dash-chromium-h_3d8vr_`. Shaka's documented active variant
  exposed original representation IDs `video=10` and `audio=13`; the browser
  observed exactly `video_576p_768k_vp9.webm` and
  `audio_en_2c_128k_aac.mp4`. The managed `selectedSegments` payload contained
  only those two active URLs. Their SegmentBase ranges were video init `0-313`,
  index `314-557`, and audio init `0-785`, index `786-1009`.
- The independent reference and native output were both `6,468,783` bytes with
  SHA-256 `137b305a8482dbd8d2e4eeb8657b3ddf92a4b7814409a525d7fa9f03073df230`.
  `ffprobe` reports VP9 video `720x576`, AAC audio at `48,000 Hz`, and duration
  `60.032000` seconds. The database has exactly one job, `completed` with
  `provisional=false`, and two media tracks. Browser ownership remained
  unhandled for trusted context-menu and Ctrl-click events; Chromium Downloads
  was empty. The probe ends with `PUBLIC-DASH-CHROMIUM-PROBE: PASS`.
- Full verification passed after the fix: npm `48` tests, `npm run build:all`,
  `npx tsc -b`, Rust `66` tests, Python compilation, and `git diff --check`.

## 2026-09-06 — Gcore Player Lab public HLS capture

- Added `fixtures/public_gcore_player_lab_chromium_probe.py` for the official
  Player Lab page `https://g-core.github.io/gcore-videoplayer-js/example/player-lab.html`.
  This is a distinct page-level initiation path from the existing Gcore VOD
  selector proof: the probe submits the real URL form, loads the public Sintel
  HLS preset, exercises the lab's real player controls, and then activates
  the injected Download Manager button with trusted Chromium input.
- Fresh Chromium reported a blob-backed playing player at `readyState=4`,
  duration `888` seconds, and a trusted button event with
  `isTrusted=true`, `defaultPrevented=false`, and target
  `dm-media-download-button`. The native job preserved the replayable finite
  HLS variant; signed query values are redacted in the retained log.
- Resident `--commit` exited `0`; exactly one media job completed with
  `provisional=false`. The browser-context CDP response was HTTP `206`,
  `application/vnd.apple.mpegurl`, `6,928` bytes, SHA-256
  `b2de34fd84ec38848b4f2becc73bb964bcff95c27535713cc45e830e5dbab685`.
  The independent reference resolved `150` finite fragments.
- Native output and independent reference both measured `216,283,085` bytes
  with SHA-256
  `637578a8c9baa988b356b12b4e94a858d334c6f25a890e8cf65d3b9e9cc49a81`.
  `ffprobe` reports H.264 `1586x720`, AAC `44,100 Hz`, and duration
  `888.083000`. Chromium Downloads stayed empty.
- Exact retained output is `/tmp/dm-public-gcore-player-lab-red.log` with
  `GCORE-LAB: PASS (...)` and
  `PUBLIC-GCORE-PLAYER-LAB-CHROMIUM-PROBE: PASS`; independent readback is
  retained at `/var/tmp/dm-public-gcore-lab-chromium-6hvm9nzt/`. No production
  source change was justified by this path.

## 2026-09-06 — ArtPlayer editor progressive capture

- Added `fixtures/public_artplayer_chromium_probe.py` against the official
  ArtPlayer editor `https://artplayer.org/`. The editor's default code created
  a real visible ArtPlayer control surface and the public progressive source
  `https://artplayer.org/assets/sample/video.mp4`.
- Fresh Chromium reached `readyState=4`, active playback, duration `90.045011`,
  and one visible video. The injected Download Manager button was unoccluded;
  its trusted event recorded `isTrusted=true`, `defaultPrevented=false`, and
  target `dm-media-download-button`.
- The native job selected the replayable ArtPlayer source. Resident `--commit`
  exited `0`; the job became `completed` with `provisional=false`.
- The independent browser reference measured `2,304,837` bytes with SHA-256
  `73a1c47b4e2da63ce67c1cf8fe7fd3332b6f6dbb08b9f0939c8dfecfc21fdb57`.
  Native output matched exactly. `ffprobe` reports H.264 `480x360`, AAC
  `44,100 Hz`, and duration `90.045011`. Chromium Downloads stayed empty.
- Exact retained output is `/tmp/dm-public-artplayer-final.log` with
  `ARTPLAYER: PASS (...)` and `ARTPLAYER-CHROMIUM-PROBE: PASS`; independent
  readback is retained at `/tmp/dm-public-artplayer-chromium-q2vn9mti/`.
  No production source change was justified by this path.

## 2026-09-06 — DPlayer official-page progressive capture

- Added `fixtures/public_dplayer_chromium_probe.py` against the official DPlayer
  page `https://dplayer.diygod.dev/`. This is a distinct custom-player path:
  DPlayer's real `<video>` starts from an API URL and the browser follows it to
  a signed `ovcdn-acc.dogevideo.com` MP4 object. Query values and signed
  redirect values are redacted in the fixture and retained log.
- The first request-level diagnostic caught a fixture-only serialization error;
  the fresh rerun then passed. Chromium reached `readyState=4`,
  `paused=false`, duration `260.179002`, and no media error. The captured
  network evidence showed the DPlayer API request, HTTP `302` redirect, and
  the CDN response as `video/mp4`, HTTP `206`, full content range
  `bytes 0-18125695/18125696`.
- The real injected button was hit through trusted Chromium input. The
  capture-phase event recorded `isTrusted=true`, `defaultPrevented=false`, and
  target `dm-media-download-button`. The native job source was the exact
  browser-observed DPlayer API URL.
- Resident `--commit` exited `0`; the only job became `completed` with
  `provisional=false`. An independent browser-context fetch of the captured
  source returned HTTP `200` and measured `18,125,696` bytes with SHA-256
  `ec2eeff713f2707f37085772a6a6ed206f1d4fd8180f679bb7387dfeb3fce00`.
  Native output matched exactly. `ffprobe` reports H.264 `964x540`, AAC
  `44,100 Hz`, and duration `260.179002`.
- Trusted context-menu and Ctrl-click ownership remained unprevented. Chromium
  Downloads stayed empty and the database contained exactly one completed job.
  Exact retained output is `/tmp/dm-public-dplayer-final.log` with
  `DPLAYER-CHROMIUM: PASS (...)` and `DPLAYER-CHROMIUM-PROBE: PASS`;
  independent readback is retained at
  `/tmp/dm-public-dplayer-chromium-_yueuxeq/`. No production source change was
  justified by this path.

## 2026-09-06 — drop-player official progressive fallback

- Added `fixtures/public_drop_player_original_chromium_probe.py` against
  `https://player.drop.mov/`. The fixture uses the page's real Quality menu and
  selects its `Original` item; it does not replace the page source. Chromium then
  leaves the blob-backed HLS source and plays the page-provided
  `https://assets.drop.mov/samples/video/01/source.mp4`.
- Fresh Chromium reached `readyState=4`, `paused=false`, duration `29.460000`,
  and no media error. Network evidence recorded a browser-owned `video/mp4`
  response, HTTP `206`, with full range `bytes 0-65474249/65474250`; the second
  range request began at byte `1409024`. The first range's `ERR_ABORTED` was
  canceled by the page while switching sources, not an unhandled media failure.
- The real injected Download button was hit through trusted Chromium input. The
  capture-phase event recorded `isTrusted=true`, `defaultPrevented=false`, and
  target `dm-media-download-button`. Resident `--commit` exited `0`; the only
  job became `completed` with `provisional=false`.
- An independent browser-context fetch returned HTTP `200`, `65,474,250` bytes,
  `video/mp4`, SHA-256
  `ec0b221ac4abb9d361ed90e3e02ec84a1a439d7cd706c19da9b06cae369c80f9`.
  Native output matched exactly. `ffprobe` reports H.264 `3840x2160`, AAC
  `48,000 Hz`, and duration `29.460000`. Trusted context-menu and Ctrl-click
  ownership remained unprevented. Chromium Downloads stayed empty and the
  database contained exactly one completed job.
- A bounded exploratory HLS check on the same page is not counted as green
  coverage. The browser observed the master, separate `stream_2.m3u8` 480p video,
  and `stream_1.m3u8` audio. Clicking after only the first fragments produced a
  completed but partial `14,720`-byte resident file against a full independent
  two-track reference of `6,932,130` bytes. Waiting until the adaptive player
  switched to 1080p removed the injected button at end-of-media. This is a
  fixture/player-lifecycle limitation, not a justified production fix.
- Exact retained output is `/tmp/dm-public-drop-player-original-final4.log` with
  `DROP-ORIGINAL-CHROMIUM: PASS (...)` and
  `DROP-ORIGINAL-CHROMIUM-PROBE: PASS`; independent readback is retained at
  `/tmp/dm-public-drop-player-original-chromium-u_96afd8/`. No production source
  change was justified by this path.

## 2026-09-06 — ReactPlayer HLS diagnostic boundary (superseded)

- Ran a fresh Chromium diagnostic against the official ReactPlayer demo
  `https://cookpete.github.io/react-player/`. The real `HLS (m3u8)` control
  selected the documented public Mux master
  `https://stream.mux.com/VcmKA6aqzIzlg3MayLJDnbF55kX00mds028Z65QxvBYaA.m3u8`
  with trusted CDP mouse input.
- The page's network path was reachable: `42` redacted Mux records were
  observed (`14` requests, `14` HTTP-200 responses, `14` finishes), including
  the master playlist, one signed rendition playlist, and finite `.ts`
  fragments. There were zero `Network.loadingFailed` records. Signed query
  values and opaque CDN path values are redacted in the retained log.
- The first diagnostic searched only light-DOM `<video>`/`<audio>` nodes. It
  therefore reported zero media and no Download button even though the page
  had mounted an open-shadow `HLS-VIDEO` element. That was an instrumentation
  defect, not a product result.
- A corrected diagnostic scanned the open shadow root, found one playing blob
  video (`readyState=4`, duration `60`), and found the injected button and
  native provisional job. The positive acceptance below supersedes this
  negative boundary; no shadow-specific production patch was needed.
- The earlier demo MP4 item remains an external media-error-4 baseline, not a
  product defect. Exact retained diagnostic output is
  `/tmp/dm-reactplayer-hls-diag.log`; its initial negative branch is historical
  evidence only.

## 2026-09-06 — ReactPlayer HLS shadow-DOM acceptance

- Added `fixtures/public_reactplayer_hls_chromium_probe.py` against
  `https://cookpete.github.io/react-player/`. The fixture clicks the page's real
  `HLS (m3u8)` control, scans the open shadow-root `HLS-VIDEO` media, and uses
  the generic extension/native path. It contains no site-specific resolver.
- Fresh Chromium evidence reached one shadow video with `readyState=4`,
  `paused=false`, duration `60.000361`, and `blob:` current source. The
  extension/native diagnostic returned `ok=true`; the real injected Download
  button was clicked through trusted input. One native provisional job was
  created from the public HLS master and committed with resident `--commit`
  exit `0`.
- Chromium observed one rendition manifest and twelve finite media segments.
  The browser-context reference fetched the observed rendition's map and
  ordered segments, transferred those bytes into the disposable run, and
  remuxed them with the same local `ffmpeg -map 0 -c copy` finalizer used by
  the product. Raw browser bytes were `16,912,480`; the independent finalized
  reference was `16,510,043` bytes with SHA-256
  `434de625f61910c5758899d73a9e3e875282757330b5df3e753b2b41c276a9f7`.
- Native output matched the independent finalized reference exactly:
  `16,510,043` bytes and the same SHA-256. The job ended
  `state=completed`, `provisional=false`, with exactly one database job.
  FFprobe reports H.264 `1280x720`, AAC `44,100 Hz`, and duration
  `59.999954` seconds.
- Trusted context-menu and Ctrl-click ownership stayed browser-owned
  (`isTrusted=true`, `defaultPrevented=false`). Chromium Downloads stayed
  empty. Signed rendition query/path values are `[REDACTED]` in retained
  output; the public master URL is the only source identifier retained.
- Focused checks passed: fixture Python compilation, `git diff --check`,
  credential-pattern scan with zero matches, and `npm test -- --run`
  (`8` files, `48` tests). Exact acceptance output is
  `/tmp/dm-public-reactplayer-hls-final.log` ending with
  `PUBLIC-REACTPLAYER-HLS-CHROMIUM-PROBE: PASS`.
- No production source change was justified. This closes one real
  shadow-DOM/custom-player HLS initiation pattern; it does not close the
  remaining ordinary one-use takeover product decision or unrelated v1 gaps.

## 2026-09-06 — Ordinary redirect ownership boundary (§5.1.1)

- Added `fixtures/ordinary_redirect_chromium_probe.py`. It uses fresh
  Chromium profiles, the built extension, the real resident binary, native
  messaging, and trusted CDP mouse input against the public GitHub release URL
  `https://github.com/cli/cli/releases/download/v2.100.0/gh_2.100.0_checksums.txt`.
  GitHub redirects this URL to a signed `release-assets.githubusercontent.com`
  object at runtime; all signed query values are `[REDACTED]` in retained
  evidence. No site-specific resolver or browser-process hook is used.
- The plain-link case had no `download` attribute and exercised the current
  observe-only `downloads.onDeterminingFilename` fallback. Chromium recorded
  one completed browser download (`1,971` bytes, SHA-256
  `6b5916dffcfa6f593b1db7890f2ddc485318e99fa263acf73aa28ebb877b53cd`). The
  resident created exactly one provisional job, was committed with `--commit`,
  and produced the same `1,971` bytes and hash. The observable product
  consumer count was `2`: one browser download record plus one native job.
  This is public redirect evidence that the fallback duplicates a reusable
  transaction; it is not a claim that a reusable redirect is one-shot-safe.
- The explicit `<a download="gh_2.100.0_checksums.txt">` case used the same
  public redirect through the generic content-script pre-browser path. The
  trusted click created exactly one native job, Chromium recorded zero matching
  downloads, and the resident output matched an independent post-run public
  validation fetch: `1,971` bytes with the same SHA-256. The observable product
  consumer count was `1`.
- A separate disposable one-use timing probe used a local mint-once endpoint
  only to isolate the race; it is not public coverage and is not a resolver.
  A plain link produced source status sequence `[200, 410, 410]`: the browser
  received the one valid `64 KiB` response and completed its browser record;
  the native job then failed with `Source returned 410 Gone`. The two native
  `410` responses are the ordinary acquisition range probe followed by its
  full GET. The run retained exactly one browser record and one native job,
  with no state in the repository.
- Decision: keep the observe-only fallback. Do not add cancel+erase takeover;
  the real one-use run proves that destroying the browser transaction is not a
  safe generic repair. The product can claim generic pre-browser ownership for
  explicit download-attributed actions, and must honestly preserve the browser
  download for browser-generated/plain-link cases where MV3 exposes no safe
  pre-browser action. No production source change was justified.
- This matches the current Chrome API boundary: `downloads.onCreated` fires
  when a download begins, while `downloads.onDeterminingFilename` holds the
  transaction only until filename listeners call `suggest`; Chrome's current
  `webRequest` documentation says blocking handlers in MV3 are policy-only.
  Sources: `https://developer.chrome.com/docs/extensions/reference/api/downloads`
  and `https://developer.chrome.com/docs/extensions/reference/api/webRequest`.
- Verification: `python3 -m py_compile
  fixtures/ordinary_redirect_chromium_probe.py`, the fresh public two-case run,
  and the disposable one-use timing run all passed. Public output is retained
  at `/tmp/dm-ordinary-redirect-public.log`; timing output is retained at
  `/tmp/dm-ordinary-one-use-timing.log`. No credentials or signed query values
  are retained. Broader v1 gaps remain open.

## 2026-09-06 — HLS multivariant master and active audio/video selection

- The unverified HLS experiment is now implemented in the four intended paths:
  `extension/src/media-candidates.ts`, `extension/src/shared.test.ts`,
  `src-tauri/src/media.rs`, and `src-tauri/src/main.rs`.
- Extension selection recognizes generic likely-master names (`master`,
  `playlist`, `manifest`, and `multivariant`) and keeps recent non-subtitle child
  manifest URLs ahead of segment hints. Native HLS parsing receives that ordered
  evidence, chooses the first matching video and grouped audio rendition, and
  falls back to the manifest's normal order only when no hint matches. Matching
  covers both directory-style segment names and public filename-embedded
  identities such as `index-...m3u8` and `segment-...ts`; pending variant state
  survives intervening HLS comments.
- Added `fixtures/public_hls_master_selection_chromium_probe.py` against the
  official hls.js demo with the public multivariant master
  `https://demo-public.gvideo.io/videos/2675_HCzdHTj79iSt3wiW/master.m3u8`.
  Fresh Chromium changed hls.js from level `2` to the selected level `1`
  (`928000` bitrate, `1124x468`). The browser observed the master, a prefetched
  high rendition, the selected mid rendition, and default English audio. The
  native job source stayed the master while its ordered `selectedSegments`
  began with the selected child manifest, then audio/other observed resources.
- The clean-scratch real run created exactly one managed job and completed the
  two-track MPEG-TS output. An independent reference fetched the selected video
  rendition and default audio rendition in playlist order, concatenated their
  124 + 124 segments, and remuxed them with explicit `-map 0:0 -map 1:0 -c
  copy`. Both reference and resident output were `91,614,656` bytes with SHA-256
  `c33db14e961fa6a6ae36c87a6728d23ae7e9ed93605f86e408a4e90231146588`.
- FFprobe on the exact resident output reports MPEG-TS with two streams:
  H.264 `1124x468`, duration `730.375000` seconds, and AAC-LC `44,100 Hz`
  stereo, with container duration `733.936322` seconds. Trusted Ctrl-click and
  context-menu ownership remained `isTrusted=true` and
  `defaultPrevented=false`; Chromium Downloads stayed empty.
- The first durable-fixture retry failed only because a preserved earlier probe
  root exhausted `/tmp` during FFmpeg muxing. The exact retained tracks were
  valid and manual mux on `/srv` passed. After removing only the known adaptive
  HLS disposable roots, the fixture passed; no production scratch behavior was
  changed.
- Exact public acceptance output is retained at
  `/tmp/dm-public-hls-master-selection-final.log`; the preserved ffprobe run is
  `/tmp/dm-public-hls-master-selection-ffprobe.log`. The independent reference
  details are printed in both logs and stored under the task-owned
  `/srv/backup-export/dm-adaptive-hls-gvideo-reference-*` roots.
- Verification on this worktree passed focused Vitest `27/27`, full Vitest
  `50/50`, Rust `70/70`, `cargo check`, `cargo build`, `npm run build:extension`,
  `npx tsc -b`, Python compilation, and `git diff --check`. Broader v1 gaps
  remain open; this closes the real multivariant-master active-rendition slice.

## 2026-09-06 — POST replay and lifecycle probes verified

- Corrected the stale description in `fixtures/postonly_replay_chromium_probe.py`:
  the current bounded urlencoded-form path observes the POST body and native
  replay sends it as POST; it no longer claims the native fallback is GET-only.
- Fresh Chromium POST-only proof passed. The visible form submission produced
  one browser download of `65,536` bytes with SHA-256
  `3473fca710f006025d284d4e32ad4fc453a1c522c36bfcde4cb19da38220d8a7`.
  The resident job completed with the same bytes/hash, exactly one managed job,
  and the fixture asserted every server request body was `fixture=post-only`.
  Evidence: `/tmp/dm-postonly-replay-baseline.log`.
- Fresh two-process single-instance proof passed: the second `--capture`
  process exited `0`, the original resident stayed alive, process count stayed
  at one, only one job was created, and the server saw one request. Evidence:
  `/tmp/dm-single-instance-final.log`.
- Fresh close-to-tray proof passed: the manager window became unmapped while
  the resident stayed alive as one process; the 64 MiB output reached
  `finalizing` with one server request. Evidence:
  `/tmp/dm-close-to-tray-final.log`.
- These probes changed no production code. Remaining lifecycle gaps include
  reopening from the tray, explicit exit behavior, and broader trusted
  context-menu/Ctrl-click coverage outside the accepted paths.

## 2026-09-06 — Lifecycle evidence correction

- Audit found that the original `close_to_tray_probe.py` clicked Close after
  the capture callback had already hidden the main window. That earlier result
  did not prove a visible user close. The fixture now maps the exact X11 window,
  asserts `Map State: IsViewable`, then clicks the real custom Close button.
- The corrected visible-close run passed: the window became `IsUnMapped`, the
  resident stayed alive as one process, the 64 MiB transfer reached
  `finalizing`, and the local source saw one request. Evidence:
  `/tmp/dm-close-to-tray-corrected.log`.
- Explicit exit remains unverified. The custom-button probe hit repeated
  WebKit Inspector startup races; the Inspector-free X11 probe did reach the
  native close handler but returned code `1` with a GDK `BadDrawable` error.
- No production close-handler change was retained from that experiment.

## 2026-09-07 — Desktop viewport containment correction

- Fresh Chromium at `1180x760` (effective viewport `1180x617`) exposed a real
  layout defect: `.manager-body` used `height: calc(100vh - 76px)` together
  with `min-height: 620px`. The shell therefore grew to `696px`, showed an
  outer page scrollbar, and also kept independent Downloads-list and inspector
  scrollbars. The Settings item and footer were below the visible viewport.
- Changed `.manager-body` in `src/styles.css` to `min-height: 0`. The live
  post-fix DOM reports `document.documentElement.scrollHeight ===
  clientHeight === 617`; only `.download-list` and `.inspector-scroll` remain
  scrollable. The desktop screenshot shows the full shell without the outer
  scrollbar: `/tmp/dm-ui-main-fixed.png`.
- Fresh Network settings navigation rendered without clipping or overflow;
  screenshot: `/tmp/dm-ui-settings-network.png`.
- Full Vitest passed `50/50`; `npm run build:extension`, `npx tsc -b`, and
  `git diff --check` passed.

## 2026-09-07 — Linux-safe mock paths

- Fresh Chromium used a new profile and CDP port `9228`; no prior localStorage
  was retained. The mock Add URL dialog showed `~/Downloads`.
- Settings → Downloads showed `~/Downloads` and
  `~/.cache/download-manager/tmp`.
- A seeded job's Overview showed
  `~/Downloads/ubuntu-24.04-desktop-amd64.iso`; its Files tab showed
  `~/.cache/download-manager/tmp/job-1.part`. The earlier intermediate probe
  caught and fixed a remaining Windows backslash in the seeded destination.
- `src/adapters.ts` now selects Windows sample paths only on Windows, uses
  Linux/macOS-safe mock defaults elsewhere, and normalizes seeded/provisional
  mock paths without changing native Rust defaults.
- Evidence screenshots: `/tmp/dm-ui-path-add-fixed.png`,
  `/tmp/dm-ui-path-settings-fixed.png`, and
  `/tmp/dm-ui-path-seeded-files-fixed.png`.
- Full Vitest passed `50/50`; `npx tsc -b`, `npm run build:extension`, and
  `git diff --check` passed.

## 2026-09-07 — Modified browser-click ownership boundary

- A fresh Chromium Ctrl-click probe exposed a generic ownership defect. The
  page received a trusted left click with `ctrlKey=true` and
  `defaultPrevented=false`, but the fallback `downloads.onDeterminingFilename`
  path still sent one `capture-acquisition` message to native. The browser
  record remained present, so the gesture was duplicated rather than owned by
  one side. Red evidence: `/tmp/dm-ctrl-click-ownership-final.log`.
- Added a bounded browser-owned-click token. The content script records only
  modified clicks on explicit `<a download>` links; the background matches the
  source URL/name for ten seconds and lets the next browser download proceed
  without native forwarding. No site-specific rule or cancellation was added.
- The repaired Ctrl-click probe passed with `isTrusted=true`, `ctrlKey=true`,
  `defaultPrevented=false`, `native_captures=0`, and one browser-owned record.
  Chromium marked this disposable fixture record `SERVER_BAD_CONTENT`, so this
  is ownership evidence, not browser-byte acceptance:
  `/tmp/dm-ctrl-click-ownership-fixed.log`.
- The existing unmodified explicit-anchor probe still passed after the fix:
  one native job, one source request pair, `65,536` byte/hash equality, and
  empty Chromium Downloads. Evidence: `/tmp/dm-explicit-anchor-after-ctrl-fix.log`.
- Changed files: `extension/src/content.ts` and
  `extension/src/background.ts`. Focused Vitest passed `27/27`,
  `npx tsc -b`, `npm run build:extension`, and the two real Chromium probes
  passed. Broader v1 work remains open.

## 2026-09-07 — Generic public player source switch

- Fresh Chromium ran the uncovered W3C HTML5 Video Events source-switch flow.
  The page's trusted `Test movie` control reused the same `<video>` element and
  changed it to `http://media.w3.org/2010/05/video/movie_300.mp4`.
- The extension followed the switched current source, kept the player-bound
  Download button attached, and created exactly one media job. Native output
  was `2,757,913` bytes and matched the independent browser reference hash
  `80c548058688a577ce9ca501cf9807311b95cc526cc82d292ec7e138e42257de`.
- The run observed three browser range requests for the switched media, ended
  with `provisional=false, state=completed`, and left Chromium Downloads empty.
  Evidence: `/tmp/dm-public-w3c-source-switch-final.log`.
- No production change was needed for this generic source-switch boundary.

## 2026-09-07 — Generic public WebM media

- Fresh Chromium ran the MDN top-level HTML5 WebM player. The real media
  element reached `readyState=4`, played for `7.8` seconds, and received the
  player-bound Download control.
- The extension selected the browser's current WebM source
  `https://mdn.github.io/learning-area/html/multimedia-and-embedding/video-and-audio-content/rabbit320.webm`.
  The resident output was `330,618` bytes and matched the independent browser
  reference SHA-256
  `074b046f0832c1c262a7a3e015b042092fa226b1550b83a7d14cca9025d34e1e`.
- The completed run had exactly one media job and zero Chromium Downloads.
  Evidence: `/tmp/dm-public-mdn-webm-final.log`.
- No production change was needed for this generic WebM boundary.

## 2026-09-07 — Context-menu ownership re-audit

- Re-ran the existing Save-Link-As boundary after the modified-click
  suppression change. Chromium delivered a trusted right-click with
  `defaultPrevented=false`; the fake native host received only policy requests,
  with zero `capture-acquisition` messages, zero browser downloads, and zero
  native jobs.
- Evidence: `/tmp/dm-save-as-ownership-after-ctrl-fix.log`.

## 2026-09-07 — Platform-neutral sign-in settings copy

- Fresh Linux Chromium exposed Windows-only wording in General settings:
  both sign-in descriptions said “when Windows starts.” Changed `src/App.tsx`
  to say “when you sign in,” which matches the setting semantics on every host.
- Fresh route `http://127.0.0.1:4177/?settings=general` now contained no
  “Windows starts” text and showed both neutral descriptions. Visual evidence:
  `/tmp/dm-ui-general-platform-neutral.png`.
- Full Vitest passed `50/50`; `npx tsc -b`, `npm run build:extension`, and
  `git diff --check` passed.

## 2026-09-07 — Resident exit lifecycle boundary

- Ran the requested isolated command repeatedly:
  `timeout 360s python3 fixtures/exit_behavior_probe.py`. The established
  X11 `WM_DELETE_WINDOW` path reached the close handler and the resident
  process terminated, but it reproducibly returned `resident_exit=1` rather
  than `0`.
- Tested two minimal native hypotheses in disposable builds. Adding
  `api.prevent_close()` before the delayed `app.exit(0)` did not improve the
  close result. Increasing the delay from 100 ms to 1 s also did not improve
  it. Both changes were reverted; no unverified lifecycle production change
  remains.
- A separate startup `SIGSEGV` occurred twice during Inspector bring-up, but
  rerunning the established probe reached the close path. It is retained as
  a startup-race observation, not lifecycle acceptance.
- Updated `fixtures/exit_behavior_probe.py` so it no longer labels every
  non-`SIGSEGV` exit a clean PASS. Exit `0` is `PASS`; exit `1` is
  `TERMINATION-PASS-WITH-EXTERNAL-BLOCKER` for the reproducible GTK/Xvfb
  shutdown status; other statuses fail. The final run reported:
  `EXIT-BEHAVIOR: TERMINATION-PASS-WITH-EXTERNAL-BLOCKER (closeBehavior=exit,
  resident_exit=1, native_close=WM_DELETE_WINDOW, blocker=GTK/Xvfb shutdown
  status)`.
- Native Cargo tests passed `70/70`; Cargo build and probe compilation passed.
  No source change remains outside the probe harness. The external blocker is
  the only unresolved part of this lifecycle acceptance.

## 2026-09-07 — Media filename follows audio/video type

- The real Able Player dynamic-audio flow exposed a generalized naming defect:
  the current MP3 source `https://ableplayer.github.io/ableplayer/media/smallf.mp3`
  created a job named `Player created dynamically | Able Player Demos.mp4`.
  Red evidence: `/tmp/dm-public-ableplayer-dynamic-audio-final.log`.
- Changed `extension/src/content.ts` so media capture derives a safe filename
  extension from the element/source MIME type or URL. Known audio types use
  `.mp3`, `.m4a`, `.ogg`, or `.wav`; known video types use `.mp4`, `.ogv`, or
  `.webm`; non-URL/unknown sources fall back by element kind.
- The repaired dynamic-audio flow now creates
  `Player created dynamically | Able Player Demos.mp3`. Native output remained
  `4,690,721` bytes with browser-reference SHA-256
  `60c777096e72ae34ceb250d66f071ba6b612f445aefba8438d12e8536288a2de`, one
  completed managed job, and empty Chromium Downloads:
  `/tmp/dm-public-ableplayer-dynamic-audio-fixed.log`.
- Re-audited the public MDN WebM flow after the shared change. It still creates
  `Simple video example.webm`, with `330,618` bytes, SHA-256
  `074b046f0832c1c262a7a3e015b042092fa226b1550b83a7d14cca9025d34e1e`, one
  completed job, and empty Chromium Downloads:
  `/tmp/dm-public-mdn-webm-after-audio-name.log`.
- Full Vitest passed `50/50`; `npx tsc -b`, `npm run build:extension`, and
  `git diff --check` passed. No provider-specific resolver code was added.
- Re-audited the sibling Able Player dynamic-video flow. It still creates
  `Player created dynamically | Able Player Demos.mp4` and completed one managed
  job with four browser range requests. Native output was `5,613,210` bytes and
  matched SHA-256
  `87716917cfefa444ecc3ae9e4a05a779dbf6621f62ad0c61bc24211283cd6e38`; Chromium
  Downloads remained empty. Evidence:
  `/tmp/dm-public-ableplayer-dynamic-video-after-name.log`.

## 2026-09-07 — Public interception-off download remains browser-owned

- Added and ran `fixtures/public_integration_off_chromium_probe.py` with a
  fresh Chromium profile, isolated HOME, the unpacked extension, and the real
  native binary available but unused. The extension policy reply was
  `interceptDownloads=false`, `showMediaButtons=true`, `excludedSites=[]`.
- A trusted page anchor initiated the public GitHub CLI checksum asset
  `https://github.com/cli/cli/releases/download/v2.100.0/gh_2.100.0_checksums.txt`.
  Chromium recorded exactly one complete download of `1,971` bytes with
  `error=null` and `byExtensionId=null`; the signed release-assets redirect is
  recorded with its query values redacted. SHA-256 was
  `6b5916dffcfa6f593b1db7890f2ddc485318e99fa263acf73aa28ebb877b53cd`.
- The browser-owned file was the only matching download. The app database was
  absent, no native process was present, and native job count was `0`. This
  closes the public real-browser §5.3 / §19.2 off-toggle evidence gap without
  claiming native takeover when interception is disabled.
- The first run exposed only a fixture-path assertion error: Chromium correctly
  saved under the isolated HOME's `Downloads/`, not the profile's default
  subdirectory. The fixture was corrected and the fresh rerun passed:
  `/tmp/dm-public-integration-off-fixed-20260907.log`.
- Verification: the corrected probe exited `0`, Python compilation passed, and
  `git diff --check` passed. No production code changed.

## 2026-09-07 — Public button-triggered ordinary download

- Added `fixtures/public_button_download_chromium_probe.py` for a different
  ordinary initiation shape. Fresh Chromium loaded the public W3Schools page
  with the persisted extension policy explicitly reporting
  `interceptDownloads=true`, `showMediaButtons=true`, and no exclusions. A
  trusted CDP click targeted a visible page `<button>`; its handler created an
  anchor without a `download` attribute and started the public GitHub CLI
  archive URL
  `https://github.com/cli/cli/archive/refs/tags/v2.100.0.tar.gz`.
- The observe-only browser fallback created one native provisional job for the
  redirected codeload source and preserved Chromium's own copy. The browser
  recorded exactly one complete item with `byExtensionId=null`, `15,067,137`
  bytes, and no error. The native job was committed through the resident
  `--commit` path and completed with the same filename and bytes.
- Browser, native, and an independent post-run public fetch all matched at
  SHA-256
  `39d5123f08a553a6fa69e46de86c22d04d97a217e03d0e6584b66d0fea50f1fe`.
  The final evidence reported `browser_records=1`, `native_jobs=1`,
  `button_initiation=true`, and `explicit_anchor_download=false`. This proves
  the public button path and confirms there was no unintended second browser
  download; the expected observe-only fallback has one browser copy plus one
  managed native copy.
- Evidence: `/tmp/dm-public-button-download-fresh-20260907.log`. No production
  or site-specific resolver change was needed.
- Verification: the fresh Chromium probe exited `0`, Python compilation and
  `git diff --check` passed.

## 2026-09-07 — Narrow viewport containment regression

- A fresh Chromium baseline at a forced `900x600` CSS viewport exposed a
  responsive regression left by the earlier desktop containment fix. The
  `@media (max-width: 1080px)` rule restored `min-height: 580px` on
  `.manager-body`, making the document `644px` high for a `600px` viewport;
  the status footer began at `601px` and the compositor showed an outer page
  scrollbar. Red evidence: `/tmp/dm-ui-responsive-red.log` and
  `/tmp/dm-ui-responsive-red.png`.
- Changed only that responsive override to `min-height: 0`, matching the base
  `.manager-body` rule. Added
  `fixtures/responsive_viewport_chromium_probe.py`, which forces the same
  viewport through CDP and checks both horizontal and vertical document/body
  containment plus footer placement.
- The fixed fresh run reported document and body `900x600` with equal client
  and scroll dimensions, manager height `536px` with `minHeight=0px`, and the
  footer ending at `591px`. The compositor screenshot shows no outer scrollbar;
  the download list and inspector retain their intended internal scroll areas:
  `/tmp/dm-ui-responsive-chromium.log` and
  `/tmp/dm-ui-responsive-chromium.png`.
- The rebuilt native Tauri/WebKit window was also resized through X11 to
  `900x600`. Its live DOM reported equal document/body client and scroll
  dimensions, `.manager-body` from `top=48` to `bottom=600` with `minHeight=0px`,
  and `.manager-statusbar` from `566` to `600`. The native DOM probe ended with
  `NATIVE-RESPONSIVE-PROBE: PASS`; WebKit's unsupported `Page.captureScreenshot`
  method was a tooling limitation only. Evidence:
  `/tmp/dm-native-responsive-dom-pass.log`.
- Verification: `npm test -- --run` passed `50/50`, `npx tsc -b`,
  `npm run build:extension`, the fresh responsive Chromium probe, and
  `git diff --check` all passed. No native or extension production behavior
  changed.

## 2026-09-07 — Appearance settings persist across restart

- Ran `timeout 240s python3 fixtures/settings_ui_persistence_probe.py` against
  the rebuilt native application with a fresh isolated HOME and Xvfb display.
  The live settings UI changed from `theme=system`,
  `accent=#0878ed`, `density=comfortable` to
  `theme=dark`, `accent=#d3138c`, `density=compact`.
- The native SQLite settings row immediately contained all three changed
  values. After terminating and starting a second resident process against the
  same HOME, the rendered Appearance page restored the same theme, accent, and
  density. The probe ended with
  `SETTINGS-UI-PERSISTENCE: PASS (theme=dark, accent=#d3138c,
  density=compact, sqlite_match=true, restart=true)` and exit `0`.
- This closes the earlier unclaimed density/restart restoration note. The
  probe used the live native settings surface and database; no production
  source change was needed. Evidence: `/tmp/dm-settings-ui-persistence-current.log`.
- Native verification on the same tree passed Rust `70/70` and `cargo build`.

## 2026-09-07 — Minimum supported window smoke

- The native configuration sets `minWidth=780` and `minHeight=560`. A fresh
  Chromium re-audit forced exactly that `780x560` CSS viewport after the narrow
  containment fix.
- The document and body both reported `780x560` client and scroll dimensions;
  `.manager-body` was `496px` with `minHeight=0px`, and the status footer ended
  at `551px`. The screenshot showed the one-column workspace and its internal
  download-list scrollbar without an outer page scrollbar:
  `/tmp/dm-ui-responsive-min.log` and `/tmp/dm-ui-responsive-min.png`.
- No additional production change was needed.

## 2026-09-07 — General startup settings effects

- Ran `timeout 240s python3 fixtures/startup_settings_probe.py` against the
  rebuilt native app with a fresh isolated HOME and Xvfb display. With
  `startAtSignIn=false`, the disposable Linux autostart desktop file was absent.
  With it true, the file was present and contained the `--startup` argument.
- A real process restart with `--startup` and `showManagerAtSignIn=false`
  produced no visible `Download Manager` main window. Setting it true and
  restarting again produced exactly one visible main window. The probe ended
  with `STARTUP-SETTINGS-PROBE: PASS` and exit `0`.
- This closes the previously unverified live effects of both General sign-in
  toggles. No production source change was needed. Evidence:
  `/tmp/dm-startup-settings-current.log`.

## 2026-09-07 — MDN styled custom-control media capture

- The existing `fixtures/public_mdn_styled_player_chromium_probe.py` had a
  reachable finite top-level player but stopped before Download Manager capture
  on a page-only control assertion. A fresh red run reproduced the exact
  boundary: trusted CDP input reached `#mute`, yet the page left
  `video.muted=false`; the same page's programmatic click changes it. Red
  evidence: `/tmp/dm-public-mdn-styled-red-current.log`.
- The probe was narrowed without changing product code. It now records the
  trusted mute result as `MDN-STYLED-MUTE-BOUNDARY` and continues to the
  player-bound Download control. Its independent reference uses the HTTPS
  origin because the page declares an HTTP source while Chromium upgrades the
  actual media request to HTTPS; the original capture job/source is unchanged.
- Fresh Chromium reached the real styled player at
  `https://iandevlin.github.io/mdn/video-player-styled/`, then a trusted click on
  `#playpause` started the visible finite Tears of Steel video. The injected
  Download control was hit at `x=1094.4453125, y=97.8359375`; the real
  extension/native path created exactly one media job for
  `http://iandevlin.github.io/mdn/video-player/video/tears-of-steel-battle-clip-medium.mp4`.
- Resident `--commit` exited `0`; the job ended `completed` with
  `provisional=false`. The native output and independent browser reference were
  both `15,256,787` bytes with SHA-256
  `8f8b69ed443be171cb505c75fab22f3af25375b09817e713fe0fd7c88c78f451`.
  Chromium Downloads remained empty and the database contained exactly one
  job. The full run is `/tmp/dm-public-mdn-styled-green.log`; a retained-output
  rerun is `/tmp/dm-public-mdn-styled-retained.log`.
- Independent `ffprobe` on
  `/tmp/dm-mdn-styled-player-chromium-f0ecuauh/Managed/mdn-styled-tears-of-steel.mp4`
  reports MP4 container, H.264 `800x332`, AAC `44,100 Hz` stereo, duration
  `70.542222`, and size `15,256,787` bytes.
- Exact output ended with
  `MDN-STYLED-HTML5-CHROMIUM: PASS (output_bytes=15256787,
  output_sha256=8f8b69ed443be171cb505c75fab22f3af25375b09817e713fe0fd7c88c78f451,
  traffic=2, jobs=1, browser_downloads=[])` and
  `MDN-STYLED-HTML5-CHROMIUM-PROBE: PASS`. Full verification passed Vitest
  `50/50`, TypeScript, extension build, Rust `70/70`, Cargo build, Python
  compilation, and `git diff --check`. No site-specific resolver or production
  source change was added.

## 2026-09-07 — Direct public Ogg/Theora/Vorbis capture

- The HTML5Demo wrapper was already known to lack a usable content-script
  context. Its exact finite asset was independently reachable at
  `https://html5demo.yo.fr/demo/media/windowsill.ogv`, returning `200` with
  `Content-Type: video/ogg` and `3,258,782` bytes. A direct top-level Chromium
  media document was used instead of relaxing the wrapper requirement.
- Added `fixtures/public_direct_ogg_chromium_probe.py` by specializing the
  existing direct-player harness only for this new public Ogg path. A fresh
  Chromium profile exposed one playable `<video>` with `readyState=4`,
  `paused=false`, and duration `20` seconds. The real injected Download button
  was trusted-clicked at `x=732.4453125, y=649.3984375`.
- The native path created exactly one media job for
  `https://html5demo.yo.fr/demo/media/windowsill.ogv`; resident `--commit`
  exited `0`; the job completed with `provisional=false`; Chromium Downloads
  stayed empty. Two fresh runs, disposable and tracked, both produced
  `3,258,782` bytes and SHA-256
  `d56958121ee39c27731a7c96a3b1be04f750cbb1fd43dc0f52dcca47b9280235`, matching
  the browser-side independent reference. Logs:
  `/tmp/dm-public-direct-ogg.log` and
  `/tmp/dm-public-direct-ogg-tracked.log`.
- Independent `ffprobe` on the retained native output
  `/tmp/dm-direct-mp4-z6fxt_n5/Managed/windowsill-direct.ogv` reports an Ogg
  container, Theora video `426x240`, Vorbis audio at `44,100 Hz` stereo,
  duration `20.000000`, and size `3,258,782` bytes.
- Exact tracked output ended with
  `DIRECT-OGG: PASS (output_bytes=3258782,
  output_sha256=d56958121ee39c27731a7c96a3b1be04f750cbb1fd43dc0f52dcca47b9280235,
  jobs=1, browser_downloads=[])` and `DIRECT-OGG-PROBE: PASS`. Python
  compilation and `git diff --check` passed; no production source or
  site-specific resolver change was needed.
- Clappr's official demo was checked as the next page candidate, but fresh
  browser inspection displayed `Could not play video. There was a problem
  trying to load the video. Error code: html5_video:4` while its declared
  `http://clappr.io/highline.mp4` source did not become a playable player. This
  is recorded as a page/player boundary; no Download Manager acceptance claim
  or workaround was added.

## 2026-09-07 — Xigua/xgplayer MSE worker-source attribution

- The official Xigua/xgplayer examples page
  `https://h5player.bytedance.com/en/examples/` exposed a real finite child-frame
  video. The player was visible, playing, `readyState=4`, and `90.08` seconds
  long. Its media element exposed a `blob:` URL while Chromium observed the
  underlying progressive MP4 at
  `https://sf1-cdn-tos.huoshanstatic.com/obj/media-fe/xgplayer_doc_video/mp4/xgplayer-demo-360p.mp4`.
- The first guarded implementation correctly refused the capture rather than
  trusting an unscoped recent request: the clicked player was frame `3` with a
  child `documentId`, while the worker requests were frame `0`. The fresh
  diagnostic showed `workerCount=60`, all `unknown`, and the ring had been filled
  by page JavaScript assets before the click. This was a generalized observation
  bug, not an Xigua-specific resolver problem.
- Fixed the generic path in `extension/src/background.ts` and
  `extension/src/media-candidates.ts`: a shared media-shape predicate retains
  response traffic only when it is a media/manifest/segment role, has an audio
  or video MIME-derived kind, or has a media-file URL. The guarded frame-zero
  worker fallback applies that same predicate and still requires a fresh,
  visible, playing sole player plus exactly one unowned source. Generic page
  assets cannot evict the observed media or become a cross-player selection.
- Added focused regression coverage in `extension/src/shared.test.ts` for the
  media-shape predicate, the JavaScript-asset flood, sole-player fallback,
  competing-player refusal, and multiple-source refusal. The new regression was
  observed red (`31 tests | 1 failed`) before the production change, then green.
- The tracked real Chromium probe
  `fixtures/public_xgplayer_chromium_probe.py` now passes the complete browser →
  extension → native provisional job → resident commit → independent browser
  reference loop. A trusted child-frame Download click created exactly one
  media job. Resident `--commit` exited `0`; the job ended `completed` with
  `provisional=false`; Chromium Downloads remained empty. The fresh tracked
  evidence is `/tmp/dm-public-xgplayer-tracked-fix.log`.
- The managed output
  `/tmp/dm-xgplayer-iframe-video-chromium-i8yz0psn/Managed/xgplayer-iframe-video.mp4`
  is `4,691,480` bytes with SHA-256
  `5b38348290651df564ec88abaf1927d4278c9c46d53b638e3d1f13903150835b`, exactly
  matching the independent browser reference. `ffprobe` reports MP4,
  H.264 `640x360`, AAC `48,000 Hz` stereo, duration `90.080000`.
- Exact probe result: `XGPLAYER-CHROMIUM-PROBE: PASS`, with `traffic=7`,
  `jobs=1`, and `browser_downloads=[]`. Full verification after the fix passed
  Vitest `55/55`, TypeScript, frontend and extension builds, Rust `70/70`,
  Cargo build, Python compilation, and `git diff --check`. No site-specific
  resolver was added.

## 2026-09-07 — Hexaglobe Player DASH thumbnail-adaptation filtering

- The official Hexaglobe Player fully featured VOD page
  `https://player-demo.hexaglobe.net/vod-fully-featured.html` loaded a real
  Shaka/MSE-backed video at `readyState=4`, active playback, and finite duration
  `634.566` seconds. Its documented public source was the static Big Buck Bunny
  MPD `https://dash.akamaized.net/akamai/bbb_30fps/bbb_with_tiled_thumbnails.mpd`.
- The first real browser → native loop exposed a generalized DASH parser defect:
  the MPD's `image/jpeg` tiled-thumbnail adaptation was emitted as a third
  native media track. The output contained an unwanted MJPEG `3200x180` stream;
  the independent audio/video reference was `83,850,531` bytes while the native
  output was `84,799,148` bytes. This was not accepted as a probe-reference
  mismatch because the extra stream was visible in native `ffprobe` and the job
  reported `mediaTracks=3`.
- Added a red-then-green Rust regression in `src-tauri/src/media.rs` and fixed
  `DashTrackBuilder::finish` to discard adaptations explicitly declared with a
  non-audio/video kind such as `image/jpeg`. Representation-level MIME
  inference remains available, and kind-less legacy media adaptations remain
  supported.
- Added `fixtures/public_hexaglobe_dash_chromium_probe.py`. It drives the real
  Hexaglobe player and Download control, records browser MPD/segment traffic,
  derives the exact browser-selected representation from native segment hints,
  independently fetches and muxes all finite audio/video DASH segments, and
  checks byte/hash equality, stream validity, duration, one managed job, and an
  empty Chromium Downloads directory.
- The fresh tracked probe passed with one native provisional job, resident
  `--commit` exit `0`, final `completed` and `provisional=false`, and no browser
  download. Independent and native output were both `83,850,531` bytes with
  SHA-256 `f4d8507d028660da6a8f70ab59964f89a5ae7b2cf08831fff74282839dc5f379`.
  The retained output was
  `/tmp/dm-hexaglobe-dash-chromium-ebzor7nb/Managed/hexa-dash-bbb.mp4`.
- `ffprobe` reports exactly H.264 `640x360` video and AAC `48,000 Hz` stereo
  audio, duration `634.566667`; the thumbnail stream is absent. The fresh log
  is `/tmp/dm-public-hexaglobe-dash-final.log`, with
  `HEXA-DASH-CHROMIUM-PROBE: PASS`, `jobs=1`, and `browser_downloads=[]`.
  The complete post-fix aggregate gate is captured at
  `/tmp/dm-hexaglobe-full-gate.log` and returned `GATE_RC=0`.
  No site-specific resolver was added.

## 2026-09-07 — Responsive inspector transition containment

- A fresh Chromium baseline at `841x560` exposed a real user-visible boundary
  defect. The `<=1080px` workspace required a `390px` list column plus a `282px`
  inspector, but the usable workspace was only `649px`. The outer shell masked
  the resulting overflow; the inspector ended at `x=855` while the workspace
  ended at `x=832`, visibly clipping the last tab and bottom actions. Red evidence:
  `/tmp/dm-ui-boundary-841.log` and `/tmp/dm-ui-boundary-841.png`.
- Changed only the responsive workspace grid in `src/styles.css` from
  `minmax(390px, 1fr) 282px` to `minmax(0, 1fr) 282px`. The existing `<=840px`
  one-column fallback remains unchanged. Updated the existing
  `fixtures/responsive_viewport_chromium_probe.py` to check the inspector edge
  and run fresh `900x600`, `841x560`, and `780x560` cases.
- The repaired `841x560` layout keeps the inspector entirely inside the
  workspace (`x=550..832`), with the `Log` tab, close control, and bottom actions
  visible. All three cases report equal document/body client and scroll
  dimensions and an in-window footer. Evidence:
  `/tmp/dm-ui-responsive-transition-fixed.log` and screenshots
  `/tmp/dm-ui-responsive-chromium.png`,
  `/tmp/dm-ui-responsive-transition.png`, and
  `/tmp/dm-ui-responsive-min.png`.
- The complete post-change gate is captured at
  `/tmp/dm-responsive-full-gate.log` and returned `GATE_RC=0`: Vitest `55/55`,
  Rust `71/71`, both production builds, Cargo build, Python compilation, and
  `git diff --check` passed. No native or extension behavior changed.

## 2026-09-07 — Transient manager-menu dismissal

- The existing real Chromium controls probe was extended with a reachable UI
  regression. It opened the Sort menu, clicked a download row, and observed that
  `.sort-menu` remained mounted (`menu_after_row=true`), leaving an overlay over
  the newly selected workspace. Red evidence: `/tmp/dm-manager-menu-dismiss-red.log`.
- Added a small `dismissMenus` state transition in `src/App.tsx`. Add URL,
  sidebar/settings navigation, row selection, row action menus, and the status
  settings action now clear transient Sort/More menus while preserving the
  selected row and sort value.
- The existing `fixtures/manager_inline_controls_chromium_probe.py` now covers
  the regression. Fresh trusted Chromium input reported
  `SORT-MENU-ROW-DISMISS: {"menu_after_row": false}` and still passed inline
  pause/resume/retry, Pause All/Resume All, sorting, Add URL open/close, and
  `MANAGER-INLINE-CONTROLS: PASS`. Evidence:
  `/tmp/dm-manager-menu-dismiss-fixed.log`.
- The complete post-change gate is captured at
  `/tmp/dm-menu-dismiss-full-gate.log` and returned `GATE_RC=0`: Vitest `55/55`,
  Rust `71/71`, both production builds, Cargo build, Python compilation, and
  `git diff --check` passed. No native or extension behavior changed.

## 2026-09-07 — Live Notifications surface updates

- A focused jsdom regression exposed that `NotificationsSurface` copied
  `snapshot.notifications` into local state once. Rerendering with a later
  application snapshot left the new card absent; red evidence:
  `/tmp/dm-notifications-live-red.log`.
- Changed `src/App.tsx` to derive visible cards from the current snapshot and
  retain only explicit dismissals as a set of notification IDs. New completion
  or failure cards now appear in an already-open surface, while per-card and
  Dismiss all actions remain effective across state updates.
- Added `src/notifications-surface.test.tsx`. The focused test passes `2/2` for
  later-card rendering and dismissal persistence:
  `/tmp/dm-notifications-live-fixed-2.log`.
- The existing real native notification probe passed after the rebuild with one
  completed card, `Open` and `Show in folder`, one `65,536`-byte managed output,
  and the expected recorded file/folder opener calls:
  `/tmp/dm-notifications-native-after-live-fix.log`.
- The corrected fail-fast full gate is captured at
  `/tmp/dm-notifications-full-gate-corrected.log` and returned `GATE_RC=0`:
  Vitest `58/58`, Rust `71/71`, both production builds, Cargo build, valid
  Python compilation, and `git diff --check` passed.

## 2026-09-07 — Settings save failure feedback

- A focused jsdom regression caught a silent rejection: settings controls called
  `adapter.updateSettings` without a rejection handler, so an IPC or store error
  left the user with no explanation.
- `src/App.tsx` now renders an accessible `role="alert"` in Settings with the
  adapter error text. It clears on the next save attempt and also handles a
  synchronous adapter throw.
- The test was red before the fix (`SETTINGS_FEEDBACK_RED_RC=1`, missing alert):
  `/tmp/dm-settings-feedback-red.log`.
- The fixed focused test passed (`SETTINGS_FEEDBACK_FIXED_RC=0`):
  `/tmp/dm-settings-feedback-fixed.log`.
- The corrected fail-fast aggregate gate passed (`GATE_RC=0`) with Vitest
  `58/58`, Rust `71/71`, both production builds, Cargo build, Python
  compilation, and `git diff --check`:
  `/tmp/dm-settings-feedback-full-gate.log`.

## 2026-09-07 — Extension popup action feedback

- The popup previously swallowed background failures for policy load/save and
  `Open Manager`, while switch state was visible only through CSS.
- `extension/src/popup.ts` now renders failure text in an aria-live status region,
  handles rejected and `{ok:false}` responses, and sets `aria-pressed` on both
  policy switches. `extension/popup.html` supplies the status region and styling.
- The focused test was red before the change (`POPUP_FEEDBACK_RED_RC=1`):
  `/tmp/dm-popup-feedback-red.log`.
- The focused popup tests passed `2/2` after the fix:
  `/tmp/dm-popup-feedback-fixed.log`.
- The existing real Chromium popup probe passed after rebuilding the extension:
  exclusion toggled off/on, `example.com` persisted, and Open Manager reached
  the manager with 14 rows:
  `/tmp/dm-popup-real-fixed-retry.log`.
- The fail-fast full gate returned `GATE_RC=0` with Vitest `60/60`, Rust `71/71`,
  both production builds, Cargo build, Python compilation, and `git diff --check`:
  `/tmp/dm-popup-feedback-full-gate.log`.

## 2026-09-07 — Browser policy persistence failure contract

- The background policy handler mutated its live policy and swallowed
  `chrome.storage.local.set` failures, replying `ok:true` even though a later
  popup reopen would lose the change.
- `extension/src/background.ts` now persists a copied next policy before
  publishing it, restores the previous policy on failure, and returns
  `{ok:false,error,policy}`. Successful saves retain the existing native push.
- The focused listener regression was red before the fix
  (`BACKGROUND_POLICY_RED_RC=1`, received `ok:true`):
  `/tmp/dm-background-policy-red.log`.
- The fixed rollback/error contract passed (`BACKGROUND_POLICY_FIXED_RC=0`):
  `/tmp/dm-background-policy-fixed.log`.
- The focused popup/background pair passed `3/3`, the extension build passed, and
  the real popup Chromium probe passed after the fix:
  `/tmp/dm-browser-policy-focused-fixed.log`,
  `/tmp/dm-browser-policy-extension-build.log`,
  `/tmp/dm-browser-policy-popup-real.log`.
- The fail-fast aggregate gate returned `GATE_RC=0` with Vitest `61/61`, Rust
  `71/71`, both production builds, Cargo build, Python compilation, and
  `git diff --check`:
  `/tmp/dm-browser-policy-full-gate.log`.

## 2026-09-07 — Browser policy load failure contract

- `loadPolicy()` previously caught storage-read failures and let `get-policy`
  return `ok:true` with defaults, hiding an unavailable browser-integration store.
- `extension/src/background.ts` now records the startup read result, makes
  `get-policy` wait for readiness, and returns `{ok:false,error,policy}` when
  the read fails. A later successful save clears the load error.
- The focused regression was red before the fix (`BACKGROUND_POLICY_LOAD_RED_RC=1`):
  `/tmp/dm-background-policy-load-red.log`.
- The fixed background pair passed `2/2`:
  `/tmp/dm-background-policy-load-fixed.log`.
- The popup/background pair passed `4/4`, the extension build passed, and the
  real popup Chromium probe passed after the change:
  `/tmp/dm-browser-policy-load-focused.log`,
  `/tmp/dm-browser-policy-load-build.log`,
  `/tmp/dm-browser-policy-load-real.log`.
- The fail-fast aggregate gate returned `GATE_RC=0` with Vitest `62/62`, Rust
  `71/71`, both production builds, Cargo build, Python compilation, and
  `git diff --check`:
  `/tmp/dm-browser-policy-load-full-gate.log`.

## 2026-09-07 — Distinguish success and failure toasts

- The main UI stored success and error messages in the same string and always
  rendered a green check icon. A failed pause, retry, cancel, or acquisition
  action could therefore look successful.
- `src/App.tsx` now carries a success/error tone through the shared action
  wrapper, manual acquisition, and context-menu paths. `NoticeToast` renders
  error messages as an assertive alert with an error icon; `src/styles.css`
  gives them a red treatment.
- The focused toast test was red before the fix (`NOTICE_TOAST_RED_RC=1`):
  `/tmp/dm-notice-toast-red.log`.
- The fixed toast test passed (`NOTICE_TOAST_FIXED_RC=0`):
  `/tmp/dm-notice-toast-fixed.log`.
- The existing real Chromium manager-controls probe passed with pause/resume,
  retry, bulk controls, sort-menu dismissal, and Add URL behavior:
  `/tmp/dm-notice-toast-real.log`.
- The fail-fast aggregate gate returned `GATE_RC=0` with Vitest `63/63`, Rust
  `71/71`, both production builds, Cargo build, Python compilation, and
  `git diff --check`:
  `/tmp/dm-notice-toast-full-gate.log`.

## 2026-09-07 — Surface native file-opening failures

- `openLocalPath` previously discarded `invoke('open_path')` failures, leaving Inspector, context-menu, and completed-notification file actions with no user feedback.
- The helper now returns a safe error string for native opener rejection, browser popup blocking, and synchronous opener errors. Inspector and notification surfaces render that result as an alert; context-menu actions route it through the error toast.
- The red helper regression returned `OPEN_PATH_RED_RC=1`; after the fix the helper and Inspector tests passed. The final refined focused run is captured at `/tmp/dm-open-path-final-focused-clean.log` and returned `OPEN_PATH_FINAL_CLEAN_RC=0`: two files, `3/3` tests, no React warnings.
- The notification opener regression passed in `/tmp/dm-open-path-all-focused-clean2.log`. The real manager-controls Chromium probe remained green with `NOTICE_TOAST_REAL_RC=0`; the real notification probe was blocked before page interaction by the known WebKit inspector startup boundary (`WebKit inspector did not become ready: [Errno 111] Connection refused`), so it is not acceptance evidence.
- The complete fail-fast gate is captured at `/tmp/dm-open-path-full-gate-final-clean.log` and returned `GATE_RC=0`: TypeScript, Vitest `66/66`, frontend and extension builds, Rust `71/71`, Cargo build, Python compilation, and `git diff --check`.

## 2026-09-07 — Report clipboard copy failures

- The context-menu `Copy source URL` action previously announced success immediately while ignoring the asynchronous Clipboard API result. MDN documents that `writeText()` returns a promise and rejects when clipboard access is denied or unavailable.
- Added `copySourceUrl` result handling. A resolved write produces the existing success notice; missing or rejected clipboard access produces the error-tone notice instead.
- The red helper test returned `COPY_SOURCE_RED_RC=1`; the fixed helper test returned `COPY_SOURCE_FIXED_RC=0`. The direct context-menu regression returned `CONTEXT_COPY_FIXED_RC=0` for the rejection path and verified that the menu closes after reporting the error.
- The existing real manager-controls Chromium probe returned `CONTEXT_COPY_REAL_RC=0`; it covered the surrounding context-menu and manager paths but did not claim native clipboard access.
- The complete fail-fast gate is captured at `/tmp/dm-copy-source-full-gate.log` and returned `GATE_RC=0`: TypeScript, Vitest `68/68`, frontend and extension builds, Rust `71/71`, Cargo build, Python compilation, and `git diff --check`.

## 2026-09-07 — Preserve newer subscription snapshots during startup

- `useAppSnapshot` subscribed to live state and requested an initial snapshot in parallel. If the subscription delivered a newer state first, the later initial promise overwrote it with stale data.
- Added a focused deferred-promise regression. Its red baseline returned `SNAPSHOT_RACE_RED_RC=1`; the monotonic subscription guard returned `SNAPSHOT_RACE_FIXED_RC=0` and preserves the live update.
- Full Vitest passed `18` files and `69/69` tests with no warnings. The real manager-controls Chromium probe returned `MANAGER_RC=0`. The existing responsive probe initially hit `127.0.0.1:4177` with no server; after starting its required disposable Vite server, all `900x600`, `841x560`, and `780x560` viewports passed with contained document/body dimensions. Evidence: `/tmp/dm-snapshot-race-manager-real.log` and `/tmp/dm-snapshot-race-responsive-real-retry.log`.
- The corrected complete fail-fast gate is captured at `/tmp/dm-snapshot-race-full-gate-corrected.log` and returned `GATE_RC=0`: TypeScript, Vitest `69/69`, frontend and extension builds, Rust `71/71`, Cargo build, Python compilation, and `git diff --check`.

## 2026-09-07 — Show provisional-creation failures in Add Download

- `AddDownloadWindow.submit()` previously used `finally` only. A rejected `onCreate()` promise escaped as an unhandled rejection, and the existing form-error region stayed empty in standalone Add windows.
- Added a focused manual-add regression. The red baseline returned `ADD_WINDOW_ERROR_RED_RC=1`, including the unhandled `Native core unavailable` rejection; the fixed test returned `ADD_WINDOW_ERROR_FIXED_RC=0` and verifies the message in `role="alert"`.
- The existing real `manual_add_ui_probe.py` completed the actual Add URL → native provisional → resident completion → commit path: `ADD_WINDOW_ERROR_REAL_RC=0`, one provisional job, one source request, `262144` output bytes, and SHA-256 `ac6533c30d2d4fcc01be82be68bd63a592d37c49fe769b05aebfb4504fa146b3`. Evidence: `/tmp/dm-add-window-error-real.log`.
- The complete fail-fast gate is captured at `/tmp/dm-add-window-error-full-gate.log` and returned `GATE_RC=0`: TypeScript, Vitest `70/70`, frontend and extension builds, Rust `71/71`, Cargo build, Python compilation, and `git diff --check`.

## 2026-09-07 — Keep Add Download open when commit fails

- Captured-job `onCommit` previously accepted only a synchronous callback. The standalone Add window fire-and-forget `commitProvisional(...).then(close)`, so a native commit failure became an unhandled rejection with no recovery surface.
- `AddDownloadWindow` now accepts synchronous or promise-returning commit callbacks, waits for async commits, and renders failures in its existing `role="alert"` form region without closing the window. The standalone callback closes only after native commit success; synchronous main-manager callbacks retain their existing global error-toast path.
- The focused failure regression initially returned `ADD_WINDOW_COMMIT_ERROR_RED_RC=1` with no alert; the refined implementation returned `ADD_WINDOW_COMMIT_ERROR_CLEAN_RC=0` for the failure case and the existing commit wiring cases (`4/4`) with no React warnings.
- The existing real `manual_add_ui_probe.py` still completed the normal captured commit path: `ADD_WINDOW_COMMIT_REAL_RC=0`, one provisional job, one source request, `262144` output bytes, and SHA-256 `ac6533c30d2d4fcc01be82be68bd63a592d37c49fe769b05aebfb4504fa146b3`. Evidence: `/tmp/dm-add-window-commit-real.log`.
- The complete fail-fast gate is captured at `/tmp/dm-add-window-commit-full-gate.log` and returned `GATE_RC=0`: TypeScript, Vitest `71/71`, frontend and extension builds, Rust `71/71`, Cargo build, Python compilation, and `git diff --check`.

## 2026-09-07 — Keep Add Download open when cancellation fails

- Captured-job `onCancel` previously accepted only a synchronous callback. The standalone Add window fire-and-forget `cancelJob(...); close()`, so a native cancellation failure could close the surface while leaving the provisional job and produce an unhandled rejection.
- `AddDownloadWindow` now accepts synchronous or promise-returning cancel callbacks, waits for async cancellation, and renders failures in its existing `role="alert"` form region without closing. The standalone callback closes only after native cancellation success; synchronous main-manager cancellation keeps its existing global error-toast path.
- The focused cancellation regression initially returned `ADD_WINDOW_CANCEL_ERROR_RED_RC=1`; the fixed combined Add-window set returned `ADD_WINDOW_CANCEL_ERROR_FIXED_RC=0` (`6/6`) with no warnings. Full Vitest later passed `21/21` files and `72/72` tests with no warnings.
- The existing real manual Add probe was attempted at `/tmp/dm-add-window-cancel-real.log` but stopped before product interaction at the known external boundary: `WebKit inspector did not become ready: [Errno 111] Connection refused`. No real cancellation acceptance claim is made for that run.
- The complete fail-fast gate is captured at `/tmp/dm-add-window-cancel-full-gate.log` and returned `GATE_RC=0`: TypeScript, Vitest `72/72`, frontend and extension builds, Rust `71/71`, Cargo build, Python compilation, and `git diff --check`.

## 2026-09-07 — Close-to-tray lifecycle re-audit boundary

- Consumed both earlier red re-audits, `/tmp/dm-close-to-tray-reaudit.log` and `/tmp/dm-close-to-tray-reaudit-retry.log`. Both stop in `wait_inspector()` with `WebKit inspector did not become ready: [Errno 111] Connection refused` before `wait_tauri()`, before any window lookup or user action.
- Ran the requested fresh clean-state current-build probe. `/tmp/dm-close-to-tray-current.log` returned `CLOSE_TRAY_CURRENT_RC=1` with the same traceback and exact inspector connection-refused error. The run did not reach the close button, X11 map-state check, tray reopen, or exit assertions, so it is not product lifecycle evidence.
- Reviewed the native wiring in `src-tauri/src/main.rs`: the main `CloseRequested` handler prevents close and hides `main` when `closeBehavior=tray`; the native tray `open-manager` item shows and focuses `main`. No lifecycle production change was made without a reachable product failure.
- The resident/process cleanup check after the blocked run found no Download Manager or Xvfb process left by the probe. This remains an external WebKit Inspector startup boundary; prior corrected close-to-tray acceptance remains the latest valid evidence.

## 2026-09-07 — Inspector removal failure feedback

- The Inspector `Remove` button previously called `adapter.removeJob(job.id)` without awaiting or handling rejection. A native/database failure could leave the user with no feedback and an unhandled promise.
- Added `src/inspector-remove.test.tsx`. Its red baseline returned `INSPECTOR_REMOVE_RED_RC=1` because no `role="alert"` was rendered after `removeJob` rejected with `Native core unavailable`.
- `Inspector` now awaits removal, closes only after success, and keeps the Inspector open with a visible error alert on synchronous or asynchronous failure. The button is disabled and labelled `Removing…` while the operation is pending.
- The existing real manager-controls Chromium probe passed after the change with all previously covered inline pause/resume/retry, bulk controls, sorting, transient-menu dismissal, and Add URL checks: `/tmp/dm-inspector-remove-manager-real-retry.log`, `MANAGER-INLINE-CONTROLS: PASS`.
- The complete fail-fast gate is captured at `/tmp/dm-inspector-remove-full-gate.log` and returned `INSPECTOR_REMOVE_GATE_RC=0`: focused Inspector removal `1/1`, TypeScript, full Vitest `22` files/`73` tests, frontend and extension builds, Rust `71/71`, Cargo build, Python compilation, and `git diff --check`.

## 2026-09-07 — Keep Inspector selection inside the active filter

- The existing `fixtures/manager_filters_context_chromium_probe.py` showed a reachable inconsistency: changing from `All` to a filter could leave no visible row selected while the Inspector continued showing the previously selected job from another filter.
- The generalized selection assertion was red before the product change: `FILTER_SELECTION_RED_RC=1`, with `Paused` showing one row, `selected=[]`, and the stale active Inspector. No new scenario-specific harness was added.
- `Manager` now derives its selected job from `filteredJobs` and synchronizes `selectedId` to the first visible job when the current selection leaves the filter. Empty filters no longer fall back to an unrelated global job.
- The existing filter/context Chromium probe returned `FILTER_SELECTION_FIXED_RC=0`. It passed selection and matching Inspector headings for `All`, `Active`, `Completed`, `Failed`, `Media`, and `Paused`, and retained its context-menu targeting checks. Evidence: `/tmp/dm-filter-selection-fixed.log`.
- The complete fail-fast gate is captured at `/tmp/dm-filter-selection-full-gate.log` and returned `FILTER_SELECTION_GATE_RC=0`: TypeScript, full Vitest `22` files/`73` tests, frontend and extension builds, Rust `71/71`, Cargo build, Python compilation, and `git diff --check`.

## 2026-09-07 — Make download rows keyboard-selectable

- Download rows were clickable `<article>` elements but had no focus target or keyboard activation. The existing manager probe’s generalized accessibility assertion was red at `/tmp/dm-row-keyboard-red.log`: `role=None`, `tabIndex=-1`, and `focused=False`.
- Rows now expose `role="button"`, `tabIndex=0`, and `aria-pressed`, and Enter/Space selects the focused row. Keyboard events from nested Pause/Resume/Retry and More actions do not select the row.
- The existing filter/context Chromium probe returned `ROW_KEYBOARD_FIXED_RC=0` and still passed every filter-selection and context-menu assertion. It selected `project-assets.zip` through Enter before restoring the normal selection. Evidence: `/tmp/dm-row-keyboard-fixed.log`.
- Focused TypeScript and Vitest checks returned `ROW_KEYBOARD_FOCUSED_RC=0`; full Vitest was `22` files/`73` tests with no warnings. Evidence: `/tmp/dm-row-keyboard-focused.log`.
- The complete fail-fast gate is captured at `/tmp/dm-row-keyboard-full-gate.log` and returned `ROW_KEYBOARD_GATE_RC=0`: TypeScript, full Vitest `22` files/`73` tests, frontend and extension builds, Rust `71/71`, Cargo build, Python compilation, and `git diff --check`.

## 2026-09-07 — Name shared toggle controls for assistive technology

- The shared React `Toggle` exposed only `aria-label="On"` or `"Off"`, so a focused setting, extension-surface, or tray-surface control did not identify which preference it changed. The existing settings regression was red at `/tmp/dm-toggle-label-red.log`: received `Off`, expected the setting name.
- `SettingToggle` and `TrayToggle` now pass their existing human labels to the shared control. `aria-pressed` continues to expose the on/off state without changing the visible UI.
- The focused settings test returned `TOGGLE_LABEL_FIXED_RC=0` (`1/1`), and TypeScript returned `TOGGLE_LABEL_TSC_RC=0`. Evidence: `/tmp/dm-toggle-label-fixed.log` and `/tmp/dm-toggle-label-tsc.log`.
- Direct rendered DOM checks returned `TOGGLE_EXTENSION_REAL_RC=0` and `TOGGLE_TRAY_REAL_RETRY_RC=0`: extension labels were `Intercept browser downloads` and `Show media buttons`; tray labels were `Browser Integration` and `Media Buttons`, each retaining `aria-pressed=true`. Evidence: `/tmp/dm-toggle-label-extension-real.log` and `/tmp/dm-toggle-label-tray-real-retry.log`.
- The complete fail-fast gate is captured at `/tmp/dm-toggle-label-full-gate.log` and returned `TOGGLE_LABEL_GATE_RC=0`: TypeScript, full Vitest `22` files/`73` tests, frontend and extension builds, Rust `71/71`, Cargo build, Python compilation, and `git diff --check`.

## 2026-09-07 — Give Network bandwidth choices semantic radio states

- The Network settings bandwidth choices were visually styled buttons without a labelled group or radio state. The focused regression was red at `/tmp/dm-radio-red.log`.
- `src/App.tsx` now exposes a `Global bandwidth limit` `radiogroup`; its `Unlimited` and `Limited to:` choices expose `role="radio"` and `aria-checked` while retaining existing click behavior and styling. The regression remains in `src/settings-feedback.test.tsx`.
- The focused test and TypeScript checks returned `RADIO_FIXED_RC=0` and `RADIO_TSC_RC=0`; the rendered Chromium DOM check returned `RADIO_REAL_RC=0` with one labelled group and `true/false` radio states. Evidence: `/tmp/dm-radio-fixed.log`, `/tmp/dm-radio-tsc.log`, and `/tmp/dm-radio-real.log`.
- The complete fail-fast gate returned `RADIO_GATE_RC=0`: Vitest `22` files/`74` tests, TypeScript, frontend and extension builds, Rust tests/build, Python compilation, and `git diff --check`. Evidence: `/tmp/dm-radio-full-gate.log`.

## 2026-09-07 — Surface live subscription failures

- `NativeAdapter.subscribe()` passed Tauri's promise-returning `listen()` through without a rejection handler. The resident UI could render its initial snapshot and then silently stop receiving live state updates if listener registration failed.
- `DownloadAdapter.subscribe()` now accepts an optional error callback. `useAppSnapshot` routes that callback through the existing disconnected state, and the native adapter reports `listen()` rejection while suppressing it after unmount. Recovery remains possible when a later state event arrives.
- The focused regression was red at `/tmp/dm-subscription-red.log`: the initial snapshot test passed, while the new failure case received no error callback (`1 failed | 1 passed`). The fixed hook test returned `SUBSCRIPTION_FIXED_RC=0` with `2/2`; TypeScript returned `SUBSCRIPTION_TSC_RC=0`.
- The existing trusted Chromium manager-controls probe passed against the changed frontend: `SUBSCRIPTION_MANAGER_REAL_RC=0`, including row actions, bulk controls, sorting, transient-menu dismissal, and Add URL. Evidence: `/tmp/dm-subscription-manager-real.log`.
- The complete fail-fast gate returned `SUBSCRIPTION_GATE_RC=0`: Vitest `22` files/`75` tests, TypeScript, frontend and extension builds, Rust `71/71` tests/build, Python compilation, and `git diff --check`. Evidence: `/tmp/dm-subscription-full-gate.log`.

## 2026-09-07 — Keep manager Add Download open on commit failure

- The manager's manual Add URL path passed commit and cancellation promises to a fire-and-forget helper, then cleared the captured window immediately. A native failure could therefore close the only recovery surface.
- `Manager` now awaits `commitProvisional` and `cancelJob`, closes the captured window only after success, and preserves the existing success notices. Rejections reach `AddDownloadWindow`'s visible `role="alert"` form region, so the user can correct the destination or retry.
- The component regression was red at `/tmp/dm-manager-add-error-red.log`: after `Destination is not writable`, the captured window was `null`. The fixed test returned `MANAGER_ADD_ERROR_FIXED_RC=0` (`1/1`), and TypeScript returned `MANAGER_ADD_ERROR_TSC_RC=0`.
- The existing trusted Chromium manager-controls probe passed after the change with the normal Add URL open/cancel path and all prior row, bulk, sorting, and menu checks: `MANAGER_ADD_ERROR_MANAGER_REAL_RC=0`. Evidence: `/tmp/dm-manager-add-error-manager-real.log`.
- The complete fail-fast gate returned `MANAGER_ADD_ERROR_GATE_RC=0`: Vitest `23` files/`76` tests, TypeScript, frontend and extension builds, Rust `71/71` tests/build, Python compilation, and `git diff --check`. Evidence: `/tmp/dm-manager-add-error-full-gate.log`.

## 2026-09-07 — Give Appearance choices semantic selected states

- Appearance theme cards and accent swatches had visible selection styling but no semantic group or selected state for assistive technology. A real Chromium audit at `/tmp/dm-appearance-audit.log` confirmed the rendered controls had no roles or checked/pressed state.
- `src/App.tsx` now exposes labelled `Theme` and `Accent color` radio groups; every choice has `role="radio"` and `aria-checked` while retaining existing click behavior and styling. The regressions live in `src/settings-feedback.test.tsx`.
- The theme regression was red at `/tmp/dm-theme-radio-red.log` (`1 failed | 2 passed`) and then passed with `THEME_RADIO_FIXED_RC=0`; the accent regression was red at `/tmp/dm-accent-radio-red.log` (`1 failed | 3 passed`) and then passed with `ACCENT_RADIO_FIXED_RC=0`. TypeScript returned `THEME_RADIO_TSC_RC=0` and `ACCENT_RADIO_TSC_RC=0`.
- Real Chromium reports both groups and selected arrays, including `Theme [true,false,false]` and `Accent [true,false,false,false,false,false,false,false]`: `ACCENT_RADIO_REAL_RC=0`. Evidence: `/tmp/dm-accent-radio-real.log` and `/tmp/dm-appearance-accessible.png`.
- The complete fail-fast gate returned `APPEARANCE_GATE_RC=0`: Vitest `23` files/`78` tests, TypeScript, frontend and extension builds, Rust `71/71` tests/build, Python compilation, and `git diff --check`. Evidence: `/tmp/dm-appearance-full-gate.log`.

## 2026-09-07 — Name editable settings fields precisely

- The settings pages rendered generic or missing accessible names for editable controls. The red focused regression returned `SETTINGS_LABELS_RED_RC=1`; it received `Folder path` for both download-folder inputs and an unnamed collision selector. Evidence: `/tmp/dm-settings-labels-red.log`.
- Added setting-specific names to `PathField`, every settings `Select`, the global/connection/retry number inputs, the excluded-media-site input, and the captured Add Download per-download bandwidth selector. The visible settings layout and update behavior are unchanged.
- The focused regression returned `SETTINGS_LABELS_FIXED_RC=0` with `5/5` tests and TypeScript returned `SETTINGS_LABELS_TSC_RC=0`. Evidence: `/tmp/dm-settings-labels-fixed.log` and `/tmp/dm-settings-labels-tsc.log`.
- Real Chromium rendered the exact names on Downloads, Network, Appearance, and Browser routes. Evidence: `/tmp/dm-settings-labels-real-downloads.log`, `/tmp/dm-settings-labels-real-network.log`, `/tmp/dm-settings-labels-real-appearance.log`, and `/tmp/dm-settings-labels-real-browser.log`; screenshots are retained beside those logs.
- The complete fail-fast gate returned `SETTINGS_LABELS_GATE_RC=0`: Vitest `23` files/`79` tests, TypeScript, frontend and extension builds, Rust `71/71` tests/build, Python compilation, and `git diff --check`. Evidence: `/tmp/dm-settings-labels-full-gate.log`.

## 2026-09-07 — Name Add Download controls and disclose Advanced state

- The real mock Chromium Add Download surface showed no explicit accessible names on the URL, filename, destination, or expanded advanced controls. The initial DOM evidence is `/tmp/dm-add-audit-before.log`; the focused regression was red at `/tmp/dm-add-accessibility-red.log` with `[null, null, null]` for the primary inputs.
- Added names for the three primary fields, per-download connection and bandwidth limits, and the per-download bandwidth unit. The Advanced button now exposes `aria-expanded`, and its Global/Limited bandwidth choices are a labelled radio group. Transfer and submission behavior are unchanged.
- The focused regression returned `ADD_ACCESSIBILITY_FIXED_RC=0` (`1/1`) and TypeScript returned `ADD_ACCESSIBILITY_TSC_RC=0`. Evidence: `/tmp/dm-add-accessibility-fixed.log` and `/tmp/dm-add-accessibility-tsc.log`.
- Real Chromium readback returned `advanced=true`, `Source URL`, `Filename`, `Save destination`, `Per-download maximum connections`, `Per-download bandwidth limit`, `Per-download bandwidth unit`, and a two-radio `Per-download bandwidth cap` group. Evidence: `/tmp/dm-add-accessibility-real-fixed.log` and `/tmp/dm-add-accessibility-real-fixed.png`.
- The complete fail-fast gate returned `ADD_ACCESSIBILITY_GATE_RC=0`: Vitest `24` files/`80` tests, TypeScript, frontend and extension builds, Rust `71/71` tests/build, Python compilation, and `git diff --check`. Evidence: `/tmp/dm-add-accessibility-full-gate.log`.

## 2026-09-07 — Identify Download row actions

- Manager row actions exposed only generic names such as `Pause`, `Retry`, and `More actions`, leaving screen-reader users to infer which job each control affected. The focused regression reproduced `Pause` instead of `Pause project-assets.zip` at `/tmp/dm-row-labels-red.log` (`ROW_LABELS_RED_RC=1`).
- Row state actions now say `Pause <job>`, `Resume <job>`, or `Retry <job>`, and overflow actions say `More actions for <job>`. The existing real filter/context probe was updated only to match the new prefix; click routing and menu behavior are unchanged.
- The focused regression returned `ROW_LABELS_FIXED_RC=0` with `2/2` tests across downloading, paused, pending, failed, and completed states. TypeScript and the updated probe compilation returned `ROW_LABELS_TSC_RC=0` and `ROW_LABELS_PROBE_COMPILE_RC=0`. Evidence: `/tmp/dm-row-labels-fixed.log`, `/tmp/dm-row-labels-tsc.log`, and the probe compile command output.
- Real Chromium filter/context verification returned `ROW_LABELS_MANAGER_REAL_RC=0`: all filter counts and selections, keyboard row selection, context-menu targeting, removal, and selection preservation passed. Evidence: `/tmp/dm-row-labels-manager-real.log`.
- The complete fail-fast gate returned `ROW_LABELS_GATE_RC=0`: Vitest `25` files/`82` tests, TypeScript, frontend and extension builds, Rust `71/71` tests/build, Python compilation, and `git diff --check`. Evidence: `/tmp/dm-row-labels-full-gate.log`.

## 2026-09-07 — Make transient menus keyboard-addressable

- Sort, toolbar More, and row context menus had no menu roles or named relationship to their triggers, and Escape did not close them. The focused regression was red at `/tmp/dm-menu-semantics-red.log` (`MENU_SEMANTICS_RED_RC=1`): both reusable menu surfaces reported no `role="menu"`.
- Sort now exposes a named `menu` with checked `menuitemradio` choices. Toolbar More and row context menus expose named `menu` containers and `menuitem` actions. Their triggers expose `aria-haspopup="menu"` and `aria-expanded`; row triggers also report their own open state. The Manager closes all transient menus on Escape.
- The focused regression returned `MENU_SEMANTICS_FIXED_RC=0` (`2/2`) and TypeScript returned `MENU_SEMANTICS_TSC_RC=0`. The existing real manager probe was extended without changing its workflow to verify Sort and toolbar More semantics, context-menu roles and names, trigger state, Escape dismissal, reopening, and the original removal path.
- Real Chromium filter/context verification returned `MENU_MANAGER_REAL_RC=0`; all filter counts/selections, keyboard row selection, context-menu targeting, Escape close/reopen, removal, and selection preservation passed. Evidence: `/tmp/dm-menu-manager-real.log`.
- The complete fail-fast gate returned `MENU_GATE_RC=0`: Vitest `26` files/`84` tests, TypeScript, frontend and extension builds, Rust `71/71` tests/build, Python compilation, and `git diff --check`. Evidence: `/tmp/dm-menu-full-gate.log`.

## 2026-09-07 — Claim explicit downloads before policy readiness

- The first real vertical Add Download audit exposed a reachable readiness race. `fixtures/add_window_chromium_probe.py` initially returned exit `1` because the source server saw `3` requests instead of the expected native request plus the independent browser reference. Temporary request-level logging showed the order: an un-ranged browser request arrived before the native `Range: bytes=0-0` acquisition, followed by the independent reference fetch. The retained Chromium Downloads directory was empty, but the one-use source had already been touched by Chromium before the native handoff. Evidence: `/tmp/dm-e2e-add-window-instrumented.log` and `/tmp/dm-e2e-add-window-keep.log`.
- The cause was in `extension/src/content.ts`: the explicit `<a download>` capture listener was registered only after the asynchronous `get-policy` response. An immediate user click could therefore reach the browser before the listener existed. `extension/src/background.ts` also forwarded ordinary capture before its authoritative policy read completed.
- `content.ts` now installs the capture-phase listeners synchronously. It claims an eligible explicit HTTP download while policy is pending, then lets the worker await policy readiness and return a safe browser fallback when interception is disabled or the page is excluded. `background.ts` now waits for the policy read and rejects excluded-page captures before native forwarding. Modified-click ownership remains policy-gated.
- Added `extension/src/early-capture-readiness.test.ts` and extended `extension/src/background-policy.test.ts`. The readiness regression was red at `EARLY_CAPTURE_RED_RC=1`; the focused fixed run returned `EARLY_CAPTURE_FOCUSED_RC=0` with the pending-policy claim and excluded-page worker checks. TypeScript returned `EARLY_CAPTURE_TSC_RC=0`, and the extension bundle rebuilt successfully. Evidence: `/tmp/dm-early-capture-red.log`, `/tmp/dm-early-capture-focused.log`, `/tmp/dm-early-capture-tsc.log`, and `/tmp/dm-early-capture-extension-build.log`.
- Real browser/native verification passed after rebuilding. Cold launch captured before any resident process and opened the provisional Add window: `EARLY_CAPTURE_COLD_FIXED_RC=0`, with no Chromium Downloads. The explicit browser path reached one provisional native job, independent Add Download commit, one final job, `65536` output bytes, matching SHA-256 `d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a`, and no browser file: `EARLY_CAPTURE_EXPLICIT_FIXED_RC=0`. The unchanged Add Download handoff probe then passed with `EARLY_CAPTURE_REAL_RC=0` and two source requests. Evidence: `/tmp/dm-early-capture-cold-fixed.log`, `/tmp/dm-early-capture-explicit-fixed.log`, and `/tmp/dm-early-capture-real.log`.
- The existing startup range-recovery proof passed with `EARLY_CAPTURE_STARTUP_RECOVERY_RC=0`: persisted `65536` bytes resumed via the exact `bytes=0-0` validator and `bytes=65536-262143` remainder requests, producing the independent SHA-256 `3f1703cb2b1a99b9b700d46a1d2bdfbec74fd50a2fcee3df1070fa6e53e81f87`. Evidence: `/tmp/dm-early-capture-startup-recovery.log`.
- The complete gate returned zero: 27 Vitest files/86 tests, `npx tsc -b`, frontend and extension builds, Rust `71/71` tests, syntax compilation of all 149 Python fixtures without writing bytecode, and `git diff --check`. Evidence: `/tmp/dm-early-capture-full-vitest.log`, `/tmp/dm-early-capture-full-build.log`, `/tmp/dm-early-capture-rust.log`, `/tmp/dm-early-capture-python.log`, and `/tmp/dm-early-capture-diffcheck.log`.
- Cleanup removed the two generated fixture bytecode files and left no disposable fixture cache. The next audit target remains a reachable horizontal v1 gap; the known close-to-tray Inspector boundary was not reopened.

## 2026-09-07 — Lock Add Download lifecycle actions while pending

- The Add Download window's `busy` state disabled only the primary action. Its title-bar Close and secondary Cancel buttons remained active while native create, commit, or cancellation promises were pending. Captured commit also displayed the misleading `Starting…` text. The focused regression was red at `/tmp/dm-add-busy-red.log` with both pending-state tests missing `aria-busy`.
- `src/App.tsx` now tracks the pending action as `create`, `commit`, or `cancel`, exposes `aria-busy`, disables Close/Cancel and the primary action while the promise is pending, and shows truthful `Starting…`, `Adding…`, and `Cancelling…` labels. A ref blocks same-tick re-entry. Synchronous callback compatibility is preserved without rendering a needless transient busy state.
- Added `src/add-window-busy.test.tsx`. The focused Add Download lifecycle set returned `ADD_BUSY_FOCUSED_FINAL_RC=0` with `5` files and `8/8` tests, warning-clean; existing commit, commit-error, cancellation-error, and create-error coverage remained green. Evidence: `/tmp/dm-add-busy-red.log` and `/tmp/dm-add-busy-focused-final.log`.
- Two real-fixture maintenance fixes were required by already-committed contracts: `manager_inline_controls_chromium_probe.py` now matches job-specific row-action labels, and `manual_add_ui_probe.py` waits for the rendered `Add URL` control instead of sampling a transient empty body. Before those changes the manager fixture failed only on stale exact labels, and the manual fixture had an immediate post-bridge timing failure.
- Real verification passed after the final rebuild. The native manual Add URL flow completed through the rendered overlay, provisional acquisition, commit, and output with one source request, `262144` bytes, and SHA-256 `ac6533c30d2d4fcc01be82be68bd63a592d37c49fe769b05aebfb4504fa146b3`: `ADD_BUSY_FINAL_MANUAL_REAL_RETRY2_RC=0`. The mock Chromium manager fixture passed row pause/resume/retry, bulk actions, sorting, menu dismissal, and Add URL: `ADD_BUSY_FINAL_MANAGER_REAL_RC=0`. Evidence: `/tmp/dm-add-busy-final-manual-real-retry2.log` and `/tmp/dm-add-busy-final-manager-real.log`.
- One intervening manual probe retry stopped before product interaction at the known WebKit Inspector connection-refused boundary (`/tmp/dm-add-busy-final-manual-real-retry.log`); it is not acceptance evidence. The stabilized rerun above passed.
- The complete final gate returned zero: 28 Vitest files/88 tests with no warnings, frontend and extension builds, TypeScript, Rust `71/71` tests, Cargo build, syntax compilation of all 149 Python fixtures without bytecode, and `git diff --check`. Evidence: `/tmp/dm-add-busy-gate-vitest.log`, `/tmp/dm-add-busy-gate-build.log`, `/tmp/dm-add-busy-gate-rust-test.log`, `/tmp/dm-add-busy-gate-cargo-build.log`, `/tmp/dm-add-busy-gate-python.log`, and `/tmp/dm-add-busy-gate-diffcheck.log`.
- No fixture bytecode or disposable Vite process remains. This slice is ready for its durable commit; the v1 mission remains open and the next pass must select another reachable horizontal gap rather than revisit green capture or menu work.

## 2026-09-07 — Gate browser fallback on the resolved policy

- The fallback `chrome.downloads.onDeterminingFilename` listener read `policy` synchronously while startup storage loading was still pending. A browser download arriving in that window could be sent to native before the stored policy was known; it also ignored an excluded referrer. The focused delayed-policy regression reproduced the native call before policy resolution and failed with `FALLBACK_POLICY_RED_VALID_RC=1` in `/tmp/dm-fallback-policy-red-valid.log`.
- `extension/src/background.ts` now centralizes ordinary-capture validation in `ordinaryCaptureError()`. Both the explicit `ordinary-capture` message path and the observe-only browser fallback await `policyReady`, then apply the same interception, excluded-site, and HTTP-source checks. Disabled or excluded browser downloads are released back to Chromium without a native capture. The fallback uses `finalUrl || url` consistently for redirected downloads.
- The listener now wraps policy wait, validation, and native forwarding in one `try/finally`, so Chrome's `onDeterminingFilename` contract receives exactly one `suggest()` even when the asynchronous bridge fails. `sendNative()` also catches synchronous bridge throws as well as rejected native-messaging promises, preserving the existing `{ok:false,error}` result contract and avoiding unhandled rejections.
- `extension/src/background-policy.test.ts` now covers delayed policy plus an excluded referrer, and the synchronous native-bridge exception boundary. The latter was intentionally red first (`FALLBACK_POLICY_CRITIQUE_RED_RC=1`, zero suggestions plus an unhandled rejection) in `/tmp/dm-fallback-policy-critique-red.log`; the final focused run returned `FALLBACK_POLICY_FOCUSED_FINAL3_RC=0`, with 1 file and 5/5 tests in `/tmp/dm-fallback-policy-focused-final3.log`.
- The smallest real Chromium/native check, `fixtures/excluded_site_chromium_probe.py`, passed against the final source with `FALLBACK_POLICY_EXCLUDED_REAL_FINAL_RC=0`. It set `excludedSites=["127.0.0.1"]`, clicked the local download from that referrer, produced exactly one browser-owned `65536`-byte file with SHA-256 `d0fb80b239a23260482afa5bc8360bcabe5604b5f50a93078c6e5c99a0b0e53a`, one source request, and zero native jobs. Evidence: `/tmp/dm-fallback-policy-excluded-real-final.log`.
- The final aggregate gate returned zero: Vitest `28` files/`90` tests, `npx tsc -b`, frontend and extension production builds, Rust `71/71` tests from `src-tauri/`, Cargo build, syntax compilation of all `149` Python fixtures without bytecode, and `git diff --check`. Evidence: `/tmp/dm-fallback-policy-full-vitest-final.log`, `/tmp/dm-fallback-policy-full-tsc-final.log`, `/tmp/dm-fallback-policy-full-build-final.log`, `/tmp/dm-fallback-policy-full-rust-test-final.log`, `/tmp/dm-fallback-policy-full-cargo-build-final.log`, `/tmp/dm-fallback-policy-full-python-final.log`, and `/tmp/dm-fallback-policy-full-diffcheck-final.log`.
- Cleanup removed the pre-existing generated `fixtures/__pycache__/` files and confirmed `FIXTURE_CACHE_EXISTS=0`. No other fixture or application process was retained. This closes the browser-fallback policy subsystem provisionally; the v1 mission remains open and the next pass must move to another reachable horizontal gap.

## 2026-09-07 — Surface synchronous live-subscription setup failures

- `useAppSnapshot()` handled a rejected asynchronous `subscribe()` setup through its error callback, but a synchronous exception thrown by `adapter.subscribe()` escaped the React effect during commit. The focused regression reproduced the effect crash at `SUBSCRIPTION_SYNC_RED_RC=1`; evidence: `/tmp/dm-subscription-sync-red.log`.
- `src/App.tsx` now initializes a no-op disposer, wraps subscription setup in `try/catch`, reports synchronous failures through the existing disconnected-state error path, and still requests the initial snapshot. Existing asynchronous rejection and subscription-vs-snapshot ordering behavior remain unchanged.
- `src/snapshot-race.test.tsx` now covers the synchronous setup boundary. The focused suite returned `SUBSCRIPTION_SYNC_FIXED_RC=0`: 1 file and 3/3 tests. Evidence: `/tmp/dm-subscription-sync-fixed.log`.
- The real Chromium manager proof passed after the change with `SUBSCRIPTION_SYNC_MANAGER_REAL_RC=0`. It exercised all existing trusted UI paths: inline pause/resume/retry, bulk actions, sorting, transient menu dismissal, and Add URL overlay. Evidence: `/tmp/dm-subscription-sync-manager-real.log`.
- The complete final gate returned zero: Vitest `28` files/`91` tests, `npx tsc -b`, frontend and extension production builds, Rust `71/71` tests from `src-tauri/`, Cargo build, syntax compilation of all `149` Python fixtures without bytecode, and `git diff --check`. Evidence: `/tmp/dm-subscription-sync-full-vitest.log`, `/tmp/dm-subscription-sync-full-tsc.log`, `/tmp/dm-subscription-sync-full-build.log`, `/tmp/dm-subscription-sync-full-rust-test.log`, `/tmp/dm-subscription-sync-full-cargo-build.log`, `/tmp/dm-subscription-sync-full-python.log`, and `/tmp/dm-subscription-sync-full-diffcheck.log`.
- Cleanup confirmed `FIXTURE_CACHE_EXISTS=0` and stopped the disposable mock Vite server on port `4173`; no Chromium fixture process remained. This closes the synchronous subscription setup seam provisionally; the v1 mission remains open and the next pass must select another reachable horizontal gap.

## 2026-09-07 — Report tray-surface action failures

- `TrayMenu` launched settings toggles and Pause/Resume All as fire-and-forget promises. A rejected settings update left the tray surface silent; a synchronous adapter throw escaped the click handler. The focused regression was red at `TRAY_FEEDBACK_RED2_RC=1` with no alert and an uncaught `tray bridge unavailable`; evidence: `/tmp/dm-tray-feedback-red2.log`.
- `src/App.tsx` now routes tray settings and bulk actions through one guarded operation helper. It catches synchronous throws and rejected promises, clears a stale error on a new attempt, and renders the existing error styling as an accessible `role="alert"`. Native tray menu wiring is unchanged.
- Added `src/tray-feedback.test.tsx`. The fixed focused run returned `TRAY_FEEDBACK_FIXED_RC=0` with both rejection and synchronous-throw tests passing (`2/2`): `/tmp/dm-tray-feedback-fixed.log`.
- A disposable real Chromium probe rendered `http://127.0.0.1:4173/?view=tray`, drove both trusted toggles, observed `true → false` state updates, found all six expected action labels, and found zero alerts on the successful path. It returned `TRAY_FEEDBACK_REAL_RC=0`; evidence: `/tmp/dm-tray-feedback-real.log`. The disposable Vite server, profile, HOME, and Xvfb were removed afterward.
- The complete final gate returned zero: Vitest `29` files/`93` tests, `npx tsc -b`, frontend and extension production builds, Rust `71/71` tests from `src-tauri/`, Cargo build, syntax compilation of all `149` Python fixtures without bytecode, and `git diff --check`. Evidence: `/tmp/dm-tray-feedback-full-vitest.log`, `/tmp/dm-tray-feedback-full-tsc.log`, `/tmp/dm-tray-feedback-full-build.log`, `/tmp/dm-tray-feedback-full-rust-test.log`, `/tmp/dm-tray-feedback-full-cargo-build.log`, `/tmp/dm-tray-feedback-full-python.log`, and `/tmp/dm-tray-feedback-full-diffcheck.log`.
- Cleanup confirmed `TRAY_FEEDBACK_VITE_CLOSED_RC=7`, `FIXTURE_CACHE_EXISTS=0`, and no retained disposable Chromium/process state. This closes tray-surface action feedback provisionally; the v1 mission remains open and the next pass must choose another reachable horizontal gap.

## 2026-09-07 — Enforce media-capture policy at the worker boundary

- Media messages previously forwarded immediately while the background policy read was pending, and they did not enforce `showMediaButtons` or excluded-site policy at the authoritative worker boundary. This could let a stale media control bypass a saved browser policy; ordinary interception must remain independent.
- `extension/src/background.ts` now awaits `policyReady` before media handling and applies `mediaCapturePolicyError()` for the media-button toggle and page-site exclusion before resolving player sources or forwarding to native. `interceptDownloads=false` does not disable an allowed media capture.
- The focused regression first reproduced the missing enforcement as `MEDIA_POLICY_RED_RC=1`. The final background-policy suite passed `7/7`, including delayed readiness, excluded media, disabled media buttons, ordinary/media independence, and the existing fallback error paths: `/tmp/dm-media-policy-focused-final.log`.
- The rebuilt extension passed `MEDIA_POLICY_EXTENSION_BUILD_RC=0` and the real direct-media Chromium/native proof passed after that rebuild: one managed media job, one committed output, `3258782` bytes, matching SHA-256 `d56958121ee39c27731a7c96a3b1be04f750cbb1fd43dc0f52dcca47b9280235`, and an empty Chromium Downloads directory. Evidence: `/tmp/dm-media-policy-direct-ogg-final.log`.
- The complete aggregate gate returned zero: Vitest `29` files/`95` tests, `npx tsc -b`, frontend and extension production builds, Rust `71/71` tests from `src-tauri/`, Cargo build, syntax compilation of all `149` Python fixtures, and `git diff --check`. Evidence: `/tmp/dm-media-gate-vitest.log`, `/tmp/dm-media-gate-tsc.log`, `/tmp/dm-media-gate-build-all.log`, `/tmp/dm-media-gate-cargo-test.log`, `/tmp/dm-media-gate-cargo-build.log`, `/tmp/dm-media-gate-python.log`, and `/tmp/dm-media-gate-diff-check.log`.
- Cleanup found no `fixtures/__pycache__/` files and no retained Chromium/native fixture process. This closes generalized media-capture policy enforcement provisionally; the v1 mission remains open and the next pass must move to another reachable horizontal gap without expanding provider resolvers.

## 2026-09-07 — Route manager action failures through one boundary

- Inline row and context-menu actions passed adapter promises into handlers before the click handler ran. A synchronous native/IPC throw therefore escaped React as an unhandled event error, with no visible feedback. The red regression captured this as `ACTION_ERROR_RED_RC=1` and zero notice calls: `/tmp/dm-action-error-red.log`.
- `src/App.tsx` now uses `runAction()` to defer every Manager toolbar, inline row, and context-menu adapter action into a promise boundary. Both synchronous throws and asynchronous rejections become the existing visible error notice; successful context actions keep their prior success message and close behavior.
- `src/action-error-boundary.test.tsx` covers synchronous context retry, asynchronous context retry, and synchronous inline row pause. The focused boundary/context/menu suite passed `6/6`: `/tmp/dm-action-error-fixed-2.log`.
- The real Chromium/native manager probe passed after starting its required disposable Vite server: 14 rows loaded; inline pause/resume/retry, Pause All/Resume All, sorting, menu dismissal, Add URL overlay, and native snapshot transitions all passed. Evidence: `/tmp/dm-action-error-manager-real-retry.log`.
- The complete aggregate gate returned zero: Vitest `30` files/`98` tests, `npx tsc -b`, frontend and extension production builds, Rust `71/71` tests from `src-tauri/`, Cargo build, syntax compilation of all `149` Python fixtures, and `git diff --check`. Evidence: `/tmp/dm-action-error-gate-vitest.log`, `/tmp/dm-action-error-gate-tsc.log`, `/tmp/dm-action-error-gate-build.log`, `/tmp/dm-action-error-gate-cargo-test.log`, `/tmp/dm-action-error-gate-cargo-build.log`, `/tmp/dm-action-error-gate-python.log`, and `/tmp/dm-action-error-gate-diff.log`.
- Cleanup stopped the disposable Vite server and probe, closed port `4173`, left no repository `__pycache__`, and left only the intended source/test changes. This closes the shared Manager action-error boundary provisionally.

## 2026-09-07 — Preserve POST context during source reattachment

- The targeted reattach branch refreshed source, selected media segments, referrer, and User-Agent but dropped the renewed `postBody`; the red native probe completed with `postBody: None` despite carrying `fixture=reattach-renewed`: `/tmp/dm-reattach-post-red.log` (`REATTACH_POST_RED_RC=1`).
- `src-tauri/src/main.rs` now copies `input.post_body` into the existing job when a compatible source is reattached. `fixtures/reattach_probe.py` accepts the optional captured body, and `fixtures/reattach_scope_probe.py` asserts it survives the renewed one-job flow.
- The real isolated native reattach probe passed: incompatible source remained isolated, compatible renewal reused `reattach-targeted-scope`, the body assertion passed, and the output hash was `3f1703cb2b1a99b9b700d46a1d2bdfbec74fd50a2fcee3df1070fa6e53e81f87`: `/tmp/dm-reattach-post-green.log`.
- The post-fix aggregate gate passed: Vitest `30` files/`98` tests, TypeScript, frontend and extension builds, Rust `71/71`, Cargo build, all `149` Python fixtures, and diff check. Evidence: `/tmp/dm-reattach-post-gate-vitest.log`, `/tmp/dm-reattach-post-gate-tsc.log`, `/tmp/dm-reattach-post-gate-build.log`, `/tmp/dm-reattach-post-gate-cargo-test.log`, `/tmp/dm-reattach-post-gate-cargo-build.log`, `/tmp/dm-reattach-post-gate-python.log`, and `/tmp/dm-reattach-post-gate-diff.log`.
- Cleanup verification found no repository `__pycache__` and no active reattach probe/Xvfb residue. This closes request-context replacement during reattach provisionally; arbitrary POST replay remains outside the bounded urlencoded contract.

## 2026-09-07 — Make snapshot replacement atomic

- `save_snapshot()` previously updated settings, deleted every job, and ignored each replacement error as separate SQLite statements. A duplicate job ID reproduced the data-loss window: the old durable row disappeared and the focused test ended with `QueryReturnedNoRows` (`/tmp/dm-snapshot-atomic-red-real.log`).
- The first interrupted rerun used `--exact` with an incomplete test name and executed zero tests; it is not evidence. The corrected focused command ran one test and reproduced the expected failure before the production change.
- `save_snapshot()` now serializes the complete snapshot before opening SQLite, performs the settings replacement, job delete, and all job inserts inside one transaction, and returns contextual errors for lock, serialization, SQL, and commit failures. `emit_snapshot()` reports persistence failure and does not emit an optimistic state event; startup propagates a failed initial save through Tauri setup.
- The regression `snapshot_persistence_is_atomic_when_job_write_fails` now requires an explicit error and verifies that both the previous job row and previous settings row survive a failed replacement. The focused fixed run returned `SNAPSHOT_ATOMIC_GREEN_FINAL_RC=0` with `1/1` test and no warnings: `/tmp/dm-snapshot-atomic-green-final.log`.
- Rebuilt native verification passed: `startup_recovery_probe.py` preserved and resumed a committed partial job byte-identically (`STARTUP-AUTO-RECOVERY`, hash `3f1703cb2b1a99b9b700d46a1d2bdfbec74fd50a2fcee3df1070fa6e53e81f87`); `settings_ui_persistence_probe.py` saved and restored dark theme, accent `#d3138c`, and compact density across restart; and the strengthened simultaneous-capture probe produced three distinct provisional jobs with six range requests. Evidence: `/tmp/dm-snapshot-startup-recovery.log`, `/tmp/dm-snapshot-settings-persistence.log`, and `/tmp/dm-snapshot-three-capture.log`.
- The final aggregate gate returned zero: Vitest `31` files/`100` tests, `npx tsc -b`, frontend and extension production builds, Rust `72/72` tests, Cargo build, syntax compilation of all `149` Python fixtures, and `git diff --check`. Evidence: `/tmp/dm-snapshot-gate-vitest.log`, `/tmp/dm-snapshot-gate-tsc.log`, `/tmp/dm-snapshot-gate-build-all.log`, `/tmp/dm-snapshot-gate-cargo-test-final.log`, `/tmp/dm-snapshot-gate-cargo-build-final.log`, `/tmp/dm-snapshot-gate-python.log`, and `/tmp/dm-snapshot-gate-diff.log`. Generated fixture bytecode was removed and no Download Manager, Xvfb, or fixture process remained. This closes snapshot atomicity and the three-capture audit provisionally; the v1 mission remains open and the next pass must choose another reachable horizontal gap.

## 2026-09-07 — Preserve native error messages across UI surfaces

- Tauri 2 documents that a command returning `Result<T, String>` rejects the frontend `invoke()` promise with a serializable error; the native command layer uses this exact pattern. Source: https://v2.tauri.app/develop/calling-rust/.
- The UI previously preserved only JavaScript `Error` instances. A native `String` rejection therefore became a generic message such as `Action failed`; the focused red regression reproduced that exact loss at `/tmp/dm-native-error-message-red.log` (`NATIVE_ERROR_MESSAGE_RED_RC=1`).
- `src/App.tsx` now has one `errorMessage()` normalizer that preserves `Error.message`, non-empty string rejections, and serialized `{message}` values. Existing snapshot, manual Add Download, manager action, inspector, settings, Add Download, and tray catches use it. The in-app Extension Popup now catches synchronous/asynchronous policy-update failures and renders an accessible `role="alert"` without changing its controls or policy semantics.
- The focused shared/action and popup regressions passed `5/5`: `/tmp/dm-popup-feedback-green.log` (`POPUP_FEEDBACK_GREEN_RC=0`). The existing real Chromium manager workflow passed after the change (`NATIVE_ERROR_MESSAGE_MANAGER_REAL_RC=0`), and the existing extension-popup workflow passed exclusion, re-enable, Open Manager, and 14-row checks (`POPUP_FEEDBACK_REAL_RC=0`). Evidence: `/tmp/dm-native-error-message-manager-real.log` and `/tmp/dm-popup-feedback-real.log`.
- The final aggregate gate returned zero: Vitest `31` files/`100` tests, TypeScript, frontend and extension builds, Rust `72/72` tests, Cargo build, all `149` Python fixtures, and `git diff --check`. Evidence: `/tmp/dm-native-error-gate-vitest.log`, `/tmp/dm-native-error-gate-tsc.log`, `/tmp/dm-native-error-gate-build.log`, `/tmp/dm-native-error-gate-cargo-test.log`, `/tmp/dm-native-error-gate-cargo-build.log`, and `/tmp/dm-native-error-gate-diff.log`. Fixture bytecode was removed and the task-owned Vite server/port `4173` was closed. This closes native error-message normalization and popup feedback provisionally; the v1 mission remains open.
