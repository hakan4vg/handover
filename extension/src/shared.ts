export const APP_BRIDGE_ORIGIN = 'http://127.0.0.1:38217';
export const APP_BRIDGE_TIMEOUT_MS = 1500;

export interface BrowserPolicy {
  interceptDownloads: boolean;
  showMediaButtons: boolean;
  excludedSites: string[];
}

export interface MediaFilterSettings {
  minimumSizeBytes: number;
  excludedFileTypes: string[];
}

export const DEFAULT_POLICY: BrowserPolicy = {
  interceptDownloads: true,
  showMediaButtons: true,
  excludedSites: [],
};

export const DEFAULT_MEDIA_FILTERS: MediaFilterSettings = {
  minimumSizeBytes: 0,
  excludedFileTypes: ['gif'],
};

const MIME_FILE_TYPES: Record<string, string> = {
  'audio/aac': 'aac',
  'audio/flac': 'flac',
  'audio/mpeg': 'mp3',
  'audio/mp4': 'm4a',
  'audio/ogg': 'ogg',
  'audio/wav': 'wav',
  'audio/webm': 'webm',
  'image/gif': 'gif',
  'image/webp': 'webp',
  'video/mp4': 'mp4',
  'video/ogg': 'ogv',
  'video/quicktime': 'mov',
  'video/webm': 'webm',
  'video/x-m4v': 'm4v',
};

export function mediaFileTypeFor(url: string, contentType = ''): string | undefined {
  const mime = contentType.toLowerCase().split(';', 1)[0].trim();
  if (MIME_FILE_TYPES[mime]) return MIME_FILE_TYPES[mime];
  if (mime.startsWith('audio/') || mime.startsWith('video/') || mime.startsWith('image/')) {
    const subtype = mime.slice(mime.indexOf('/') + 1).replace(/^x-/, '').split('+', 1)[0].trim();
    if (subtype) return subtype === 'mpeg' ? 'mp3' : subtype;
  }
  try {
    const parsed = new URL(url);
    const queryMime = (parsed.searchParams.get('mime') || '').toLowerCase().split(';', 1)[0].trim();
    if (MIME_FILE_TYPES[queryMime]) return MIME_FILE_TYPES[queryMime];
    if (queryMime.startsWith('audio/') || queryMime.startsWith('video/') || queryMime.startsWith('image/')) {
      const subtype = queryMime.slice(queryMime.indexOf('/') + 1).replace(/^x-/, '').split('+', 1)[0].trim();
      if (subtype) return subtype === 'mpeg' ? 'mp3' : subtype;
    }
    const path = parsed.pathname.toLowerCase();
    const extension = path.match(/\.([a-z0-9]{1,12})$/)?.[1];
    return extension || undefined;
  } catch {
    return undefined;
  }
}

export function normalizeMediaFilterType(value: string): string | undefined {
  const token = value.trim().toLowerCase();
  if (!token) return undefined;
  if (token.includes('/')) return mediaFileTypeFor('', token);
  const normalized = token.replace(/^\./, '');
  return /^[a-z0-9]{1,12}$/.test(normalized) ? normalized : undefined;
}

export function normalizeMediaFilterSettings(value: unknown): MediaFilterSettings {
  const record = value && typeof value === 'object' ? value as Partial<MediaFilterSettings> : {};
  const minimumSizeBytes = typeof record.minimumSizeBytes === 'number' && Number.isFinite(record.minimumSizeBytes) && record.minimumSizeBytes >= 0
    ? Math.min(Math.floor(record.minimumSizeBytes), Number.MAX_SAFE_INTEGER)
    : DEFAULT_MEDIA_FILTERS.minimumSizeBytes;
  const excludedFileTypes = Array.isArray(record.excludedFileTypes)
    ? [...new Set(record.excludedFileTypes.filter((item): item is string => typeof item === 'string').map(normalizeMediaFilterType).filter((item): item is string => !!item))]
    : [...DEFAULT_MEDIA_FILTERS.excludedFileTypes];
  return { minimumSizeBytes, excludedFileTypes };
}

export function isMediaFilterExcluded(settings: MediaFilterSettings, url: string, contentType = ''): boolean {
  const type = mediaFileTypeFor(url, contentType);
  return !!type && settings.excludedFileTypes.includes(type);
}

export function siteOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '').toLowerCase();
  } catch {
    return '';
  }
}

export function siteOfDocument(url: string, referrer: string, ancestorOrigin = ''): string {
  return siteOf(url) || siteOf(referrer) || siteOf(ancestorOrigin);
}

export function mediaSourceFromValues(
  currentSrc: string | null | undefined,
  elementSrc: string | null | undefined,
  childSrc: string | null | undefined,
  baseUrl: string,
): string {
  const candidates = [currentSrc, elementSrc, childSrc]
    .map((value) => value?.trim() || '')
    .filter(Boolean)
    .map((raw) => {
      try {
        return new URL(raw, baseUrl).href;
      } catch {
        return raw;
      }
    });
  return candidates.find((candidate) => isHttp(candidate)) || candidates[0] || '';
}

export function isHttp(url: string): boolean {
  try {
    const scheme = new URL(url).protocol;
    return scheme === 'http:' || scheme === 'https:';
  } catch {
    return false;
  }
}
