/**
 * Trace store + network classification (branch `harness/capture-traces` only).
 *
 * Imported by `background.ts` and `popup.ts` — both module contexts, so the
 * bundler may emit this as a shared chunk safely. Never imported by a classic
 * content script or the page bridge.
 *
 * Records live in `chrome.storage.local` as append-only chunks so they survive
 * service-worker restarts (the very thing the harness is meant to observe) and
 * so the popup can export them without a message-size limit.
 */
import type { ProbeManifestSummary, ProbeResult, TraceEnvelope, TraceMeta } from './trace-schema';
import { TRACE_CHUNK_PREFIX, TRACE_ENABLED_KEY, TRACE_META_KEY, TRACE_SCHEMA_VERSION } from './trace-schema';

export const TRACE_MAX_CHUNK_EVENTS = 250;
export const TRACE_MAX_CHUNK_BYTES = 2_000_000;
export const TRACE_EVENT_BYTES = 400_000;

export const swInstance: string = (() => {
  try {
    return crypto.randomUUID();
  } catch {
    return `sw-${Math.random().toString(36).slice(2)}`;
  }
})();
export const swStartedAt = Date.now();

let enabled = true;
let meta: TraceMeta = {
  schemaVersion: TRACE_SCHEMA_VERSION,
  startedAt: Date.now(),
  updatedAt: 0,
  events: 0,
  bytes: 0,
  chunks: 0,
  enabled: true,
  swStarts: 0,
};
let metaReady: Promise<void> | null = null;
const pending: TraceEnvelope[] = [];
let flushTimer: number | null = null;
let flushInFlight: Promise<void> | null = null;
let sessionCounted = false;

async function ensureMeta(): Promise<void> {
  if (!metaReady) {
    metaReady = (async () => {
      try {
        const stored = (await chrome.storage.local.get([TRACE_META_KEY, TRACE_ENABLED_KEY])) as Record<string, unknown>;
        const saved = stored[TRACE_META_KEY] as Partial<TraceMeta> | undefined;
        if (saved && saved.schemaVersion === TRACE_SCHEMA_VERSION) {
          meta = {
            schemaVersion: TRACE_SCHEMA_VERSION,
            startedAt: saved.startedAt ?? Date.now(),
            updatedAt: saved.updatedAt ?? 0,
            events: saved.events ?? 0,
            bytes: saved.bytes ?? 0,
            chunks: saved.chunks ?? 0,
            enabled: saved.enabled !== false,
            swStarts: saved.swStarts ?? 0,
          };
        }
        const flag = stored[TRACE_ENABLED_KEY];
        enabled = typeof flag === 'boolean' ? flag : meta.enabled !== false;
      } catch {
        // Storage unavailable; keep in-memory defaults.
      }
    })();
  }
  await metaReady;
}

function sizeOf(envelope: TraceEnvelope): number {
  try {
    return JSON.stringify(envelope).length;
  } catch {
    return 0;
  }
}

function boundEnvelope(envelope: TraceEnvelope): TraceEnvelope {
  if (sizeOf(envelope) <= TRACE_EVENT_BYTES) return envelope;
  const copy: TraceEnvelope = { ...envelope, payload: { ...envelope.payload } };
  const payload = copy.payload as Record<string, unknown>;
  for (const key of ['tree', 'census', 'wrappers', 'snapshot', 'diff']) {
    if (key in payload) {
      delete payload[key];
      payload[`${key}Dropped`] = 'oversized';
    }
  }
  let guard = 0;
  while (sizeOf(copy) > TRACE_EVENT_BYTES && guard < 40) {
    const firstKey = Object.keys(payload).find((key) => typeof payload[key] === 'string' && (payload[key] as string).length > 400);
    if (!firstKey) break;
    payload[firstKey] = `${(payload[firstKey] as string).slice(0, 300)}…`;
    guard += 1;
  }
  return copy;
}

async function writePending(): Promise<void> {
  if (!enabled || pending.length === 0) return;
  await ensureMeta();
  const batch = pending.splice(0, pending.length).map(boundEnvelope);
  if (!sessionCounted) {
    sessionCounted = true;
    meta.swStarts += 1;
  }
  const serialized = JSON.stringify(batch);
  const key = `${TRACE_CHUNK_PREFIX}${String(meta.chunks).padStart(4, '0')}`;
  meta = {
    ...meta,
    updatedAt: Date.now(),
    events: meta.events + batch.length,
    bytes: meta.bytes + serialized.length,
    chunks: meta.chunks + 1,
    enabled,
  };
  try {
    await chrome.storage.local.set({ [key]: { at: Date.now(), events: batch }, [TRACE_META_KEY]: meta });
  } catch {
    // Storage full: keep the events in memory and retry on the next flush.
    pending.unshift(...batch);
  }
}

