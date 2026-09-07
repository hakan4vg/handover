// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it } from 'vitest';
import { AddDownloadWindow } from './App';
import type { AppSettings } from './types';

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

function settings(): AppSettings {
  return {
    defaultFolder: '/tmp/dm-test-downloads',
    maxConnections: 8,
    perDownloadOverrides: true,
  } as AppSettings;
}

afterEach(() => {
  document.body.replaceChildren();
});

describe('AddDownloadWindow accessibility', () => {
  it('names the form and advanced controls and exposes expansion state', () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = document.createElement('div');
    host.appendChild(root);
    const rendered = createRoot(root);
    act(() => {
      rendered.render(
        <AddDownloadWindow
          settings={settings()}
          onCancel={() => {}}
          onClose={() => {}}
        />,
      );
    });

    expect(Array.from(root.querySelectorAll('.form-field input')).map((input) => input.getAttribute('aria-label'))).toEqual([
      'Source URL',
      'Filename',
      'Save destination',
    ]);
    const advanced = root.querySelector('.advanced-toggle');
    if (!(advanced instanceof HTMLButtonElement)) throw new Error('advanced toggle missing');
    expect(advanced.getAttribute('aria-expanded')).toBe('false');

    act(() => advanced.click());

    expect(advanced.getAttribute('aria-expanded')).toBe('true');
    expect(Array.from(root.querySelectorAll('.advanced-fields input')).map((input) => input.getAttribute('aria-label'))).toEqual([
      'Per-download maximum connections',
      'Per-download bandwidth limit',
    ]);
    expect(root.querySelector('.advanced-fields select')?.getAttribute('aria-label')).toBe('Per-download bandwidth unit');
    const group = root.querySelector('.advanced-fields .radio-row');
    expect(group?.getAttribute('role')).toBe('radiogroup');
    expect(group?.getAttribute('aria-label')).toBe('Per-download bandwidth cap');
    expect(Array.from(group?.querySelectorAll('[role="radio"]') ?? [])).toHaveLength(2);

    act(() => rendered.unmount());
    host.remove();
  });
});
