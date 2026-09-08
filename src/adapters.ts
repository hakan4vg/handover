import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { sanitizeCapBps } from './bandwidth';
import type {
  AppSettings,
  AppSnapshot,
  DownloadAdapter,
  DownloadJob,
  JobEvent,
} from './types';

const SETTINGS_KEY = 'download-manager.settings';

const now = () => new Date();
const isoNow = () => now().toISOString();
const timeLabel = (date = now()) => date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
const mockPlatform = typeof navigator === 'undefined' ? '' : `${navigator.userAgent} ${navigator.platform}`.toLowerCase();
const isWindowsMock = mockPlatform.includes('windows');
const mockDefaultFolder = isWindowsMock ? 'C:\\Users\\mroth\\Downloads' : '~/Downloads';
const mockTempFolder = isWindowsMock ? 'C:\\Users\\mroth\\AppData\\Local\\DM\\Temp' : '~/.cache/download-manager/tmp';

function platformPath(value: string) {
  if (isWindowsMock) return value;
  return value
    .replace('C:\\Users\\mroth\\Downloads', mockDefaultFolder)
    .replace('C:\\Users\\mroth\\AppData\\Local\\DM\\Temp', mockTempFolder)
    .replaceAll('\\', '/');
}

const defaults: AppSettings = {
  startAtSignIn: true,
  showManagerAtSignIn: true,
  closeBehavior: 'tray',
  defaultFolder: mockDefaultFolder,
  tempFolder: mockTempFolder,
  collisionBehavior: 'rename',
  interceptDownloads: true,
  showMediaButtons: true,
  excludedSites: ['twitter.com', 'reddit.com', 'instagram.com', 'tiktok.com'],
  bandwidthLimit: null,
  bandwidthUnit: 'MB/s',
  maxConnections: 8,
  perDownloadOverrides: true,
  retryAutomatically: true,
  maxRetries: 5,
  completionNotifications: true,
  failureNotifications: true,
  theme: 'system',
  accent: '#0878ed',
  density: 'comfortable',
};

function readSettings(): AppSettings {
  try {
    const saved = localStorage.getItem(SETTINGS_KEY);
    return saved ? { ...defaults, ...JSON.parse(saved) } : { ...defaults };
  } catch {
    return { ...defaults };
  }
}

function persistSettings(settings: AppSettings) {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    return;
  }
}

/**
 * Client-side parity guard for the native settings patch contract.
 *
 * Rust's apply_settings_patch remains authoritative for schema validation and
 * persistence. This helper mirrors its supported enum and numeric bounds so
 * mock/native adapter paths do not accept values the native core will ignore.
 */
export function sanitizeSettingsPatch(patch: Partial<AppSettings>): Partial<AppSettings> {
  const next = { ...patch };
  if (typeof next.defaultFolder === 'string' && next.defaultFolder.trim().length === 0) delete next.defaultFolder;
  if (typeof next.tempFolder === 'string' && next.tempFolder.trim().length === 0) delete next.tempFolder;
  if (next.closeBehavior !== undefined && !['tray', 'exit'].includes(next.closeBehavior)) delete next.closeBehavior;
  if (next.collisionBehavior !== undefined && !['rename', 'replace'].includes(next.collisionBehavior)) delete next.collisionBehavior;
  if (next.bandwidthLimit !== undefined && next.bandwidthLimit !== null && (!Number.isInteger(next.bandwidthLimit) || next.bandwidthLimit <= 0)) delete next.bandwidthLimit;
  if (next.bandwidthUnit !== undefined && !['KB/s', 'MB/s', 'GB/s'].includes(next.bandwidthUnit)) delete next.bandwidthUnit;
  if (next.maxConnections !== undefined && (!Number.isInteger(next.maxConnections) || next.maxConnections < 1 || next.maxConnections > 32)) delete next.maxConnections;
  if (next.maxRetries !== undefined && (!Number.isInteger(next.maxRetries) || next.maxRetries < 0 || next.maxRetries > 20)) delete next.maxRetries;
  if (next.theme !== undefined && !['system', 'light', 'dark'].includes(next.theme)) delete next.theme;
  if (next.density !== undefined && !['comfortable', 'compact'].includes(next.density)) delete next.density;
  return next;
}