export function flushTrace(): Promise<void> {
  if (flushInFlight) return flushInFlight;
  if (flushTimer !== null) {
    clearTimeout(flushTimer);
    flushTimer = null;
  }
  flushInFlight = writePending().finally(() => {
    flushInFlight = null;
  });
  return flushInFlight;
}

function scheduleFlush(): void {
  if (pending.length >= TRACE_MAX_CHUNK_EVENTS * 2 || pending.reduce((sum, item) => sum + sizeOf(item), 0) >= TRACE_MAX_CHUNK_BYTES) {
    void flushTrace();
    return;
  }
  if (flushTimer !== null) return;
  flushTimer = setTimeout(() => {
    flushTimer = null;
    void flushTrace();
  }, 750) as unknown as number;
}

export interface TraceInput {
  kind: string;
  phase: TraceEnvelope['phase'];
  t?: number;
  pageUrl?: string;
  tabId?: number;
  frameId?: number;
  playerKey?: string;
  mediaIdentity?: string;
  payload: Record<string, unknown>;
}

export async function recordTrace(input: TraceInput): Promise<void> {
  await ensureMeta();
  if (!enabled) return;
  const envelope: TraceEnvelope = {
    t: input.t ?? Date.now(),
    kind: input.kind,
    phase: input.phase,
    sw: swInstance,
    swStartedAt,
    ...(input.pageUrl ? { pageUrl: input.pageUrl } : {}),
    ...(input.tabId !== undefined ? { tabId: input.tabId } : {}),
    ...(input.frameId !== undefined ? { frameId: input.frameId } : {}),
    ...(input.playerKey ? { playerKey: input.playerKey } : {}),
    ...(input.mediaIdentity ? { mediaIdentity: input.mediaIdentity } : {}),
    payload: input.payload,
  };
  pending.push(envelope);
  scheduleFlush();
}

export async function traceStatus(): Promise<{ enabled: boolean; meta: TraceMeta; pending: number; sw: string; swStartedAt: number }> {
  await ensureMeta();
  return { enabled, meta, pending: pending.length, sw: swInstance, swStartedAt };
}

export async function setTraceEnabled(value: boolean): Promise<void> {
  await ensureMeta();
  enabled = value;
  meta = { ...meta, enabled: value };
  try {
    await chrome.storage.local.set({ [TRACE_ENABLED_KEY]: value, [TRACE_META_KEY]: meta });
  } catch {
    // Best effort.
  }
}

interface StoredChunk {
  at: number;
  events: TraceEnvelope[];
}

/** All recorded events in order, for export. */
export async function readTraceEvents(): Promise<TraceEnvelope[]> {
  await flushTrace();
  const all = (await chrome.storage.local.get(null)) as Record<string, unknown>;
  const keys = Object.keys(all)
    .filter((key) => key.startsWith(TRACE_CHUNK_PREFIX))
    .sort();
  const events: TraceEnvelope[] = [];
  for (const key of keys) {
    const chunk = all[key] as StoredChunk | undefined;
    if (chunk && Array.isArray(chunk.events)) events.push(...chunk.events);
  }
  return events;
}

export async function clearTrace(): Promise<void> {
  const all = (await chrome.storage.local.get(null)) as Record<string, unknown>;
  const keys = Object.keys(all).filter((key) => key.startsWith(TRACE_CHUNK_PREFIX));
  await chrome.storage.local.remove([...keys, TRACE_META_KEY]);
  meta = {
    schemaVersion: TRACE_SCHEMA_VERSION,
    startedAt: Date.now(),
    updatedAt: 0,
    events: 0,
    bytes: 0,
    chunks: 0,
    enabled,
    swStarts: meta.swStarts,
  };
  await chrome.storage.local.set({ [TRACE_META_KEY]: meta });
}

export function toJsonl(events: TraceEnvelope[]): string {
  const header = JSON.stringify({ kind: 'trace.header', schemaVersion: TRACE_SCHEMA_VERSION, exportedAt: Date.now(), events: events.length });
  return [header, ...events.map((event) => JSON.stringify(event))].join('\n');
}

