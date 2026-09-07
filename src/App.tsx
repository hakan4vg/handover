import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, MouseEvent as ReactMouseEvent, ReactNode } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { createAdapter, formatBytes, formatSpeed } from './adapters';
import { BANDWIDTH_UNITS, bandwidthToBps, bpsToParts, type BandwidthUnit } from './bandwidth';
import { Icon, type IconName } from './icons';
import { settingsPageFromSearch } from './settings-route';
import { normalizeSite } from './site';
import type {
  AppSettings,
  AppSnapshot,
  DownloadAdapter,
  DownloadJob,
  DownloadState,
  FilterKey,
  SettingsPage,
} from './types';

const filterLabels: Array<{ key: FilterKey; label: string; icon: IconName }> = [
  { key: 'all', label: 'All', icon: 'all' },
  { key: 'active', label: 'Active', icon: 'active' },
  { key: 'paused', label: 'Paused', icon: 'pause' },
  { key: 'completed', label: 'Completed', icon: 'completed' },
  { key: 'failed', label: 'Failed', icon: 'failed' },
  { key: 'media', label: 'Media', icon: 'media' },
];

const settingsNav: Array<{ key: SettingsPage; label: string; icon: IconName }> = [
  { key: 'general', label: 'General', icon: 'settings' },
  { key: 'downloads', label: 'Downloads', icon: 'download' },
  { key: 'browser', label: 'Browser Integration', icon: 'globe' },
  { key: 'network', label: 'Network', icon: 'network' },
  { key: 'notifications', label: 'Notifications', icon: 'bell' },
  { key: 'appearance', label: 'Appearance', icon: 'palette' },
];

export function useAppSnapshot(adapter: DownloadAdapter) {
  const [snapshot, setSnapshot] = useState<AppSnapshot | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let mounted = true;
    let receivedSubscriptionSnapshot = false;
    const reportError = (reason: unknown) => {
      if (mounted) setError(reason instanceof Error ? reason.message : 'The application core could not be reached.');
    };
    const dispose = adapter.subscribe((next) => {
      if (mounted) {
        receivedSubscriptionSnapshot = true;
        setSnapshot(next);
        setError('');
      }
    }, reportError);
    adapter.getSnapshot().then((next) => {
      if (mounted && !receivedSubscriptionSnapshot) setSnapshot(next);
    }).catch(reportError);
    return () => {
      mounted = false;
      dispose();
    };
  }, [adapter]);
  return { snapshot, error };
}

function App() {
  const adapter = useMemo(() => createAdapter(), []);
  const { snapshot, error } = useAppSnapshot(adapter);
  const params = new URLSearchParams(window.location.search);
  const surface = params.get('view');
  const isAddWindow = params.get('window') === 'add';

  if (error) return <Disconnected message={error} />;
  if (!snapshot) return <Loading />;
  if (surface === 'extension') return <ExtensionPopup adapter={adapter} snapshot={snapshot} />;
  if (surface === 'tray') return <TrayMenu adapter={adapter} snapshot={snapshot} />;
  if (surface === 'notifications') return <NotificationsSurface snapshot={snapshot} />;
  if (isAddWindow) return <StandaloneAddWindow adapter={adapter} snapshot={snapshot} />;
  return <Manager adapter={adapter} snapshot={snapshot} />;
}

function Loading() {
  return <div className="loading-screen"><div className="loading-mark"><Icon name="download" size={23} /></div><span>Connecting to Download Manager…</span></div>;
}

function Disconnected({ message }: { message: string }) {
  return <div className="loading-screen disconnected"><div className="loading-mark"><Icon name="error" size={23} /></div><strong>Download Manager is unavailable</strong><span>{message}</span><button className="button primary" onClick={() => window.location.reload()}>Try again</button></div>;
}

