/**
 * Trace-harness event vocabulary (branch `harness/capture-traces` only).
 *
 * This module holds types and constants exclusively so every context (content
 * script, page bridge, service worker, popup) can import it without creating a
 * shared runtime chunk — a classic content script cannot load ES module
 * chunks. `import type` is erased at build time.
 *
 * Every record the harness writes carries a stable `kind`. The analysis side
 * (see extension/HARNESS.md) groups records into three phases the owner named:
 *
 *   recognition  — what the page exposes and how it obfuscates it
 *                  (kinds: `dom.snapshot`, `dom.census`, `button.*`, `player.state`)
 *   acquirement  — what the capture pipeline decided and what the network
 *                  said back (kinds: `capture.click`, `capture.evidence`,
 *                  `capture.decision`, `capture.handoff`, `probe.*`)
 *   handoff      — what the resident received (folded into `capture.handoff`
 *                  plus the app's reply, and later observed terminal states)
 */

export const TRACE_PREFIX = 'dm-trace-';
export const TRACE_META_KEY = 'dm-trace-meta';
export const TRACE_CHUNK_PREFIX = 'dm-trace-chunk-';
export const TRACE_ENABLED_KEY = 'dm-trace-enabled';
export const TRACE_SCHEMA_VERSION = 1;

/** Message types the harness adds on top of the product protocol. */
export const TRACE_RECORD = 'trace-record';
export const TRACE_STATUS = 'trace-status';
export const TRACE_FLUSH = 'trace-flush';
export const TRACE_CLEAR = 'trace-clear';
export const TRACE_SET_ENABLED = 'trace-set-enabled';
export const TRACE_PROBE_EXTENSION = 'trace-probe-extension';
export const TRACE_PROBE_PAGE = 'dm-trace-probe';
export const TRACE_PROBE_PAGE_RESPONSE = 'dm-trace-probe-response';

export type TracePhase = 'recognition' | 'acquirement' | 'handoff';

export interface TraceMeta {
  schemaVersion: number;
  startedAt: number;
  updatedAt: number;
  events: number;
  bytes: number;
  chunks: number;
  enabled: boolean;
  swStarts: number;
}

export interface TraceEnvelope {
  /** wall-clock ms at record time */
  t: number;
  /** monotonic ms since the content-script/page-bridge world started, when known */
  mono?: number;
  kind: string;
  phase: TracePhase;
  /** service worker instance — changes whenever the background was restarted,
   *  which is itself evidence for the traffic-buffer lifetime analysis */
  sw: string;
  swStartedAt: number;
  pageUrl?: string;
  tabId?: number;
  frameId?: number;
  playerKey?: string;
  mediaIdentity?: string;
  payload: Record<string, unknown>;
}

/** Bounded structural snapshot of a DOM subtree. */
export interface SnapshotNode {
  tag: string;
  /** depth first tag path, e.g. `video>source` — the node's identity in a diff */
  path: string;
  id?: string;
  cls?: string[];
  attrs?: Record<string, string>;
  text?: string;
  shadow?: 'open' | 'closed';
  /** children the snapshot kept, vs. how many the element really has */
  kids: number;
  children: SnapshotNode[];
}

export interface PlayerStateSummary {
  tag: string;
  paused: boolean;
  ended: boolean;
  muted: boolean;
  volume: number;
  readyState: number;
  networkState: number;
  currentTime: number;
  duration: number | null;
  playbackRate: number;
  currentSrcKind: 'http' | 'blob' | 'other' | 'none';
  currentSrc: string;
  srcAttr?: string;
  sourceChildren?: string[];
  poster?: string;
  inShadowRoot: boolean;
  /** whether the element has an open shadow root of its own */
  ownShadow: 'open' | 'closed' | 'none';
  usesMediaSource: boolean;
}

export interface WrapperSummary {
  tag: string;
  id?: string;
  cls?: string[];
  attrs?: Record<string, string>;
  rect: { w: number; h: number };
  text?: string;
}

export interface ElementCensusEntry {
  key: string;
  playerKey?: string;
  state: PlayerStateSummary;
  rect: { x: number; y: number; w: number; h: number };
  visible: boolean;
  attached: boolean;
  anchor: { x: number; y: number; w: number; h: number };
  wrappers: WrapperSummary[];
}

export interface ProbeManifestSummary {
  format: 'hls' | 'dash';
  variants: number;
  segments: number;
  firstChild?: string;
  live?: boolean;
}

export interface ProbeResult {
  mode: 'page' | 'extension-omit' | 'extension-include';
  url: string;
  t: number;
  durationMs: number;
  ok: boolean;
  /** whether the probe sent a Range header (`ranged`) or a plain GET (`plain`) */
  requestRange?: 'ranged' | 'plain';
  status?: number;
  statusText?: string;
  finalUrl?: string;
  redirected?: boolean;
  contentType?: string;
  contentLength?: string;
  contentRange?: string;
  acceptRanges?: string;
  cacheControl?: string;
  server?: string;
  vary?: string;
  bytesRead?: number;
  sniff?: string;
  bodyKind?: 'html' | 'hls' | 'dash' | 'mp4' | 'mpegts' | 'json' | 'text' | 'binary' | 'empty';
  manifest?: ProbeManifestSummary;
  firstChild?: ProbeResult;
  error?: string;
}
