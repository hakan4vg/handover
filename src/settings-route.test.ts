import { describe, expect, it } from 'vitest';
import { settingsPageFromSearch } from './settings-route';

describe('settingsPageFromSearch', () => {
  it('resolves each known settings page', () => {
    for (const page of ['general', 'downloads', 'browser', 'network', 'notifications', 'appearance'] as const) {
      expect(settingsPageFromSearch(`?settings=${page}`)).toBe(page);
    }
  });

  it('defaults to general when the parameter is absent', () => {
    expect(settingsPageFromSearch('')).toBe('general');
    expect(settingsPageFromSearch('?job=abc')).toBe('general');
  });

  it('defaults to general for unknown or empty values instead of rendering a blank page', () => {
    expect(settingsPageFromSearch('?settings=bogus')).toBe('general');
    expect(settingsPageFromSearch('?settings=')).toBe('general');
    expect(settingsPageFromSearch('?settings=NETWORK')).toBe('general');
  });

  it('ignores unrelated parameters', () => {
    expect(settingsPageFromSearch('?job=x&settings=browser')).toBe('browser');
  });
});
