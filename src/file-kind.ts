/** What kind of file a download is, judged by the only evidence the manager
 *  has: the name it will be saved under and the MIME the source reported. The
 *  core does not classify files, so the row icon and the mock's media flag come
 *  from one rule instead of from a field nothing ever filled. */
export type FileKind = 'video' | 'audio' | 'archive' | 'disk' | 'document';

const VIDEO_EXTENSIONS = ['mp4', 'm4v', 'mkv', 'webm', 'mov', 'ogv', 'ts', 'flv', 'wmv'];
const AUDIO_EXTENSIONS = ['mp3', 'm4a', 'aac', 'wav', 'ogg', 'oga', 'opus', 'flac'];
const ARCHIVE_EXTENSIONS = ['zip', 'tar', 'gz', 'bz2', 'xz', '7z', 'rar'];
const DISK_EXTENSIONS = ['iso', 'img'];

export function fileKind(name: string, mime?: string | null): FileKind {
  const type = (mime ?? '').toLowerCase();
  if (type.startsWith('video/')) return 'video';
  if (type.startsWith('audio/')) return 'audio';
  const extension = name.split('.').pop()?.toLowerCase() ?? '';
  if (VIDEO_EXTENSIONS.includes(extension)) return 'video';
  if (AUDIO_EXTENSIONS.includes(extension)) return 'audio';
  if (ARCHIVE_EXTENSIONS.includes(extension)) return 'archive';
  if (DISK_EXTENSIONS.includes(extension)) return 'disk';
  return 'document';
}

/** Media rows are the ones the Media filter and the Media inspector tab are
 *  about; anything that plays is one. */
export function isMediaFile(name: string, mime?: string | null): boolean {
  const kind = fileKind(name, mime);
  return kind === 'video' || kind === 'audio';
}
