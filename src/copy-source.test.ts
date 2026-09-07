// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { copySourceUrl } from './App';

const originalClipboard = navigator.clipboard;

afterEach(() => {
  Object.defineProperty(navigator, 'clipboard', { value: originalClipboard, configurable: true });
});

describe('copySourceUrl', () => {
  it('returns a visible error when clipboard access is denied', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('clipboard denied'));
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });

    await expect(copySourceUrl('https://example.com/file.bin')).resolves.toBe('clipboard denied');
  });
});
