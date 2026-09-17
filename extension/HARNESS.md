# Capture trace harness (branch `harness/capture-traces`)

A passive, always-on recorder for the media-capture pipeline. Load this branch's extension, browse
normally, click the media buttons as usual — the harness writes a trace you can export as JSONL and
analyze offline. Nothing here changes capture behaviour; every hook is best-effort.

Goal of the data: decide the final architecture for **recognition** (what a player exposes and how
providers obfuscate it), **acquirement** (how a source is resolved and what the network actually
answers) and **handoff** (what the resident receives and what it does with it). See
`AUDIT-LIVE-RUN-2026-09-17.md` for the live-run defects this harness exists to explain.

## Build and load

```
npm run build:extension          # writes extension/dist/ (gitignored)
```

Then in `chrome://extensions`: enable Developer mode, keep the existing unpacked entry (or load
unpacked fresh), press **Reload** on it. The popup gains a **Capture traces** section.

Recording is **on by default**. Chrome keeps the store in `chrome.storage.local` (the branch adds
`unlimitedStorage`); events survive service-worker restarts and browser restarts until you press
**Clear**.

## Using it during a session

1. Open the pages you want to test. Scroll, hover, play, pause — the recorder streams DOM structure
   and player state changes as they happen.
2. Click the media button exactly as a user would. The click, the evidence exchange, the capture
   decision, the handoff payload + app reply, and the source probes are all recorded.
3. Watch the counters in the popup (`N events · X KB · worker restarts: n`).
4. When done: **Export traces** → `dm-trace-<timestamp>.jsonl` lands in your downloads folder.
   Keep the file out of the repo (it contains signed URLs and page content samples).
   **Pause tracing** stops recording without losing the buffer; **Clear** resets it.

## What is recorded

Every line is one JSON object: `{ t, kind, phase, sw, swStartedAt, pageUrl?, tabId?, frameId?,
playerKey?, mediaIdentity?, payload }`. `sw` changes whenever the service worker restarted, which
is itself evidence (the traffic ring is in-memory and dies with it).

### recognition

| kind | when | payload highlights |
| --- | --- | --- |
| `dom.census` | every ~2 s while media buttons are active, when the summary changed | every `video`/`audio` on the page (up to 24) with `state` (paused/readyState/currentSrc kind, blob vs http, `usesMediaSource`, shadow-root flags), geometry, `visible`, and the `wrappers` chain (tag/id/classes/attrs/rect) |
| `dom.media-tree` | when the media element's subtree **changes** (hash-deduplicated; plus a 60 s heartbeat) | bounded structural `tree` (≤320 nodes, classes/attrs/URL shape only, style numbers and URL tokens normalized), `changedPaths` diff vs the previous snapshot, `rateLimited` after 120 snapshots on one element |
| `dom.wrapper-tree` | same rule, for the nearest player wrapper (`[class*=player]`, `video-js`, `.plyr`, `html5-video-player`, …) | ≤150 nodes — this is where players hide the interesting markup |
| `button.attach` / `button.detach` / `button.unavailable` | button lifecycle | label, hovered/playing, `mediaFirstSeenAgoMs`, anchor rect, player state, wrappers |

### acquirement

| kind | when | payload highlights |
| --- | --- | --- |
| `capture.click` | media button clicked, before anything else | **`buttonAgeMs`**, `mediaFirstSeenAgoMs`, hovered/playing/visible, pointer, currentSrc and its kind, player state, wrappers |
| `capture.evidence` | page-bridge evidence reply (or timeout) | `durationMs`, the full evidence object (`source`, `sourceIdentity`, `selectedSegments` hints, manifest match) or `found:false` |
| `capture.decision` | the worker's decision, recorded at every exit | `result`: `policy-error` / `no-source` / `filtered` / (handoff follows); `kindInfo` (expected/decided/url kinds, role, and whether the kind guard cleared the source); for the fallback path: `solePlayingPlayer`, and full `mediaBuffer` (count, newest ages, per-entry role/kind/age/document+playerKey match) and `playerBuffer` (per-player playing/hovered/visible/age) snapshots |
| `probe.page` | after a successful handoff | the source fetched **from the page's own context** (cookies + natural Referer): `role=primary` ranged with one-level manifest child, then `role=primary-plain` with **no Range header**. `elapsedMs`/`answered` distinguish a slow answer from a silent bridge |
| `probe.extension` | after a successful handoff | the same URL fetched **from the service worker**: primary `omit+ranged`, primary `omit+plain`, primary `include+ranged`, then up to 2 candidates (`omit+ranged`). `requestRange` says which lens a row belongs to. A ranged 403 next to a plain 200 is the signature for CDNs that sign a byte range into the URL itself |

### handoff

| kind | when | payload highlights |
| --- | --- | --- |
| `capture.handoff.worker` | app reply received | `durationMs`, `ok`, app error, and the full outbound payload (source, selectedSegments, candidates, companionAudio, name) |
| `capture.handoff.content` | content script got the worker's reply | `ok`, error, `submittedSource`, `fromEvidence` |

The app's *terminal* outcome (403 after N retries, truncated-track failure) happens after the
handoff and is not polled by the harness — it is visible in the manager while you test, and the
probes usually explain it (a page probe that succeeds where the extension-omit probe 403s is the
cookie story; a 62-byte `200 video/mp4` is the truncated-track story).

## Volume and safety

* Trees are hash-deduplicated: unchanged structure is never re-sent, only a 60 s heartbeat.
* Snapshot rate is capped per element (one per 400 ms) and after 120 snapshots the payload drops
  the tree and keeps hashes/diffs.
* Census re-sends only when its summary changes (or a 60 s heartbeat).
* Event records are clamped to ~400 KB; oversized payloads drop the tree/census fields and say so.
* Probes are bounded (≤64 KiB read, 8 s timeout, ≤6 fetches per click, 30 s per-URL dedupe) and run
  only after the capture handoff has been answered — they never delay a capture. They DO appear in
  the page's own network activity; the bridge issues them with the native fetch so the page-side
  observer does not mistake them for player traffic.
* Hooks are guarded; a tracing failure cannot break capture. `chrome.runtime.onMessage` is
  optional-chained so trimmed runtimes (tests) stay inert.

## Known limits

* Closed shadow roots are invisible to the snapshotter (open ones are recorded).
* Frames record separately (`frameId` distinguishes them); a player inside a cross-origin iframe
  gets its own `playerKey` namespace within that frame.
* Blob URL tokens rotate per session; the census records them verbatim, so `currentSrcKind` and the
  blob string are meaningful within a trace, not across sessions.
* Trace files contain full URLs including one-use tokens and page snippets. They are local
  diagnostics: don't commit them, and prefer exporting to a scratch folder you will delete.

## What the analysis will look for

1. **Recognition coverage per provider**: which players expose a usable `currentSrc`/source chain
   and which only ever show up as tab traffic — from `dom.*` + `capture.evidence`.
2. **The reload window**: `capture.decision` records carry the buffer ages and the sole-player
   verdict at click time — the difference between a failed click and a successful one is visible
   line by line, including `sw` restarts.
3. **Handoff fidelity**: the probe triple (page / extension-omit / extension-include) plus the
   outbound payload shows whether a failure is cookies, referer, quality/variant choice, or the
   URL itself.
4. **How providers obfuscate**: wrapper trees and census snapshots over time (class churn, shadow
   DOM, MSE usage, blob rotation) become the input to a generic recognition layer instead of
   per-site rules.
