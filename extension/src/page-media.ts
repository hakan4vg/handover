import type { MediaEvidence, MediaKind } from './media-candidates';

const MESSAGE_MARKER = 'download-manager-media-v1';
const MAX_URL = 4096;
const MAX_MANIFEST = 256 * 1024;
const MAX_OBSERVATIONS = 32;
const MAX_MANIFEST_HINTS = 8;
const MAX_MANIFEST_LINKS = 2048;
const MAX_MANIFEST_INDEX_BYTES = 192 * 1024;
const MAX_MANIFEST_RECORDS = 64;
const MAX_MEDIA_SOURCE_BLOBS = 32;
const MAX_MEDIA_SOURCE_ELEMENTS = 16;
const MAX_KNOWN_MEDIA_ELEMENTS = 128;
const MAX_REQUEST_ID = 128;
const MEDIA_EVIDENCE_UPDATE = 'dm-media-evidence-update';
const MEDIA_EVIDENCE_QUERY = 'dm-media-evidence-query';
const MEDIA_EVIDENCE_RESPONSE = 'dm-media-evidence-response';

interface Provenance {
  url: string;
  mime: string;
  kind: MediaKind;
  role: 'unknown' | 'manifest' | 'segment';
}

interface ManifestLink {
  url: string;
  role: 'unknown' | 'manifest' | 'representation' | 'segment';
}

interface ManifestRecord extends Provenance {
  links: ManifestLink[];
  linkBytes: number;
}

interface MediaObservation extends Provenance {
  at: number;
}

interface MediaSourceState {
  blobUrl: string;
  identity: string;
  observations: MediaObservation[];
  buffers: SourceBufferState[];
  elements: Set<HTMLMediaElement>;
  at: number;
}

interface SourceBufferState {
  source: MediaSourceState;
  mime: string;
  latest?: MediaObservation;
}

interface ElementState {
  source: string;
  generation: number;
}

const responseSources = new WeakMap<Response, Provenance>();
const streamSources = new WeakMap<ReadableStream<unknown>, Provenance>();
const bufferSources = new WeakMap<ArrayBuffer, Provenance>();
const xhrSources = new WeakMap<XMLHttpRequest, Provenance>();
const xhrObserved = new WeakSet<XMLHttpRequest>();
const sourceBufferStates = new WeakMap<SourceBuffer, { source: MediaSourceState; mime: string }>();
const mediaSourceStates = new WeakMap<MediaSource, MediaSourceState>();
const mediaSourceByBlob = new Map<string, MediaSourceState>();
const elementStates = new WeakMap<HTMLMediaElement, ElementState>();
const manifestRecords = new Map<string, ManifestRecord>();
const observedElements = new WeakSet<HTMLMediaElement>();
const knownMediaElements = new Set<HTMLMediaElement>();

let nextGeneration = 1;
let nextSourceIdentity = 1;

function validUrl(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= MAX_URL;
}

function httpUrl(value: unknown, base = location.href): string | undefined {
  if (!validUrl(value)) return undefined;
  try {
    const parsed = new URL(value, base);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return undefined;
    parsed.hash = '';
    return parsed.href;
  } catch {
    return undefined;
  }
}

