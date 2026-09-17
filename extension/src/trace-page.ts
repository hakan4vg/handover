/**
 * Content-side trace helpers (branch `harness/capture-traces` only).
 *
 * Imported exclusively by `content.ts` so the bundler inlines it: content
 * scripts are classic scripts and cannot load ES module chunks. Everything
 * here is pure DOM work — snapshots, change signatures, diffs, censuses.
 * Network classification lives in `trace.ts` (background), because the page
 * bridge only ships raw byte samples.
 */
import type { ElementCensusEntry, PlayerStateSummary, SnapshotNode, WrapperSummary } from './trace-schema';

const SNAPSHOT_MAX_DEPTH = 7;
const SNAPSHOT_MAX_NODES = 320;
const SNAPSHOT_MAX_KIDS = 12;
const SNAPSHOT_MAX_TEXT = 90;
const ATTR_VALUE_MAX = 140;
const CLASS_MAX = 5;
const WRAPPER_MAX_DEPTH = 6;

// Attribute names worth keeping; everything else is preserved only when it
// looks deliberate (data-*, aria-*), and long/volatile values are clamped.
const ATTR_KEEP = new Set([
  'id', 'src', 'type', 'role', 'poster', 'preload', 'controls', 'autoplay', 'loop',
  'muted', 'playsinline', 'crossorigin', 'width', 'height', 'title', 'name', 'srcset',
  'sizes', 'media', 'for', 'href', 'target', 'rel', 'hidden', 'inert', 'tabindex',
  'disablepictureinpicture', 'disableremoteplayback', 'x-webkit-airplay',
]);

const STYLE_NOISE = /\d+(?:\.\d+)?(?:px|%|s|ms|em|rem|vh|vw)/gi;
const URL_NOISE = /https?:\/\/[^\s"')]+/gi;
// Live clocks and progress readouts churn every frame on most players; they
// are not structure, so both the payload and the signature store them masked.
const CLOCK_NOISE = /^(?:\d{1,3}:)?\d{1,3}:\d{2}(?:\.\d+)?$/;
const NUMBER_NOISE = /^\d+(?:[.,]\d+)?%?$/;

function stabilizeText(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return '';
  if (CLOCK_NOISE.test(trimmed) || NUMBER_NOISE.test(trimmed)) return '#';
  return trimmed;
}

function clamp(value: string, max: number): string {
  return value.length <= max ? value : `${value.slice(0, max)}…`;
}

/** Numbers in style/class strings are normalized so animations do not read as
 *  structural change; URLs are masked so signed tokens do not churn the
 *  signature every few seconds. */
function stabilize(value: string): string {
  return value.replace(STYLE_NOISE, '#').replace(URL_NOISE, (url) => `url(${hostOf(url)})`);
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return 'invalid';
  }
}

function keepAttr(name: string): boolean {
  if (ATTR_KEEP.has(name)) return true;
  if (name.startsWith('data-') || name.startsWith('aria-')) return true;
  return false;
}

function attrValue(name: string, value: string): string {
  if (name === 'style') return clamp(stabilize(value), ATTR_VALUE_MAX);
  if (/^(?:src|srcset|poster|href)$/.test(name)) {
    // Keep the shape of the URL, not its one-use tokens.
    try {
      const url = new URL(value, location.href);
      return `${url.origin}${url.pathname}`;
    } catch {
      return clamp(stabilize(value), ATTR_VALUE_MAX);
    }
  }
  return clamp(value, ATTR_VALUE_MAX);
}

function classesOf(el: Element): string[] | undefined {
  const list = el.classList;
  if (!list.length) return undefined;
  return Array.from(list).slice(0, CLASS_MAX).map((name) => clamp(name, 60));
}

function attrsOf(el: Element): Record<string, string> | undefined {
  const out: Record<string, string> = {};
  let kept = 0;
  for (const attr of Array.from(el.attributes)) {
    if (kept >= 18) break;
    const name = attr.name.toLowerCase();
    if (!keepAttr(name)) continue;
    out[name] = attrValue(name, attr.value);
    kept += 1;
  }
  return kept ? out : undefined;
}

function textOf(el: Element): string | undefined {
  if (el.childElementCount > 0) return undefined;
  const text = el.textContent?.trim().replace(/\s+/g, ' ');
  if (!text) return undefined;
  const stabilized = stabilizeText(text);
  return stabilized ? clamp(stabilized, SNAPSHOT_MAX_TEXT) : undefined;
}

