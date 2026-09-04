const FALLBACK_MARKER = 'data-dm-browser-fallback';

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