export function resumePlan(provisional: DownloadJob['provisional'], progress: number): { state: 'finalizing' | 'downloading'; shouldStart: boolean } {
  const ready = provisional === true && progress >= 100;
  return ready ? { state: 'finalizing', shouldStart: false } : { state: 'downloading', shouldStart: true };
}

function event(message: string, tone: JobEvent['tone'] = 'normal'): JobEvent {
  return { at: timeLabel(), message, tone };
}

function guessKind(name: string): DownloadJob['kind'] {
  const ext = name.split('.').pop()?.toLowerCase();
  if (['mp4', 'mkv', 'webm', 'mov'].includes(ext ?? '')) return 'video';
  if (['mp3', 'm4a', 'aac', 'wav'].includes(ext ?? '')) return 'audio';
  if (['zip', 'tar', 'gz', 'xz', '7z', 'rar'].includes(ext ?? '')) return 'archive';
  if (ext === 'iso' || ext === 'img') return 'disk';
  return 'document';
}

function sourceName(source: string) {
  try {
    const url = new URL(source);
    const segment = decodeURIComponent(url.pathname.split('/').filter(Boolean).pop() ?? 'download');
    return segment.includes('.') ? segment : `${segment || 'download'}.bin`;
  } catch {
    return 'download.bin';
  }
}

function domainOf(source: string) {
  try {
    return new URL(source).hostname.replace(/^www\./, '');
  } catch {
    return 'source unavailable';
  }
}

