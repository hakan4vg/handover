const FALLBACK_MARKER = 'data-dm-browser-fallback';

/**
 * The click was already preventDefaulted before the worker answered, so any
 * answer other than an explicit success must replay the browser download.
 * Background already tried the downloads API before answering ok:false; the
 * synthetic anchor is the last resort and uses a different mechanism.
 */
export function captureNeedsBrowserRestore(response: unknown): boolean {
  if (!response || typeof response !== 'object') return true;
  return (response as { ok?: unknown }).ok !== true;
}

/**
 * Replays an anchor after the content script had to prevent its original
 * click. The marker lets the capture listener ignore this synthetic click so
 * it cannot recurse into itself.
 */
export function restoreBrowserDownload(source: string, name?: string): void {
  const anchor = document.createElement('a');
  anchor.href = source;
  anchor.setAttribute(FALLBACK_MARKER, 'true');
  if (name) anchor.download = name;
  anchor.hidden = true;
  (document.body ?? document.documentElement).appendChild(anchor);
  anchor.click();
  anchor.remove();
}
