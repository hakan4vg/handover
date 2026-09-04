import { describe, expect, it } from 'vitest';
import { sanitizeSettingsPatch } from './adapters';

describe('sanitizeSettingsPatch', () => {
  it('strips blank folder paths but keeps the good keys around them', () => {
    expect(sanitizeSettingsPatch({ defaultFolder: '   ', tempFolder: '', maxConnections: 6 })).toEqual({ maxConnections: 6 });
  });

  it('keeps non-blank folders', () => {
    expect(sanitizeSettingsPatch({ defaultFolder: '/new-dl' })).toEqual({ defaultFolder: '/new-dl' });
  });

  it('drops malformed enums and numeric limits', () => {
    const malformed = {
      closeBehavior: 'unexpected',
      collisionBehavior: 'overwrite-everything',
      bandwidthLimit: 0,
      bandwidthUnit: 'bits/s',
      maxConnections: 0,
      maxRetries: 21,
      theme: 'neon',
      density: 'tiny',
    } as unknown as Parameters<typeof sanitizeSettingsPatch>[0];
    expect(sanitizeSettingsPatch(malformed)).toEqual({});
  });

  it('keeps valid bounded settings and an explicit unlimited value', () => {
    expect(sanitizeSettingsPatch({ closeBehavior: 'exit', collisionBehavior: 'rename', bandwidthLimit: null, bandwidthUnit: 'MB/s', maxConnections: 32, maxRetries: 20, theme: 'dark', density: 'compact' })).toEqual({ closeBehavior: 'exit', collisionBehavior: 'rename', bandwidthLimit: null, bandwidthUnit: 'MB/s', maxConnections: 32, maxRetries: 20, theme: 'dark', density: 'compact' });
  });

  it('passes anything else through untouched', () => {
    expect(sanitizeSettingsPatch({ maxRetries: 3 })).toEqual({ maxRetries: 3 });
    expect(sanitizeSettingsPatch({})).toEqual({});
  });
});
