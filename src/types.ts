export type DownloadState =
  | 'connecting'
  | 'downloading'
  | 'paused'
  | 'pending'
  | 'finalizing'
  | 'completed'
  | 'failed';

export type TransferMode = 'whole-object' | 'segments' | 'single-stream';
export type FilterKey = 'all' | 'active' | 'paused' | 'completed' | 'failed' | 'media';
export type SettingsPage = 'general' | 'downloads' | 'browser' | 'network' | 'notifications' | 'appearance';

export interface JobEvent {
  at: string;
  message: string;
  tone?: 'normal' | 'success' | 'warning' | 'error';
}

export interface ByteRange {
  start: number;
  end: number;
}

export interface ResourceIdentity {
  length?: number;
  etag?: string;
  lastModified?: string;
}

export interface DownloadJob {
  id: string;
  name: string;
  source: string;
  domain: string;
  kind: 'video' | 'archive' | 'disk' | 'document' | 'audio';
  state: DownloadState;
  progress: number;
  downloaded: number;
  total?: number;
  speed: number;
  eta?: string;
  connections: number;
  maxConnections: number;
  /** Per-job bandwidth cap in bytes/sec. Absent/null = follow the global setting. */
  bandwidthLimit?: number | null;
  mode: TransferMode;
  media: boolean;
  mediaDetails?: string;
  mediaTracks?: number;
  destination: string;
  tempPath: string;
  resumable: boolean;
  mime?: string;
  error?: string;
  created: string;
  started?: string;
  completed?: string;
  provisional?: boolean;
  segments?: { completed: number; total: number; identity?: string };
  completedRanges?: ByteRange[];
  resourceIdentity?: ResourceIdentity;
  destinationReservation?: string;
  events: JobEvent[];
}

export interface AppSettings {
  startAtSignIn: boolean;
  showManagerAtSignIn: boolean;
  closeBehavior: 'tray' | 'exit';
  defaultFolder: string;
  tempFolder: string;
  collisionBehavior: 'rename' | 'replace';
  interceptDownloads: boolean;
  showMediaButtons: boolean;
  excludedSites: string[];
  bandwidthLimit: number | null;
  bandwidthUnit: 'KB/s' | 'MB/s' | 'GB/s';
  maxConnections: number;
  perDownloadOverrides: boolean;
  retryAutomatically: boolean;
  maxRetries: number;
  completionNotifications: boolean;
  failureNotifications: boolean;
  theme: 'system' | 'light' | 'dark';
  accent: string;
  density: 'comfortable' | 'compact';
}

export interface NotificationItem {
  id: string;
  type: 'completed' | 'failed';
  title: string;
  detail: string;
  time: string;
  jobId: string;
}

export interface AppSnapshot {
  jobs: DownloadJob[];
  settings: AppSettings;
  connected: boolean;
  aggregateSpeed: number;
  notifications: NotificationItem[];
  bridgeAvailable?: boolean;
}

export interface DownloadAdapter {
  getSnapshot(): Promise<AppSnapshot>;
  subscribe(listener: (snapshot: AppSnapshot) => void, onError?: (reason: unknown) => void): () => void;
  pauseJob(id: string): Promise<void>;
  resumeJob(id: string): Promise<void>;
  retryJob(id: string): Promise<void>;
  cancelJob(id: string): Promise<void>;
  removeJob(id: string): Promise<void>;
  pauseAll(): Promise<void>;
  resumeAll(): Promise<void>;
  createProvisional(input: { source: string; name?: string; media?: boolean; maxConnections?: number; bandwidthLimit?: number | null }): Promise<string>;
  commitProvisional(id: string, input: { name: string; destination: string; maxConnections?: number; bandwidthLimit?: number | null }): Promise<void>;
  // bandwidthLimit wire contract (commit): undefined = keep existing cap,
  // null = clear back to the global setting, number = set cap in bytes/sec.
  updateSettings(patch: Partial<AppSettings>): Promise<void>;
  reattachJob(id: string): Promise<void>;
}