function initialJobs(): DownloadJob[] {
  const base = 'C:\\Users\\mroth\\Downloads';
  const temp = 'C:\\Users\\mroth\\AppData\\Local\\DM\\Temp';
  const make = (job: Omit<DownloadJob, 'events'> & { events?: JobEvent[] }): DownloadJob => ({
    ...job,
    events: job.events ?? [event('Acquisition created')],
  });
  return [
    make({
      id: 'job-1', name: 'ubuntu-24.04-desktop-amd64.iso', source: 'https://releases.ubuntu.com/24.04/ubuntu-24.04-desktop-amd64.iso', domain: 'releases.ubuntu.com', kind: 'disk', state: 'downloading', progress: 49.8, downloaded: 2.34 * 1024 ** 3, total: 4.70 * 1024 ** 3, speed: 42.3 * 1024 ** 2, eta: '36s left', connections: 4, maxConnections: 16, mode: 'whole-object', media: false, destination: `${base}\\ubuntu-24.04-desktop-amd64.iso`, tempPath: `${temp}\\job-1.part`, resumable: true, mime: 'application/x-iso9660-image', created: 'Today, 9:41 AM', started: 'Today, 9:41 AM', events: [event('Range support verified'), event('4 workers downloading', 'success')],
    }),
    make({
      id: 'job-2', name: 'Big Buck Bunny (1080p).mkv', source: 'https://media.example.org/vod/big-buck-bunny/1080p/manifest.mpd', domain: 'media.example.org', kind: 'video', state: 'finalizing', progress: 95, downloaded: 1.18 * 1024 ** 3, total: 1.28 * 1024 ** 3, speed: 0, eta: 'Finalizing', connections: 4, maxConnections: 16, mode: 'segments', media: true, mediaDetails: '1080p · H.264 + AAC', destination: `${base}\\Big Buck Bunny (1080p).mkv`, tempPath: `${temp}\\job-2\\`, resumable: true, mime: 'video/x-matroska', created: 'Today, 9:41 AM', started: 'Today, 9:41 AM', segments: { completed: 118, total: 124 }, events: [event('Manifest parsed: 124 fragments'), event('Media parts acquired', 'success'), event('Merging container (95%)', 'warning')],
    }),
    make({
      id: 'job-3', name: 'project-assets.zip', source: 'https://cdn.example.com/releases/project-assets.zip', domain: 'cdn.example.com', kind: 'archive', state: 'paused', progress: 0, downloaded: 0, total: 512 * 1024 ** 2, speed: 0, eta: 'Paused', connections: 0, maxConnections: 8, mode: 'whole-object', media: false, destination: `${base}\\project-assets.zip`, tempPath: `${temp}\\job-3.part`, resumable: true, mime: 'application/zip', created: 'Today, 9:38 AM', events: [event('Paused before start', 'warning')],
    }),
    make({
      id: 'job-4', name: 'Nature Documentary (4K).mkv', source: 'https://media.example.org/nature/4k/documentary.mkv', domain: 'media.example.org', kind: 'video', state: 'completed', progress: 100, downloaded: 2.85 * 1024 ** 3, total: 2.85 * 1024 ** 3, speed: 0, eta: undefined, connections: 0, maxConnections: 8, mode: 'segments', media: true, mediaDetails: '2160p · HEVC + AAC', destination: `${base}\\Nature Documentary (4K).mkv`, tempPath: `${temp}\\job-4\\`, resumable: true, mime: 'video/x-matroska', created: 'Yesterday, 3:12 PM', started: 'Yesterday, 3:12 PM', completed: 'Yesterday, 3:27 PM', segments: { completed: 284, total: 284 }, events: [event('Download completed', 'success')],
    }),
    make({
      id: 'job-5', name: 'Fedora-Workstation-Live-x86_64.iso', source: 'https://download.fedoraproject.org/pub/fedora/linux/releases/41/Workstation/x86_64/iso/Fedora-Workstation-Live-x86_64.iso', domain: 'download.fedoraproject.org', kind: 'disk', state: 'downloading', progress: 54.2, downloaded: 1.09 * 1024 ** 3, total: 2.01 * 1024 ** 3, speed: 18.7 * 1024 ** 2, eta: '51s left', connections: 3, maxConnections: 8, mode: 'whole-object', media: false, destination: `${base}\\Fedora-Workstation-Live-x86_64.iso`, tempPath: `${temp}\\job-5.part`, resumable: true, mime: 'application/x-iso9660-image', created: 'Today, 9:26 AM', started: 'Today, 9:26 AM', events: [event('Range support verified'), event('3 workers downloading', 'success')],
    }),
    make({
      id: 'job-6', name: 'old-archive.tar.xz', source: 'https://archive.example.net/old-archive.tar.xz', domain: 'archive.example.net', kind: 'archive', state: 'failed', progress: 0, downloaded: 0, total: 128 * 1024 ** 2, speed: 0, eta: undefined, connections: 0, maxConnections: 8, mode: 'single-stream', media: false, destination: `${base}\\old-archive.tar.xz`, tempPath: `${temp}\\job-6.part`, resumable: true, error: 'Network error: Connection reset by peer', created: 'Today, 8:58 AM', events: [event('Connection reset by peer', 'error')],
    }),
    make({ id: 'job-7', name: 'Lecture 12 — Distributed Systems.mp4', source: 'https://video.university.example/lecture/12.mp4', domain: 'video.university.example', kind: 'video', state: 'completed', progress: 100, downloaded: 846 * 1024 ** 2, total: 846 * 1024 ** 2, speed: 0, connections: 0, maxConnections: 8, mode: 'whole-object', media: true, mediaDetails: '1080p · H.264 + AAC', destination: `${base}\\Lecture 12 — Distributed Systems.mp4`, tempPath: `${temp}\\job-7.part`, resumable: true, completed: 'Today, 8:34 AM', created: 'Today, 8:12 AM', events: [event('Download completed', 'success')] }),
    make({ id: 'job-8', name: 'conference-keynote.webm', source: 'https://events.example.org/2026/keynote.webm', domain: 'events.example.org', kind: 'video', state: 'completed', progress: 100, downloaded: 1.3 * 1024 ** 3, total: 1.3 * 1024 ** 3, speed: 0, connections: 0, maxConnections: 8, mode: 'whole-object', media: true, mediaDetails: '1440p · VP9 + Opus', destination: `${base}\\conference-keynote.webm`, tempPath: `${temp}\\job-8.part`, resumable: true, completed: 'Today, 7:15 AM', created: 'Today, 6:51 AM', events: [event('Download completed', 'success')] }),
    make({ id: 'job-9', name: 'design-system.pdf', source: 'https://docs.example.org/design-system.pdf', domain: 'docs.example.org', kind: 'document', state: 'completed', progress: 100, downloaded: 42 * 1024 ** 2, total: 42 * 1024 ** 2, speed: 0, connections: 0, maxConnections: 8, mode: 'whole-object', media: false, destination: `${base}\\design-system.pdf`, tempPath: `${temp}\\job-9.part`, resumable: true, completed: 'Yesterday, 6:22 PM', created: 'Yesterday, 6:20 PM', events: [event('Download completed', 'success')] }),
    make({ id: 'job-10', name: 'studio-recording.m4a', source: 'https://audio.example.org/studio-recording.m4a', domain: 'audio.example.org', kind: 'audio', state: 'completed', progress: 100, downloaded: 212 * 1024 ** 2, total: 212 * 1024 ** 2, speed: 0, connections: 0, maxConnections: 8, mode: 'whole-object', media: true, mediaDetails: 'AAC · stereo', destination: `${base}\\studio-recording.m4a`, tempPath: `${temp}\\job-10.part`, resumable: true, completed: 'Yesterday, 4:42 PM', created: 'Yesterday, 4:37 PM', events: [event('Download completed', 'success')] }),
    make({ id: 'job-11', name: 'City Walk (4K).mp4', source: 'https://media.example.org/city-walk/4k.mp4', domain: 'media.example.org', kind: 'video', state: 'completed', progress: 100, downloaded: 3.6 * 1024 ** 3, total: 3.6 * 1024 ** 3, speed: 0, connections: 0, maxConnections: 8, mode: 'whole-object', media: true, mediaDetails: '2160p · H.265 + AAC', destination: `${base}\\City Walk (4K).mp4`, tempPath: `${temp}\\job-11.part`, resumable: true, completed: 'Yesterday, 12:06 PM', created: 'Yesterday, 11:46 AM', events: [event('Download completed', 'success')] }),
    make({ id: 'job-12', name: 'open-source-icons.zip', source: 'https://assets.example.com/open-source-icons.zip', domain: 'assets.example.com', kind: 'archive', state: 'completed', progress: 100, downloaded: 188 * 1024 ** 2, total: 188 * 1024 ** 2, speed: 0, connections: 0, maxConnections: 8, mode: 'whole-object', media: false, destination: `${base}\\open-source-icons.zip`, tempPath: `${temp}\\job-12.part`, resumable: true, completed: 'Yesterday, 10:11 AM', created: 'Yesterday, 10:07 AM', events: [event('Download completed', 'success')] }),
    make({ id: 'job-13', name: 'product-tour.mp4', source: 'https://media.example.org/product-tour.mp4', domain: 'media.example.org', kind: 'video', state: 'completed', progress: 100, downloaded: 632 * 1024 ** 2, total: 632 * 1024 ** 2, speed: 0, connections: 0, maxConnections: 8, mode: 'whole-object', media: true, mediaDetails: '1080p · H.264 + AAC', destination: `${base}\\product-tour.mp4`, tempPath: `${temp}\\job-13.part`, resumable: true, completed: 'Monday, 2:30 PM', created: 'Monday, 2:20 PM', events: [event('Download completed', 'success')] }),
    make({ id: 'job-14', name: 'backup-manifest.json', source: 'https://backup.example.net/manifest.json', domain: 'backup.example.net', kind: 'document', state: 'failed', progress: 38, downloaded: 12 * 1024 ** 2, total: 31 * 1024 ** 2, speed: 0, connections: 0, maxConnections: 8, mode: 'single-stream', media: false, destination: `${base}\\backup-manifest.json`, tempPath: `${temp}\\job-14.part`, resumable: false, error: 'The remote server returned 503', created: 'Monday, 9:03 AM', events: [event('Server returned 503', 'error')] }),
  ].map((job) => ({
    ...job,
    destination: platformPath(job.destination),
    tempPath: platformPath(job.tempPath),
  }));
}

