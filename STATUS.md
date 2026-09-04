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
