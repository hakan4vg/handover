// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, expect, it } from 'vitest';
import { NoticeToast } from './App';

describe('NoticeToast', () => {
  it('renders failures as failures rather than success', () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    act(() => root.render(<NoticeToast message="Download failed" tone="error" />));

    const toast = host.querySelector('.toast');
    expect(toast?.classList.contains('toast-error')).toBe(true);
    expect(toast?.getAttribute('role')).toBe('alert');
    expect(toast?.querySelector('[data-notice-icon="error"]')).not.toBeNull();

    act(() => root.unmount());
    host.remove();
  });
});