function mediaUrl(value: unknown): string | undefined {
  if (!validUrl(value)) return undefined;
  try {
    const parsed = new URL(value, location.href);
    if (parsed.protocol === 'blob:') {
      return parsed.origin === location.origin ? parsed.href : undefined;
    }
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}

function mimeKind(value: string): MediaKind {
  const mime = value.toLowerCase().split(';', 1)[0].trim();
  if (mime.startsWith('audio/')) return 'audio';
  if (mime.startsWith('video/')) return 'video';
  return 'unknown';
}

function kindForUrl(url: string): MediaKind {
  try {
    const parsed = new URL(url);
    const queryMime = (parsed.searchParams.get('mime') || '').toLowerCase();
    if (queryMime.startsWith('audio/')) return 'audio';
    if (queryMime.startsWith('video/')) return 'video';
    const path = parsed.pathname.toLowerCase();
    if (/(^|[/_-])audio([/_.-]|$)/.test(path) || /\.(?:m4a|aac|mp3|oga|ogg|opus|flac)(?:$|[?#])/.test(path)) return 'audio';
    if (/(^|[/_-])video([/_.-]|$)/.test(path) || /\.(?:webm|m4v|ogv)(?:$|[?#])/.test(path)) return 'video';
  } catch {
    return 'unknown';
  }
  return 'unknown';
}

function roleForUrl(url: string, mime = ''): 'unknown' | 'manifest' | 'segment' {
  const hint = `${mime} ${url}`.toLowerCase();
  if (hint.includes('mpegurl') || hint.includes('dash+xml') || /\.(?:m3u8|mpd)(?:[?#]|$)/i.test(url)) return 'manifest';
  if (hint.includes('video/mp2t') || hint.includes('iso.segment') || /(?:^|[./_-])(?:m4[asv]|ts|cmf[av])(?:[?#]|$)/i.test(url)) return 'segment';
  return 'unknown';
}

function representationForUrl(url: string, mime = ''): boolean {
  const kind = mimeKind(mime);
  if (kind !== 'unknown') return true;
  try {
    const parsed = new URL(url);
    if (/\.(?:mp4|webm|m4a|m4v|mov|ogv|mp3|aac|ogg|opus|flac)(?:$|[?#])/i.test(parsed.pathname)) return true;
    const queryMime = (parsed.searchParams.get('mime') || '').toLowerCase();
    return queryMime.startsWith('audio/') || queryMime.startsWith('video/');
  } catch {
    return false;
  }
}

function normalizedSourceUrl(url: string): string {
  try {
    const parsed = new URL(url);
    parsed.hash = '';
    if (parsed.searchParams.has('range')) {
      const signedParameters = (parsed.searchParams.get('sparams') || '').split(',').map((item) => item.trim().toLowerCase());
      if (!signedParameters.includes('range')) parsed.searchParams.delete('range');
    }
    return parsed.href;
  } catch {
    return url;
  }
}

function provenance(url: string, mime = ''): Provenance | undefined {
  const resolved = httpUrl(url);
  if (!resolved) return undefined;
  return { url: resolved, mime: mime.slice(0, 256), kind: mimeKind(mime) === 'unknown' ? kindForUrl(resolved) : mimeKind(mime), role: roleForUrl(resolved, mime) };
}

function addBufferProvenance(value: unknown, source: Provenance): void {
  if (value instanceof ArrayBuffer) {
    bufferSources.set(value, source);
    return;
  }
  if (ArrayBuffer.isView(value)) bufferSources.set(value.buffer as ArrayBuffer, source);
}

function sourceFromBuffer(value: unknown): Provenance | undefined {
  if (value instanceof ArrayBuffer) return bufferSources.get(value);
  if (ArrayBuffer.isView(value)) return bufferSources.get(value.buffer as ArrayBuffer);
  return undefined;
}

function manifestLinkRole(url: string): ManifestLink['role'] {
  if (roleForUrl(url) === 'manifest') return 'manifest';
  return representationForUrl(url) ? 'representation' : 'unknown';
}

function addManifestLink(record: ManifestRecord, value: string, role?: ManifestLink['role']): void {
  if (value.includes('{') || value.includes('$')) return;
  const url = httpUrl(value, record.url);
  if (!url || url === record.url || record.links.some((item) => item.url === url)) return;
  const linkRole = role ?? manifestLinkRole(url);
  const linkBytes = url.length + linkRole.length + 8;
  if (record.links.length >= MAX_MANIFEST_LINKS || record.linkBytes + linkBytes > MAX_MANIFEST_INDEX_BYTES) return;
  record.links.push({ url, role: linkRole });
  record.linkBytes += linkBytes;
}

function recordManifestText(source: Provenance, text: string): void {
  if (source.role !== 'manifest' || typeof text !== 'string' || text.length > MAX_MANIFEST) return;
  const record: ManifestRecord = { ...source, links: [], linkBytes: 0 };
  const lines = text.split(/\r?\n/);
  let pendingRole: ManifestLink['role'] | undefined;
  for (const line of lines) {
    const item = line.trim();
    if (!item) continue;
    if (item.startsWith('#')) {
      const tag = item.slice(1).split(':', 1)[0].toUpperCase();
      const uri = item.match(/\bURI\s*=\s*["']([^"']+)["']/i)?.[1];
      if (tag === 'EXT-X-STREAM-INF') pendingRole = 'manifest';
      else if (tag === 'EXT-X-MEDIA' || tag === 'EXT-X-I-FRAME-STREAM-INF') {
        if (uri) addManifestLink(record, uri, 'manifest');
        pendingRole = undefined;
      } else if (tag === 'EXT-X-MAP' || tag === 'EXT-X-PART') {
        if (uri) addManifestLink(record, uri, 'segment');
        pendingRole = undefined;
      } else {
        pendingRole = undefined;
      }
      continue;
    }
    addManifestLink(record, item, pendingRole ?? 'segment');
    pendingRole = undefined;
  }
  const basePattern = /<BaseURL[^>]*>([^<]+)<\//gi;
  let match: RegExpExecArray | null;
  while ((match = basePattern.exec(text)) !== null) addManifestLink(record, match[1].trim(), 'representation');
  const segmentPattern = /<SegmentURL[^>]*\b(?:media|initialization)\s*=\s*["']([^"']+)["'][^>]*\/?\s*>/gi;
  while ((match = segmentPattern.exec(text)) !== null) addManifestLink(record, match[1], 'segment');
  const sourcePattern = /\bsourceURL\s*=\s*["']([^"']+)["']/gi;
  while ((match = sourcePattern.exec(text)) !== null) addManifestLink(record, match[1], 'representation');
  if (manifestRecords.has(source.url)) manifestRecords.delete(source.url);
  while (manifestRecords.size >= MAX_MANIFEST_RECORDS) {
    const oldest = manifestRecords.keys().next().value;
    if (typeof oldest !== 'string') break;
    manifestRecords.delete(oldest);
  }
  manifestRecords.set(source.url, record);
}

function ensureMediaSourceState(source: MediaSource): MediaSourceState {
  const existing = mediaSourceStates.get(source);
  if (existing) return existing;
  const state: MediaSourceState = {
    blobUrl: '',
    identity: `source-${nextSourceIdentity++}`,
    observations: [],
    buffers: [],
    elements: new Set<HTMLMediaElement>(),
    at: Date.now(),
  };
  mediaSourceStates.set(source, state);
  return state;
}

function activeObservations(state: MediaSourceState): MediaObservation[] {
  return state.buffers
    .map((buffer) => buffer.latest)
    .filter((observation): observation is MediaObservation => !!observation);
}

function rememberObservation(buffer: SourceBufferState, source: Provenance): void {
  const state = buffer.source;
  const observation: MediaObservation = {
    ...source,
    mime: buffer.mime || source.mime,
    kind: source.kind === 'unknown' ? mimeKind(buffer.mime) : source.kind,
    at: Date.now(),
  };
  buffer.latest = observation;
  const existing = state.observations.find((item) => item.url === observation.url && item.kind === observation.kind);
  if (existing) Object.assign(existing, observation);
  else state.observations.push(observation);
  state.observations.sort((left, right) => right.at - left.at);
  state.observations.splice(MAX_OBSERVATIONS);
  state.at = Date.now();
  for (const element of state.elements) {
    const current = currentSrcFor(element as HTMLMediaElement);
    if (current === state.blobUrl) publishEvidence(element as HTMLMediaElement);
  }
}

function currentSrcFor(element: HTMLMediaElement): string {
  const current = element.currentSrc || element.getAttribute('src') || element.querySelector('source[src]')?.getAttribute('src') || '';
  return mediaUrl(current) || '';
}

function stateForElement(element: HTMLMediaElement): ElementState {
  const source = currentSrcFor(element);
  const old = elementStates.get(element);
  if (old && old.source === source) return old;
  if (old?.source.startsWith('blob:')) mediaSourceByBlob.get(old.source)?.elements.delete(element);
  const state = { source, generation: nextGeneration++ };
  elementStates.set(element, state);
  if (source.startsWith('blob:')) {
    const mediaSource = mediaSourceByBlob.get(source);
    if (mediaSource) {
      while (mediaSource.elements.size >= MAX_MEDIA_SOURCE_ELEMENTS) {
        const oldest = mediaSource.elements.values().next().value;
        if (!oldest) break;
        mediaSource.elements.delete(oldest);
      }
      mediaSource.elements.add(element);
    }
  }
  return state;
}

interface ManifestMatch {
  record: ManifestRecord;
  observed: MediaObservation;
  link?: ManifestLink;
}

function matchingManifest(observations: MediaObservation[]): ManifestMatch | undefined {
  for (const observation of observations) {
    const direct = manifestRecords.get(observation.url);
    if (direct) return { record: direct, observed: observation };
  }
  for (const observation of observations) {
    const records = [...manifestRecords.values()].reverse();
    for (const record of records) {
      const link = record.links.find((item) => item.url === observation.url);
      if (link) return { record, observed: observation, link };
    }
  }
  return undefined;
}

function uniqueSources(observations: MediaObservation[], kind: Exclude<MediaKind, 'unknown'>): MediaObservation[] {
  const seen = new Set<string>();
  const result: MediaObservation[] = [];
  for (const observation of observations) {
    if (observation.kind !== kind || observation.role === 'segment' || !representationForUrl(observation.url, observation.mime)) continue;
    const url = normalizedSourceUrl(observation.url);
    if (seen.has(url)) continue;
    seen.add(url);
    result.push(observation);
  }
  return result;
}

// Resource timing is the page's own record of everything it loaded, including
// requests made before this observer was installed or through a path we do not
// patch. It names no player, so it is only ever used as a hint list: the
// background decides ownership, and the resident verifies what it fetches.
const RESOURCE_HINT_PATTERN = /\.(?:m3u8|mpd|mp4|m4s|m4a|m4v|ts|webm|mkv|mp3|aac|ogg|oga|opus|flac|mov)(?:$|[?#])/i;

function resourceHintSources(limit = 8): string[] {
  try {
    const entries = performance.getEntriesByType('resource') as PerformanceResourceTiming[];
    const media = entries
      .filter((entry) => RESOURCE_HINT_PATTERN.test(entry.name) && entry.name.startsWith('http'))
      .sort((left, right) => right.startTime - left.startTime)
      .slice(0, limit)
      .map((entry) => entry.name);
    return [...new Set(media)];
  } catch {
    return [];
  }
}

/** Evidence that names plausible sources without claiming exact identity. */
function hintEvidence(currentSrc: string, kind: Exclude<MediaKind, 'unknown'>, sourceIdentity: string): MediaEvidence | undefined {
  const hints = resourceHintSources();
  if (!hints.length) return undefined;
  return { currentSrc, sourceIdentity, playerKind: kind, selectedSegments: hints };
}

function evidenceForSource(currentSrc: string, kind: Exclude<MediaKind, 'unknown'>, sourceIdentity: string): MediaEvidence | undefined {
  if (!currentSrc) return undefined;
  if (currentSrc.startsWith('http')) {
    const directKind = kindForUrl(currentSrc);
    return {
      currentSrc,
      sourceIdentity,
      source: directKind !== 'unknown' && directKind !== kind ? undefined : currentSrc,
      playerKind: kind,
      selectedSegments: [],
    };
  }
  if (!currentSrc.startsWith('blob:')) return undefined;
  const sourceState = mediaSourceByBlob.get(currentSrc);
  if (!sourceState) return hintEvidence(currentSrc, kind, sourceIdentity);
  const observations = activeObservations(sourceState);
  const kindObservations = observations.filter((observation) => observation.kind === kind || observation.kind === 'unknown');
  const manifest = matchingManifest(kindObservations);
  if (manifest) {
    let selected = manifest.record;
    const hints = [manifest.observed.url];
    const visited = new Set([selected.url]);
    for (let depth = 0; depth < 4; depth++) {
      const parents = [...manifestRecords.values()].filter((record) =>
        !visited.has(record.url) && record.links.some((link) => link.role === 'manifest' && link.url === selected.url));
      if (parents.length !== 1) break;
      hints.unshift(selected.url);
      selected = parents[0];
      visited.add(selected.url);
    }
    for (const observation of observations) {
      if (observation.kind === kind) continue;
      const companion = matchingManifest([observation]);
      if (companion && selected.links.some((link) => link.role === 'manifest' && link.url === companion.record.url)) {
        hints.push(companion.record.url);
      }
    }
    return {
      currentSrc,
      sourceIdentity,
      source: selected.url,
      playerKind: kind,
      selectedSegments: [...new Set(hints)].slice(0, MAX_MANIFEST_HINTS),
    };
  }
  const primary = uniqueSources(observations, kind);
  if (primary.length !== 1) return hintEvidence(currentSrc, kind, sourceIdentity);
  const result: MediaEvidence = {
    currentSrc,
    sourceIdentity,
    source: normalizedSourceUrl(primary[0].url),
    playerKind: kind,
    selectedSegments: [],
  };
  if (kind === 'video') {
    const audio = uniqueSources(observations, 'audio');
    if (audio.length === 1) result.companionAudio = normalizedSourceUrl(audio[0].url);
  }
  return result;
}

function evidenceFor(element: HTMLMediaElement, expectedKind?: Exclude<MediaKind, 'unknown'>): MediaEvidence | undefined {
  const elementState = stateForElement(element);
  const kind = expectedKind ?? (element instanceof HTMLAudioElement ? 'audio' : 'video');
  return evidenceForSource(elementState.source, kind, `${elementState.generation}:${elementState.source}`);
}

function postMessage(type: string, payload: Record<string, unknown>): void {
  window.postMessage({ marker: MESSAGE_MARKER, type, ...payload }, location.origin);
}

function publishEvidence(element: HTMLMediaElement): void {
  const evidence = evidenceFor(element);
  if (evidence) postMessage(MEDIA_EVIDENCE_UPDATE, { evidence });
}

function requestEvidence(requestId: string, currentSrc: string, expectedKind?: Exclude<MediaKind, 'unknown'>): void {
  const kind = expectedKind ?? 'video';
  const mediaSource = mediaSourceByBlob.get(currentSrc);
  const identity = mediaSource ? mediaSource.identity : `direct:${currentSrc.slice(0, 220)}`;
  const evidence = evidenceForSource(currentSrc, kind, identity);
  postMessage(MEDIA_EVIDENCE_RESPONSE, { requestId, evidence: evidence ?? null });
}

function validMessage(event: MessageEvent): Record<string, unknown> | undefined {
  if (event.source !== window || event.origin !== location.origin || !event.data || typeof event.data !== 'object') return undefined;
  const data = event.data as Record<string, unknown>;
  return data.marker === MESSAGE_MARKER ? data : undefined;
}

window.addEventListener('message', (event) => {
  const data = validMessage(event);
  if (!data) return;
  if (data.type !== MEDIA_EVIDENCE_QUERY) return;
  const requestId = typeof data.requestId === 'string' && data.requestId.length <= MAX_REQUEST_ID ? data.requestId : '';
  const currentSrc = mediaUrl(data.currentSrc);
  const expectedKind = data.playerKind === 'audio' || data.playerKind === 'video' ? data.playerKind : undefined;
  if (!requestId || !currentSrc) return;
  requestEvidence(requestId, currentSrc, expectedKind);
});

function patchFetch(): void {
  const nativeFetch = globalThis.fetch;
  if (typeof nativeFetch !== 'function') return;
  globalThis.fetch = function patchedFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    const requestUrl = input instanceof Request ? input.url : String(input);
    return nativeFetch.call(this, input, init).then((response) => {
      const source = provenance(response.url || requestUrl, response.headers.get('content-type') || '');
      if (source) {
        responseSources.set(response, source);
        if (response.body) streamSources.set(response.body as ReadableStream<unknown>, source);
      }
      return response;
    });
  };
}

function patchResponse(): void {
  const responsePrototype = Response.prototype as Response & { arrayBuffer: () => Promise<ArrayBuffer>; text: () => Promise<string> };
  const nativeArrayBuffer = responsePrototype.arrayBuffer;
  const nativeText = responsePrototype.text;
  responsePrototype.arrayBuffer = function patchedArrayBuffer(this: Response): Promise<ArrayBuffer> {
    const source = responseSources.get(this);
    const result = nativeArrayBuffer.call(this);
    return source ? result.then((value) => { addBufferProvenance(value, source); return value; }) : result;
  };
  responsePrototype.text = function patchedText(this: Response): Promise<string> {
    const source = responseSources.get(this);
    const result = nativeText.call(this);
    return source ? result.then((value) => { recordManifestText(source, value); return value; }) : result;
  };
}

function patchStreams(): void {
  const streamPrototype = ReadableStream.prototype as unknown as { getReader: (options?: ReadableStreamGetReaderOptions) => ReadableStreamDefaultReader<unknown> };
  const nativeGetReader = streamPrototype.getReader;
  streamPrototype.getReader = function patchedGetReader(this: ReadableStream<unknown>, options?: ReadableStreamGetReaderOptions): ReadableStreamDefaultReader<unknown> {
    const source = streamSources.get(this);
    const reader = nativeGetReader.call(this, options);
    if (!source) return reader;
    return new Proxy(reader, {
      get(target, property) {
        if (property === 'read') {
          return (...args: unknown[]) => Promise.resolve((target.read as unknown as (...values: unknown[]) => Promise<unknown>).apply(target, args)).then((result) => {
            if (result && typeof result === 'object' && 'value' in result) addBufferProvenance((result as { value?: unknown }).value, source);
            return result;
          });
        }
        const value = Reflect.get(target, property);
        return typeof value === 'function' ? value.bind(target) : value;
      },
    });
  };
}

function patchArrayBuffers(): void {
  const arrayBufferPrototype = ArrayBuffer.prototype as ArrayBuffer & { slice: (start?: number, end?: number) => ArrayBuffer };
  const nativeSlice = arrayBufferPrototype.slice;
  arrayBufferPrototype.slice = function patchedSlice(this: ArrayBuffer, start?: number, end?: number): ArrayBuffer {
    const result = nativeSlice.call(this, start, end);
    const source = bufferSources.get(this);
    if (source) bufferSources.set(result, source);
    return result;
  };
  const typedArrayPrototype = Object.getPrototypeOf(Uint8Array.prototype) as unknown as { slice: (...args: number[]) => Uint8Array };
  const nativeTypedSlice = typedArrayPrototype.slice;
  if (typeof nativeTypedSlice === 'function') {
    typedArrayPrototype.slice = function patchedTypedSlice(this: Uint8Array, ...args: number[]): Uint8Array {
      const result = nativeTypedSlice.apply(this, args);
      const source = bufferSources.get(this.buffer as ArrayBuffer);
      if (source) bufferSources.set(result.buffer as ArrayBuffer, source);
      return result;
    };
  }
}

function xhrSourceFor(xhr: XMLHttpRequest): Provenance | undefined {
  const source = xhrSources.get(xhr);
  if (!source) return undefined;
  let mime = '';
  try {
    mime = xhr.getResponseHeader('content-type') || '';
  } catch {
    // Response headers can be unavailable before completion.
  }
  return provenance(xhr.responseURL || source.url, mime) || source;
}

function captureXhrResponse(xhr: XMLHttpRequest): void {
  const source = xhrSourceFor(xhr);
  if (!source) return;
  try {
    if (xhr.responseType === 'arraybuffer') addBufferProvenance(xhr.response, source);
    else if (xhr.responseType === '' || xhr.responseType === 'text') recordManifestText(source, xhr.responseText);
  } catch {
    // Access can fail while an XHR is still changing state.
  }
}

function patchXhr(): void {
  const prototype = XMLHttpRequest.prototype;
  const nativeOpen = prototype.open;
  const nativeSend = prototype.send;
  prototype.open = function patchedOpen(method: string, url: string | URL, ...rest: unknown[]): void {
    xhrSources.delete(this);
    const source = provenance(String(url));
    if (source) xhrSources.set(this, source);
    (nativeOpen as unknown as ((method: string, url: string | URL, async?: boolean, username?: string, password?: string) => void)).call(this, method, url, ...(rest as [boolean?, string?, string?]));
  };
  prototype.send = function patchedSend(body?: Document | XMLHttpRequestBodyInit | null): void {
    const xhr = this;
    if (!xhrObserved.has(xhr)) {
      xhrObserved.add(xhr);
      xhr.addEventListener('readystatechange', () => {
        if (xhr.readyState === XMLHttpRequest.DONE) captureXhrResponse(xhr);
      }, { capture: true });
      xhr.addEventListener('load', () => captureXhrResponse(xhr), { capture: true });
    }
    nativeSend.call(this, body);
  };
}

function patchXhrResponseGetters(): void {
  const prototype = XMLHttpRequest.prototype as XMLHttpRequest & Record<string, unknown>;
  const wrap = (name: 'response' | 'responseText', onRead: (xhr: XMLHttpRequest, value: unknown) => void): void => {
    let owner: object | null = prototype;
    while (owner && !Object.prototype.hasOwnProperty.call(owner, name)) owner = Object.getPrototypeOf(owner);
    const descriptor = owner ? Object.getOwnPropertyDescriptor(owner, name) : undefined;
    if (!owner || !descriptor?.get || descriptor.configurable === false) return;
    try {
      Object.defineProperty(owner, name, {
        ...descriptor,
        get(this: XMLHttpRequest) {
          const value = descriptor.get!.call(this);
          onRead(this, value);
          return value;
        },
      });
    } catch {
      // Some engines expose non-configurable XHR accessors.
    }
  };
  wrap('response', (xhr, value) => {
    const source = xhrSourceFor(xhr);
    if (!source) return;
    if (xhr.responseType === 'arraybuffer') addBufferProvenance(value, source);
    else if ((xhr.responseType === '' || xhr.responseType === 'text') && typeof value === 'string') recordManifestText(source, value);
  });
  wrap('responseText', (xhr, value) => {
    const source = xhrSourceFor(xhr);
    if (source && typeof value === 'string') recordManifestText(source, value);
  });
}

function patchMediaSource(): void {
  const mediaSourceConstructor = (globalThis as { MediaSource?: typeof MediaSource }).MediaSource;
  if (!mediaSourceConstructor) return;
  const nativeCreateObjectUrl = URL.createObjectURL.bind(URL);
  URL.createObjectURL = function patchedCreateObjectURL(value: Blob | MediaSource): string {
    const url = nativeCreateObjectUrl(value);
    if (value instanceof mediaSourceConstructor) {
      const state = ensureMediaSourceState(value);
      state.blobUrl = url;
      while (mediaSourceByBlob.size >= MAX_MEDIA_SOURCE_BLOBS) {
        const oldest = mediaSourceByBlob.keys().next().value;
        if (typeof oldest !== 'string') break;
        mediaSourceByBlob.delete(oldest);
      }
      mediaSourceByBlob.set(url, state);
    }
    return url;
  };
  const mediaSourcePrototype = mediaSourceConstructor.prototype;
  const nativeAddSourceBuffer = mediaSourcePrototype.addSourceBuffer;
  mediaSourcePrototype.addSourceBuffer = function patchedAddSourceBuffer(type: string): SourceBuffer {
    const buffer = nativeAddSourceBuffer.call(this, type);
    const state = ensureMediaSourceState(this);
    const bufferState: SourceBufferState = { source: state, mime: type };
    sourceBufferStates.set(buffer, bufferState);
    state.buffers.push(bufferState);
    if (state.buffers.length > MAX_MEDIA_SOURCE_ELEMENTS) state.buffers.shift();
    return buffer;
  };
  const nativeRemoveSourceBuffer = mediaSourcePrototype.removeSourceBuffer;
  mediaSourcePrototype.removeSourceBuffer = function patchedRemoveSourceBuffer(buffer: SourceBuffer): void {
    nativeRemoveSourceBuffer.call(this, buffer);
    const removed = sourceBufferStates.get(buffer);
    if (removed) {
      removed.source.buffers = removed.source.buffers.filter((item) => item !== removed);
      sourceBufferStates.delete(buffer);
    }
  };
  const sourceBufferPrototype = SourceBuffer.prototype;
  const nativeAppendBuffer = sourceBufferPrototype.appendBuffer;
  sourceBufferPrototype.appendBuffer = function patchedAppendBuffer(data: AllowSharedBufferSource): void {
    const state = sourceBufferStates.get(this);
    const source = sourceFromBuffer(data);
    nativeAppendBuffer.call(this, data as BufferSource);
    if (state && source) rememberObservation(state, source);
  };
}

function observeMediaElement(media: HTMLMediaElement): void {
  stateForElement(media);
  if (observedElements.has(media)) return;
  observedElements.add(media);
  while (knownMediaElements.size >= MAX_KNOWN_MEDIA_ELEMENTS) {
    const oldest = knownMediaElements.values().next().value;
    if (!oldest) break;
    knownMediaElements.delete(oldest);
  }
  knownMediaElements.add(media);
  for (const event of ['loadedmetadata', 'durationchange', 'emptied', 'loadstart', 'play']) media.addEventListener(event, () => publishEvidence(media), { passive: true });
}

function scanMediaRoot(root: Document | ShadowRoot | Element): void {
  if (root instanceof HTMLMediaElement) {
    observeMediaElement(root);
    return;
  }
  if ('querySelectorAll' in root) {
    root.querySelectorAll('video, audio').forEach((element) => observeMediaElement(element as HTMLMediaElement));
  }
}

function observeMediaRoot(root: Document | ShadowRoot): void {
  scanMediaRoot(root);
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === 'attributes' && record.target instanceof HTMLMediaElement) observeMediaElement(record.target);
      for (const node of Array.from(record.addedNodes)) {
        if (node instanceof HTMLMediaElement) observeMediaElement(node);
        else if (node instanceof Element) scanMediaRoot(node);
      }
    }
  });
  observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ['src'] });
}

function patchShadowRoots(): void {
  const nativeAttachShadow = Element.prototype.attachShadow;
  if (typeof nativeAttachShadow !== 'function') return;
  Element.prototype.attachShadow = function patchedAttachShadow(init: ShadowRootInit): ShadowRoot {
    const root = nativeAttachShadow.call(this, init);
    if (init.mode === 'open') observeMediaRoot(root);
    return root;
  };
}

function observeMediaElements(): void {
  observeMediaRoot(document);
  patchShadowRoots();
}

patchFetch();
patchResponse();
patchStreams();
patchArrayBuffers();
patchXhr();
patchXhrResponseGetters();
patchMediaSource();
observeMediaElements();