function cloneSnapshot(snapshot: AppSnapshot): AppSnapshot {
  return structuredClone(snapshot);
}

class MockAdapter implements DownloadAdapter {
  private snapshot: AppSnapshot = {
    jobs: initialJobs(),
    settings: readSettings(),
    connected: true,
    aggregateSpeed: 61.0 * 1024 ** 2,
    notifications: [
      { id: 'notice-1', type: 'completed', title: 'Download completed', detail: 'Nature Documentary (4K).mkv · 2.85 GB', time: '11:03 AM', jobId: 'job-4' },
      { id: 'notice-2', type: 'failed', title: 'Download failed', detail: 'old-archive.tar.xz · Network error: Connection reset by peer', time: '11:05 AM', jobId: 'job-6' },
    ],
    bridgeAvailable: true,
  };

  private listeners = new Set<(snapshot: AppSnapshot) => void>();
  private nextId = 1;
  private timer: number;

  constructor() {
    this.timer = window.setInterval(() => this.tick(), 1200);
  }

  async getSnapshot() {
    return cloneSnapshot(this.snapshot);
  }

  subscribe(listener: (snapshot: AppSnapshot) => void) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit() {
    this.snapshot.aggregateSpeed = this.snapshot.jobs
      .filter((job) => ['downloading', 'connecting'].includes(job.state))
      .reduce((total, job) => total + job.speed, 0);
    const next = cloneSnapshot(this.snapshot);
    this.listeners.forEach((listener) => listener(next));
  }

