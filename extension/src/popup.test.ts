// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

const policy = {
  interceptDownloads: true,
  showMediaButtons: true,
  excludedSites: [],
};

function mountPopup() {
  document.body.innerHTML = `
    <h1>Download Manager</h1>
    <button id="intercept" class="switch"></button>
    <button id="media" class="switch"></button>
    <strong id="site"></strong>
    <span id="site-state"></span>
    <button id="site-toggle"></button>
    <button id="open"></button>
    <div id="status" role="status" aria-live="polite" hidden></div>
  `;
}

afterEach(() => {
  vi.resetModules();
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

describe('extension popup', () => {
  it('shows a status when opening the manager fails', async () => {
    mountPopup();
    const sendMessage = vi.fn()
      .mockResolvedValueOnce({ ok: true, policy })
      .mockRejectedValueOnce(new Error('resident unavailable'));
    vi.stubGlobal('chrome', {
      tabs: { query: vi.fn().mockResolvedValue([{ url: 'https://example.com/downloads' }]) },
      runtime: { sendMessage },
    });

    await import('./popup');
    await new Promise((resolve) => setTimeout(resolve, 0));
    (document.getElementById('open') as HTMLButtonElement).click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(document.getElementById('status')?.textContent).toContain('resident unavailable');
    expect(document.getElementById('status')?.hidden).toBe(false);
  });

  it('shows the actual policy state to assistive technology', async () => {
    mountPopup();
    const sendMessage = vi.fn().mockResolvedValue({ ok: true, policy });
    vi.stubGlobal('chrome', {
      tabs: { query: vi.fn().mockResolvedValue([{ url: 'https://example.com/downloads' }]) },
      runtime: { sendMessage },
    });

    await import('./popup');
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(document.getElementById('intercept')?.getAttribute('aria-checked')).toBe('true');
    expect(document.getElementById('media')?.getAttribute('aria-checked')).toBe('true');
  });

  it('explains that site state is inactive when media buttons are off globally', async () => {
    mountPopup();
    const sendMessage = vi.fn().mockResolvedValue({ ok: true, policy: { ...policy, showMediaButtons: false } });
    vi.stubGlobal('chrome', {
      tabs: { query: vi.fn().mockResolvedValue([{ url: 'https://example.com/watch' }]) },
      runtime: { sendMessage },
    });

    await import('./popup');
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(document.getElementById('site-state')?.textContent).toBe('Media buttons are off globally');
    expect(document.getElementById('site-state')?.classList.contains('enabled-copy')).toBe(false);
  });
});
