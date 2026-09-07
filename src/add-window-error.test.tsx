// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AddDownloadWindow } from './App';
import type { AppSettings } from './types';

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

function settings(): AppSettings {
  return {
    defaultFolder: '/tmp/dm-test-downloads',
    maxConnections: 8,
    perDownloadOverrides: false,
  } as AppSettings;
}

function setInputValue(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

afterEach(() => {
  document.body.replaceChildren();
});

describe('AddDownloadWindow creation feedback', () => {
  it('shows a form error when provisional creation fails', async () => {
    const onCreate = vi.fn().mockRejectedValue(new Error('Native core unavailable'));
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    act(() => {
      root.render(
        <AddDownloadWindow
          settings={settings()}
          onCreate={onCreate}
          onCancel={() => {}}
          onClose={() => {}}
        />,
      );
    });

    const source = host.querySelector('input[placeholder="https://example.com/file.iso"]');
    const submit = host.querySelector('.add-actions .button.primary');
    if (!(source instanceof HTMLInputElement) || !(submit instanceof HTMLButtonElement)) throw new Error('manual form controls missing');
    act(() => setInputValue(source, 'https://example.com/file.iso'));
    await act(async () => {
      submit.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(onCreate).toHaveBeenCalledWith('https://example.com/file.iso', '', 8, null);
    expect(host.querySelector('[role="alert"]')?.textContent).toContain('Native core unavailable');
    act(() => root.unmount());
  });
});