interface SnapshotBudget {
  nodes: number;
}

function snapshotNode(node: Node, path: string, depth: number, budget: SnapshotBudget): SnapshotNode | undefined {
  if (budget.nodes <= 0) return undefined;
  if (node.nodeType === Node.COMMENT_NODE) return undefined;

  if (node.nodeType === Node.TEXT_NODE) return undefined;

  if (!(node instanceof Element)) {
    if (node instanceof ShadowRoot) {
      const children: SnapshotNode[] = [];
      for (const child of Array.from(node.children)) {
        if (budget.nodes <= 0) break;
        const built = snapshotNode(child, `${path}>#shadow`, depth + 1, budget);
        if (built) children.push(built);
      }
      budget.nodes -= 1;
      return { tag: '#shadow-root', path, shadow: 'open', kids: node.children.length, children };
    }
    return undefined;
  }

  budget.nodes -= 1;
  const el = node as Element;
  const tag = el.tagName.toLowerCase();
  const nodePath = path ? `${path}>${tag}` : tag;
  let shadow: 'open' | 'closed' | undefined;
  const children: SnapshotNode[] = [];
  let realKids = el.childElementCount;

  if (depth < SNAPSHOT_MAX_DEPTH && budget.nodes > 0) {
    const shadowRoot = (el as HTMLElement).shadowRoot;
    if (shadowRoot) {
      shadow = 'open';
      for (const child of Array.from(shadowRoot.children)) {
        if (children.length >= SNAPSHOT_MAX_KIDS || budget.nodes <= 0) break;
        const built = snapshotNode(child, `${nodePath}>#shadow`, depth + 1, budget);
        if (built) children.push(built);
      }
      realKids += shadowRoot.childElementCount;
    }
    for (const child of Array.from(el.children)) {
      if (children.length >= SNAPSHOT_MAX_KIDS || budget.nodes <= 0) break;
      const built = snapshotNode(child, nodePath, depth + 1, budget);
      if (built) children.push(built);
    }
  }

  const cls = classesOf(el);
  const attrs = attrsOf(el);
  const text = textOf(el);
  return {
    tag,
    path: nodePath,
    ...(el.id ? { id: clamp(el.id, 80) } : {}),
    ...(cls ? { cls } : {}),
    ...(attrs ? { attrs } : {}),
    ...(text ? { text } : {}),
    ...(shadow ? { shadow } : {}),
    kids: realKids,
    children,
  };
}

export function snapshotElement(el: Element, maxNodes = SNAPSHOT_MAX_NODES): SnapshotNode | undefined {
  const budget: SnapshotBudget = { nodes: maxNodes };
  return snapshotNode(el, '', 0, budget);
}

/** The change signature of a snapshot: same structure ⇒ same string. */
export function signatureOf(node: SnapshotNode | undefined): string {
  if (!node) return '';
  const walk = (item: SnapshotNode): unknown => [
    item.path,
    item.id ?? '',
    item.cls ?? [],
    item.attrs ?? {},
    item.text ?? '',
    item.shadow ?? '',
    item.kids === item.children.length ? 0 : item.kids,
    item.children.map(walk),
  ];
  return JSON.stringify(walk(node));
}

export function hashString(value: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, '0');
}

interface FlatEntry {
  path: string;
  sig: string;
}

function flatten(node: SnapshotNode, out: FlatEntry[], counter: { n: number }): void {
  if (counter.n > 2000) return;
  counter.n += 1;
  const { children, ...head } = node;
  out.push({ path: node.path, sig: JSON.stringify(head) });
  for (const child of children) flatten(child, out, counter);
}

export interface SnapshotDiff {
  added: string[];
  removed: string[];
  changed: string[];
  addedCount: number;
  removedCount: number;
  changedCount: number;
}

