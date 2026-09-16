import { describe, expect, it } from 'vitest';
import {
  DEFAULT_MEDIA_FILTERS,
  isMediaFilterExcluded,
  mediaFileTypeFor,
  normalizeMediaFilterSettings,
} from './shared';

describe('media filter settings', () => {
  it('uses a disabled size threshold and gif exclusion by default', () => {
    expect(DEFAULT_MEDIA_FILTERS).toEqual({ minimumSizeBytes: 0, excludedFileTypes: ['gif'] });
  });

  it('normalizes comma-friendly extensions and MIME values', () => {
    expect(normalizeMediaFilterSettings({ minimumSizeBytes: 12_345.9, excludedFileTypes: [' .GIF ', 'image/webp', 'GIF', ''] })).toEqual({
      minimumSizeBytes: 12_345,
      excludedFileTypes: ['gif', 'webp'],
    });
  });

  it('prefers MIME type and falls back to a direct source suffix', () => {
    expect(mediaFileTypeFor('https://cdn.test/asset.bin', 'image/gif')).toBe('gif');
    expect(mediaFileTypeFor('https://cdn.test/asset.webp?token=1')).toBe('webp');
    expect(mediaFileTypeFor('https://cdn.test/videoplayback?mime=video%2Fmp4')).toBe('mp4');
  });

  it('does not invent a type for extensionless unknown media', () => {
    const settings = normalizeMediaFilterSettings({ minimumSizeBytes: 0, excludedFileTypes: ['gif'] });
    expect(mediaFileTypeFor('https://cdn.test/videoplayback')).toBeUndefined();
    expect(isMediaFilterExcluded(settings, 'https://cdn.test/videoplayback')).toBe(false);
  });
});
