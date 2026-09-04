import { describe, expect, it } from 'vitest';
import { sanitizeSettingsPatch } from './adapters';

describe('sanitizeSettingsPatch', () => {
  it('strips blank folder paths but keeps the good keys around them', () => {
    expect(sanitizeSettingsPatch({ defaultFolder: '   ', tempFolder: '', maxConnections: 6 })).toEqual({ maxConnections: 6 });
  });

  it('keeps non-blank folders', () => {
    expect(sanitizeSettingsPatch({ defaultFolder: '/new-dl' })).toEqual({ defaultFolder: '/new-dl' });
  });

  it('passes anything else through untouched', () => {
    expect(sanitizeSettingsPatch({ maxRetries: 3 })).toEqual({ maxRetries: 3 });
    expect(sanitizeSettingsPatch({})).toEqual({});
  });
});