export function diffSnapshots(previous: SnapshotNode | undefined, next: SnapshotNode | undefined): SnapshotDiff {
  const before: FlatEntry[] = [];
  const after: FlatEntry[] = [];
  if (previous) flatten(previous, before, { n: 0 });
  if (next) flatten(next, after, { n: 0 });
  const beforeMap = new Map<string, string>();
  for (const entry of before) beforeMap.set(entry.path, entry.sig);
  const afterMap = new Map<string, string>();
  for (const entry of after) afterMap.set(entry.path, entry.sig);
  const added: string[] = [];
  const removed: string[] = [];
  const changed: string[] = [];
  for (const entry of after) {
    const old = beforeMap.get(entry.path);
    if (old === undefined) added.push(entry.path);
    else if (old !== entry.sig) changed.push(entry.path);
  }
  for (const entry of before) {
    if (!afterMap.has(entry.path)) removed.push(entry.path);
  }
  const LIMIT = 12;
  return {
    added: added.slice(0, LIMIT),
    removed: removed.slice(0, LIMIT),
    changed: changed.slice(0, LIMIT),
    addedCount: added.length,
    removedCount: removed.length,
    changedCount: changed.length,
  };
}

function roundedRect(el: Element): { x: number; y: number; w: number; h: number } {
  const rect = el.getBoundingClientRect();
  return { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) };
}

export function playerStateSummary(el: HTMLMediaElement): PlayerStateSummary {
  const currentSrc = el.currentSrc || '';
  const sourceChildren = Array.from(el.querySelectorAll('source[src]'))
    .slice(0, 4)
    .map((child) => child.getAttribute('src') ?? '')
    .filter((value) => !!value);
  const ownShadow = el.shadowRoot ? 'open' : 'none';
  return {
    tag: el.tagName.toLowerCase(),
    paused: el.paused,
    ended: el.ended,
    muted: el.muted,
    volume: Number(el.volume.toFixed(3)),
    readyState: el.readyState,
    networkState: el.networkState,
    currentTime: Number(el.currentTime.toFixed(2)),
    duration: Number.isFinite(el.duration) ? Number(el.duration.toFixed(2)) : null,
    playbackRate: el.playbackRate,
    currentSrcKind: currentSrc.startsWith('blob:') ? 'blob' : currentSrc.startsWith('http') ? 'http' : currentSrc ? 'other' : 'none',
    currentSrc: clamp(currentSrc, 400),
    ...(el.getAttribute('src') ? { srcAttr: clamp(el.getAttribute('src') ?? '', 400) } : {}),
    ...(sourceChildren.length ? { sourceChildren } : {}),
    ...(el.getAttribute('poster') ? { poster: clamp(el.getAttribute('poster') ?? '', 300) } : {}),
    inShadowRoot: typeof ShadowRoot !== 'undefined' && el.getRootNode() instanceof ShadowRoot,
    ownShadow,
    usesMediaSource: currentSrc.startsWith('blob:') || !!el.querySelector('source'),
  };
}

export function wrapperChain(el: Element, maxDepth = WRAPPER_MAX_DEPTH): WrapperSummary[] {
  const out: WrapperSummary[] = [];
  let node: Element | null = el.parentElement;
  for (let depth = 0; node && depth < maxDepth; depth += 1) {
    if (node === document.body || node === document.documentElement) break;
    const summary: WrapperSummary = {
      tag: node.tagName.toLowerCase(),
      ...(node.id ? { id: clamp(node.id, 80) } : {}),
      ...(classesOf(node) ? { cls: classesOf(node) } : {}),
      ...(attrsOf(node) ? { attrs: attrsOf(node) } : {}),
      rect: (() => {
        const rect = roundedRect(node as Element);
        return { w: rect.w, h: rect.h };
      })(),
    };
    const text = node.textContent?.trim().replace(/\s+/g, ' ');
    if (text) {
      const stabilized = stabilizeText(text);
      if (stabilized) summary.text = clamp(stabilized, 60);
    }
    out.push(summary);
    node = node.parentElement;
  }
  return out;
}

export function censusEntry(
  el: HTMLMediaElement,
  key: string,
  anchor: { x: number; y: number; w: number; h: number },
  visible: boolean,
  playerKey?: string,
): ElementCensusEntry {
  return {
    key,
    ...(playerKey ? { playerKey } : {}),
    state: playerStateSummary(el),
    rect: roundedRect(el),
    visible,
    attached: el.isConnected,
    anchor,
    wrappers: wrapperChain(el),
  };
}

/** Stable identity for an element inside one page world (survives re-renders
 *  of unrelated subtrees, unlike array indices). */
const elementKeys = new WeakMap<Element, string>();
let nextElementKey = 1;

export function elementKey(el: Element): string {
  const existing = elementKeys.get(el);
  if (existing) return existing;
  const key = `el-${nextElementKey++}`;
  elementKeys.set(el, key);
  return key;
}
