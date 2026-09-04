// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { captureNeedsBrowserRestore, restoreBrowserDownload } from './download-fallback';

describe('restoreBrowserDownload', () => {
  it('replays the prevented anchor as a browser download', () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      this.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    });
    const seen: { href: string; download: string; marker: string | null }[] = [];
    document.addEventListener('click', (event) => {
      const anchor = event.target as HTMLAnchorElement;
      seen.push({ href: anchor.href, download: anchor.download, marker: anchor.getAttribute('data-dm-browser-fallback') });
      event.preventDefault();
    }, { once: true });

    restoreBrowserDownload('https://cdn.example.test/file.zip', 'file.zip');

    expect(seen).toEqual([{
      href: 'https://cdn.example.test/file.zip',
      download: 'file.zip',
      marker: 'true',
    }]);
    expect(document.querySelector('[data-dm-browser-fallback]')).toBeNull();
    click.mockRestore();
  });
});

describe('captureNeedsBrowserRestore', () => {
  it('restores only when the worker did not report success', () => {
    expect(captureNeedsBrowserRestore({ ok: true })).toBe(false);
    expect(captureNeedsBrowserRestore({ ok: false, error: 'native host and browser fallback unavailable' })).toBe(true);
    expect(captureNeedsBrowserRestore({})).toBe(true);
    expect(captureNeedsBrowserRestore(null)).toBe(true);
    expect(captureNeedsBrowserRestore(undefined)).toBe(true);
  });
});
