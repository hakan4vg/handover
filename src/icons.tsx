import type { CSSProperties } from 'react';
import {
  AddRegular, AddFilled,
  AlertRegular, AlertFilled,
  AppsRegular, AppsFilled,
  ArrowCircleDownRegular, ArrowCircleDownFilled,
  ArrowClockwiseRegular, ArrowClockwiseFilled,
  ArrowDownloadRegular, ArrowDownloadFilled,
  ArrowSortRegular, ArrowSortFilled,
  CheckmarkCircleRegular, CheckmarkCircleFilled,
  CheckmarkRegular, CheckmarkFilled,
  ChevronDownRegular, ChevronDownFilled,
  ChevronLeftRegular, ChevronLeftFilled,
  ChevronRightRegular, ChevronRightFilled,
  ClockRegular, ClockFilled,
  ColorRegular, ColorFilled,
  CopyRegular, CopyFilled,
  DeleteRegular, DeleteFilled,
  DesktopRegular, DesktopFilled,
  DismissCircleRegular, DismissCircleFilled,
  DismissRegular, DismissFilled,
  DocumentRegular, DocumentFilled,
  ErrorCircleRegular, ErrorCircleFilled,
  FolderOpenRegular, FolderOpenFilled,
  FolderRegular, FolderFilled,
  FolderZipRegular, FolderZipFilled,
  GlobeRegular, GlobeFilled,
  HardDriveRegular, HardDriveFilled,
  InfoRegular, InfoFilled,
  LinkRegular, LinkFilled,
  MoreHorizontalRegular, MoreHorizontalFilled,
  MusicNote2Regular, MusicNote2Filled,
  OpenRegular, OpenFilled,
  PauseRegular, PauseFilled,
  PlayRegular, PlayFilled,
  PowerRegular, PowerFilled,
  PuzzlePieceRegular, PuzzlePieceFilled,
  SettingsRegular, SettingsFilled,
  ShieldLockRegular, ShieldLockFilled,
  VideoRegular, VideoFilled,
  WarningRegular, WarningFilled,
  WeatherMoonRegular, WeatherMoonFilled,
  WeatherSunnyRegular, WeatherSunnyFilled,
  WindowRegular, WindowFilled,
  type FluentIcon,
} from '@fluentui/react-icons';

/** The app's icons, by what they mean; each is a Fluent System Icon with a
 *  regular and a filled form (filled marks the selected item). */
const icons = {
  settings: [SettingsRegular, SettingsFilled],
  all: [AppsRegular, AppsFilled],
  active: [ArrowCircleDownRegular, ArrowCircleDownFilled],
  pending: [ClockRegular, ClockFilled],
  completed: [CheckmarkCircleRegular, CheckmarkCircleFilled],
  failed: [ErrorCircleRegular, ErrorCircleFilled],
  media: [VideoRegular, VideoFilled],
  pause: [PauseRegular, PauseFilled],
  play: [PlayRegular, PlayFilled],
  more: [MoreHorizontalRegular, MoreHorizontalFilled],
  add: [AddRegular, AddFilled],
  sort: [ArrowSortRegular, ArrowSortFilled],
  globe: [GlobeRegular, GlobeFilled],
  browser: [PuzzlePieceRegular, PuzzlePieceFilled],
  folder: [FolderRegular, FolderFilled],
  'folder-open': [FolderOpenRegular, FolderOpenFilled],
  open: [OpenRegular, OpenFilled],
  refresh: [ArrowClockwiseRegular, ArrowClockwiseFilled],
  delete: [DeleteRegular, DeleteFilled],
  remove: [DismissCircleRegular, DismissCircleFilled],
  copy: [CopyRegular, CopyFilled],
  link: [LinkRegular, LinkFilled],
  'chevron-down': [ChevronDownRegular, ChevronDownFilled],
  'chevron-left': [ChevronLeftRegular, ChevronLeftFilled],
  'chevron-right': [ChevronRightRegular, ChevronRightFilled],
  close: [DismissRegular, DismissFilled],
  check: [CheckmarkRegular, CheckmarkFilled],
  info: [InfoRegular, InfoFilled],
  warning: [WarningRegular, WarningFilled],
  error: [ErrorCircleRegular, ErrorCircleFilled],
  download: [ArrowDownloadRegular, ArrowDownloadFilled],
  network: [GlobeRegular, GlobeFilled],
  bell: [AlertRegular, AlertFilled],
  palette: [ColorRegular, ColorFilled],
  power: [PowerRegular, PowerFilled],
  window: [WindowRegular, WindowFilled],
  shield: [ShieldLockRegular, ShieldLockFilled],
  file: [DocumentRegular, DocumentFilled],
  disk: [HardDriveRegular, HardDriveFilled],
  archive: [FolderZipRegular, FolderZipFilled],
  audio: [MusicNote2Regular, MusicNote2Filled],
  monitor: [DesktopRegular, DesktopFilled],
  light: [WeatherSunnyRegular, WeatherSunnyFilled],
  dark: [WeatherMoonRegular, WeatherMoonFilled],
} satisfies Record<string, [FluentIcon, FluentIcon]>;

export type IconName = keyof typeof icons;

interface IconProps {
  name: IconName;
  size?: number;
  filled?: boolean;
  className?: string;
  style?: CSSProperties;
  title?: string;
}

export function Icon({ name, size = 18, filled = false, className, style, title }: IconProps) {
  const Glyph = icons[name][filled ? 1 : 0];
  return <Glyph aria-hidden={title ? undefined : true} aria-label={title} role={title ? 'img' : undefined} className={className} fontSize={size} style={style} />;
}