  private tick() {
    let changed = false;
    this.snapshot.jobs = this.snapshot.jobs.map((job) => {
      if (job.state !== 'downloading' || !job.total) return job;
      const downloaded = Math.min(job.total, job.downloaded + job.speed * 1.2);
      const progress = (downloaded / job.total) * 100;
      changed = true;
      if (progress >= 100) {
        const completed = { ...job, state: 'completed' as const, progress: 100, downloaded: job.total, speed: 0, connections: 0, eta: undefined, completed: 'Just now', events: [event('Download completed', 'success'), ...job.events] };
        if (this.snapshot.settings.completionNotifications && !this.snapshot.notifications.some((item) => item.jobId === job.id)) {
          this.snapshot.notifications = [{ id: `notice-${job.id}`, type: 'completed', title: 'Download completed', detail: `${job.name} · ${formatBytes(job.total)}`, time: timeLabel(), jobId: job.id }, ...this.snapshot.notifications];
        }
        return completed;
      }
      const remaining = job.total - downloaded;
      const seconds = job.speed ? Math.ceil(remaining / job.speed) : 0;
      return { ...job, downloaded, progress, eta: `${Math.max(1, seconds)}s left` };
    });
    if (changed) this.emit();
  }

  private update(id: string, updater: (job: DownloadJob) => DownloadJob) {
    this.snapshot.jobs = this.snapshot.jobs.map((job) => job.id === id ? updater(job) : job);
    this.emit();
  }

  async pauseJob(id: string) {
    this.update(id, (job) => job.state === 'downloading' || job.state === 'connecting' ? { ...job, state: 'paused', speed: 0, connections: 0, events: [event('Paused by user', 'warning'), ...job.events] } : job);
  }

  async resumeJob(id: string) {
    this.update(id, (job) => { const plan = resumePlan(job.provisional, job.progress); return job.state === 'paused' || job.state === 'pending' ? { ...job, state: plan.state, speed: plan.shouldStart ? (job.speed || 12.4 * 1024 ** 2) : 0, connections: plan.shouldStart ? Math.min(job.maxConnections, 4) : 0, started: job.started ?? 'Just now', eta: plan.shouldStart ? (job.total ? `${Math.max(1, Math.ceil((job.total - job.downloaded) / (job.speed || 1)))}s left` : 'Connecting…') : 'Ready to save', events: [event('Resumed', 'success'), ...job.events] } : job; });
  }

