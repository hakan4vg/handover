// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => {
  document.body.replaceChildren();
  vi.resetModules();
  vi.unstubAllGlobals();
});

describe('early ordinary-download capture', () => {
  it('claims an explicit download before the asynchronous policy read resolves', async () => {
    let resolvePolicy: (value: unknown) => void = () => undefined;
    const policy = new Promise((resolve) => { resolvePolicy = resolve; });
    const sendMessage = vi.fn((message: { type?: string }) => (
      message.type === 'get-policy' ? policy : Promise.resolve({ ok: true })
    ));
    vi.stubGlobal('chrome', {
      runtime: { sendMessage },
      storage: { onChanged: { addListener: vi.fn() } },
    });

    await import('./content');

    const anchor = document.createElement('a');
    anchor.href = 'https://cdn.example.test/file.zip';
    anchor.download = 'file.zip';
    document.body.appendChild(anchor);
    const event = new MouseEvent('click', { button: 0, bubbles: true, cancelable: true });
    anchor.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(true);
    expect(sendMessage).toHaveBeenCalledWith(expect.objectContaining({ type: 'ordinary-capture' }));

    resolvePolicy({ ok: true, policy: { interceptDownloads: true, showMediaButtons: true, excludedSites: [] } });
  });
});