function Manager({ adapter, snapshot }: { adapter: DownloadAdapter; snapshot: AppSnapshot }) {
  const [filter, setFilter] = useState<FilterKey>(() => new URLSearchParams(window.location.search).has('settings') ? 'settings' as FilterKey : 'all');
  const [settingsPage, setSettingsPage] = useState<SettingsPage>(() => settingsPageFromSearch(window.location.search));
  const [selectedId, setSelectedId] = useState(() => new URLSearchParams(window.location.search).get('job') ?? 'job-2');
  const [addWindowId, setAddWindowId] = useState<string | null>(null);
  const [showManualAdd, setShowManualAdd] = useState(false);
  const [contextJobId, setContextJobId] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const [noticeTone, setNoticeTone] = useState<'success' | 'error'>('success');
  const [sortBy, setSortBy] = useState<'created' | 'name' | 'size' | 'state'>('created');
  const [sortOpen, setSortOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(true);

  const dismissMenus = () => {
    setSortOpen(false);
    setMoreOpen(false);
  };

  const active = snapshot.jobs.filter((job) => ['connecting', 'downloading', 'finalizing'].includes(job.state));
  const paused = snapshot.jobs.filter((job) => job.state === 'paused' || job.state === 'pending');
  const contextJob = contextJobId ? snapshot.jobs.find((job) => job.id === contextJobId) : undefined;
  const filteredJobs = useMemo(() => {
    const jobs = snapshot.jobs.filter((job) => {
      if (filter === 'all') return true;
      if (filter === 'active') return ['connecting', 'downloading', 'finalizing'].includes(job.state);
      if (filter === 'paused') return job.state === 'paused' || job.state === 'pending';
      if (filter === 'media') return job.media;
      return job.state === filter;
    });
    return [...jobs].sort((left, right) => {
      if (sortBy === 'name') return left.name.localeCompare(right.name);
      if (sortBy === 'size') return (right.total ?? 0) - (left.total ?? 0);
      if (sortBy === 'state') return stateText(left.state).localeCompare(stateText(right.state));
      return snapshot.jobs.indexOf(left) - snapshot.jobs.indexOf(right);
    });
  }, [filter, snapshot.jobs, sortBy]);
  const selected = filteredJobs.find((job) => job.id === selectedId) ?? filteredJobs[0];

  useEffect(() => {
    const nextVisibleId = filteredJobs[0]?.id ?? '';
    if (selectedId && filteredJobs.some((job) => job.id === selectedId)) return;
    if (selectedId !== nextVisibleId) setSelectedId(nextVisibleId);
  }, [filteredJobs, selectedId]);

  useEffect(() => {
    if (!notice) return;
    const id = window.setTimeout(() => setNotice(''), 2800);
    return () => window.clearTimeout(id);
  }, [notice]);

  useEffect(() => {
    if (!sortOpen && !moreOpen && !contextJobId) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      setSortOpen(false);
      setMoreOpen(false);
      setContextJobId(null);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [contextJobId, moreOpen, sortOpen]);

  const showNotice = (message: string, tone: 'success' | 'error' = 'success') => {
    setNoticeTone(tone);
    setNotice(message);
  };

  const run = (promise: Promise<void>, success?: string) => {
    promise.then(() => success && showNotice(success)).catch((reason: unknown) => showNotice(reason instanceof Error ? reason.message : 'Action failed', 'error'));
  };

  const openAdd = () => {
    dismissMenus();
    setContextJobId(null);
    setShowManualAdd(true);
    setAddWindowId(null);
  };

  const handleManualSubmit = async (source: string, name: string, maxConnections: number, bandwidthLimit: number | null) => {
    try {
      const id = await adapter.createProvisional({ source, name, maxConnections, bandwidthLimit });
      setShowManualAdd(false);
      setAddWindowId(id);
      setSelectedId(id);
    } catch (reason) {
      showNotice(reason instanceof Error ? reason.message : 'Could not start acquisition', 'error');
    }
  };

  const handleCommit = async (id: string, name: string, destination: string, maxConnections: number, bandwidthLimit: number | null) => {
    await adapter.commitProvisional(id, { name, destination, maxConnections, bandwidthLimit });
    setAddWindowId(null);
    showNotice('Download added to Manager');
  };

  const handleCancel = async (id: string) => {
    await adapter.cancelJob(id);
    setAddWindowId(null);
    showNotice('Acquisition cancelled');
  };

  const isSettings = filter === ('settings' as FilterKey);

  return (
    <div className="desktop-shell" style={{ '--accent': snapshot.settings.accent } as CSSProperties} data-density={snapshot.settings.density} data-theme={snapshot.settings.theme}>
      <div className="titlebar" data-tauri-drag-region="true" onMouseDown={startWindowDrag}>
        <div className="product-title"><span className="product-logo"><Icon name="download" size={18} /></span><span>Download Manager</span></div>
        <WindowControls />
      </div>
      <div className="manager-body">
        <aside className="sidebar">
          <nav className="sidebar-nav" aria-label="Download filters">
            {filterLabels.map((item) => <SidebarItem key={item.key} icon={item.icon} label={item.label} active={filter === item.key} count={countFor(item.key, snapshot.jobs)} onClick={() => { setFilter(item.key); dismissMenus(); setContextJobId(null); }} />)}
          </nav>
          <div className="sidebar-spacer" />
          <SidebarItem icon="settings" label="Settings" active={isSettings} onClick={() => { setFilter('settings' as FilterKey); dismissMenus(); setContextJobId(null); }} />
          <div className="sidebar-footer"><span className="status-dot" /> <span>Connected</span><span className="footer-divider" /><span>Browser integration {snapshot.settings.interceptDownloads ? 'on' : 'off'}</span></div>
        </aside>
        {isSettings ? <SettingsView adapter={adapter} settings={snapshot.settings} page={settingsPage} onPageChange={setSettingsPage} /> : (
          <main className="manager-main">
            <div className="toolbar">
              <div className="toolbar-heading"><h1>{titleFor(filter)}</h1><span className="heading-count">{filteredJobs.length}</span></div>
              <div className="toolbar-actions">
                {active.length > 0 ? <button className="button" onClick={() => run(adapter.pauseAll())}><Icon name="pause" size={15} /> Pause All</button> : paused.length > 0 ? <button className="button" onClick={() => run(adapter.resumeAll())}><Icon name="play" size={15} /> Resume All</button> : null}
                <button className="button primary" onClick={openAdd}><Icon name="add" size={17} /> Add URL</button>
                 <div className="toolbar-menu-wrap"><button className="icon-button toolbar-more" aria-label="More options" aria-haspopup="menu" aria-expanded={moreOpen} onClick={() => { setMoreOpen(!moreOpen); setSortOpen(false); }}><Icon name="more" size={19} /></button>{moreOpen && <div className="toolbar-menu" role="menu" aria-label="Manager options"><button role="menuitem" onClick={() => { setFilter('settings' as FilterKey); dismissMenus(); setContextJobId(null); }}><Icon name="settings" size={15} /> Settings</button><button role="menuitem" onClick={() => { setSortBy('created'); setMoreOpen(false); }}><Icon name="refresh" size={15} /> Reset sort</button></div>}</div>
              </div>
            </div>
            <div className="workspace-columns">
              <section className="download-list" aria-label="Downloads">
                 <div className="list-header"><span>Downloads</span><div className="list-header-actions"><button className="subtle-button" aria-haspopup="menu" aria-expanded={sortOpen} onClick={() => { setSortOpen(!sortOpen); setMoreOpen(false); }}><Icon name="sort" size={15} /> Sort <Icon name="chevron-down" size={13} /></button>{sortOpen && <SortMenu value={sortBy} onChange={(value) => { setSortBy(value); setSortOpen(false); }} />}</div></div>
                <div className="rows">
                  {filteredJobs.length ? filteredJobs.map((job) => <DownloadRow key={job.id} job={job} selected={job.id === selected?.id} menuOpen={contextJobId === job.id} onSelect={() => { setSelectedId(job.id); setInspectorOpen(true); dismissMenus(); setContextJobId(null); }} onPause={() => run(adapter.pauseJob(job.id))} onResume={() => run(adapter.resumeJob(job.id))} onRetry={() => run(adapter.retryJob(job.id))} onMenu={() => { dismissMenus(); setContextJobId(contextJobId === job.id ? null : job.id); }} />) : <EmptyState filter={filter} onAdd={openAdd} />}
                </div>
                {contextJob && <JobContextMenu job={contextJob} adapter={adapter} onClose={() => setContextJobId(null)} onNotice={showNotice} />}
              </section>
              {selected && (inspectorOpen ? <Inspector job={selected} adapter={adapter} onClose={() => setInspectorOpen(false)} /> : <button className="inspector-reopen" onClick={() => setInspectorOpen(true)} aria-label="Open inspector"><Icon name="chevron-left" size={17} /><span>Details</span></button>)}
            </div>
             <div className="manager-statusbar"><div className="aggregate-status"><span className="status-dot" /><span>Connected</span><span className="footer-divider" /><span>{active.length} active</span><span>·</span><span>{formatSpeed(snapshot.aggregateSpeed)}</span></div><span className="status-live">Live transfer state</span><button className="icon-button" aria-label="Settings" onClick={() => { setFilter('settings' as FilterKey); dismissMenus(); setContextJobId(null); }}><Icon name="settings" size={16} /></button></div>
          </main>
        )}
      </div>
      {showManualAdd && <AddDownloadWindow adapter={adapter} settings={snapshot.settings} onCreate={handleManualSubmit} onCancel={() => setShowManualAdd(false)} onClose={() => setShowManualAdd(false)} />}
      {addWindowId && <AddDownloadWindow adapter={adapter} settings={snapshot.settings} job={snapshot.jobs.find((item) => item.id === addWindowId)} onCommit={handleCommit} onCancel={handleCancel} onClose={() => handleCancel(addWindowId)} />}
      {notice && <NoticeToast message={notice} tone={noticeTone} />}
    </div>
  );
}

export function NoticeToast({ message, tone }: { message: string; tone: 'success' | 'error' }) {
  const error = tone === 'error';
  return <div className={`toast ${error ? 'toast-error' : 'toast-success'}`} role={error ? 'alert' : 'status'} aria-live={error ? 'assertive' : 'polite'}><span data-notice-icon={tone}><Icon name={error ? 'error' : 'check'} size={16} /></span>{message}</div>;
}

function WindowControls() {
  const native = Boolean((window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__);
  const run = (action: 'minimize' | 'maximize' | 'close') => {
    if (!native) return;
    const current = getCurrentWindow();
    void (action === 'minimize' ? current.minimize() : action === 'maximize' ? current.toggleMaximize() : current.close());
  };
  return <div className="window-controls" aria-label="Window controls"><button aria-label="Minimize" onClick={() => run('minimize')}><span className="minimize-glyph" /></button><button aria-label="Maximize" onClick={() => run('maximize')}><span className="maximize-glyph" /></button><button aria-label="Close" onClick={() => run('close')}><Icon name="close" size={15} /></button></div>;
}

function startWindowDrag(event: ReactMouseEvent<HTMLElement>) {
  if (event.button !== 0 || (event.target instanceof Element && event.target.closest('button, input, select, textarea, a'))) return;
  if ((window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__) void getCurrentWindow().startDragging();
}

export async function openLocalPath(path: string): Promise<string | undefined> {
  if ((window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__) {
    try {
      await invoke('open_path', { path });
      return undefined;
    } catch (reason) {
      return reason instanceof Error && reason.message ? reason.message : 'Could not open the selected path.';
    }
  }
  try {
    const opened = window.open(`file:///${path.replaceAll('\\', '/')}`, '_blank', 'noopener');
    return opened ? undefined : 'Could not open the selected path.';
  } catch {
    return 'Could not open the selected path.';
  }
}

export async function copySourceUrl(source: string): Promise<string | undefined> {
  try {
    if (!navigator.clipboard) return 'Clipboard is unavailable.';
    await navigator.clipboard.writeText(source);
    return undefined;
  } catch (reason) {
    return reason instanceof Error && reason.message ? reason.message : 'Could not copy the source URL.';
  }
}

function openManagerSurface(jobId?: string) {
  window.location.href = `${window.location.pathname}${jobId ? `?job=${encodeURIComponent(jobId)}` : ''}`;
}

function closeSurface() {
  if ((window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__) void getCurrentWindow().close();
  else window.close();
}

function countFor(key: FilterKey, jobs: DownloadJob[]) {
  if (key === 'all') return jobs.length;
  if (key === 'active') return jobs.filter((job) => ['connecting', 'downloading', 'finalizing'].includes(job.state)).length;
  if (key === 'paused') return jobs.filter((job) => job.state === 'paused' || job.state === 'pending').length;
  if (key === 'media') return jobs.filter((job) => job.media).length;
  return jobs.filter((job) => job.state === key).length;
}

function titleFor(filter: FilterKey) {
  if (filter === 'all') return 'All Downloads';
  return `${filter[0].toUpperCase()}${filter.slice(1)} Downloads`;
}

function SidebarItem({ icon, label, count, active, onClick }: { icon: IconName; label: string; count?: number; active: boolean; onClick: () => void }) {
  return <button className={`sidebar-item ${active ? 'selected' : ''}`} onClick={onClick}><Icon name={icon} size={18} /><span>{label}</span>{count !== undefined && <span className="nav-count">{count}</span>}</button>;
}

export function SortMenu({ value, onChange }: { value: 'created' | 'name' | 'size' | 'state'; onChange: (value: 'created' | 'name' | 'size' | 'state') => void }) {
  const options: Array<['created' | 'name' | 'size' | 'state', string]> = [['created', 'Recently added'], ['name', 'Name'], ['size', 'File size'], ['state', 'Status']];
  return <div className="sort-menu" role="menu" aria-label="Sort downloads">{options.map(([key, label]) => <button key={key} className={value === key ? 'selected' : ''} role="menuitemradio" aria-checked={value === key} onClick={() => onChange(key)}><span>{label}</span>{value === key && <Icon name="check" size={14} />}</button>)}</div>;
}

export function DownloadRow({ job, selected, menuOpen = false, onSelect, onPause, onResume, onRetry, onMenu }: { job: DownloadJob; selected: boolean; menuOpen?: boolean; onSelect: () => void; onPause: () => void; onResume: () => void; onRetry: () => void; onMenu: () => void }) {
  const isTransfer = ['downloading', 'connecting', 'paused', 'pending', 'finalizing'].includes(job.state);
  const stateLabel = stateText(job.state);
  const action = job.state === 'downloading' || job.state === 'connecting' || job.state === 'finalizing' ? onPause : job.state === 'paused' || job.state === 'pending' ? onResume : job.state === 'failed' ? onRetry : undefined;
  return <article className={`download-row ${selected ? 'selected' : ''}`} role="button" tabIndex={0} aria-pressed={selected} onClick={onSelect} onKeyDown={(event) => { if (event.target !== event.currentTarget) return; if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(); } }}>
    <div className="file-cell"><FileIcon kind={job.kind} /><span className="file-type">{typeLabel(job.name)}</span></div>
    <div className="row-main">
      <div className="row-title-line"><strong title={job.name}>{job.name}</strong><span className={`state ${stateTone(job.state)}`}>{stateLabel}</span></div>
      <div className="row-source"><Icon name="globe" size={12} />{job.domain}</div>
      <div className="progress-track"><span className={`progress-fill ${stateTone(job.state)}`} style={{ width: `${job.progress}%` }} /></div>
      <div className="row-meta"><span>{formatBytes(job.downloaded)}{job.total ? ` / ${formatBytes(job.total)}` : ''}</span><span>{job.eta ?? (job.state === 'completed' ? 'Completed' : '—')}</span>{job.mediaDetails && <span>{job.mediaDetails}</span>}{isTransfer && <span>{job.connections ? `${job.connections} connection${job.connections === 1 ? '' : 's'}` : 'No active connections'}</span>}</div>
    </div>
    <div className="row-speed">{job.speed ? formatSpeed(job.speed) : job.state === 'completed' ? formatBytes(job.total) : '—'}</div>
    <div className="row-actions">{action && <button className="row-action" aria-label={job.state === 'failed' ? `Retry ${job.name}` : job.state === 'paused' || job.state === 'pending' ? `Resume ${job.name}` : `Pause ${job.name}`} onClick={(event) => { event.stopPropagation(); action(); }}><Icon name={job.state === 'failed' ? 'refresh' : job.state === 'paused' || job.state === 'pending' ? 'play' : 'pause'} size={16} /></button>}<button className="row-action" aria-haspopup="menu" aria-expanded={menuOpen} aria-label={`More actions for ${job.name}`} onClick={(event) => { event.stopPropagation(); onMenu(); }}><Icon name="more" size={16} /></button></div>
  </article>;
}

function stateText(state: DownloadState) {
  return ({ connecting: 'Connecting', downloading: 'Downloading', paused: 'Paused', pending: 'Waiting', finalizing: 'Merging', completed: 'Completed', failed: 'Failed' })[state];
}

function stateTone(state: DownloadState) {
  if (state === 'failed') return 'danger';
  if (state === 'paused' || state === 'pending') return 'muted';
  if (state === 'finalizing') return 'accent';
  if (state === 'completed') return 'success';
  return 'active';
}

function FileIcon({ kind }: { kind: DownloadJob['kind'] }) {
  const icon = kind === 'video' ? 'media' : kind === 'disk' ? 'disk' : kind === 'archive' ? 'archive' : kind === 'audio' ? 'audio' : 'file';
  return <div className={`file-icon file-${kind}`}><Icon name={icon} size={25} /></div>;
}

function typeLabel(name: string) {
  const lower = name.toLowerCase();
  if (lower.endsWith('.tar.gz') || lower.endsWith('.tar.xz') || lower.endsWith('.tar.bz2')) return 'TAR';
  const ext = name.split('.').pop()?.toUpperCase().slice(0, 5) ?? '';
  return ext || 'FILE';
}

function EmptyState({ filter, onAdd }: { filter: FilterKey; onAdd: () => void }) {
  const title = filter === 'all' ? 'No downloads' : `No ${filter} downloads`;
  return <div className="empty-state"><div className="empty-icon"><Icon name={filter === 'failed' ? 'check' : 'download'} size={24} /></div><strong>{title}</strong><span>Downloads matching this view will appear here.</span>{filter === 'all' && <button className="button primary" onClick={onAdd}><Icon name="add" size={16} /> Add URL</button>}</div>;
}

export function Inspector({ job, adapter, onClose }: { job: DownloadJob; adapter: DownloadAdapter; onClose: () => void }) {
  const [tab, setTab] = useState<'Overview' | 'Network' | 'Media' | 'Files' | 'Log'>('Overview');
  const [pathError, setPathError] = useState('');
  const [removeError, setRemoveError] = useState('');
  const [removing, setRemoving] = useState(false);
  const availableTabs = job.media ? ['Overview', 'Network', 'Media', 'Files', 'Log'] as const : ['Overview', 'Network', 'Files', 'Log'] as const;
  useEffect(() => { if (!availableTabs.includes(tab as never)) setTab('Overview'); }, [job.id, job.media]);
  const openPath = (path: string) => {
    setPathError('');
    void openLocalPath(path).then((error) => { if (error) setPathError(error); });
  };
  const openFile = () => openPath(job.destination);
  const folder = job.destination.slice(0, Math.max(job.destination.lastIndexOf('\\'), job.destination.lastIndexOf('/')));
  const openFolder = () => openPath(folder);
  const remove = async () => {
    setRemoveError('');
    setRemoving(true);
    try {
      await adapter.removeJob(job.id);
      onClose();
    } catch (reason) {
      setRemoveError(reason instanceof Error && reason.message ? reason.message : 'Could not remove the download.');
    } finally {
      setRemoving(false);
    }
  };
  return <aside className="inspector"><div className="inspector-tabs">{availableTabs.map((item) => <button key={item} className={tab === item ? 'active' : ''} onClick={() => setTab(item)}>{item}</button>)}<button className="icon-button inspector-close" aria-label="Close inspector" onClick={onClose}><Icon name="close" size={15} /></button></div><div className="inspector-scroll"><div className="inspector-heading"><FileIcon kind={job.kind} /><div><h2>{job.name}</h2><span>{job.domain}</span></div></div>{tab === 'Overview' && <Overview job={job} onOpen={openFolder} />}{tab === 'Network' && <NetworkDetails job={job} />}{tab === 'Media' && <MediaDetails job={job} />}{tab === 'Files' && <FileDetails job={job} />}{tab === 'Log' && <JobLog job={job} />}</div><div className="inspector-actions">{pathError && <div className="form-error" role="alert"><Icon name="error" size={14} />{pathError}</div>}{removeError && <div className="form-error" role="alert"><Icon name="error" size={14} />{removeError}</div>}<button className="button" disabled={job.state !== 'completed'} onClick={openFile}><Icon name="open" size={15} /> Open file</button><button className="button" disabled={removing} onClick={() => void remove()}><Icon name="delete" size={15} /> {removing ? 'Removing…' : 'Remove'}</button></div></aside>;
}

function DetailGrid({ items }: { items: Array<{ label: string; value: string; tone?: string }> }) {
  return <div className="detail-grid">{items.map((item) => <div className="detail-row" key={item.label}><span>{item.label}</span><strong className={item.tone ?? ''}>{item.value}</strong></div>)}</div>;
}

function Overview({ job, onOpen }: { job: DownloadJob; onOpen: () => void }) {
  return <><div className="inspector-status"><span className={`status-pill ${stateTone(job.state)}`}><span className="status-dot" />{stateText(job.state)}</span><span className="inspector-progress">{Math.round(job.progress)}%</span></div><div className="inspector-progress-track"><span className={`progress-fill ${stateTone(job.state)}`} style={{ width: `${job.progress}%` }} /></div><DetailGrid items={[{ label: 'Status', value: job.error ?? (job.state === 'finalizing' ? job.eta ?? 'Finalizing' : stateText(job.state)), tone: stateTone(job.state) }, { label: 'Save to', value: job.destination, }, { label: 'File size', value: formatBytes(job.total) }, { label: 'Downloaded', value: `${formatBytes(job.downloaded)}${job.total ? ` (${Math.round(job.progress)}%)` : ''}` }, { label: 'Speed', value: formatSpeed(job.speed) }, { label: 'ETA', value: job.eta ?? '—' }, { label: 'Connections', value: `${job.connections} of ${job.maxConnections}` }, { label: 'Created', value: job.created }, { label: 'Started', value: job.started ?? 'Not started' }, { label: 'Resumable', value: job.resumable ? 'Yes' : 'No' }]} />{job.state === 'completed' && <button className="text-link" onClick={onOpen}><Icon name="folder" size={14} /> Open containing folder</button>}</>;
}

function NetworkDetails({ job }: { job: DownloadJob }) {
  return <><SectionTitle icon="network" title="Connection" /><DetailGrid items={[{ label: 'Transfer mode', value: job.mode === 'whole-object' ? 'Whole file · ranges' : job.mode === 'segments' ? 'Ordered segments' : 'Single stream' }, { label: 'Active connections', value: `${job.connections}` }, { label: 'Maximum allowed', value: `${job.maxConnections}` }, { label: 'Bandwidth cap', value: job.bandwidthLimit ? formatSpeed(job.bandwidthLimit) : 'Global setting' }, { label: 'Source', value: job.source }, { label: 'MIME type', value: job.mime ?? 'Detecting' }, { label: 'Resumability', value: job.resumable ? 'Verified' : 'Unknown' }]} /><div className="info-callout"><Icon name="shield" size={16} /><span>Credentials and request context are kept only for this acquisition.</span></div></>;
}

function MediaDetails({ job }: { job: DownloadJob }) {
  return <><SectionTitle icon="media" title="Current media" /><DetailGrid items={[{ label: 'Presentation', value: job.mediaDetails ?? 'Finite browser media' }, { label: 'Acquisition', value: job.mode === 'segments' ? 'Manifest and ordered fragments' : 'Progressive resource' }, { label: 'Container', value: job.mime?.split('/')[1]?.toUpperCase() ?? 'Detecting' }, { label: 'Tracks', value: job.mediaTracks ? `${job.mediaTracks} · ${job.mediaTracks > 1 ? 'separate streams' : 'single stream'}` : 'Detecting' }, { label: 'Segments', value: job.segments ? `${job.segments.completed} of ${job.segments.total}` : 'Not segmented' }, { label: 'Finalization', value: job.state === 'finalizing' ? 'In progress' : job.state === 'completed' ? 'Complete' : 'Not started' }]} /><div className="info-callout"><Icon name="info" size={16} /><span>The selected source follows the media currently playing in the browser.</span></div></>;
}

function FileDetails({ job }: { job: DownloadJob }) {
  return <><SectionTitle icon="folder" title="Managed files" /><DetailGrid items={[{ label: 'Output', value: job.destination }, { label: 'Temporary data', value: job.tempPath }, { label: 'Size on disk', value: formatBytes(job.downloaded) }, { label: 'Resource identity', value: job.resumable ? 'Verified from source' : 'Not established' }]} /></>;
}

function JobLog({ job }: { job: DownloadJob }) {
  return <div className="job-log">{job.events.slice(0, 8).map((item, index) => <div className="log-item" key={`${item.at}-${index}`}><span>{item.at}</span><strong className={item.tone ?? ''}>{item.message}</strong></div>)}</div>;
}

function SectionTitle({ icon, title }: { icon: IconName; title: string }) {
  return <div className="section-title"><Icon name={icon} size={16} /><strong>{title}</strong></div>;
}

export function JobContextMenu({ job, adapter, onClose, onNotice }: { job: DownloadJob; adapter: DownloadAdapter; onClose: () => void; onNotice: (message: string, tone?: 'success' | 'error') => void }) {
  const run = (action: Promise<void>, message?: string) => action.then(() => { if (message) onNotice(message, 'success'); onClose(); }).catch((reason: unknown) => onNotice(reason instanceof Error ? reason.message : 'Action failed', 'error'));
  const pauseAction = ['downloading', 'connecting', 'finalizing'].includes(job.state);
  const folder = job.destination.slice(0, Math.max(job.destination.lastIndexOf('\\'), job.destination.lastIndexOf('/')));
  return <div className="context-menu" role="menu" aria-label={`Actions for ${job.name}`} onClick={(event) => event.stopPropagation()}>{(pauseAction || job.state === 'paused' || job.state === 'pending') && <MenuAction icon={pauseAction ? 'pause' : 'play'} label={pauseAction ? 'Pause' : 'Resume'} onClick={() => run(pauseAction ? adapter.pauseJob(job.id) : adapter.resumeJob(job.id))} />}{['downloading', 'connecting', 'paused', 'pending', 'finalizing'].includes(job.state) && <MenuAction icon="close" label="Cancel" onClick={() => run(adapter.cancelJob(job.id), 'Download cancelled')} />}{job.state === 'failed' && <MenuAction icon="refresh" label="Retry" onClick={() => run(adapter.retryJob(job.id), 'Retrying download')} />}{job.state === 'completed' && <MenuAction icon="open" label="Open file" onClick={() => { void openLocalPath(job.destination).then((error) => { if (error) onNotice(error, 'error'); onClose(); }); }} />}{<MenuAction icon="folder" label="Open containing folder" onClick={() => { void openLocalPath(folder).then((error) => { if (error) onNotice(error, 'error'); onClose(); }); }} />}{<MenuAction icon="copy" label="Copy source URL" onClick={() => { void copySourceUrl(job.source).then((error) => { if (error) onNotice(error, 'error'); else onNotice('Source URL copied', 'success'); onClose(); }); }} />}{job.state !== 'completed' && <MenuAction icon="link" label="Reattach download" onClick={() => run(adapter.reattachJob(job.id), 'Waiting for renewed source')} />}{<div className="menu-divider" />}{<MenuAction danger icon="delete" label="Remove from list" onClick={() => run(adapter.removeJob(job.id), 'Removed from list')} />}</div>;
}

function MenuAction({ icon, label, danger, onClick }: { icon: IconName; label: string; danger?: boolean; onClick: () => void }) {
  return <button role="menuitem" className={`menu-action ${danger ? 'danger' : ''}`} onClick={onClick}><Icon name={icon} size={16} /><span>{label}</span></button>;
}

export function SettingsView({ adapter, settings, page, onPageChange }: { adapter: DownloadAdapter; settings: AppSettings; page: SettingsPage; onPageChange: (page: SettingsPage) => void }) {
  const [saveError, setSaveError] = useState('');
  const update = (patch: Partial<AppSettings>) => {
    setSaveError('');
    try {
      void adapter.updateSettings(patch).catch((reason: unknown) => setSaveError(reason instanceof Error ? reason.message : 'Could not save settings'));
    } catch (reason) {
      setSaveError(reason instanceof Error ? reason.message : 'Could not save settings');
    }
  };
  return <main className="settings-main"><div className="settings-nav"><div className="settings-nav-title">Settings</div>{settingsNav.map((item) => <button className={`settings-nav-item ${page === item.key ? 'selected' : ''}`} key={item.key} onClick={() => onPageChange(item.key)}><Icon name={item.icon} size={17} /><span>{item.label}</span></button>)}</div><div className="settings-content">{saveError && <div className="form-error settings-error" role="alert"><Icon name="error" size={14} />{saveError}</div>}{page === 'general' && <GeneralSettings settings={settings} update={update} />}{page === 'downloads' && <DownloadSettings settings={settings} update={update} />}{page === 'browser' && <BrowserSettings settings={settings} update={update} />}{page === 'network' && <NetworkSettings settings={settings} update={update} />}{page === 'notifications' && <NotificationSettings settings={settings} update={update} />}{page === 'appearance' && <AppearanceSettings settings={settings} update={update} />}</div></main>;
}

function SettingsHeading({ title, description }: { title: string; description?: string }) {
  return <div className="settings-heading"><h1>{title}</h1>{description && <p>{description}</p>}</div>;
}

function SettingToggle({ icon, title, description, checked, onChange }: { icon?: IconName; title: string; description?: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return <div className="setting-line">{icon && <Icon name={icon} size={20} />}<div className="setting-copy"><strong>{title}</strong>{description && <span>{description}</span>}</div><Toggle label={title} checked={checked} onChange={onChange} /></div>;
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return <button aria-label={label} aria-pressed={checked} className={`toggle ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)}><span /></button>;
}

function SettingCard({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <section className={`setting-card ${className}`}>{children}</section>;
}

function GeneralSettings({ settings, update }: { settings: AppSettings; update: (patch: Partial<AppSettings>) => void }) {
  return <><SettingsHeading title="General" /><SettingCard><SettingToggle icon="power" title="Start download service at sign-in" description="Run the download service in the background when you sign in." checked={settings.startAtSignIn} onChange={(startAtSignIn) => update({ startAtSignIn })} /><SettingToggle icon="window" title="Start manager UI at sign-in" description="Launch the manager interface automatically when you sign in." checked={settings.showManagerAtSignIn} onChange={(showManagerAtSignIn) => update({ showManagerAtSignIn })} /><div className="setting-line"><Icon name="power" size={20} /><div className="setting-copy"><strong>Close button behavior</strong><span>Choose what happens when the main window close button is clicked.</span></div><Select label="Close button behavior" value={settings.closeBehavior} onChange={(value) => update({ closeBehavior: value as AppSettings['closeBehavior'] })} options={[['tray', 'Minimize to tray'], ['exit', 'Exit application']]} /></div></SettingCard></>;
}

function DownloadSettings({ settings, update }: { settings: AppSettings; update: (patch: Partial<AppSettings>) => void }) {
  return <><SettingsHeading title="Downloads" /><SettingCard><FieldHeading title="Default download folder" description="All downloads will be saved to this folder." /><PathField label="Default download folder" value={settings.defaultFolder} onChange={(defaultFolder) => update({ defaultFolder })} /><FieldHeading title="Temporary / cache folder" description="Temporary files and parts will be stored here." /><PathField label="Temporary / cache folder" value={settings.tempFolder} onChange={(tempFolder) => update({ tempFolder })} /></SettingCard><SettingCard><FieldHeading title="File name collisions" description="Choose how to handle an existing file with the same name." /><Select label="File name collisions" value={settings.collisionBehavior} onChange={(collisionBehavior) => update({ collisionBehavior: collisionBehavior as AppSettings['collisionBehavior'] })} options={[['rename', 'Create a numbered copy'], ['replace', 'Replace existing file']]} /></SettingCard></>;
}

function BrowserSettings({ settings, update }: { settings: AppSettings; update: (patch: Partial<AppSettings>) => void }) {
  const [newSite, setNewSite] = useState('');
  const addSite = () => { const site = normalizeSite(newSite); if (site && !settings.excludedSites.includes(site)) update({ excludedSites: [...settings.excludedSites, site] }); setNewSite(''); };
  return <><SettingsHeading title="Browser Integration" /><SettingCard><SettingToggle title="Intercept browser downloads" description="Detect downloads from supported browsers." checked={settings.interceptDownloads} onChange={(interceptDownloads) => update({ interceptDownloads })} /><SettingToggle title="Show media buttons" description="Display download buttons on videos and media." checked={settings.showMediaButtons} onChange={(showMediaButtons) => update({ showMediaButtons })} /></SettingCard><SettingCard><FieldHeading title="Excluded media sites" description="These sites are excluded from browser integration." /><div className="excluded-list">{settings.excludedSites.map((site) => <div className="excluded-item" key={site}><span>{site}</span><button aria-label={`Remove ${site}`} onClick={() => update({ excludedSites: settings.excludedSites.filter((item) => item !== site) })}><Icon name="close" size={15} /></button></div>)}<div className="add-site"><input aria-label="Add excluded media site" value={newSite} onChange={(event) => setNewSite(event.target.value)} onKeyDown={(event) => event.key === 'Enter' && addSite()} placeholder="Add a site, such as example.com" /><button className="button" onClick={addSite}>Add</button></div></div><div className="info-callout"><Icon name="info" size={16} /><span>Excluded sites are shared with the extension popup.</span></div></SettingCard></>;
}

function NetworkSettings({ settings, update }: { settings: AppSettings; update: (patch: Partial<AppSettings>) => void }) {
  return <><SettingsHeading title="Network" /><div className="network-grid"><SettingCard><FieldHeading title="Global bandwidth limit (aggregate)" description="Applies to all active downloads combined." /><div className="radio-row" role="radiogroup" aria-label="Global bandwidth limit"><Radio checked={settings.bandwidthLimit === null} onClick={() => update({ bandwidthLimit: null })} label="Unlimited" /><Radio checked={settings.bandwidthLimit !== null} onClick={() => update({ bandwidthLimit: 50 })} label="Limited to:" /><input aria-label="Global bandwidth limit" className="number-input" type="number" min="1" value={settings.bandwidthLimit ?? 50} onClick={(event) => event.currentTarget.select()} onChange={(event) => update({ bandwidthLimit: Number(event.target.value) || 1 })} /><Select label="Bandwidth unit" value={settings.bandwidthUnit} onChange={(bandwidthUnit) => update({ bandwidthUnit: bandwidthUnit as AppSettings['bandwidthUnit'] })} options={[['KB/s', 'KB/s'], ['MB/s', 'MB/s'], ['GB/s', 'GB/s']]} /></div><FieldHeading title="Default maximum connections per download" description="Maximum number of connections used for each download." /><input aria-label="Default maximum connections per download" className="number-input large" type="number" min="1" max="32" value={settings.maxConnections} onClick={(event) => event.currentTarget.select()} onChange={(event) => update({ maxConnections: Math.max(1, Math.min(32, Number(event.target.value) || 1)) })} /><SettingToggle title="Per-download override allowed" description="Allow custom limits and connections per download." checked={settings.perDownloadOverrides} onChange={(perDownloadOverrides) => update({ perDownloadOverrides })} /><SettingToggle title="Retry failed downloads automatically" description="Retry downloads that fail due to network issues." checked={settings.retryAutomatically} onChange={(retryAutomatically) => update({ retryAutomatically })} /><FieldHeading title="Max retry attempts" description="Number of times to retry before giving up." /><input className="number-input large" type="number" min="0" max="20" aria-label="Max retry attempts" value={settings.maxRetries} onClick={(event) => event.currentTarget.select()} onChange={(event) => update({ maxRetries: Math.max(0, Math.min(20, Number(event.target.value) || 0)) })} /></SettingCard><NetworkLiveStats /></div></>;
}

function NetworkLiveStats() {
  return <SettingCard className="usage-card"><div className="usage-title"><strong>Live transfer status</strong><span>Read-only</span></div><div className="live-status-card"><Icon name="chart" size={28} /><div><strong>Current aggregate speed</strong><span>Live speed is shown in the manager status bar.</span></div></div><div className="info-callout"><Icon name="info" size={16} /><span>Historical usage is not recorded by this build.</span></div></SettingCard>;
}

function NotificationSettings({ settings, update }: { settings: AppSettings; update: (patch: Partial<AppSettings>) => void }) {
  return <><SettingsHeading title="Notifications" /><SettingCard><SettingToggle title="Show notifications for completed downloads" description="Notify when a download finishes successfully." checked={settings.completionNotifications} onChange={(completionNotifications) => update({ completionNotifications })} /><SettingToggle title="Show notifications for failed downloads" description="Notify when a download fails." checked={settings.failureNotifications} onChange={(failureNotifications) => update({ failureNotifications })} /></SettingCard><div className="wide-info"><Icon name="info" size={17} /><div>Notifications use Windows system toasts and respect Focus Assist / Do Not Disturb settings.</div></div></>;
}

function AppearanceSettings({ settings, update }: { settings: AppSettings; update: (patch: Partial<AppSettings>) => void }) {
  const accents = ['#0878ed', '#0d9f57', '#9141c9', '#d3138c', '#f28b14', '#e33b31', '#149fb5', '#6c7785'];
  return <><SettingsHeading title="Appearance" /><SettingCard><FieldHeading title="Theme" /><div className="theme-options" role="radiogroup" aria-label="Theme"><ThemeOption value="system" label="Follow system" icon="monitor" current={settings.theme} onClick={() => update({ theme: 'system' })} /><ThemeOption value="light" label="Light" icon="power" current={settings.theme} onClick={() => update({ theme: 'light' })} /><ThemeOption value="dark" label="Dark" icon="pending" current={settings.theme} onClick={() => update({ theme: 'dark' })} /></div></SettingCard><SettingCard><FieldHeading title="Accent color" /><div className="accent-options" role="radiogroup" aria-label="Accent color">{accents.map((accent) => <button key={accent} role="radio" aria-checked={settings.accent === accent} aria-label={`Use ${accent} accent`} className={`accent-swatch ${settings.accent === accent ? 'selected' : ''}`} style={{ '--swatch': accent } as CSSProperties} onClick={() => update({ accent })}><span /></button>)}</div></SettingCard><SettingCard><FieldHeading title="App density" description="Choose how compact the interface should be." /><Select label="App density" value={settings.density} onChange={(density) => update({ density: density as AppSettings['density'] })} options={[['comfortable', 'Comfortable'], ['compact', 'Compact']]} /></SettingCard></>;
}

function ThemeOption({ value, label, icon, current, onClick }: { value: string; label: string; icon: IconName; current: string; onClick: () => void }) {
  return <button className={`theme-option ${current === value ? 'selected' : ''} theme-${value}`} role="radio" aria-checked={current === value} onClick={onClick}><span className="theme-preview"><Icon name={icon} size={24} /></span><span>{label}</span></button>;
}

function FieldHeading({ title, description }: { title: string; description?: string }) {
  return <div className="field-heading"><strong>{title}</strong>{description && <span>{description}</span>}</div>;
}

function PathField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return <div className="path-field"><input aria-label={label} value={value} onChange={(event) => onChange(event.target.value)} /></div>;
}

function Select({ label, value, onChange, options }: { label: string; value: string; onChange: (value: string) => void; options: Array<[string, string]> }) {
  return <label className="select-wrap"><select aria-label={label} value={value} onChange={(event) => onChange(event.target.value)}>{options.map(([optionValue, label]) => <option key={optionValue} value={optionValue}>{label}</option>)}</select><Icon name="chevron-down" size={14} /></label>;
}

function Radio({ checked, onClick, label }: { checked: boolean; onClick: () => void; label: string }) {
  return <button className="radio" role="radio" aria-checked={checked} onClick={onClick}><span className={checked ? 'checked' : ''} />{label}</button>;
}

export function AddDownloadWindow({ settings, job, onCreate, onCommit, onCancel, onClose }: { adapter?: DownloadAdapter; settings: AppSettings; job?: DownloadJob; onCreate?: (source: string, name: string, maxConnections: number, bandwidthLimit: number | null) => Promise<void>; onCommit?: (id: string, name: string, destination: string, maxConnections: number, bandwidthLimit: number | null) => void | Promise<void>; onCancel: (id: string) => void | Promise<void>; onClose: () => void }) {
  const [source, setSource] = useState(job?.source ?? '');
  const [name, setName] = useState(job?.name ?? '');
  const [nameTouched, setNameTouched] = useState(false);
  const [destination, setDestination] = useState(job?.destination ?? settings.defaultFolder);
  const [destTouched, setDestTouched] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const busyRef = useRef(false);
  const [busyAction, setBusyAction] = useState<'create' | 'commit' | 'cancel' | null>(null);
  const busy = busyAction !== null;
  const [formError, setFormError] = useState('');
  const [maxConnections, setMaxConnections] = useState(job?.maxConnections ?? settings.maxConnections);
  const capInitial = bpsToParts(job?.bandwidthLimit ?? 50 * 1024 ** 2);
  const [capLimited, setCapLimited] = useState(job?.bandwidthLimit != null);
  const [capValue, setCapValue] = useState(capInitial.value);
  const [capUnit, setCapUnit] = useState<BandwidthUnit>(capInitial.unit);
  useEffect(() => { if (job) { setSource(job.source); if (!nameTouched) setName(job.name); if (!destTouched) setDestination(job.destination); } }, [job?.id, job?.source, job?.name, job?.destination]);
  useEffect(() => { if (job?.maxConnections) setMaxConnections(job.maxConnections); }, [job?.id, job?.maxConnections]);
  useEffect(() => { if (job?.bandwidthLimit != null) { const parts = bpsToParts(job.bandwidthLimit); setCapLimited(true); setCapValue(parts.value); setCapUnit(parts.unit); } }, [job?.id]);
  const submit = async () => {
    if (busy || busyRef.current) return;
    const bandwidthLimit = capLimited ? bandwidthToBps(capValue, capUnit) : null;
    if (job && onCommit) {
      busyRef.current = true;
      setFormError('');
      let waitingForCommit = false;
      try {
        const result = onCommit(job.id, name, destination, maxConnections, bandwidthLimit);
        if (result && typeof result.then === 'function') {
          waitingForCommit = true;
          setBusyAction('commit');
          await result;
        }
      } catch (reason) {
        setFormError(reason instanceof Error && reason.message ? reason.message : 'Could not add the download.');
      } finally {
        busyRef.current = false;
        if (waitingForCommit) setBusyAction(null);
      }
      return;
    }
    if (!source.trim() || !onCreate) return;
    try {
      const parsed = new URL(source.trim());
      if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('Use an HTTP or HTTPS URL.');
    } catch (reason) {
      setFormError(reason instanceof Error && reason.message === 'Use an HTTP or HTTPS URL.' ? reason.message : 'Enter a valid HTTP or HTTPS URL.');
      return;
    }
    busyRef.current = true;
    setFormError('');
    setBusyAction('create');
    try { await onCreate(source.trim(), name.trim(), maxConnections, bandwidthLimit); } catch (reason) { setFormError(reason instanceof Error && reason.message ? reason.message : 'Could not start the download.'); } finally { busyRef.current = false; setBusyAction(null); }
  };
  const cancel = async () => {
    if (busy || busyRef.current) return;
    if (job && job.provisional !== false) {
      busyRef.current = true;
      setFormError('');
      let waitingForCancel = false;
      try {
        const result = onCancel(job.id);
        if (result && typeof result.then === 'function') {
          waitingForCancel = true;
          setBusyAction('cancel');
          await result;
        }
      } catch (reason) {
        setFormError(reason instanceof Error && reason.message ? reason.message : 'Could not cancel the download.');
      } finally {
        busyRef.current = false;
        if (waitingForCancel) setBusyAction(null);
      }
      return;
    }
    onClose();
  };
  return <section className={`add-download-window ${job ? 'captured' : 'manual'}`} aria-label="Add Download" aria-busy={busy}><div className="add-titlebar" data-tauri-drag-region="true" onMouseDown={startWindowDrag}><strong>Add Download</strong><button aria-label="Close" disabled={busy} onClick={cancel}><Icon name="close" size={17} /></button></div><div className="add-content"><label className="form-field"><span>URL</span><input aria-label="Source URL" autoFocus={!job} value={source} onChange={(event) => { setSource(event.target.value); setFormError(''); }} placeholder="https://example.com/file.iso" /></label>{formError && <div className="form-error" role="alert"><Icon name="error" size={14} />{formError}</div>}<label className="form-field"><span>Filename</span><div className="input-with-detail"><input aria-label="Filename" value={name} onChange={(event) => { setName(event.target.value); setNameTouched(true); }} placeholder="file.iso" /><em>{job?.total ? formatBytes(job.total) : 'Detecting size'}</em></div></label><label className="form-field"><span>Save to</span><div className="path-field"><input aria-label="Save destination" value={destination} onChange={(event) => { setDestination(event.target.value); setDestTouched(true); }} /></div></label>{job && <div className="provisional-panel"><Metric icon="download" label="Downloaded" value={`${formatBytes(job.downloaded)}${job.total ? ` / ${formatBytes(job.total)}` : ''}`} /><Metric icon="pending" label="Status" value={stateText(job.state)} tone={stateTone(job.state)} /><Metric icon="network" label="Connections" value={job.connections ? `${job.connections} active` : job.state === 'connecting' ? 'Connecting…' : job.state === 'finalizing' ? 'Ready' : '—'} /><Metric icon="shield" label="Resumable" value={job.resumable ? 'Yes' : 'Checking…'} /></div>}{settings.perDownloadOverrides && <><button className="advanced-toggle" aria-expanded={advanced} onClick={() => setAdvanced(!advanced)}><Icon name={advanced ? 'chevron-down' : 'chevron-right'} size={15} /> Advanced</button>{advanced && <div className="advanced-fields"><div><span>Connections</span><input aria-label="Per-download maximum connections" className="number-input" type="number" min="1" max="32" value={maxConnections} onChange={(event) => setMaxConnections(Math.max(1, Math.min(32, Number(event.target.value) || 1)))} /></div><div><span>Bandwidth cap</span><div className="radio-row" role="radiogroup" aria-label="Per-download bandwidth cap"><Radio checked={!capLimited} onClick={() => setCapLimited(false)} label="Global" /><Radio checked={capLimited} onClick={() => setCapLimited(true)} label="Limited to:" /><input aria-label="Per-download bandwidth limit" className="number-input" type="number" min="1" value={capValue} onChange={(event) => { setCapValue(Number(event.target.value) || 1); setCapLimited(true); }} /><Select label="Per-download bandwidth unit" value={capUnit} onChange={(unit) => setCapUnit(unit as BandwidthUnit)} options={BANDWIDTH_UNITS.map((unit) => [unit, unit] as [string, string])} /></div></div><span className="advanced-note">The per-download limit is applied when the acquisition is accepted.</span></div>}</>}<div className="add-actions"><button className="button primary" disabled={busy || (!job && !source.trim())} onClick={() => void submit()}><Icon name={job ? 'check' : 'download'} size={16} />{busyAction === 'create' ? 'Starting…' : busyAction === 'commit' ? 'Adding…' : job ? 'Download' : 'Start Download'}</button><button className="button" disabled={busy} onClick={cancel}>{busyAction === 'cancel' ? 'Cancelling…' : 'Cancel'}</button></div></div></section>;
}

function Metric({ icon, label, value, tone }: { icon: IconName; label: string; value: string; tone?: string }) {
  return <div className="metric"><Icon name={icon} size={16} /><span>{label}</span><strong className={tone ?? ''}>{value}</strong></div>;
}

function StandaloneAddWindow({ adapter, snapshot }: { adapter: DownloadAdapter; snapshot: AppSnapshot }) {
  const params = new URLSearchParams(window.location.search);
  const source = params.get('url') ?? '';
  const jobId = params.get('id') ?? undefined;
  const job = snapshot.jobs.find((item) => item.id === jobId);
  const [createdId, setCreatedId] = useState<string | undefined>(jobId);
  const currentJob = snapshot.jobs.find((item) => item.id === createdId);
  const close = () => window.close();
  const create = async (url: string, name: string, maxConnections: number, bandwidthLimit: number | null) => setCreatedId(await adapter.createProvisional({ source: url, name, maxConnections, bandwidthLimit }));
  return <div className="standalone-surface" data-theme={snapshot.settings.theme} style={{ '--accent': snapshot.settings.accent } as CSSProperties}><AddDownloadWindow adapter={adapter} settings={snapshot.settings} job={currentJob ?? job} onCreate={create} onCommit={async (id, name, destination, maxConnections, bandwidthLimit) => { await adapter.commitProvisional(id, { name, destination, maxConnections, bandwidthLimit }); close(); }} onCancel={async (id) => { await adapter.cancelJob(id); close(); }} onClose={close} /></div>;
}

function ExtensionPopup({ adapter, snapshot }: { adapter: DownloadAdapter; snapshot: AppSnapshot }) {
  const site = new URLSearchParams(window.location.search).get('site') ?? 'twitter.com';
  const excluded = snapshot.settings.excludedSites.includes(site);
  const update = (patch: Partial<AppSettings>) => { void adapter.updateSettings(patch); };
  const toggleSite = () => update({ excludedSites: excluded ? snapshot.settings.excludedSites.filter((item) => item !== site) : [...snapshot.settings.excludedSites, site] });
  return <div className="popup-surface" data-theme={snapshot.settings.theme} style={{ '--accent': snapshot.settings.accent } as CSSProperties}><div className="popup-header"><span className="product-logo"><Icon name="download" size={17} /></span><strong>Download Manager</strong><button className="icon-button" aria-label="Close" onClick={closeSurface}><Icon name="close" size={15} /></button></div><div className="popup-toggles"><SettingToggle icon="download" title="Intercept browser downloads" checked={snapshot.settings.interceptDownloads} onChange={(interceptDownloads) => update({ interceptDownloads })} /><SettingToggle icon="media" title="Show media buttons" checked={snapshot.settings.showMediaButtons} onChange={(showMediaButtons) => update({ showMediaButtons })} /></div><div className="popup-site"><span className="eyebrow">Current site</span><strong>{site}</strong>{excluded ? <span className="excluded-copy">Media buttons excluded on this site</span> : <span className="enabled-copy">Media buttons enabled on this site</span>}<button className="button" onClick={toggleSite}>{excluded ? 'Enable on this site' : 'Exclude this site'}</button></div><button className="popup-manager-button" onClick={() => openManagerSurface()}><span>Open Manager</span><Icon name="open" size={15} /></button></div>;
}

function TrayMenu({ adapter, snapshot }: { adapter: DownloadAdapter; snapshot: AppSnapshot }) {
  const active = snapshot.jobs.filter((job) => ['downloading', 'connecting', 'finalizing'].includes(job.state)).length;
  const paused = active === 0 && snapshot.jobs.some((job) => job.state === 'paused' || job.state === 'pending');
  const update = (patch: Partial<AppSettings>) => { void adapter.updateSettings(patch); };
  return <div className="tray-surface" data-theme={snapshot.settings.theme} style={{ '--accent': snapshot.settings.accent } as CSSProperties}><div className="tray-status"><span className="status-dot" /><span>{active} active downloads</span><strong>{formatSpeed(snapshot.aggregateSpeed)}</strong></div><TrayAction icon="window" label="Open Download Manager" onClick={openManagerSurface} /><TrayAction icon={paused ? 'play' : 'pause'} label={paused ? 'Resume All' : 'Pause All'} onClick={() => void (paused ? adapter.resumeAll() : adapter.pauseAll())} /><div className="tray-divider" /><TrayToggle icon="globe" label="Browser Integration" checked={snapshot.settings.interceptDownloads} onChange={(interceptDownloads) => update({ interceptDownloads })} /><TrayToggle icon="media" label="Media Buttons" checked={snapshot.settings.showMediaButtons} onChange={(showMediaButtons) => update({ showMediaButtons })} /><TrayAction icon="network" label="Set Bandwidth Limit" chevron onClick={() => { window.location.href = `${window.location.pathname}?settings=network`; }} /><div className="tray-divider" /><TrayAction icon="power" label="Exit Manager" danger onClick={closeSurface} /></div>;
}

function TrayAction({ icon, label, onClick, chevron, danger }: { icon: IconName; label: string; onClick?: () => void; chevron?: boolean; danger?: boolean }) {
  return <button className={`tray-action ${danger ? 'danger' : ''}`} onClick={onClick}><Icon name={icon} size={17} /><span>{label}</span>{chevron && <Icon name="chevron-right" size={14} />}</button>;
}

function TrayToggle({ icon, label, checked, onChange }: { icon: IconName; label: string; checked: boolean; onChange: (value: boolean) => void }) {
  return <div className="tray-toggle"><Icon name={icon} size={17} /><span>{label}</span><Toggle label={label} checked={checked} onChange={onChange} /></div>;
}

export function NotificationsSurface({ snapshot }: { snapshot: AppSnapshot }) {
  const [dismissedIds, setDismissedIds] = useState<Set<string>>(() => new Set());
  const [pathError, setPathError] = useState('');
  const items = snapshot.notifications.filter((item) => !dismissedIds.has(item.id));
  const dismiss = (id: string) => setDismissedIds((current) => new Set(current).add(id));
  const dismissAll = () => setDismissedIds((current) => {
    const next = new Set(current);
    snapshot.notifications.forEach((item) => next.add(item.id));
    return next;
  });
  const openPath = (path: string) => {
    setPathError('');
    void openLocalPath(path).then((error) => { if (error) setPathError(error); });
  };
  return <div className="notifications-surface" data-theme={snapshot.settings.theme} style={{ '--accent': snapshot.settings.accent } as CSSProperties}><div className="notification-stack-title"><strong>Notifications</strong><button className="icon-button" aria-label="Dismiss all" onClick={dismissAll}><Icon name="close" size={16} /></button></div>{pathError && <div className="form-error" role="alert"><Icon name="error" size={14} />{pathError}</div>}{items.map((item) => <NotificationCard key={item.id} item={item} job={snapshot.jobs.find((job) => job.id === item.jobId)} onDismiss={() => dismiss(item.id)} onOpen={openPath} />)}{!items.length && <div className="empty-notifications"><Icon name="check" size={24} /><span>You're all caught up</span></div>}</div>;
}

function NotificationCard({ item, job, onDismiss, onOpen }: { item: AppSnapshot['notifications'][number]; job?: DownloadJob; onDismiss: () => void; onOpen: (path: string) => void }) {
  const completed = item.type === 'completed';
  const folder = job?.destination.slice(0, Math.max(job.destination.lastIndexOf('\\'), job.destination.lastIndexOf('/')));
  return <article className={`notification-card ${completed ? 'completed' : 'failed'}`}><div className="notification-icon"><Icon name={completed ? 'check' : 'error'} size={20} /></div><div className="notification-copy"><div><strong>{item.title}</strong><span>{item.time}</span></div><p>{item.detail}</p><div className="notification-actions"><button className="button" onClick={() => completed && job ? onOpen(job.destination) : openManagerSurface(item.jobId)}>{completed ? 'Open' : 'View details'}</button><button className="button" onClick={() => completed && folder ? onOpen(folder) : openManagerSurface(item.jobId)}>{completed ? 'Show in folder' : 'Open Manager'}</button></div></div><button className="notification-close" aria-label="Dismiss" onClick={onDismiss}><Icon name="close" size={15} /></button></article>;
}

export default App;