  async retryJob(id: string) {
    this.update(id, (job) => ({ ...job, state: 'connecting', speed: 0, connections: 0, error: undefined, eta: 'Connecting…', events: [event('Retrying connection'), ...job.events] }));
    window.setTimeout(() => this.update(id, (job) => ({ ...job, state: 'downloading', speed: 9.8 * 1024 ** 2, connections: Math.min(job.maxConnections, 3), eta: job.total ? '1m left' : 'Receiving metadata', events: [event('Connection established', 'success'), ...job.events] })), 900);
  }

  async cancelJob(id: string) {
    this.snapshot.jobs = this.snapshot.jobs.filter((job) => job.id !== id || !job.provisional);
    this.update(id, (job) => ({ ...job, state: 'failed', speed: 0, connections: 0, error: 'Cancelled by user', events: [event('Provisional acquisition cancelled', 'warning'), ...job.events] }));
  }

  async removeJob(id: string) {
    this.snapshot.jobs = this.snapshot.jobs.filter((job) => job.id !== id);
    this.emit();
  }

  async pauseAll() {
    this.snapshot.jobs = this.snapshot.jobs.map((job) => ['downloading', 'connecting', 'finalizing'].includes(job.state) ? { ...job, state: 'paused' as const, speed: 0, connections: 0, events: [event('Paused with Pause All', 'warning'), ...job.events] } : job);
    this.emit();
  }

  async resumeAll() {
    this.snapshot.jobs = this.snapshot.jobs.map((job) => { const plan = resumePlan(job.provisional, job.progress); return ['paused', 'pending'].includes(job.state) ? { ...job, state: plan.state, speed: plan.shouldStart ? 11.2 * 1024 ** 2 : 0, connections: plan.shouldStart ? Math.min(job.maxConnections, 3) : 0, eta: plan.shouldStart ? (job.total ? '1m left' : 'Connecting…') : 'Ready to save', events: [event('Resumed with Resume All', 'success'), ...job.events] } : job; });
    this.emit();
  }

  async createProvisional(input: { source: string; name?: string; media?: boolean; maxConnections?: number; bandwidthLimit?: number | null }) {
    const name = input.name?.trim() || sourceName(input.source);
    const media = Boolean(input.media) || ['video', 'audio'].includes(guessKind(name));
    const id = `provisional-${this.nextId++}`;
    const job: DownloadJob = {
      id,
      name,
      source: input.source,
      domain: domainOf(input.source),
      kind: guessKind(name),
      state: 'connecting',
      progress: 0,
      downloaded: 0,
      total: undefined,
      speed: 0,
      eta: 'Connecting…',
      connections: 0,
       maxConnections: Math.max(1, Math.min(32, input.maxConnections ?? this.snapshot.settings.maxConnections)),
      bandwidthLimit: sanitizeCapBps(input.bandwidthLimit) ?? null,
      mode: media ? 'segments' : 'single-stream',
      media,
      mediaDetails: media ? 'Detecting current media…' : undefined,
      destination: platformPath(`${this.snapshot.settings.defaultFolder}\\${name}`),
      tempPath: platformPath(`${this.snapshot.settings.tempFolder}\\${id}.part`),
      resumable: false,
      created: 'Just now',
      provisional: true,
      events: [event('Provisional acquisition created'), event('Connecting to source…')],
    };
    this.snapshot.jobs = [job, ...this.snapshot.jobs];
    this.emit();
    window.setTimeout(() => {
      this.update(id, (current) => ({ ...current, state: 'downloading', started: current.started ?? 'Just now', total: media ? 768 * 1024 ** 2 : 1.25 * 1024 ** 3, downloaded: 6.5 * 1024 ** 2, progress: media ? 0.85 : 0.5, speed: media ? 7.2 * 1024 ** 2 : 14.8 * 1024 ** 2, connections: media ? 2 : 1, mode: media ? 'segments' : 'whole-object', resumable: true, eta: media ? '1m 47s left' : '1m 24s left', mediaDetails: media ? '1080p · source selected from playback' : undefined, events: [event('Source metadata received', 'success'), ...current.events] }));
    }, 700);
    return id;
  }

