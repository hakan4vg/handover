import type { CSSProperties, ReactNode } from 'react';

export type IconName =
  | 'settings'
  | 'all'
  | 'active'
  | 'pending'
  | 'completed'
  | 'failed'
  | 'media'
  | 'pause'
  | 'play'
  | 'more'
  | 'add'
  | 'sort'
  | 'filter'
  | 'search'
  | 'globe'
  | 'folder'
  | 'open'
  | 'refresh'
  | 'delete'
  | 'copy'
  | 'link'
  | 'chevron-down'
  | 'chevron-left'
  | 'chevron-right'
  | 'close'
  | 'check'
  | 'info'
  | 'warning'
  | 'error'
  | 'download'
  | 'network'
  | 'bell'
  | 'palette'
  | 'power'
  | 'window'
  | 'shield'
  | 'chart'
  | 'file'
  | 'disk'
  | 'archive'
  | 'audio'
  | 'monitor';

interface IconProps {
  name: IconName;
  size?: number;
  strokeWidth?: number;
  className?: string;
  style?: CSSProperties;
  title?: string;
}

const paths: Record<IconName, ReactNode> = {
  settings: <><path d="M12 8.2a3.8 3.8 0 1 0 0 7.6 3.8 3.8 0 0 0 0-7.6Z"/><path d="m19.4 13.5 1.1.8-1.5 2.6-1.3-.5a8.4 8.4 0 0 1-1.6.9l-.2 1.4h-3l-.2-1.4a8.4 8.4 0 0 1-1.6-.9l-1.3.5-1.5-2.6 1.1-.8a7.4 7.4 0 0 1 0-1.9l-1.1-.8 1.5-2.6 1.3.5a8.4 8.4 0 0 1 1.6-.9l.2-1.4h3l.2 1.4a8.4 8.4 0 0 1 1.6.9l1.3-.5 1.5 2.6-1.1.8a7.4 7.4 0 0 1 0 1.9Z"/></>,
  all: <><path d="M5 5h5v5H5zM14 5h5v5h-5zM5 14h5v5H5zM14 14h5v5h-5z"/></>,
  active: <><path d="M6 12h12"/><path d="M12 6v12"/><path d="m17 8 3 4-3 4"/><path d="m7 8-3 4 3 4"/></>,
  pending: <><circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3 2"/></>,
  completed: <><circle cx="12" cy="12" r="8.5"/><path d="m8 12 2.6 2.6L16.5 9"/></>,
  failed: <><path d="M12 3.7 20.5 19H3.5L12 3.7Z"/><path d="M12 9v4.2M12 16.2v.1"/></>,
  media: <><rect x="4" y="4" width="16" height="16" rx="3"/><path d="m10 8 6 4-6 4V8Z"/></>,
  pause: <><path d="M8 5.5v13M16 5.5v13"/></>,
  play: <path d="m9 6.5 8 5.5-8 5.5v-11Z"/>,
  more: <><circle cx="5" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1" fill="currentColor" stroke="none"/></>,
  add: <><path d="M12 5v14M5 12h14"/></>,
  sort: <><path d="M7 5v14M4 8l3-3 3 3M17 19V5M14 16l3 3 3-3"/></>,
  filter: <path d="M4 6h16M7 12h10M10 18h4"/>,
  search: <><circle cx="10.5" cy="10.5" r="5.7"/><path d="m15 15 4.5 4.5"/></>,
  globe: <><circle cx="12" cy="12" r="8.5"/><path d="M3.7 12h16.6M12 3.5c2.2 2.3 3.2 5.1 3.2 8.5s-1 6.2-3.2 8.5c-2.2-2.3-3.2-5.1-3.2-8.5s1-6.2 3.2-8.5Z"/></>,
  folder: <path d="M3.8 7.2h5l1.7 1.8h9.7v8.4a2 2 0 0 1-2 2H5.8a2 2 0 0 1-2-2V7.2Z"/>,
  open: <><path d="M7 17.5h10a2 2 0 0 0 2-2V9.8"/><path d="M12 5h7v7M19 5l-8 8"/></>,
  refresh: <><path d="M19 8.5A7.7 7.7 0 0 0 5 7.3L4 10"/><path d="M4 5.8V10h4"/><path d="M5 15.5A7.7 7.7 0 0 0 19 16.7L20 14"/><path d="M20 18.2V14h-4"/></>,
  delete: <><path d="M5 7h14M10 4h4l1 3H9l1-3ZM7 7l.7 12h8.6L17 7M10 10v6M14 10v6"/></>,
  copy: <><rect x="8" y="8" width="10" height="11" rx="1.5"/><path d="M16 8V6.5A1.5 1.5 0 0 0 14.5 5h-8A1.5 1.5 0 0 0 5 6.5v9A1.5 1.5 0 0 0 6.5 17H8"/></>,
  link: <><path d="m9.2 14.8-1.4 1.4a3.4 3.4 0 0 1-4.8-4.8l2.5-2.5a3.4 3.4 0 0 1 4.8 0"/><path d="m14.8 9.2 1.4-1.4a3.4 3.4 0 0 1 4.8 4.8l-2.5 2.5a3.4 3.4 0 0 1-4.8 0"/><path d="m8.5 15.5 7-7"/></>,
  'chevron-down': <path d="m7 9 5 5 5-5"/>,
  'chevron-left': <path d="m15 6-6 6 6 6"/>,
  'chevron-right': <path d="m9 6 6 6-6 6"/>,
  close: <><path d="m6 6 12 12M18 6 6 18"/></>,
  check: <path d="m5 12 4.2 4.2L19 6.5"/>,
  info: <><circle cx="12" cy="12" r="8.5"/><path d="M12 10.5v5M12 7.5v.1"/></>,
  warning: <><path d="M12 3.7 20.5 19H3.5L12 3.7Z"/><path d="M12 9v4.2M12 16.2v.1"/></>,
  error: <><circle cx="12" cy="12" r="8.5"/><path d="m9 9 6 6M15 9l-6 6"/></>,
  download: <><path d="M12 4v10M8 11l4 4 4-4M5 19h14"/></>,
  network: <><circle cx="12" cy="5" r="2.2"/><circle cx="5.5" cy="18" r="2.2"/><circle cx="18.5" cy="18" r="2.2"/><path d="M11 7 6.7 16M13 7l4.3 9M7.8 18h8.4"/></>,
  bell: <><path d="M6.5 16.5h11l-1.3-1.8V10a4.2 4.2 0 0 0-8.4 0v4.7l-1.3 1.8ZM10 19h4"/></>,
  palette: <path d="M12 4.2a7.8 7.8 0 1 0 0 15.6h1.5c1.1 0 1.8-1.2 1.2-2.2-.7-1.2.2-2.7 1.6-2.7H18a3.8 3.8 0 0 0 3.8-3.8A7.8 7.8 0 0 0 12 4.2Z"/>,
  power: <><path d="M12 3.7v8.2"/><path d="M17.8 6.5a7.5 7.5 0 1 1-11.6 0"/></>,
  window: <><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M4 9h16"/><path d="M7 7h.1M9.5 7h.1"/></>,
  shield: <path d="M12 3.8 19 6v5.2c0 4.2-2.9 7.2-7 9-4.1-1.8-7-4.8-7-9V6l7-2.2Z"/>,
  chart: <><path d="M4 19V5M4 19h16"/><path d="m7 15 3-4 3 2 4-6"/></>,
  file: <><path d="M6 3.8h7l5 5v11.4H6V3.8Z"/><path d="M13 3.8v5h5"/></>,
  disk: <><circle cx="12" cy="12" r="8.6"/><circle cx="12" cy="12" r="2.1"/><path d="M12 3.4v5.4"/></>,
  archive: <><path d="M4.5 6.5h15v12h-15zM3.5 4h17v2.5h-17z"/><path d="M9.5 10.5h5"/></>,
  audio: <><path d="M5 15V9l7-3v12l-7-3Z"/><path d="M12 9c3.6-.2 5.7 1.3 5.7 4.1S15.6 17.5 12 18"/></>,
  monitor: <><rect x="4" y="4.5" width="16" height="11" rx="1.6"/><path d="M9 19.5h6M12 15.5v4"/></>,
};

export function Icon({ name, size = 18, strokeWidth = 1.7, className, style, title }: IconProps) {
  return (
    <svg
      aria-hidden={title ? undefined : true}
      aria-label={title}
      className={className}
      fill="none"
      height={size}
      role={title ? 'img' : undefined}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth={strokeWidth}
      style={style}
      viewBox="0 0 24 24"
      width={size}
    >
      {paths[name]}
    </svg>
  );
}