// ---------------------------------------------------------------------------
// Body classification (single implementation, used for page and extension
// probe results alike).
// ---------------------------------------------------------------------------

export interface SampleVerdict {
  bodyKind: 'html' | 'hls' | 'dash' | 'mp4' | 'mpegts' | 'json' | 'text' | 'binary' | 'empty';
  sniff: string;
  manifest?: ProbeManifestSummary;
}

function printableSample(bytes: Uint8Array, limit = 160): string {
  const slice = bytes.subarray(0, limit);
  let out = '';
  for (const byte of slice) {
    out += byte >= 32 && byte < 127 ? String.fromCharCode(byte) : byte === 10 || byte === 13 ? ' ' : '.';
  }
  return out;
}

function textOfSample(bytes: Uint8Array, limit = 64 * 1024): string {
  const slice = bytes.subarray(0, limit);
  try {
    return new TextDecoder('utf-8', { fatal: false }).decode(slice);
  } catch {
    return '';
  }
}

function firstUri(block: string): string | undefined {
  for (const rawLine of block.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    return line;
  }
  return undefined;
}

export function parseHlsManifest(text: string, baseUrl: string): ProbeManifestSummary | undefined {
  if (!text.trimStart().startsWith('#EXTM3U')) return undefined;
  const variants = (text.match(/#EXT-X-STREAM-INF:/g) ?? []).length;
  const segmentLines = text.split(/\r?\n/).filter((line) => {
    const trimmed = line.trim();
    return !!trimmed && !trimmed.startsWith('#');
  }).length;
  const resolve = (value: string | undefined): string | undefined => {
    if (!value) return undefined;
    try {
      return new URL(value, baseUrl).href;
    } catch {
      return value;
    }
  };
  return {
    format: 'hls',
    variants,
    segments: segmentLines,
    live: !text.includes('#EXT-X-ENDLIST') && text.includes('#EXT-X-MEDIA-SEQUENCE'),
    ...(resolve(firstUri(text)) ? { firstChild: resolve(firstUri(text)) as string } : {}),
  };
}

export function parseDashManifest(text: string, baseUrl: string): ProbeManifestSummary | undefined {
  const start = text.indexOf('<MPD');
  if (start < 0) return undefined;
  const representations = (text.match(/<Representation\b/g) ?? []).length;
  const segments = (text.match(/<S\b[^>]*\bd="[0-9]+"/g) ?? []).length;
  const first = text.match(/<BaseURL[^>]*>([^<]+)<\/BaseURL>/i)?.[1];
  let firstChild: string | undefined;
  if (first) {
    try {
      firstChild = new URL(first.trim(), baseUrl).href;
    } catch {
      firstChild = first.trim();
    }
  }
  return { format: 'dash', variants: representations, segments, ...(firstChild ? { firstChild } : {}) };
}

export function classifySample(bytes: Uint8Array, contentType = '', baseUrl = ''): SampleVerdict {
  const sniff = printableSample(bytes);
  if (bytes.length === 0) return { bodyKind: 'empty', sniff };
  const mime = contentType.toLowerCase().split(';', 1)[0].trim();
  const head = textOfSample(bytes, 2048).trimStart().toLowerCase();
  if (head.startsWith('<html') || head.startsWith('<!doctype html') || mime === 'text/html') {
    return { bodyKind: 'html', sniff };
  }
  const text = textOfSample(bytes);
  if (text.trimStart().startsWith('#EXTM3U')) {
    return { bodyKind: 'hls', sniff, manifest: parseHlsManifest(text, baseUrl) };
  }
  if (text.includes('<MPD')) {
    return { bodyKind: 'dash', sniff, manifest: parseDashManifest(text, baseUrl) };
  }
  if (bytes.length >= 12 && bytes[4] === 0x66 && bytes[5] === 0x74 && bytes[6] === 0x79 && bytes[7] === 0x70) {
    const hasMoof = indexOfAscii(bytes, 'moof') >= 0;
    const hasMdat = indexOfAscii(bytes, 'mdat') >= 0;
    return { bodyKind: 'mp4', sniff: `${sniff} [moof:${hasMoof ? 'y' : 'n'} mdat:${hasMdat ? 'y' : 'n'}]` };
  }
  if (bytes.length > 377 && bytes[0] === 0x47 && bytes[188] === 0x47) {
    return { bodyKind: 'mpegts', sniff };
  }
  if (head.startsWith('{') || head.startsWith('[')) return { bodyKind: 'json', sniff };
  if (mime.startsWith('text/') || mime.includes('json') || mime.includes('xml')) {
    return { bodyKind: 'text', sniff };
  }
  return { bodyKind: 'binary', sniff };
}

function indexOfAscii(bytes: Uint8Array, needle: string): number {
  const first = needle.charCodeAt(0);
  outer: for (let index = 0; index + needle.length <= bytes.length; index += 1) {
    if (bytes[index] !== first) continue;
    for (let offset = 1; offset < needle.length; offset += 1) {
      if (bytes[index + offset] !== needle.charCodeAt(offset)) continue outer;
    }
    return index;
  }
  return -1;
}

const HEADER_KEEP = ['content-type', 'content-length', 'content-range', 'accept-ranges', 'cache-control', 'server', 'vary', 'date', 'last-modified', 'etag', 'age', 'via', 'cf-cache-status', 'x-cache'];

function headerSubset(headers: Headers): Pick<ProbeResult, 'contentType' | 'contentLength' | 'contentRange' | 'acceptRanges' | 'cacheControl' | 'server' | 'vary'> {
  const picked: Record<string, string> = {};
  for (const name of HEADER_KEEP) {
    const value = headers.get(name);
    if (value) picked[name] = value.slice(0, 200);
  }
  return {
    ...(picked['content-type'] ? { contentType: picked['content-type'] } : {}),
    ...(picked['content-length'] ? { contentLength: picked['content-length'] } : {}),
    ...(picked['content-range'] ? { contentRange: picked['content-range'] } : {}),
    ...(picked['accept-ranges'] ? { acceptRanges: picked['accept-ranges'] } : {}),
    ...(picked['cache-control'] ? { cacheControl: picked['cache-control'] } : {}),
    ...(picked['server'] ? { server: picked['server'] } : {}),
    ...(picked['vary'] ? { vary: picked['vary'] } : {}),
  };
}

export interface ExtensionProbeOptions {
  credentials: 'omit' | 'include';
  /** `'none'` sends no Range header at all — the control for signed-range URLs. */
  range?: 'none' | string;
  rangeBytes?: number;
  timeoutMs?: number;
  followChild?: boolean;
}

/** Probe a URL from the service worker (extension context). This is the
 *  closest extension-side mirror of what the resident does: no page referer,
 *  and either no cookies or the extension's view of them. */
export async function probeFromExtension(url: string, options: ExtensionProbeOptions): Promise<ProbeResult> {
  const started = Date.now();
  const rangeHeader = options.range === 'none' ? undefined : options.range ?? `bytes=0-${(options.rangeBytes ?? 65536) - 1}`;
  const result: ProbeResult = {
    mode: options.credentials === 'include' ? 'extension-include' : 'extension-omit',
    url,
    t: started,
    durationMs: 0,
    ok: false,
    requestRange: rangeHeader ? 'ranged' : 'plain',
  };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? 8000);
  try {
    const response = await fetch(url, {
      method: 'GET',
      credentials: options.credentials,
      redirect: 'follow',
      cache: 'no-store',
      ...(rangeHeader ? { headers: { Range: rangeHeader } } : {}),
      signal: controller.signal,
    });
    result.status = response.status;
    result.statusText = response.statusText;
    result.ok = response.ok;
    result.finalUrl = response.url.slice(0, 500);
    result.redirected = response.redirected;
    Object.assign(result, headerSubset(response.headers));
    const bytes = new Uint8Array(await readBounded(response, options.rangeBytes ?? 65536));
    result.bytesRead = bytes.length;
    const verdict = classifySample(bytes, result.contentType ?? '', response.url);
    result.bodyKind = verdict.bodyKind;
    result.sniff = verdict.sniff;
    if (verdict.manifest) result.manifest = verdict.manifest;
  } catch (reason) {
    result.error = reason instanceof Error ? `${reason.name}: ${reason.message}` : String(reason);
  } finally {
    clearTimeout(timer);
    result.durationMs = Date.now() - started;
  }
  return result;
}

async function readBounded(response: Response, limit: number): Promise<ArrayBuffer> {
  if (!response.body) {
    const buffer = await response.arrayBuffer();
    return buffer.byteLength <= limit ? buffer : buffer.slice(0, limit);
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (total < limit) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value) {
        const slice = value.subarray(0, Math.max(0, limit - total));
        chunks.push(slice);
        total += slice.length;
      }
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      // The stream may already be closed.
    }
  }
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.length;
  }
  return merged.buffer;
}
