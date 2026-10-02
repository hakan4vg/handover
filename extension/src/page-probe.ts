// Runs in the page's own JavaScript world, which is the only place that can
// see what a <video> or <audio> element is fed. A Media Source Extensions
// player creates a MediaSource, hands the element a blob: URL for it, adds a
// SourceBuffer per track and appends the bytes it downloaded. Remembering, per
// MediaSource, each SourceBuffer's MIME type and the size and time of its last
// appends tells exactly what the element plays, without knowing anything about
// the site: the extension matches those sizes to the tab's responses.
//
// It wraps four functions (URL.createObjectURL, MediaSource#addSourceBuffer,
// SourceBuffer#appendBuffer and #changeType), each a constant-time note, and
// answers only when the extension asks about one element. It declares nothing
// in the page's scope (the build wraps it), and the wrappers keep the
// originals' names and native-looking source text.

const REQUEST = 'dm-player-evidence-request';
const ANSWER = 'dm-player-evidence';
/** Appends remembered per SourceBuffer: the most recent ones. */
const APPENDS_KEPT = 96;
/** blob: URLs remembered (a page revokes and recreates them). */
const SOURCES_KEPT = 64;

interface TrackRecord {
  mime: string;
  /** [byte length, time] */
  appends: Array<[number, number]>;
}

const sources = new Map<string, WeakRef<MediaSource>>();
const tracksOf = new WeakMap<MediaSource, TrackRecord[]>();
const trackOf = new WeakMap<SourceBuffer, TrackRecord>();

function wrap<T extends object>(target: T | undefined, name: string, replacement: (original: (...args: unknown[]) => unknown, self: unknown, args: unknown[]) => unknown): void {
  const holder = target as Record<string, unknown> | undefined;
  const original = holder?.[name];
  if (!holder || typeof original !== 'function') return;
  // A Proxy keeps the original's name, length and source text.
  holder[name] = new Proxy(original, {
    apply: (fn, self, args: unknown[]) => replacement(fn as (...args: unknown[]) => unknown, self, args),
  });
}

function byteLength(data: unknown): number | undefined {
  return ArrayBuffer.isView(data) || data instanceof ArrayBuffer ? data.byteLength : undefined;
}

if (typeof MediaSource === 'function' && typeof SourceBuffer === 'function') {
  wrap(URL, 'createObjectURL', (original, self, args) => {
    const url = Reflect.apply(original, self, args);
    if (args[0] instanceof MediaSource && typeof url === 'string') {
      sources.set(url, new WeakRef(args[0]));
      if (sources.size > SOURCES_KEPT) sources.delete(sources.keys().next().value as string);
    }
    return url;
  });

  wrap(MediaSource.prototype, 'addSourceBuffer', (original, self, args) => {
    const buffer = Reflect.apply(original, self, args);
    if (self instanceof MediaSource && buffer instanceof SourceBuffer) {
      const track: TrackRecord = { mime: String(args[0]).slice(0, 200), appends: [] };
      trackOf.set(buffer, track);
      const tracks = tracksOf.get(self) ?? [];
      tracks.push(track);
      tracksOf.set(self, tracks);
    }
    return buffer;
  });

  wrap(SourceBuffer.prototype, 'appendBuffer', (original, self, args) => {
    const track = self instanceof SourceBuffer ? trackOf.get(self) : undefined;
    const bytes = byteLength(args[0]);
    if (track && bytes !== undefined) {
      track.appends.push([bytes, Date.now()]);
      if (track.appends.length > APPENDS_KEPT) track.appends.shift();
    }
    return Reflect.apply(original, self, args);
  });

  wrap(SourceBuffer.prototype, 'changeType', (original, self, args) => {
    const track = self instanceof SourceBuffer ? trackOf.get(self) : undefined;
    if (track) track.mime = String(args[0]).slice(0, 200);
    return Reflect.apply(original, self, args);
  });

  // The extension's content script asks about one element by dispatching an
  // event on it; the answer goes back on the same element.
  document.addEventListener(REQUEST, (event) => {
    // A player in an open shadow root reaches the document as its host.
    const element = event.composedPath()[0] ?? event.target;
    if (!(element instanceof HTMLMediaElement)) return;
    const source = sources.get(element.currentSrc || element.src)?.deref();
    const tracks = source ? tracksOf.get(source) ?? [] : [];
    element.dispatchEvent(new CustomEvent(ANSWER, { detail: JSON.stringify(tracks) }));
  }, true);
}