  async commitProvisional(id: string, input: { name: string; destination: string; maxConnections?: number; bandwidthLimit?: number | null }) {
    this.update(id, (job) => ({ ...job, name: input.name.trim() || job.name, destination: input.destination.trim() || job.destination, maxConnections: Math.max(1, Math.min(32, input.maxConnections ?? job.maxConnections)), bandwidthLimit: input.bandwidthLimit === undefined ? job.bandwidthLimit : sanitizeCapBps(input.bandwidthLimit) ?? null, provisional: false, resumable: true, state: job.state === 'connecting' ? 'downloading' : job.state, speed: job.speed || 9.4 * 1024 ** 2, connections: job.connections || 1, events: [event('Accepted as managed download', 'success'), ...job.events] }));
  }

  async updateSettings(patch: Partial<AppSettings>) {
    this.snapshot.settings = { ...this.snapshot.settings, ...sanitizeSettingsPatch(patch) };
    persistSettings(this.snapshot.settings);
    this.emit();
  }

  async reattachJob(id: string) {
    this.update(id, (job) => ({ ...job, state: 'connecting', error: undefined, eta: 'Reattaching…', events: [event('Waiting for a renewed browser source'), ...job.events] }));
    window.setTimeout(() => this.update(id, (job) => ({ ...job, state: 'downloading', speed: 10.1 * 1024 ** 2, connections: Math.min(3, job.maxConnections), eta: '2m left', events: [event('Source reattached', 'success'), ...job.events] })), 1100);
  }
}

class NativeAdapter implements DownloadAdapter {
  private ensureRuntime() {
    if (!(window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__) {
      throw new Error('The native Download Manager core is not connected. Launch this build inside Tauri.');
    }
  }

  async getSnapshot() {
    this.ensureRuntime();
    return invoke<AppSnapshot>('get_snapshot');
  }

  subscribe(listener: (snapshot: AppSnapshot) => void, onError?: (reason: unknown) => void) {
    let stopped = false;
    let unlisten: (() => void) | undefined;
    listen<AppSnapshot>('state-changed', (event) => listener(event.payload)).then((dispose) => {
      if (stopped) dispose();
      else unlisten = dispose;
    }).catch((reason: unknown) => {
      if (!stopped) onError?.(reason);
    });
    return () => { stopped = true; unlisten?.(); };
  }

  private async command<T>(name: string, payload?: Record<string, unknown>) {
    this.ensureRuntime();
    return invoke<T>(name, payload);
  }

  pauseJob(id: string) { return this.command<void>('pause_job', { id }); }
  resumeJob(id: string) { return this.command<void>('resume_job', { id }); }
  retryJob(id: string) { return this.command<void>('retry_job', { id }); }
  cancelJob(id: string) { return this.command<void>('cancel_job', { id }); }
  removeJob(id: string) { return this.command<void>('remove_job', { id }); }
  pauseAll() { return this.command<void>('pause_all'); }
  resumeAll() { return this.command<void>('resume_all'); }
  createProvisional(input: { source: string; name?: string; media?: boolean; maxConnections?: number; bandwidthLimit?: number | null }) { return this.command<string>('create_provisional', { input }); }
  commitProvisional(id: string, input: { name: string; destination: string; maxConnections?: number; bandwidthLimit?: number | null }) { return this.command<void>('commit_provisional', { id, input }); }
  updateSettings(patch: Partial<AppSettings>) { return this.command<void>('update_settings', { patch: sanitizeSettingsPatch(patch) }); }
  reattachJob(id: string) { return this.command<void>('reattach_job', { id }); }
}

export function createAdapter(): DownloadAdapter {
  if (import.meta.env.VITE_DATA_MODE === 'mock') return new MockAdapter();
  return new NativeAdapter();
}

export function formatBytes(bytes?: number | null) {
  if (bytes === undefined || bytes === null || Number.isNaN(bytes)) return 'Unknown size';
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(bytes >= 10 * 1024 ** 3 ? 0 : 2)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(bytes >= 100 * 1024 ** 2 ? 0 : 1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${Math.round(bytes)} B`;
}

export function formatSpeed(bytes?: number) {
  if (!bytes) return '—';
  return `${formatBytes(bytes)}/s`;
}
