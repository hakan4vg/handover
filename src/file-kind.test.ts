import { describe, expect, it } from 'vitest';
import { fileKind, isMediaFile } from './file-kind';

describe('file kind', () => {
  it('prefers the reported MIME over the name', () => {
    expect(fileKind('clip.bin', 'video/mp4')).toBe('video');
    expect(fileKind('track.bin', 'audio/mpeg; charset=utf-8')).toBe('audio');
    // A media name is what the source said it was, so the name still decides.
    expect(fileKind('segment.ts')).toBe('video');
  });

  it('classifies the names the manager actually sees', () => {
    expect(fileKind('Big Buck Bunny (1080p).mkv')).toBe('video');
    expect(fileKind('studio-recording.m4a')).toBe('audio');
    expect(fileKind('project-assets.zip')).toBe('archive');
    expect(fileKind('ubuntu-24.04-desktop-amd64.iso')).toBe('disk');
    expect(fileKind('design-system.pdf')).toBe('document');
    expect(fileKind('no-extension')).toBe('document');
    expect(fileKind('')).toBe('document');
  });

  it('treats only playable files as media', () => {
    expect(isMediaFile('lecture.mp4')).toBe(true);
    expect(isMediaFile('podcast.opus')).toBe(true);
    expect(isMediaFile('backup.tar.xz')).toBe(false);
    expect(isMediaFile('report.pdf', 'application/pdf')).toBe(false);
  });
});
