#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod ipc;
mod lifecycle;
mod media;
mod notify;
mod protect;
mod startup;

use futures_util::{future::Abortable, FutureExt, StreamExt};
use lifecycle::{state_allows_transfer, TransferRegistry};
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    io::{SeekFrom, Write},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicU64, Ordering},
        Mutex
    }
};
use tauri::image::Image;
use tauri::menu::{CheckMenuItem, CheckMenuItemBuilder, MenuBuilder, MenuItemBuilder};
use tauri::tray::TrayIconBuilder;
use tauri::{webview::PageLoadEvent, AppHandle, Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tokio::time::{sleep, Duration};
use tokio::{fs::{File, OpenOptions}, io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt}};
use uuid::Uuid;

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct JobEvent { at: String, message: String, tone: Option<String> }

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct DownloadJob {
    id: String,
    name: String,
    source: String,
    domain: String,
    state: String,
    progress: f64,
    downloaded: u64,
    total: Option<u64>,
    speed: u64,
    eta: Option<String>,
    connections: u32,
    max_connections: u32,
    /// Per-job bandwidth cap in bytes/sec (SPEC §8.6: constrains the job
    /// inside the global limit). None = no per-job cap. Serde default keeps
    /// databases written before this field existed loadable.
    #[serde(default)]
    bandwidth_limit: Option<u64>,
    mode: String,
    media: bool,
    #[serde(default)]
    media_tracks: Option<u32>,
    destination: String,
    temp_path: String,
    resumable: bool,
    mime: Option<String>,
    error: Option<String>,
    created: String,
    started: Option<String>,
    completed: Option<String>,
    provisional: Option<bool>,
    segments: Option<SegmentState>,
    #[serde(default)]
    completed_ranges: Vec<ByteRange>,
    #[serde(default)]
    resource_identity: Option<ResourceIdentity>,
    #[serde(default)]
    destination_reservation: Option<String>,
    #[serde(default)]
    selected_segments: Vec<String>,
    /// The player track requested by the capture. Old jobs omit this and
    /// retain the historical video default for media acquisitions.
    #[serde(default)]
    player_kind: Option<String>,
    /// Explicit direct companion audio URL supplied by the extension.
    /// Selected manifest hints must never be interpreted as this field.
    #[serde(default)]
    companion_audio: Option<String>,
    /// Capture-page URL replayed as Referer (SPEC §5.1/§16, scoped per
    /// request by referer_value). Serde default keeps old DBs loadable.
    #[serde(default)]
    referrer: Option<String>,
    /// Observed urlencoded form body replayed as the request body (SPEC §5.1
    /// method/body). Capped at parse; serde default keeps old DBs loadable.
    #[serde(default)]
    post_body: Option<String>,
    /// Browser User-Agent observed for this captured request. Only this safe,
    /// non-credential header is replayed; old jobs continue using the client
    /// default through serde default.
    #[serde(default)]
    user_agent: Option<String>,
    /// Alternate sources a capture handed over when the page could not prove
    /// which one feeds the player (SPEC §6.2). Resolved and cleared on the first
    /// acquisition attempt, so it never lingers in the store.
    #[serde(default)]
    candidates: Vec<String>,
    events: Vec<JobEvent>,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct SegmentState { completed: u32, total: u32, #[serde(default)] identity: Option<String> }

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ByteRange { start: u64, end: u64 }

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ResourceIdentity { length: u64, etag: Option<String>, last_modified: Option<String> }

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct AppSettings {
    start_at_sign_in: bool,
    show_manager_at_sign_in: bool,
    close_behavior: String,
    default_folder: String,
    #[serde(default = "default_collision_behavior")]
    collision_behavior: String,
    intercept_downloads: bool,
    show_media_buttons: bool,
    excluded_sites: Vec<String>,
    bandwidth_limit: Option<u64>,
    bandwidth_unit: String,
    max_connections: u32,
    per_download_overrides: bool,
    retry_automatically: bool,
    max_retries: u32,
    completion_notifications: bool,
    failure_notifications: bool,
    theme: String,
    accent: String,
    density: String,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct NotificationItem { id: String, #[serde(rename = "type")] notification_type: String, title: String, detail: String, time: String, job_id: String }

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct AppSnapshot { jobs: Vec<DownloadJob>, settings: AppSettings, connected: bool, aggregate_speed: u64, notifications: Vec<NotificationItem>, bridge_available: bool }

struct CoreState { snapshot: Mutex<AppSnapshot>, database: Mutex<Connection>, reattach_target: Mutex<Option<String>>, bandwidth: Mutex<BandwidthBucket>, job_bandwidth: Mutex<std::collections::HashMap<String, BandwidthBucket>>, transfer_controls: TransferRegistry, lifecycle: Mutex<()>, tray_checks: Mutex<Option<(CheckMenuItem<tauri::Wry>, CheckMenuItem<tauri::Wry>)>>, progress: Mutex<ProgressThrottle>, viability: Mutex<std::collections::HashMap<String, Viability>>, captures: Mutex<std::collections::VecDeque<(String, CaptureRecord)>>, adoptable_names: Mutex<std::collections::HashSet<String>>, publishing: Mutex<std::collections::HashSet<String>> }

/// Hot-loop progress bookkeeping (F09): transfer chunks mark their job dirty
/// instead of rewriting the whole database. UI emits run at most 4 Hz shared
/// across jobs; dirty jobs persist at most every 2 s plus on every terminal
/// transition (which keeps full emit_snapshot). A crash loses at most 2 s of
/// progress claims — refetched, never trusted.
struct ProgressThrottle { dirty_jobs: std::collections::HashSet<String>, last_emit: std::time::Instant, last_persist: std::time::Instant }

impl Default for ProgressThrottle {
    fn default() -> Self {
        let now = std::time::Instant::now();
        Self { dirty_jobs: std::collections::HashSet::new(), last_emit: now, last_persist: now }
    }
}

/// Resolved once per acquisition that asked to prove itself before the browser
/// lets go of its own copy: Ok when the first response is the file, Err with
/// the reason otherwise (SPEC §5.1.1).
type Viability = tokio::sync::oneshot::Sender<Result<(), String>>;

/// What the bridge knows about an extension-generated capture id. A capture
/// whose acknowledgement was lost is cancelled by id, and that cancel may
/// arrive before the create it refers to.
#[derive(Clone)]
enum CaptureRecord { Job(String), Cancelled }
const CAPTURE_LEDGER_MAX: usize = 256;

const PROGRESS_EMIT_INTERVAL: Duration = Duration::from_millis(250);
const PROGRESS_PERSIST_INTERVAL: Duration = Duration::from_secs(2);

fn emit_progress(app: &AppHandle, state: &CoreState, id: &str) {
    let now = std::time::Instant::now();
    let (should_emit, should_persist) = match state.progress.lock() {
        Ok(mut progress) => {
            progress.dirty_jobs.insert(id.to_string());
            let should_emit = now.duration_since(progress.last_emit) >= PROGRESS_EMIT_INTERVAL;
            let should_persist = now.duration_since(progress.last_persist) >= PROGRESS_PERSIST_INTERVAL;
            if should_emit {
                progress.last_emit = now;
            }
            if should_persist {
                progress.last_persist = now;
            }
            (should_emit, should_persist)
        }
        Err(_) => (true, true),
    };
    if should_persist {
        if let Err(error) = persist_dirty_jobs(state) {
            eprintln!("Progress checkpoint failed: {error}");
        }
    }
    if should_emit {
        if let Err(error) = emit_snapshot_event(app, state) {
            eprintln!("Progress emit failed: {error}");
        }
    }
}

fn persist_dirty_jobs(state: &CoreState) -> Result<(), String> {
    loop {
        let next = state
            .progress
            .lock()
            .map_err(|_| "State unavailable".to_string())?
            .dirty_jobs
            .iter()
            .next()
            .cloned();
        let Some(id) = next else { break };
        persist_job(state, &id)?;
        state
            .progress
            .lock()
            .map_err(|_| "State unavailable".to_string())?
            .dirty_jobs
            .remove(&id);
    }
    Ok(())
}

fn persist_job(state: &CoreState, id: &str) -> Result<(), String> {
    let (created, payload) = {
        let snapshot = state.snapshot.lock().map_err(|_| "State unavailable".to_string())?;
        let job = snapshot
            .jobs
            .iter()
            .find(|job| job.id == id)
            .ok_or_else(|| "Acquisition no longer exists".to_string())?;
        let mut stored = job.clone();
        protect_job_for_storage(&mut stored);
        stored.error = stored.error.map(|error| redact_url_credentials(&error));
        (
            job.created.clone(),
            serde_json::to_string(&stored).map_err(|error| format!("Could not serialize job {id}: {error}"))?,
        )
    };
    let database = state.database.lock().map_err(|_| "Database unavailable".to_string())?;
    database
        .execute(
            "INSERT INTO jobs (id, created_at, payload) VALUES (?1, ?2, ?3) ON CONFLICT(id) DO UPDATE SET created_at = excluded.created_at, payload = excluded.payload",
            params![id, created, payload],
        )
        .map_err(|error| format!("Could not persist job {id}: {error}"))?;
    Ok(())
}

// Transfer ownership is separate from persisted job state. Pause and cancel
// abort the owning task and free its ownership at once, so Resume can start a
// new task immediately. The aborted task runs no further engine code (its
// future is dropped at its next poll) and its generation is no longer
// current, so nothing gated on transfer_is_current proceeds. What can still
// land is a file operation already handed to the blocking pool: the same
// bytes of the same resource at the same offset, or a part file that is
// truncated and rewritten before it is ever renamed into place.

// One shared token bucket paces every transfer, so the bandwidth used in
// aggregate never exceeds the global limit no matter how many workers or jobs
// run (SPEC §8.6). Per-worker sleeping would multiply the limit by the worker
// count; a shared bucket cannot.
struct BandwidthBucket { tokens: f64, updated: std::time::Instant }

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ProvisionalInput { source: String, name: Option<String>, media: Option<bool>, max_connections: Option<u32>, bandwidth_limit: Option<u64>, #[serde(default)] selected_segments: Vec<String>, #[serde(default)] candidates: Vec<String>, #[serde(default)] player_kind: Option<String>, #[serde(default)] companion_audio: Option<String>, #[serde(default, alias = "pageUrl")] referrer: Option<String>, #[serde(default)] post_body: Option<String>, #[serde(default)] user_agent: Option<String>, #[serde(default)] name_is_hint: bool, #[serde(default)] destination: Option<String> }

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct CommitInput { name: String, destination: String, max_connections: Option<u32>, #[serde(default, deserialize_with = "opt_opt_u64")] bandwidth_limit: Option<Option<u64>> }

// Three-state optional: missing key -> None (keep), explicit null ->
// Some(None) (clear), number -> Some(Some(n)) (set). Plain
// Option<Option<u64>> would collapse null into None and make "clear"
// indistinguishable from "keep".
fn opt_opt_u64<'de, D>(deserializer: D) -> Result<Option<Option<u64>>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    Option::<u64>::deserialize(deserializer).map(Some)
}

/// Machine timestamps (F15): every created/started/completed/event stamp is a
/// real UTC ISO-8601 instant. The UI formats it local; created_at ordering
/// stays chronological across restarts.
fn now_label() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or(0);
    utc_iso_label(secs)
}

fn utc_iso_label(secs: i64) -> String {
    // Days-from-civil, proleptic Gregorian.
    let days = secs.div_euclid(86_400);
    let time = secs.rem_euclid(86_400);
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let mut year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = (if mp < 10 { mp + 3 } else { mp - 9 }) as u32;
    year += if month <= 2 { 1 } else { 0 };
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z", year, month, day, time / 3600, time % 3600 / 60, time % 60)
}

fn default_collision_behavior() -> String { "rename".into() }

fn clamp_connections(value: u32) -> u32 { value.clamp(1, 32) }

#[cfg(not(windows))]
const APP_IDENTIFIER: &str = "com.downloadmanager.app";

fn home_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("USERPROFILE").map(PathBuf::from))
        .unwrap_or_else(|| PathBuf::from("."))
}

// Single resolver for the per-user application data directory on every OS.
// Mirrors the platform convention Tauri itself uses for the same bundle
// identifier, so the database and browser policy use one stable location.
fn app_data_root() -> PathBuf {
    #[cfg(windows)]
    {
        return std::env::current_exe()
            .ok()
            .and_then(|executable| executable.parent().map(|parent| parent.join("data")))
            .unwrap_or_else(|| PathBuf::from("data"));
    }
    #[cfg(not(windows))]
    {
        #[cfg(target_os = "macos")]
        let base = home_dir().join("Library").join("Application Support");
        #[cfg(all(unix, not(target_os = "macos")))]
        let base = std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
            .unwrap_or_else(|| home_dir().join(".local").join("share"));
        base.join(APP_IDENTIFIER)
    }
}

/// Job temporary data lives in the portable application data folder, beside the
/// database and the WebView2 profile, so moving or renaming the product folder
/// moves all of its state together. It is deliberately not a setting: a portable
/// product must not scatter parts of a download into the user profile.
fn temp_root() -> PathBuf {
    app_data_root().join("tmp")
}

fn default_settings() -> AppSettings {
    let home = home_dir();
    let default_folder = home.join("Downloads");
    AppSettings {
        start_at_sign_in: true,
        show_manager_at_sign_in: true,
        close_behavior: "tray".into(),
        default_folder: default_folder.to_string_lossy().into_owned(),
        collision_behavior: default_collision_behavior(),
        intercept_downloads: true,
        show_media_buttons: true,
        excluded_sites: vec![],
        bandwidth_limit: None,
        bandwidth_unit: "MB/s".into(),
        max_connections: 8,
        per_download_overrides: true,
        retry_automatically: true,
        max_retries: 5,
        completion_notifications: true,
        failure_notifications: true,
        theme: "system".into(),
        accent: "#0878ed".into(),
        density: "comfortable".into()
    }
}

type BrowserPolicy = (bool, bool, Vec<String>);

fn browser_policy_root() -> PathBuf {
    app_data_root()
}

fn browser_policy_value(policy: &BrowserPolicy) -> Value {
    json!({ "interceptDownloads": policy.0, "showMediaButtons": policy.1, "excludedSites": policy.2 })
}

fn normalize_policy_site(value: &str) -> String {
    let input = value.trim();
    if input.is_empty() {
        return String::new();
    }
    let candidate = if input.starts_with("//") {
        format!("https:{input}")
    } else if input.contains("://") {
        input.to_string()
    } else {
        format!("https://{input}")
    };
    reqwest::Url::parse(&candidate)
        .ok()
        .and_then(|url| {
            url.host_str()
                .map(|host| host.trim_start_matches("www.").to_ascii_lowercase())
        })
        .unwrap_or_else(|| {
            input
                .split(['/', '?', '#'])
                .next()
                .unwrap_or("")
                .trim_start_matches("www.")
                .to_ascii_lowercase()
        })
}

fn browser_policy_from_value(value: &Value) -> Option<BrowserPolicy> {
    let payload = value.get("payload").unwrap_or(value);
    let intercept = payload.get("interceptDownloads").and_then(Value::as_bool)?;
    let media = payload.get("showMediaButtons").and_then(Value::as_bool)?;
    let excluded = payload.get("excludedSites").and_then(Value::as_array)?.iter().filter_map(Value::as_str).map(normalize_policy_site).filter(|site| !site.is_empty()).collect::<Vec<_>>();
    Some((intercept, media, excluded))
}

fn write_browser_policy(root: &Path, policy: &BrowserPolicy) -> Result<(), String> {
    // The JSON file is a coherence cache for the browser side, never a second
    // authority: failures propagate so callers roll back instead of diverging
    // silently (F14).
    std::fs::create_dir_all(root).map_err(|error| error.to_string())?;
    let contents = serde_json::to_string(&browser_policy_value(policy)).map_err(|error| error.to_string())?;
    std::fs::write(root.join("browser-policy.json"), contents).map_err(|error| error.to_string())
}

fn load_browser_policy(root: &Path) -> Option<BrowserPolicy> {
    let contents = std::fs::read_to_string(root.join("browser-policy.json")).ok()?;
    browser_policy_from_value(&serde_json::from_str(&contents).ok()?)
}

fn settings_policy(settings: &AppSettings) -> BrowserPolicy {
    (settings.intercept_downloads, settings.show_media_buttons, settings.excluded_sites.clone())
}

// Per-field settings recovery: overlay stored keys onto defaults one at a
// time, keeping a key only if the whole struct still parses. A single corrupt
// value (e.g. a float where the schema wants u64) previously discarded the
// user's folders, toggles, and limits wholesale; now only that key falls back
// to its default. Runs once per boot, so the per-key re-parse cost is trivial.
fn settings_from_stored(stored: &str) -> AppSettings {
    let defaults = default_settings();
    let Ok(overlay) = serde_json::from_str::<Value>(stored) else {
        return defaults;
    };
    let mut settings = apply_settings_patch(&defaults, &overlay);
    if settings.default_folder.trim().is_empty() {
        settings.default_folder = defaults.default_folder.clone();
    }
    settings
}

// Per-key patch application shared by boot recovery and the live
// update_settings command: overlay entries onto a base, keeping an entry
// only if the whole struct still parses. Non-object patches leave the base
// untouched. One bad value can no longer veto the good keys around it.
// Blank folder paths are rejected outright: the folder fields are free-text
// inputs, and an empty download folder would silently turn every later
// destination into a relative path into the process working directory.
fn valid_setting_value(key: &str, value: &Value) -> bool {
    match key {
        "closeBehavior" => matches!(value.as_str(), Some("tray" | "exit")),
        "collisionBehavior" => matches!(value.as_str(), Some("rename" | "replace")),
        "bandwidthLimit" => value.is_null() || value.as_u64().is_some_and(|limit| limit > 0),
        "bandwidthUnit" => matches!(value.as_str(), Some("KB/s" | "MB/s" | "GB/s")),
        "maxConnections" => value.as_u64().is_some_and(|count| (1..=32).contains(&count)),
        "maxRetries" => value.as_u64().is_some_and(|count| count <= 20),
        "theme" => matches!(value.as_str(), Some("system" | "light" | "dark")),
        "density" => matches!(value.as_str(), Some("comfortable" | "compact")),
        _ => true,
    }
}

fn apply_settings_patch(current: &AppSettings, patch: &Value) -> AppSettings {
    let Value::Object(entries) = patch else { return current.clone(); };
    let mut merged = serde_json::to_value(current).unwrap_or(Value::Null);
    for (key, value) in entries {
        if !valid_setting_value(key, value) { continue; }
        if key == "defaultFolder" && value.as_str().is_some_and(|text| text.trim().is_empty()) { continue; }
        let previous = if let Value::Object(ref mut base) = merged { base.insert(key.clone(), value.clone()) } else { break; };
        if serde_json::from_value::<AppSettings>(merged.clone()).is_err() {
            if let Value::Object(ref mut base) = merged {
                if let Some(old) = previous { base.insert(key.clone(), old); } else { base.remove(key); }
            }
        }
    }
    serde_json::from_value(merged).unwrap_or_else(|_| current.clone())
}

fn apply_browser_policy(app: &AppHandle, state: &CoreState, policy: BrowserPolicy) -> Result<(), String> {
    let previous = state
        .snapshot
        .lock()
        .map(|snapshot| settings_policy(&snapshot.settings))
        .map_err(|_| "State unavailable".to_string())?;
    write_browser_policy(&browser_policy_root(), &policy)?;
    let patch = json!({
        "interceptDownloads": policy.0,
        "showMediaButtons": policy.1,
        "excludedSites": policy.2,
    });
    if let Err(error) = update_settings_snapshot(state, &patch) {
        let _ = write_browser_policy(&browser_policy_root(), &previous);
        return Err(error);
    }
    sync_tray_checks(app, policy.0, policy.1);
    emit_snapshot_event(app, state)
}

fn cleanup_media_track_files(temp_path: &str) {
    let path = Path::new(temp_path);
    let Some(parent) = path.parent() else { return; };
    let Some(name) = path.file_name().and_then(|value| value.to_str()) else { return; };
    let prefix = format!("{name}.track-");
    let Ok(entries) = std::fs::read_dir(parent) else { return; };
    for entry in entries.flatten() {
        let entry_path = entry.path();
        let is_track_file = entry.file_type().map(|kind| kind.is_file()).unwrap_or(false)
            && entry.file_name().to_string_lossy().starts_with(&prefix);
        if is_track_file { let _ = std::fs::remove_file(entry_path); }
    }
}

/// The temp root holds exactly one class of data: artifacts of the jobs in the
/// store, each named after the job id (`<id>.part`, plus its `.segments`
/// directory, `.track-NN` files, and `.mux.*` outputs). Boot is the only moment
/// guaranteed to be free of live transfers, so anything else found there is
/// debris — a crashed transfer, an aborted provisional, or an older layout — and
/// is removed. Artifacts of non-completed jobs are kept: pause and resume depend
/// on them.
fn sweep_temp_root(temp_root: &Path, jobs: &[DownloadJob]) {
    let Ok(entries) = std::fs::read_dir(temp_root) else { return; };
    let prefixes = jobs
        .iter()
        .filter_map(|job| {
            Path::new(&job.temp_path)
                .file_name()
                .and_then(|name| name.to_str())
                .map(str::to_string)
        })
        .collect::<Vec<_>>();
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue; };
        let belongs = prefixes
            .iter()
            .any(|prefix| name == prefix || name.starts_with(&format!("{prefix}.")));
        if belongs {
            continue;
        }
        let path = entry.path();
        let _ = if entry.file_type().map(|kind| kind.is_dir()).unwrap_or(false) {
            std::fs::remove_dir_all(&path)
        } else {
            std::fs::remove_file(&path)
        };
    }
}

#[cfg(windows)]
fn copy_directory(from: &Path, to: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(to)?;
    for entry in std::fs::read_dir(from)? {
        let entry = entry?;
        let destination = to.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_directory(&entry.path(), &destination)?;
        } else {
            std::fs::copy(entry.path(), destination)?;
        }
    }
    Ok(())
}

#[cfg(windows)]
fn relocate_temp_artifact(from: &Path, to: &Path) -> bool {
    if from == to || !from.exists() || to.exists() {
        return true;
    }
    if let Some(parent) = to.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if std::fs::rename(from, to).is_ok() {
        return true;
    }
    if from.is_dir() {
        if copy_directory(from, to).is_ok() {
            let _ = std::fs::remove_dir_all(from);
            return true;
        }
    } else if std::fs::copy(from, to).is_ok() {
        let _ = std::fs::remove_file(from);
        return true;
    }
    false
}

#[cfg(windows)]
fn relocate_job_temp_artifacts(job: &mut DownloadJob, temp_folder: &Path) {
    let old_path = PathBuf::from(&job.temp_path);
    let new_path = temp_folder.join(format!("{}.part", job.id));
    if old_path == new_path {
        return;
    }
    if relocate_temp_artifact(&old_path, &new_path) {
        relocate_temp_artifact(
            &PathBuf::from(format!("{}.segments", old_path.display())),
            &PathBuf::from(format!("{}.segments", new_path.display()))
        );
        if let (Some(old_parent), Some(old_name), Some(new_parent), Some(new_name)) = (
            old_path.parent(),
            old_path.file_name(),
            new_path.parent(),
            new_path.file_name()
        ) {
            let prefix = format!("{}.", old_name.to_string_lossy());
            if let Ok(entries) = std::fs::read_dir(old_parent) {
                for entry in entries.flatten() {
                    let name = entry.file_name();
                    let name = name.to_string_lossy();
                    let Some(suffix) = name.strip_prefix(&prefix) else {
                        continue;
                    };
                    relocate_temp_artifact(
                        &entry.path(),
                        &new_parent.join(format!("{}.{}", new_name.to_string_lossy(), suffix))
                    );
                }
            }
        }
        job.temp_path = new_path.to_string_lossy().into_owned();
    }
}

fn snapshot_from_database(database: &Connection, settings: AppSettings) -> AppSnapshot {
    let mut jobs = Vec::new();
    let mut unlistable = Vec::new();
    #[cfg(windows)]
    let temp_folder = temp_root();
    if let Ok(mut statement) = database.prepare("SELECT id, payload FROM jobs ORDER BY created_at DESC")
    {
        if let Ok(rows) = statement.query_map([], |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))) {
            for (id, payload) in rows.flatten() {
                if let Ok(mut job) = serde_json::from_str::<DownloadJob>(&payload) {
                    if unprotect_job_from_storage(&mut job).is_err() {
                        // Foreign machine/user or corrupt envelope: bytes on
                        // disk are intact, but replay context is unusable here.
                        // Keep the shell, drop the secrets, route through
                        // Reattach instead of failing or leaking (F11).
                        job.source.clear();
                        job.referrer = None;
                        job.post_body = None;
                        job.selected_segments.clear();
                        if job.state != "completed" {
                            job.state = "paused".into();
                            job.events.insert(0, job_event("Saved source is unavailable on this machine — the folder may have moved. Use Reattach download.", Some("warning")));
                        }
                    }
                    #[cfg(windows)]
                    relocate_job_temp_artifacts(&mut job, &temp_folder);
                    if let Some(marker) = job.destination_reservation.clone() {
                        match reconcile_destination_reservation(
                            Path::new(&job.destination),
                            &marker,
                            job.total == Some(0)
                        ) {
                            DestinationReservationRecovery::Retry
                            | DestinationReservationRecovery::Missing => {
                                job.destination_reservation = None
                            }
                            DestinationReservationRecovery::Completed => {
                                job.destination_reservation = None;
                                if TRANSFER_STATES.contains(&job.state.as_str())
                                {
                                    complete_job(&mut job);
                                    let _ = std::fs::remove_file(&job.temp_path);
                                    let _ = std::fs::remove_dir_all(format!(
                                        "{}.segments",
                                        job.temp_path
                                    ));
                                    cleanup_media_track_files(&job.temp_path);
                                }
                            }
                            DestinationReservationRecovery::Unknown => {}
                        }
                    }
                    if job.provisional == Some(true) {
                        let _ = std::fs::remove_file(&job.temp_path);
                        let _ = std::fs::remove_dir_all(format!("{}.segments", job.temp_path));
                        cleanup_media_track_files(&job.temp_path);
                        unlistable.push(id);
                        continue;
                    }
                    jobs.push(job);
                } else {
                    eprintln!("Dropping stored job {id}: payload is not a readable job");
                    unlistable.push(id);
                }
            }
        }
    }
    // The list and the store must describe the same downloads. A row that cannot
    // be listed — unreadable, or an unaccepted provisional, which is explicitly
    // not durable — is removed here rather than lingering invisibly until the
    // next full rewrite.
    for id in unlistable {
        if let Err(error) = database.execute("DELETE FROM jobs WHERE id = ?1", params![id]) {
            eprintln!("Could not drop unlistable job {id}: {error}");
        }
    }
    sweep_temp_root(&temp_root(), &jobs);
    // Invariant: a notification always describes a job in the list (see
    // add_notification). Boot re-establishes it, because provisional rows are
    // dropped above and the center must not survive them.
    let mut notifications = load_notifications(database);
    notifications.retain(|item| jobs.iter().any(|job| job.id == item.job_id));
    AppSnapshot {
        jobs,
        settings,
        connected: true,
        aggregate_speed: 0,
        notifications,
        bridge_available: true,
    }
}

fn save_snapshot(state: &CoreState) -> Result<(), String> {
    let snapshot = state.snapshot.lock().map_err(|_| "State unavailable".to_string())?;
    let settings_payload = serde_json::to_string(&snapshot.settings).map_err(|error| format!("Could not serialize settings: {error}"))?;
    let jobs = snapshot.jobs.iter().map(|job| {
        let mut stored = job.clone();
        protect_job_for_storage(&mut stored);
        stored.error = stored.error.map(|error| redact_url_credentials(&error));
        serde_json::to_string(&stored)
            .map(|payload| (job.id.clone(), job.created.clone(), payload))
            .map_err(|error| format!("Could not serialize job {}: {error}", job.id))
    }).collect::<Result<Vec<_>, String>>()?;
    let mut database = state.database.lock().map_err(|_| "Database unavailable".to_string())?;
    let transaction = database.transaction().map_err(|error| format!("Could not begin snapshot transaction: {error}"))?;
    transaction.execute("INSERT OR REPLACE INTO settings (id, payload) VALUES (1, ?1)", params![settings_payload]).map_err(|error| format!("Could not persist settings: {error}"))?;
    transaction.execute("DELETE FROM jobs", []).map_err(|error| format!("Could not replace jobs: {error}"))?;
    for (id, created, payload) in jobs {
        transaction.execute("INSERT INTO jobs (id, created_at, payload) VALUES (?1, ?2, ?3)", params![id, created, payload]).map_err(|error| format!("Could not persist job {id}: {error}"))?;
    }
    transaction.commit().map_err(|error| format!("Could not commit snapshot: {error}"))
}

fn clear_destination_reservation(app: &AppHandle, state: &CoreState, id: &str) {
    let reservation = state.snapshot.lock().ok().and_then(|snapshot| {
        snapshot
            .jobs
            .iter()
            .find(|job| job.id == id)
            .and_then(|job| {
                job.destination_reservation
                    .as_ref()
                    .map(|marker| (job.destination.clone(), marker.clone()))
            })
    });
    if let Some((destination, marker)) = reservation {
        if holds_reservation_marker(Path::new(&destination), &marker) {
            let _ = std::fs::remove_file(destination);
        }
        emit_job(state, id, |job| job.destination_reservation = None);
        emit_snapshot(app, state);
    }
}

fn emit_snapshot_event(app: &AppHandle, state: &CoreState) -> Result<(), String> {
    let snapshot = state.snapshot.lock().map_err(|_| "State unavailable".to_string())?.clone();
    app.emit("state-changed", snapshot).map_err(|error| error.to_string())?;
    refresh_tray(app, state);
    Ok(())
}

fn emit_snapshot(app: &AppHandle, state: &CoreState) {
    // The windows show the live state even when storage refuses a write;
    // callers that must not proceed without durability persist explicitly.
    if let Err(error) = save_snapshot(state) {
        eprintln!("Snapshot save failed: {error}");
    }
    if let Err(error) = emit_snapshot_event(app, state) {
        eprintln!("Snapshot event failed: {error}");
    }
}

// SPEC §12: the tray shows the live active-download count and aggregate
// speed. The tooltip is the flicker-free surface for it; menu labels stay
// static so the menu never rebuilds under the user's cursor.
fn tray_status_text(active: usize, aggregate_speed: u64) -> String {
    if active == 0 {
        return "Download Manager — idle".into();
    }
    let noun = if active == 1 { "download" } else { "downloads" };
    format!("Download Manager — {active} active {noun} · {}/s", format_bytes(Some(aggregate_speed)))
}

fn refresh_tray(app: &AppHandle, state: &CoreState) {
    let status = state.snapshot.lock().ok().map(|snapshot| {
        let active = snapshot.jobs.iter().filter(|job| TRANSFER_STATES.contains(&job.state.as_str())).count();
        tray_status_text(active, snapshot.aggregate_speed)
    });
    if let Some(text) = status {
        if let Some(tray) = app.tray_by_id("main-tray") { let _ = tray.set_tooltip(Some(text)); }
    }
}

fn job_event(message: &str, tone: Option<&str>) -> JobEvent { JobEvent { at: now_label(), message: redact_url_credentials(message), tone: tone.map(str::to_string) } }

/// Mark of the Web: the same Attachment Manager record Chromium writes for a
/// download, so SmartScreen, Office Protected View and "unblock" prompts treat
/// the file as coming from the internet. Intercepting a browser download must
/// not strip that protection. Query strings, fragments and userinfo are
/// dropped: signed tokens do not belong in file metadata.
fn mark_downloaded_file(destination: &str, source: &str, referrer: Option<&str>) {
    #[cfg(windows)]
    {
        let origin = |value: &str| {
            reqwest::Url::parse(value)
                .ok()
                .filter(|url| matches!(url.scheme(), "http" | "https"))
                .map(|mut url| {
                    url.set_query(None);
                    url.set_fragment(None);
                    let _ = url.set_username("");
                    let _ = url.set_password(None);
                    url.to_string()
                })
        };
        if !Path::new(destination).is_file() {
            return;
        }
        let mut record = String::from("[ZoneTransfer]\r\nZoneId=3\r\n");
        if let Some(referrer) = referrer.and_then(origin) {
            record.push_str(&format!("ReferrerUrl={referrer}\r\n"));
        }
        if let Some(host) = origin(source) {
            record.push_str(&format!("HostUrl={host}\r\n"));
        }
        if let Err(error) = std::fs::write(format!("{destination}:Zone.Identifier"), record) {
            eprintln!("Could not mark the download's origin: {error}");
        }
    }
    #[cfg(not(windows))]
    let _ = (destination, source, referrer);
}

fn complete_job(job: &mut DownloadJob) {
    // A finished acquisition knows its size even when the source never
    // advertised one (segmented or unknown-length transfers).
    if job.total.is_none() { job.total = Some(job.downloaded); }
    mark_downloaded_file(&job.destination, &job.source, job.referrer.as_deref());
    job.speed = 0;
    job.connections = 0;
    job.eta = None;
    job.state = "completed".into();
    job.progress = 100.0;
    job.completed = Some(now_label());
    // SPEC §16: replay context is only needed while the job can transfer
    // again. A completed job never resumes, retries, or reattaches, so its
    // capture-page URL, form body, and browser UA must not outlive it in
    // the database. Failed jobs keep theirs: they are still resumable.
    job.referrer = None;
    job.post_body = None;
    job.user_agent = None;
    job.companion_audio = None;
    job.candidates.clear();
    job.events.insert(0, job_event("Download completed", Some("success")));
}

/// The bytes are acquired, but the job is still a provisional the user has not
/// accepted. It waits for that decision instead of reporting transfer progress:
/// nothing is moving, so nothing can be paused, and the Add Download surface is
/// where the user either saves it or cancels it.
fn mark_ready_for_confirmation(job: &mut DownloadJob, event: &str) {
    job.state = "ready".into();
    job.progress = 100.0;
    job.speed = 0;
    job.connections = 0;
    job.eta = None;
    job.events.insert(0, job_event(event, Some("warning")));
}

/// SPEC §16: transport errors embed the request URL, which may carry
/// single-use tokens in its query or fragment. Persist the host/path so a
/// failure stays diagnosable, never the credential tail. Identity for text
/// without URLs.
fn redact_url_credentials(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    loop {
        let next = ["http://", "https://"].iter().filter_map(|scheme| rest.find(scheme)).min();
        let Some(offset) = next else { break };
        let (head, tail) = rest.split_at(offset);
        out.push_str(head);
        let end = tail.find(|char: char| char.is_whitespace() || matches!(char, '"' | '\'' | ')' | '<')).unwrap_or(tail.len());
        let (url, after) = tail.split_at(end);
        match url.find(|char| char == '?' || char == '#') {
            Some(cut) => { out.push_str(&url[..cut]); out.push_str("[redacted]"); }
            None => out.push_str(url),
        }
        rest = after;
    }
    out.push_str(rest);
    out
}

/// SPEC §16: sources, replay context, and segment URLs are opaque at rest.
/// The in-memory job keeps plaintext for display and transfer; only the
/// database payload carries envelopes (F11).
fn protect_job_for_storage(job: &mut DownloadJob) {
    job.source = protect::protect_field(&job.source);
    if let Some(referrer) = job.referrer.take() {
        job.referrer = Some(protect::protect_field(&referrer));
    }
    if let Some(body) = job.post_body.take() {
        job.post_body = Some(protect::protect_field(&body));
    }
    job.selected_segments = job
        .selected_segments
        .iter()
        .map(|segment| protect::protect_field(segment))
        .collect();
    if let Some(companion_audio) = job.companion_audio.take() {
        job.companion_audio = Some(protect::protect_field(&companion_audio));
    }
}

fn unprotect_job_from_storage(job: &mut DownloadJob) -> Result<(), String> {
    job.source = protect::unprotect_field(&job.source)?;
    if let Some(referrer) = job.referrer.take() {
        job.referrer = Some(protect::unprotect_field(&referrer)?);
    }
    if let Some(body) = job.post_body.take() {
        job.post_body = Some(protect::unprotect_field(&body)?);
    }
    job.selected_segments = job
        .selected_segments
        .iter()
        .map(|segment| protect::unprotect_field(segment))
        .collect::<Result<Vec<_>, String>>()?;
    if let Some(companion_audio) = job.companion_audio.take() {
        job.companion_audio = Some(protect::unprotect_field(&companion_audio)?);
    }
    Ok(())
}

/// SPEC §16: the database holds resumable sources and replay context, so
/// the data dir and database are owner-only on Unix. Sources and replay
/// context are DPAPI-enveloped at rest on Windows (protect.rs); owner-only
/// Windows ACLs on the folder itself remain deferred.
fn restrict_data_dir(root: &Path) {
    let _ = std::fs::create_dir_all(root);
    #[cfg(unix)] {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(root, std::fs::Permissions::from_mode(0o700));
    }
    #[cfg(not(unix))] { let _ = root; }
}

fn restrict_file(path: &Path) {
    #[cfg(unix)] {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    #[cfg(not(unix))] { let _ = path; }
}

fn domain(source: &str) -> String { reqwest::Url::parse(source).ok().and_then(|url| url.host_str().map(str::to_string)).unwrap_or_else(|| "source unavailable".into()) }

fn source_compatible(existing: &str, candidate: &str) -> bool {
    let Some(existing) = reqwest::Url::parse(existing).ok() else { return false; };
    let Some(candidate) = reqwest::Url::parse(candidate).ok() else { return false; };
    existing.scheme() == candidate.scheme() && existing.host() == candidate.host() && existing.port_or_known_default() == candidate.port_or_known_default() && existing.path() == candidate.path()
}

fn safe_filename(value: &str) -> String {
    let leaf = value.trim().rsplit(|character| character == '/' || character == '\\').next().unwrap_or("").trim();
    let cleaned = leaf
        .chars()
        .map(|character| {
            if character.is_control() || matches!(character, '<' | '>' | ':' | '"' | '|' | '?' | '*') {
                '_'
            } else {
                character
            }
        })
        .collect::<String>()
        .trim_end_matches([' ', '.'])
        .to_string();
    if cleaned.is_empty() || cleaned == "." || cleaned == ".." {
        return "download.bin".into();
    }
    let cleaned = cap_filename(&cleaned);
    #[cfg(windows)]
    if windows_device_name(&cleaned) {
        return format!("_{cleaned}");
    }
    cleaned
}

#[cfg(windows)]
fn windows_device_name(value: &str) -> bool {
    let stem = value
        .split('.')
        .next()
        .unwrap_or_default()
        .trim_end_matches([' ', '.'])
        .to_ascii_uppercase();
    matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL" | "COM1" | "COM2" | "COM3" | "COM4" | "COM5" | "COM6" | "COM7" | "COM8" | "COM9" | "LPT1" | "LPT2" | "LPT3" | "LPT4" | "LPT5" | "LPT6" | "LPT7" | "LPT8" | "LPT9")
}

fn destination_for_filename(folder: &str, name: &str) -> String {
    Path::new(folder).join(safe_filename(name)).to_string_lossy().into_owned()
}

/// Manifest acquisition learns the real container only after parsing, while
/// the browser supplies a page-title name (usually ending in `.mp4`). The
/// acquired bytes decide the extension; the user's stem is preserved and the
/// destination stays consistent with the displayed name (F02).
fn manifest_output_name(current: &str, container_ext: &str) -> String {
    let extension = Path::new(current)
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("");
    if extension.eq_ignore_ascii_case(container_ext) {
        return current.to_string();
    }
    let stem = Path::new(current)
        .file_stem()
        .and_then(|value| value.to_str())
        .filter(|stem| !stem.is_empty())
        .unwrap_or("media");
    format!("{stem}.{container_ext}")
}

/// A file name longer than this is shortened, keeping its extension: common
/// Windows file systems allow 255 UTF-16 units per name, and the folder path
/// still has to fit around it.
const FILENAME_MAX_CHARS: usize = 180;

fn cap_filename(name: &str) -> String {
    if name.chars().count() <= FILENAME_MAX_CHARS {
        return name.to_string();
    }
    let extension = Path::new(name)
        .extension()
        .and_then(|value| value.to_str())
        .filter(|value| !value.is_empty() && value.chars().count() <= 16)
        .map(|value| format!(".{value}"))
        .unwrap_or_default();
    let stem: String = name
        .chars()
        .take(FILENAME_MAX_CHARS - extension.chars().count())
        .collect();
    format!("{}{extension}", stem.trim_end_matches([' ', '.']))
}

/// The URL's last path segment, percent-decoded as a browser would name it
/// (`My%20File.pdf` saves as `My File.pdf`). Bytes that do not decode to
/// UTF-8 keep their escapes rather than turning into replacement characters.
fn source_name(source: &str) -> String {
    reqwest::Url::parse(source)
        .ok()
        .and_then(|url| {
            url.path_segments()
                .and_then(|segments| segments.last())
                .map(str::to_string)
        })
        .filter(|name| !name.is_empty())
        .map(|name| {
            let bytes = name.as_bytes();
            let mut decoded = Vec::with_capacity(bytes.len());
            let mut index = 0;
            while index < bytes.len() {
                let hex = (bytes[index] == b'%')
                    .then(|| bytes.get(index + 1..index + 3))
                    .flatten()
                    .and_then(|pair| std::str::from_utf8(pair).ok())
                    .and_then(|pair| u8::from_str_radix(pair, 16).ok());
                match hex {
                    Some(byte) => {
                        decoded.push(byte);
                        index += 3;
                    }
                    None => {
                        decoded.push(bytes[index]);
                        index += 1;
                    }
                }
            }
            String::from_utf8(decoded).unwrap_or(name)
        })
        .unwrap_or_else(|| "download.bin".into())
}

/// The filename a Content-Disposition header names, if any. RFC 6266:
/// `filename*` (RFC 8187, percent-encoded with a charset) wins over `filename`.
fn disposition_filename(value: &str) -> Option<String> {
    let mut params = Vec::new();
    let (mut current, mut quoted, mut escaped) = (String::new(), false, false);
    for character in value.chars() {
        if escaped {
            current.push(character);
            escaped = false;
        } else if quoted && character == '\\' {
            escaped = true;
        } else if character == '"' {
            quoted = !quoted;
        } else if character == ';' && !quoted {
            params.push(std::mem::take(&mut current));
        } else {
            current.push(character);
        }
    }
    params.push(current);
    let mut plain = None;
    for param in params {
        let Some((key, raw)) = param.split_once('=') else { continue };
        let (key, raw) = (key.trim().to_ascii_lowercase(), raw.trim());
        if key == "filename*" {
            let mut parts = raw.splitn(3, '\'');
            let (Some(charset), Some(_language), Some(encoded)) = (parts.next(), parts.next(), parts.next()) else { continue };
            let mut bytes = Vec::with_capacity(encoded.len());
            let mut input = encoded.bytes();
            while let Some(byte) = input.next() {
                if byte != b'%' {
                    bytes.push(byte);
                    continue;
                }
                let hex = [input.next().unwrap_or(b'_'), input.next().unwrap_or(b'_')];
                bytes.push(std::str::from_utf8(&hex).ok().and_then(|hex| u8::from_str_radix(hex, 16).ok()).unwrap_or(b'_'));
            }
            let decoded = if charset.eq_ignore_ascii_case("utf-8") {
                String::from_utf8_lossy(&bytes).into_owned()
            } else {
                bytes.iter().map(|&byte| byte as char).collect()
            };
            if !decoded.trim().is_empty() {
                return Some(decoded);
            }
        } else if key == "filename" && !raw.is_empty() {
            plain = Some(raw.to_string());
        }
    }
    plain
}

/// Chromium's precedence for a download's name: the server's filename, then
/// the link's download attribute, then the URL. A job created from a hint or
/// the URL takes the server's name at its first file response, once, while it
/// is still provisional; a name the user committed is never replaced.
fn adopt_response_name(app: &AppHandle, state: &CoreState, id: &str, disposition: Option<&str>) {
    let adoptable = state.adoptable_names.lock().map(|mut adoptable| adoptable.remove(id)).unwrap_or(false);
    if !adoptable {
        return;
    }
    let Some(named) = disposition.and_then(disposition_filename).map(|value| safe_filename(&value)) else { return };
    let mut renamed = false;
    emit_job(state, id, |job| {
        if job.provisional != Some(true) || job.name == named {
            return;
        }
        job.name = named.clone();
        let mut destination = PathBuf::from(&job.destination);
        destination.set_file_name(&named);
        job.destination = destination.to_string_lossy().into_owned();
        renamed = true;
    });
    if renamed {
        emit_snapshot(app, state);
    }
}

fn collision_destination(path: &str, behavior: &str) -> String {
    let candidate = PathBuf::from(path);
    if behavior == "replace" || !candidate.exists() { return path.to_string(); }
    let stem = candidate.file_stem().and_then(|value| value.to_str()).unwrap_or("download");
    let extension = candidate.extension().and_then(|value| value.to_str()).map(|value| format!(".{value}")).unwrap_or_default();
    for index in 1..10000 {
        let mut next = candidate.clone();
        next.set_file_name(format!("{stem} ({index}){extension}"));
        if !next.exists() { return next.to_string_lossy().into_owned(); }
    }
    path.to_string()
}

const DESTINATION_RESERVATION_PREFIX: &str = "download-manager-reservation-v1:";

#[derive(Debug, PartialEq, Eq)]
enum DestinationReservationRecovery { Retry, Completed, Missing, Unknown }

fn destination_reservation_marker() -> String { format!("{DESTINATION_RESERVATION_PREFIX}{}", Uuid::new_v4()) }

/// The first `limit` bytes of a file and its full length. Reservation checks
/// only ever need a marker's worth of a destination, which may by then be a
/// finished multi-gigabyte download (F13).
fn file_prefix(path: &Path, limit: usize) -> std::io::Result<(u64, Vec<u8>)> {
    let file = std::fs::File::open(path)?;
    let length = file.metadata()?.len();
    let mut prefix = Vec::with_capacity(limit.min(length as usize));
    std::io::Read::read_to_end(&mut std::io::Read::take(file, limit as u64), &mut prefix)?;
    Ok((length, prefix))
}

/// Whether `path` holds exactly `marker`: only a file of the marker's length
/// is read at all.
fn holds_reservation_marker(path: &Path, marker: &str) -> bool {
    file_prefix(path, marker.len())
        .is_ok_and(|(length, prefix)| length == marker.len() as u64 && prefix == marker.as_bytes())
}

async fn holds_reservation_marker_async(path: &str, marker: &str) -> bool {
    let (path, marker) = (PathBuf::from(path), marker.to_string());
    tauri::async_runtime::spawn_blocking(move || holds_reservation_marker(&path, &marker))
        .await
        .unwrap_or(false)
}

fn reconcile_destination_reservation(
    path: &Path,
    marker: &str,
    allow_empty_completed: bool
) -> DestinationReservationRecovery {
    // At most a marker's worth is read; a length equal to the prefix's
    // means the prefix is the whole file.
    match file_prefix(path, marker.len()) {
        Ok((length, bytes))
            if length == bytes.len() as u64
                && (bytes == marker.as_bytes()
                    || (bytes.len() < marker.len()
                        && marker.as_bytes().starts_with(&bytes)
                        && bytes.starts_with(DESTINATION_RESERVATION_PREFIX.as_bytes()))) =>
        {
            match std::fs::remove_file(path) {
                Ok(()) => DestinationReservationRecovery::Retry,
                Err(_) => DestinationReservationRecovery::Unknown
            }
        }
        Ok((_, bytes)) if bytes.starts_with(DESTINATION_RESERVATION_PREFIX.as_bytes()) => {
            DestinationReservationRecovery::Unknown
        }
        Ok((length, _)) if length == 0 && !allow_empty_completed => match std::fs::remove_file(path)
        {
            Ok(()) => DestinationReservationRecovery::Retry,
            Err(_) => DestinationReservationRecovery::Unknown
        },
        Ok(_) => DestinationReservationRecovery::Completed,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            DestinationReservationRecovery::Missing
        }
        Err(_) => DestinationReservationRecovery::Unknown
    }
}

fn reserve_collision_destination_with_marker(path: &str) -> Result<(String, String), String> {
    let candidate = PathBuf::from(path);
    let stem = candidate.file_stem().and_then(|value| value.to_str()).unwrap_or("download");
    let extension = candidate.extension().and_then(|value| value.to_str()).map(|value| format!(".{value}")).unwrap_or_default();
    let marker = destination_reservation_marker();
    for index in 0..10000 {
        let mut next = candidate.clone();
        if index > 0 { next.set_file_name(format!("{stem} ({index}){extension}")); }
        match std::fs::OpenOptions::new().write(true).create_new(true).open(&next) {
            Ok(mut file) => {
                if let Err(error) = file.write_all(marker.as_bytes()).and_then(|_| file.sync_all()) {
                    let _ = std::fs::remove_file(&next);
                    return Err(error.to_string());
                }
                return Ok((next.to_string_lossy().into_owned(), marker));
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.to_string()),
        }
    }
    Err("Could not reserve a unique destination".into())
}

fn managed_destination(path: &str, replace_existing: bool) -> Result<(String, bool, Option<String>), String> {
    if replace_existing { Ok((path.to_string(), false, None)) } else { reserve_collision_destination_with_marker(path).map(|(destination, marker)| (destination, true, Some(marker))) }
}

fn header_string(response: &reqwest::Response, name: reqwest::header::HeaderName) -> Option<String> {
    response.headers().get(name).and_then(|value| value.to_str().ok()).map(str::to_string)
}

fn merge_range(ranges: &[ByteRange], next: ByteRange) -> Vec<ByteRange> {
    let mut all = ranges.to_vec();
    all.push(next);
    all.sort_by_key(|range| range.start);
    let mut merged: Vec<ByteRange> = Vec::new();
    for range in all {
        if let Some(previous) = merged.last_mut() {
            if range.start <= previous.end.saturating_add(1) { previous.end = previous.end.max(range.end); continue; }
        }
        merged.push(range);
    }
    merged
}

fn covered_bytes(ranges: &[ByteRange]) -> u64 {
    ranges.iter().map(|range| range.end.saturating_sub(range.start).saturating_add(1)).sum()
}

fn identity_from_response(response: &reqwest::Response, length: u64) -> ResourceIdentity {
    ResourceIdentity { length, etag: header_string(response, reqwest::header::ETAG), last_modified: header_string(response, reqwest::header::LAST_MODIFIED) }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ResumeEvidence {
    /// Validators both sides expose agree, so the stored bytes belong to this
    /// resource.
    Trusted,
    /// No validator can vouch for the resource. The stored bytes have to prove
    /// themselves against what the source sends now.
    Sampled,
    /// The evidence contradicts the stored state: start over.
    Rejected,
}

/// SPEC §8.8: never merge partial data because a filename or URL matches. The
/// final length and every validator both sides expose must agree; what remains
/// is either strong evidence (agreed validators) or sampled evidence (the bytes
/// already on disk, checked against a fresh response).
fn resume_evidence(
    existing: Option<&ResourceIdentity>,
    current: &ResourceIdentity,
) -> ResumeEvidence {
    let Some(existing) = existing else {
        return ResumeEvidence::Rejected;
    };
    if existing.length != current.length {
        return ResumeEvidence::Rejected;
    }
    let mut validator_seen = false;
    for (stored, fresh) in [
        (existing.etag.as_ref(), current.etag.as_ref()),
        (existing.last_modified.as_ref(), current.last_modified.as_ref()),
    ] {
        match (stored, fresh) {
            (Some(stored), Some(fresh)) => {
                if stored != fresh {
                    return ResumeEvidence::Rejected;
                }
                validator_seen = true;
            }
            // A validator only one side exposes cannot confirm anything, and a
            // validator that disappeared must not be read as agreement.
            (Some(_), None) | (None, Some(_)) | (None, None) => {}
        }
    }
    if validator_seen {
        ResumeEvidence::Trusted
    } else {
        ResumeEvidence::Sampled
    }
}

/// Sampled evidence: the leading bytes a fresh response just delivered must be
/// byte-identical to what the partial file already holds. Anything else means
/// the source is not the resource that was paused.
async fn temp_prefix_matches(temp_path: &str, fresh: &[u8]) -> bool {
    let Ok(mut file) = File::open(temp_path).await else {
        return false;
    };
    let mut existing = vec![0u8; fresh.len()];
    file.read_exact(&mut existing).await.is_ok() && existing == fresh
}

fn valid_range_identity(response: &reqwest::Response, expected: &ResourceIdentity) -> bool {
    let etag = header_string(response, reqwest::header::ETAG);
    let last_modified = header_string(response, reqwest::header::LAST_MODIFIED);
    expected.etag.as_ref().map_or(true, |value| etag.as_ref() == Some(value)) && expected.last_modified.as_ref().map_or(true, |value| last_modified.as_ref() == Some(value))
}

/// States a transfer runs in.
const TRANSFER_STATES: [&str; 3] = ["connecting", "downloading", "finalizing"];
/// Finalizing is local assembly of bytes already on disk, so it is not
/// pausable: recovery then always finishes it from disk instead of asking a
/// source that may have expired.
const PAUSABLE_STATES: [&str; 2] = ["connecting", "downloading"];

fn emit_job(state: &CoreState, id: &str, update: impl FnOnce(&mut DownloadJob)) {
    // The job log is a bounded human-readable history (newest first), never a
    // debug dump: every mutation path funnels through here (F15).
    if let Ok(mut snapshot) = state.snapshot.lock() {
        if let Some(job) = snapshot.jobs.iter_mut().find(|job| job.id == id) {
            update(job);
            if job.state != "downloading" {
                job.speed = 0;
            }
            job.events.truncate(50);
            snapshot.aggregate_speed = snapshot.jobs.iter().filter(|item| item.state == "downloading").map(|item| item.speed).sum();
        }
    }
}
fn missing_ranges(total: u64, completed: &[ByteRange], target_workers: u32) -> Vec<(u64, u64)> {
    if total == 0 { return Vec::new(); }
    let chunk = (total / u64::from(target_workers.clamp(1, 32).saturating_mul(4))).max(1024 * 1024).min(16 * 1024 * 1024);
    let mut gaps = Vec::new();
    let mut cursor = 0u64;
    for range in completed {
        if range.start > cursor { gaps.push((cursor, range.start - 1)); }
        cursor = cursor.max(range.end.saturating_add(1));
    }
    if cursor < total { gaps.push((cursor, total - 1)); }
    let mut chunks = Vec::new();
    for (start, end) in gaps {
        let mut cursor = start;
        while cursor <= end {
            let next = cursor.saturating_add(chunk).saturating_sub(1).min(end);
            chunks.push((cursor, next));
            if next == u64::MAX { break; }
            cursor = next + 1;
        }
    }

    chunks
}


/// The in-app notification center is durable, unlike the OS toast: persist
/// the bounded center so restarts keep it (F16). Failures only log — a
/// missing center table must never break the toast itself.
fn persist_notifications(state: &CoreState) {
    let items = state
        .snapshot
        .lock()
        .ok()
        .map(|snapshot| snapshot.notifications.clone())
        .unwrap_or_default();
    let mut database = match state.database.lock() {
        Ok(database) => database,
        Err(_) => return,
    };
    if database
        .execute_batch("CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, payload TEXT)")
        .is_err()
    {
        return;
    }
    let result = (|| {
        let transaction = database.transaction().map_err(|error| error.to_string())?;
        transaction.execute("DELETE FROM notifications", []).map_err(|error| error.to_string())?;
        for item in &items {
            let payload = serde_json::to_string(item).map_err(|error| error.to_string())?;
            transaction
                .execute("INSERT INTO notifications (id, payload) VALUES (?1, ?2)", params![item.id, payload])
                .map_err(|error| error.to_string())?;
        }
        transaction.commit().map_err(|error| error.to_string())
    })();
    if let Err(error) = result {
        eprintln!("Notification center unavailable: {error}");
    }
}

fn load_notifications(database: &Connection) -> Vec<NotificationItem> {
    // The table is rewritten wholesale on every change, newest item first, so
    // insertion order is the order to read back (DESC returned the oldest first).
    let mut items = Vec::new();
    if let Ok(mut statement) = database.prepare("SELECT payload FROM notifications ORDER BY rowid ASC LIMIT 40") {
        if let Ok(rows) = statement.query_map([], |row| row.get::<_, String>(0)) {
            for payload in rows.flatten() {
                if let Ok(item) = serde_json::from_str::<NotificationItem>(&payload) {
                    items.push(item);
                }
            }
        }
    }
    items
}

fn add_notification(app: &AppHandle, state: &CoreState, id: &str, kind: &str) {
    let (item, enabled, destination) = match state.snapshot.lock() {
        Ok(mut snapshot) => {
            let Some(job) = snapshot.jobs.iter().find(|job| job.id == id).cloned() else { return; };
            // The center describes managed downloads. A provisional the user has
            // not accepted is announced by its Add Download window instead, and
            // cancelling or removing a job drops its notifications with it, so
            // the center never describes something the list does not contain.
            if job.provisional == Some(true) { return; }
            let enabled = if kind == "completed" { snapshot.settings.completion_notifications } else { snapshot.settings.failure_notifications };
            let destination = job.destination.clone();
            let item = NotificationItem { id: format!("{kind}-{id}"), notification_type: kind.into(), title: if kind == "completed" { "Download completed".into() } else { "Download failed".into() }, detail: if kind == "completed" { format!("{} · {}", job.name, format_bytes(job.total)) } else { format!("{} · {}", job.name, redact_url_credentials(&job.error.unwrap_or_else(|| "The source could not be acquired".into()))) }, time: now_label(), job_id: id.into() };
            if enabled && !snapshot.notifications.iter().any(|current| current.id == item.id) { snapshot.notifications.insert(0, item.clone()); snapshot.notifications.truncate(40); }
            (item, enabled, destination)
        }
        Err(_) => return,
    };
    persist_notifications(state);
    emit_snapshot(app, state);
    if enabled { notify::show_job_notification(app, &item.title, &item.detail, kind, &item.job_id, &destination); }
}

fn format_bytes(value: Option<u64>) -> String {
    let Some(value) = value else { return "Unknown size".into(); };
    if value >= 1024 * 1024 * 1024 { return format!("{:.2} GB", value as f64 / (1024.0 * 1024.0 * 1024.0)); }
    if value >= 1024 * 1024 { return format!("{:.1} MB", value as f64 / (1024.0 * 1024.0)); }
    if value >= 1024 { return format!("{} KB", value / 1024); }
    format!("{value} B")
}

fn http_client() -> reqwest::Client {
    reqwest::Client::builder().connect_timeout(Duration::from_secs(15)).read_timeout(Duration::from_secs(30)).user_agent("Download Manager/0.1").build().unwrap_or_else(|_| reqwest::Client::new())
}

// SPEC §5.1 request-context replay under §16 scoping: the capture page is
// replayed as Referer with a strict-origin-when-cross-origin rule. Same
// origin as the request target gets the full page URL (hotlink checks);
// any other origin gets only the page origin, so paths and query strings
// never leak cross-origin. Non-HTTP(S) or unparsable values yield nothing.
fn referer_value(referrer: &str, url: &str) -> Option<String> {
    let page = reqwest::Url::parse(referrer.trim()).ok()?;
    if !matches!(page.scheme(), "http" | "https") { return None; }
    let target = reqwest::Url::parse(url).ok()?;
    if page.origin() == target.origin() { return Some(page.to_string()); }
    Some(page.origin().ascii_serialization())
}

fn user_agent_value(value: &str) -> Option<&str> {
    let value = value.trim();
    (!value.is_empty() && value.len() <= 512 && !value.contains(['\r', '\n'])).then_some(value)
}

fn player_kind_value(value: &str) -> Option<String> {
    match value.trim().to_ascii_lowercase().as_str() {
        "video" => Some("video".into()),
        "audio" => Some("audio".into()),
        _ => None,
    }
}

fn validated_http_url(value: &str) -> Option<String> {
    let value = value.trim();
    let parsed = reqwest::Url::parse(value).ok()?;
    matches!(parsed.scheme(), "http" | "https").then(|| parsed.to_string())
}

fn job_context(app: &AppHandle, id: &str) -> (Option<String>, Option<String>, Option<String>) {
    app.state::<CoreState>()
        .snapshot
        .lock()
        .ok()
        .and_then(|snapshot| {
            snapshot.jobs.iter().find(|item| item.id == id).map(|job| {
                (
                    job.referrer.clone(),
                    job.post_body.clone(),
                    job.user_agent.clone()
                )
            })
        })
        .unwrap_or((None, None, None))
}

fn acquisition_request(client: &reqwest::Client, app: &AppHandle, id: &str, url: &str) -> reqwest::RequestBuilder {
    let (referrer, _, user_agent) = job_context(app, id);
    let request = match user_agent.as_deref().and_then(user_agent_value) {
        Some(value) => client.get(url).header(reqwest::header::USER_AGENT, value),
        None => client.get(url),
    };
    match referrer.as_deref().and_then(|value| referer_value(value, url)) {
        Some(value) => request.header(reqwest::header::REFERER, value),
        None => request,
    }
}

// POST replay for form-originated captures (SPEC §5.1 method/body). The
// extension only records bodies it observed on a browser POST, so a present
// body is replayed first in acquire_once; the 405 path below stays as the
// fallback for captures without one. Same Referer scoping as GET requests.
fn post_replay_request(client: &reqwest::Client, app: &AppHandle, id: &str, url: &str) -> Option<reqwest::RequestBuilder> {
    let (referrer, post_body, user_agent) = job_context(app, id);
    let body = post_body?;
    let request = client.post(url).body(body).header(reqwest::header::CONTENT_TYPE, "application/x-www-form-urlencoded");
    let request = match user_agent.as_deref().and_then(user_agent_value) {
        Some(value) => request.header(reqwest::header::USER_AGENT, value),
        None => request,
    };
    Some(match referrer.as_deref().and_then(|value| referer_value(value, url)) {
        Some(value) => request.header(reqwest::header::REFERER, value),
        None => request,
    })
}

fn retryable_status(status: reqwest::StatusCode) -> bool {
    status == reqwest::StatusCode::REQUEST_TIMEOUT || status == reqwest::StatusCode::TOO_MANY_REQUESTS || status.is_server_error()
}

/// 404/410 mean the URL itself is dead: retrying, degrading, or refetching
/// cannot revive it. Failing fast also stops the engine grinding through the
/// parallel → one-connection → single-stream sequence against a URL whose
/// single redemption the capability probe already consumed (F05).
fn terminal_source_status(status: reqwest::StatusCode) -> bool {
    status == reqwest::StatusCode::NOT_FOUND || status == reqwest::StatusCode::GONE
}

fn terminal_source_error(status: reqwest::StatusCode) -> String {
    format!("source is gone (HTTP {status}); the URL may have expired or allow a single use")
}

fn is_terminal_source_error(error: &str) -> bool {
    error.starts_with("source is gone (HTTP ")
}

/// A 200 HTML page without a file disposition is never the named download:
/// form results, login walls, and soft-error pages must fail honestly instead
/// of completing as the target file (F06). Filenames that are themselves HTML
/// stay downloadable.
fn page_instead_of_file(mime: Option<&str>, disposition: Option<&str>, filename: &str) -> bool {
    let is_html = mime
        .map(|value| value.split(';').next().unwrap_or(value).trim())
        .is_some_and(|kind| kind.eq_ignore_ascii_case("text/html"));
    if !is_html {
        return false;
    }
    let disposition_names_file = disposition
        .map(|value| value.to_ascii_lowercase().contains("filename"))
        .unwrap_or(false);
    if disposition_names_file {
        return false;
    }
    let lower = filename.to_ascii_lowercase();
    !(lower.ends_with(".html") || lower.ends_with(".htm"))
}

/// The header check plus the body sniff: a response is a page when either its
/// headers or its first bytes say HTML and nothing names it as an HTML file.
/// Every path that turns a response into the job's file asks this one question.
fn response_is_page(mime: Option<&str>, disposition: Option<&str>, filename: &str, prefix: &[u8]) -> bool {
    if page_instead_of_file(mime, disposition, filename) {
        return true;
    }
    let names_file = disposition
        .map(|value| value.to_ascii_lowercase().contains("filename"))
        .unwrap_or(false);
    let lower = filename.to_ascii_lowercase();
    body_looks_like_html(prefix) && !names_file && !(lower.ends_with(".html") || lower.ends_with(".htm"))
}

fn manifest_mime(mime: Option<&str>) -> bool {
    mime.map(|value| {
        let value = value.to_ascii_lowercase();
        value.contains("mpegurl") || value.contains("dash+xml")
    }).unwrap_or(false)
}

fn media_mime_kind(mime: Option<&str>) -> Option<&'static str> {
    let value = mime?.split(';').next()?.trim().to_ascii_lowercase();
    if value.starts_with("audio/") && !value.contains("mpegurl") {
        Some("audio")
    } else if value.starts_with("video/") && !value.contains("mpegurl") {
        Some("video")
    } else {
        None
    }
}

fn mime_conflicts_player_kind(expected: Option<&str>, mime: Option<&str>) -> bool {
    matches!((expected, media_mime_kind(mime)), (Some("video"), Some("audio")) | (Some("audio"), Some("video")))
}

fn partial_response_is_complete(response: &reqwest::Response) -> Result<Option<u64>, String> {
    if response.status() != reqwest::StatusCode::PARTIAL_CONTENT {
        return Ok(response.content_length());
    }
    let Some((start, end, total)) = content_range(response) else {
        return Err("The source returned a partial response without a valid Content-Range".into());
    };
    let Some(length) = response.content_length() else {
        return Err("The source returned a partial response without a content length".into());
    };
    let complete = start == 0
        && end >= start
        && end.checked_add(1) == Some(total)
        && length == total;
    if complete {
        Ok(Some(total))
    } else {
        Err("The source returned a partial response instead of the complete object".into())
    }
}

fn supports_safe_initial_ranges(response: &reqwest::Response, total: Option<u64>, mime: Option<&str>) -> bool {
    let Some(total) = total else { return false; };
    if total <= 1 || response.status() != reqwest::StatusCode::OK || manifest_mime(mime) {
        return false;
    }
    let Some(accept_ranges) = response.headers().get(reqwest::header::ACCEPT_RANGES).and_then(|value| value.to_str().ok()) else {
        return false;
    };
    if !accept_ranges.split(',').any(|value| value.trim().eq_ignore_ascii_case("bytes")) {
        return false;
    }
    true
}

const BODY_SNIFF_LIMIT: usize = 8192;

fn looks_like_mpeg_audio_header(header: &[u8]) -> bool {
    if header.len() < 4 || header[0] != 0xff || header[1] & 0xe0 != 0xe0 {
        return false;
    }
    let version = (header[1] >> 3) & 0x03;
    let layer = (header[1] >> 1) & 0x03;
    let bitrate = (header[2] >> 4) & 0x0f;
    let sample_rate = (header[2] >> 2) & 0x03;
    version != 1 && layer != 0 && bitrate != 0 && bitrate != 0x0f && sample_rate != 0x03
}

fn body_looks_like_audio(body: &[u8]) -> bool {
    if body.starts_with(b"ID3") || body.starts_with(b"fLaC") {
        return true;
    }
    if body.len() >= 12 && body.starts_with(b"RIFF") && &body[8..12] == b"WAVE" {
        return true;
    }
    if body.starts_with(b"OggS")
        && (body.windows(8).any(|window| window == b"OpusHead")
            || body.windows(6).any(|window| window == b"vorbis"))
    {
        return true;
    }
    if !looks_like_mpeg_audio_header(body) || (body[1] >> 1) & 3 != 1 {
        return false;
    }
    let version = (body[1] >> 3) & 3;
    let bitrates = if version == 3 {
        [0usize, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
    } else {
        [0usize, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
    };
    let sample_rate = [44100usize, 48000, 32000][((body[2] >> 2) & 3) as usize]
        / if version == 3 { 1 } else if version == 2 { 2 } else { 4 };
    let frame_length = if version == 3 { 144 } else { 72 }
        * bitrates[(body[2] >> 4) as usize] * 1000 / sample_rate
        + ((body[2] >> 1) & 1) as usize;
    body.get(frame_length..).is_some_and(|next| {
        looks_like_mpeg_audio_header(next)
            && next[1] & 0xfe == body[1] & 0xfe
            && next[2] & 0x0c == body[2] & 0x0c
    })
}

fn body_looks_like_html(body: &[u8]) -> bool {
    let text = String::from_utf8_lossy(body);
    let text = text.trim_start_matches('\u{feff}').trim_start().to_ascii_lowercase();
    text.starts_with("<!doctype html") || text.starts_with("<html") || text.starts_with("<head")
}

fn mark_acquisition_failed(app: &AppHandle, state: &CoreState, id: &str, error: String, event: &str) {
    emit_job(state, id, |job| {
        job.state = "failed".into();
        job.error = Some(redact_url_credentials(&error));
        job.connections = 0;
        job.speed = 0;
        job.eta = None;
        job.events.insert(0, job_event(event, Some("error")));
    });
    emit_snapshot(app, state);
    add_notification(app, state, id, "failed");
}

async fn finalize_media(temp_path: &str, destination: &str) -> Result<(), String> {
    let extension = PathBuf::from(destination)
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase);
    if matches!(extension.as_deref(), None | Some("ts") | Some("m4s")) {
        return Ok(());
    }
    // The container kind lives in the leading bytes; the rest of the assembled
    // media is validated where it lies, so finalizing a multi-gigabyte download
    // never loads it into memory.
    let mut header = [0u8; 4];
    let mut file = File::open(temp_path).await.map_err(|error| error.to_string())?;
    if file.read_exact(&mut header).await.is_err() {
        return Err("Media finalization failed: the assembled media is empty; downloaded parts were preserved".into());
    }
    drop(file);
    if looks_like_webm(&header) {
        return Ok(());
    }
    media::validate_fmp4_file(Path::new(temp_path)).map_err(|error| {
        format!("Media finalization failed: {error}; downloaded parts were preserved")
    })?;
    Ok(())
}

fn move_needs_fallback(error: &std::io::Error) -> bool {
    error.kind() == std::io::ErrorKind::AlreadyExists || matches!(error.raw_os_error(), Some(17) | Some(18) | Some(183))
}

async fn remove_owned_reservation(destination: &str, marker: Option<&str>) {
    let Some(marker) = marker else { return; };
    if holds_reservation_marker_async(destination, marker).await {
        let _ = tokio::fs::remove_file(destination).await;
    }
}

async fn cleanup_reserved_destination(destination: &str, marker: Option<&str>) {
    remove_owned_reservation(destination, marker).await;
}

async fn restore_replacement_backup(backup: &str, destination: &str) -> Result<(), String> {
    tokio::fs::rename(backup, destination).await.map_err(|error| error.to_string())
}

async fn remove_completed_source(source: &str) -> Result<(), String> {
    tokio::fs::remove_file(source).await.map_err(|error| error.to_string())
}

enum ReservationRestore {
    Restored,
    DestinationChanged,
    Failed(String),
}

async fn restore_reservation_marker(destination: &str, marker: &str) -> ReservationRestore {
    let mut file = match OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(destination)
        .await
    {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            return ReservationRestore::DestinationChanged
        }
        Err(error) => return ReservationRestore::Failed(error.to_string())
    };
    if let Err(error) = file.write_all(marker.as_bytes()).await {
        return ReservationRestore::Failed(error.to_string());
    }
    if let Err(error) = file.sync_all().await {
        return ReservationRestore::Failed(error.to_string());
    }
    ReservationRestore::Restored
}

fn reservation_restore_message(result: &ReservationRestore) -> String {
    match result {
        ReservationRestore::Restored => "reservation marker restored".into(),
        ReservationRestore::DestinationChanged => {
            "destination changed; reservation marker not restored".into()
        }
        ReservationRestore::Failed(error) => {
            format!("could not restore reservation marker: {error}")
        }
    }
}

async fn install_reserved_staging(staging: &str, destination: &str, marker: &str) -> Result<(), String> {
    let owns_reservation = holds_reservation_marker_async(destination, marker).await;
    if !owns_reservation {
        let _ = tokio::fs::remove_file(staging).await;
        return Err("fallback destination reservation changed".into());
    }
    if let Err(remove_error) = tokio::fs::remove_file(destination).await {
        let restoration = if remove_error.kind() == std::io::ErrorKind::NotFound {
            Some(restore_reservation_marker(destination, marker).await)
        } else {
            None
        };
        let _ = tokio::fs::remove_file(staging).await;
        return Err(match restoration {
            Some(result) => format!("reservation removal failed: {remove_error}; {}", reservation_restore_message(&result)),
            None => format!("reservation removal failed: {remove_error}"),
        });
    }

    match tokio::fs::rename(staging, destination).await {
        Ok(()) => Ok(()),
        Err(install_error) => {
            let restoration = restore_reservation_marker(destination, marker).await;
            let _ = tokio::fs::remove_file(staging).await;
            Err(format!("final staging rename failed: {install_error}; {}", reservation_restore_message(&restoration)))
        }
    }
}

async fn move_completed_file(source: &str, destination: &str, replace_existing: bool, reservation_marker: Option<&str>) -> Result<(), String> {
    let reserved = reservation_marker.is_some();
    if let Some(parent) = PathBuf::from(destination).parent() { tokio::fs::create_dir_all(parent).await.map_err(|error| error.to_string())?; }
    match tokio::fs::rename(source, destination).await {
        Ok(()) => Ok(()),
        Err(error) if move_needs_fallback(&error) && reserved => {
            let staging = format!("{destination}.download-manager-staging-{}", uuid::Uuid::new_v4());
            if let Err(copy_error) = tokio::fs::copy(source, &staging).await {
                let _ = tokio::fs::remove_file(&staging).await;
                remove_owned_reservation(destination, reservation_marker).await;
                return Err(format!("{error}; fallback copy failed: {copy_error}"));
            }
            match tokio::fs::rename(&staging, destination).await {
                Ok(()) => {}
                Err(rename_error) if move_needs_fallback(&rename_error) => {
                    if let Err(install_error) = install_reserved_staging(&staging, destination, reservation_marker.expect("reserved fallback has marker")).await {
                        return Err(format!("{error}; {install_error}"));
                    }
                }
                Err(rename_error) => {
                    let _ = tokio::fs::remove_file(&staging).await;
                    remove_owned_reservation(destination, reservation_marker).await;
                    return Err(format!("{error}; fallback move failed: {rename_error}"));
                }
            }
            match remove_completed_source(source).await {
                Ok(()) => Ok(()),
                Err(remove_error) => Err(remove_error),
            }
        }
        Err(error) if move_needs_fallback(&error) && replace_existing => {
            let staging = format!("{destination}.download-manager-staging-{}", uuid::Uuid::new_v4());
            if let Err(copy_error) = tokio::fs::copy(source, &staging).await {
                let _ = tokio::fs::remove_file(&staging).await;
                return Err(format!("{error}; fallback copy failed: {copy_error}"));
            }
            let backup = format!("{destination}.download-manager-backup-{}", uuid::Uuid::new_v4());
            let had_existing = match tokio::fs::rename(destination, &backup).await {
                Ok(()) => true,
                Err(rename_error) if rename_error.kind() == std::io::ErrorKind::NotFound => false,
                Err(rename_error) => {
                    let _ = tokio::fs::remove_file(&staging).await;
                    return Err(format!("{error}; could not stage existing destination: {rename_error}"));
                }
            };
            match tokio::fs::rename(&staging, destination).await {
                Ok(()) => {
                    if had_existing { let _ = tokio::fs::remove_file(&backup).await; }
                    match remove_completed_source(source).await {
                        Ok(()) => Ok(()),
                        Err(remove_error) => Err(remove_error),
                    }
                }
                Err(rename_error) => {
                    let _ = tokio::fs::remove_file(&staging).await;
                    let restoration_error = if had_existing {
                        match restore_replacement_backup(&backup, destination).await {
                            Ok(()) => None,
                            Err(error) => Some(format!("; original destination restoration failed: {error}; backup preserved at {backup}")),
                        }
                    } else {
                        None
                    };
                    Err(format!("{error}; fallback move failed: {rename_error}{}", restoration_error.unwrap_or_default()))
                }
            }
        }
        Err(error) => { if reserved { remove_owned_reservation(destination, reservation_marker).await; } Err(error.to_string()) },
    }
}

fn media_extension(destination: &str) -> String {
    PathBuf::from(destination).extension().and_then(|value| value.to_str()).map(str::to_ascii_lowercase).unwrap_or_else(|| "mkv".into())
}

fn looks_like_webm(input: &[u8]) -> bool {
    input.len() >= 4 && &input[..4] == [0x1a, 0x45, 0xdf, 0xa3]
}

fn destination_with_output_extension(destination: &str, output_path: &str) -> String {
    let Some(extension) = Path::new(output_path)
        .extension()
        .and_then(|value| value.to_str())
    else {
        return destination.to_string();
    };
    let mut path = PathBuf::from(destination);
    path.set_extension(extension);
    path.to_string_lossy().into_owned()
}

async fn mux_media_tracks(track_paths: &[String], output_path: &str) -> Result<String, String> {
    let inputs: Vec<PathBuf> = track_paths.iter().map(PathBuf::from).collect();
    let output = PathBuf::from(output_path);
    // The mux reads and writes files of any size (F13): it runs on the
    // blocking pool so a long finalization never stalls other downloads.
    tauri::async_runtime::spawn_blocking(move || media::mux_track_files(&inputs, &output))
        .await
        .map_err(|error| {
            format!("Media track finalization stopped: {error}; downloaded parts were preserved")
        })?
        .map(|path| path.to_string_lossy().into_owned())
}

fn job_state(app: &AppHandle, id: &str) -> Option<String> {
    let state = app.state::<CoreState>();
    state.snapshot.lock().ok().and_then(|snapshot| {
        snapshot
            .jobs
            .iter()
            .find(|job| job.id == id)
            .map(|job| job.state.clone())
    })
}

fn transfer_is_current(app: &AppHandle, id: &str, generation: u64) -> bool {
    app.state::<CoreState>().transfer_controls.is_current(id, generation)
}

/// Moving the finished file into place is the point of no return: once the
/// rename is issued it completes even if its task is aborted, so a pause or
/// cancel that lands during it would leave a "paused" job whose file is
/// already published (and a resume would download it again). The move is
/// claimed only while the job is still wanted, and pause/cancel are declined
/// while a claim is held (F08). Lock order: publishing, then snapshot.
struct Publishing<'a> {
    state: &'a CoreState,
    id: String,
}

impl Drop for Publishing<'_> {
    fn drop(&mut self) {
        if let Ok(mut publishing) = self.state.publishing.lock() {
            publishing.remove(&self.id);
        }
    }
}

fn begin_publishing<'a>(state: &'a CoreState, id: &str, still_wanted: impl FnOnce() -> bool) -> Option<Publishing<'a>> {
    let mut publishing = state.publishing.lock().ok()?;
    if !still_wanted() {
        return None;
    }
    publishing.insert(id.to_string());
    Some(Publishing { state, id: id.to_string() })
}

/// Records why a pause or cancel did not apply; `publishing` is the held claim set.
fn decline_while_publishing(state: &CoreState, publishing: &std::collections::HashSet<String>, id: &str, action: &str) -> bool {
    if !publishing.contains(id) {
        return false;
    }
    emit_job(state, id, |job| {
        job.events.insert(0, job_event(&format!("{action} did not apply: the file was already being saved"), Some("warning")));
    });
    true
}

fn transfer_can_continue(app: &AppHandle, id: &str, generation: u64) -> bool {
    transfer_is_current(app, id, generation) && state_allows_transfer(job_state(app, id).as_deref())
}

fn transfer_is_downloading(app: &AppHandle, id: &str, generation: u64) -> bool {
    transfer_is_current(app, id, generation) && job_state(app, id).as_deref() == Some("downloading")
}

fn transfer_is_active(state: &CoreState, id: &str) -> bool {
    state.transfer_controls.is_active(id)
}

fn abort_transfer(state: &CoreState, id: &str) {
    state.transfer_controls.abort(id);
}

fn report_viability(app: &AppHandle, id: &str, result: Result<(), String>) {
    let sender = app
        .state::<CoreState>()
        .viability
        .lock()
        .ok()
        .and_then(|mut pending| pending.remove(id));
    if let Some(sender) = sender {
        let _ = sender.send(result);
    }
}

/// No-op once viability was reported; otherwise the acquisition ended without
/// reaching its file, and whoever is waiting learns why.
fn report_viability_failure(app: &AppHandle, id: &str) {
    let error = app
        .state::<CoreState>()
        .snapshot
        .lock()
        .ok()
        .and_then(|snapshot| snapshot.jobs.iter().find(|job| job.id == id).and_then(|job| job.error.clone()))
        .unwrap_or_else(|| "The acquisition stopped before it received the file".into());
    report_viability(app, id, Err(error));
}

fn spawn_transfer(app: &AppHandle, state: &CoreState, id: String, source: String) -> bool {
    let Some((generation, registration)) = state.transfer_controls.claim_with_generation(&id) else { return false; };
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        let transfer = Abortable::new(acquire(handle.clone(), id.clone(), source, generation), registration);
        // A panic inside the engine (a malformed manifest, an unexpected
        // body) must end as a visible failure, and must never skip the
        // release below: a stranded owner makes Retry and Resume no-ops.
        let panicked = std::panic::AssertUnwindSafe(transfer).catch_unwind().await.is_err();
        let state = handle.state::<CoreState>();
        state.transfer_controls.release_if_current(&id, generation);
        if let Ok(mut buckets) = state.job_bandwidth.lock() { buckets.remove(&id); };
        if panicked && state_allows_transfer(job_state(&handle, &id).as_deref()) {
            mark_acquisition_failed(
                &handle,
                &state,
                &id,
                "An internal error stopped this download; the source may be malformed".into(),
                "Acquisition stopped by an internal error",
            );
        }
        report_viability_failure(&handle, &id);
    });
    true
}

// SPEC §8.6: a per-job cap constrains the job inside the global limit — the
// binding rate is the minimum of the two. Non-positive values are ignored so
// a zero can never wedge a transfer.
/// Add the tokens accrued since the last refill, holding at most a second's
/// worth (and never less than 64 KiB, so one network chunk always fits).
fn refill(bucket: &mut BandwidthBucket, rate: f64) {
    let now = std::time::Instant::now();
    bucket.tokens = (bucket.tokens + now.duration_since(bucket.updated).as_secs_f64() * rate).min(rate.max(64.0 * 1024.0));
    bucket.updated = now;
}

async fn throttle(app: &AppHandle, id: &str, bytes: usize, generation: u64) -> bool {
    // Token-bucket pacing with ~100ms responsiveness: take what is available,
    // sleep only until more accrues, and re-check job state every slice so
    // Pause/Cancel take effect promptly even mid-chunk. Returns false when
    // the job left "downloading" (caller must stop).
    //
    // The global limit is one bucket every job draws from, refilled at the
    // global rate. A job's own cap is a second bucket shared by that job's
    // workers, refilled at the job's rate. Bytes are taken from both, so each
    // limit holds on its own.
    let (global, job) = app.state::<CoreState>().snapshot.lock().ok().map(|snapshot| {
        let global = snapshot.settings.bandwidth_limit.map(|value| {
            let multiplier = match snapshot.settings.bandwidth_unit.as_str() { "GB/s" => 1024f64 * 1024f64 * 1024f64, "MB/s" => 1024f64 * 1024f64, _ => 1024f64 };
            value as f64 * multiplier
        });
        let job = snapshot.jobs.iter().find(|item| item.id == id).and_then(|item| item.bandwidth_limit.map(|value| value as f64));
        (global.filter(|rate| *rate > 0.0), job.filter(|rate| *rate > 0.0))
    }).unwrap_or((None, None));
    if job.is_none() {
        // The cap was cleared: drop its bucket.
        if let Ok(mut buckets) = app.state::<CoreState>().job_bandwidth.lock() { buckets.remove(id); }
        if global.is_none() {
            return transfer_can_continue(app, id, generation);
        }
    }
    let mut remaining = bytes as f64;
    while remaining > 0.0 {
        if !transfer_can_continue(app, id, generation) { return false; }
        let wait = {
            let state = app.state::<CoreState>();
            let Ok(mut shared) = state.bandwidth.lock() else { return false; };
            let Ok(mut buckets) = state.job_bandwidth.lock() else { return false; };
            let mut limits: Vec<(&mut BandwidthBucket, f64)> = Vec::with_capacity(2);
            if let Some(rate) = global {
                limits.push((&mut *shared, rate));
            }
            if let Some(rate) = job {
                let own = buckets.entry(id.to_string()).or_insert(BandwidthBucket { tokens: 0.0, updated: std::time::Instant::now() });
                limits.push((own, rate));
            }
            let mut take = remaining;
            for (bucket, rate) in limits.iter_mut() {
                refill(bucket, *rate);
                take = take.min(bucket.tokens);
            }
            remaining -= take;
            limits.iter_mut().fold(0.0f64, |wait, (bucket, rate)| {
                bucket.tokens -= take;
                wait.max((remaining - bucket.tokens).max(0.0) / *rate)
            })
        };
        if remaining > 0.0 {
            sleep(Duration::from_secs_f64(wait.clamp(0.001, 0.1))).await;
        }
    }
    transfer_can_continue(app, id, generation)
}

async fn fragment_bytes(
    client: &reqwest::Client,
    app: &AppHandle,
    id: &str,
    source: &str,
    byte_range: Option<(u64, u64)>,
    retries: u32,
    generation: u64
) -> Result<Vec<u8>, String> {
    let attempts = retries.saturating_add(1).max(1);
    let mut last_error = String::from("fragment request failed");
    let expected_length = byte_range.map(|(_, length)| length);
    let mut pacing = None;
    for attempt in 0..attempts {
        if attempt > 0 && !wait_before_retry(app, id, generation, attempt, pacing.take()).await {
            return Err("paused".to_string());
        }
        let mut request = acquisition_request(client, app, id, source);
        if let Some((start, length)) = byte_range {
            let Some(end) = start
                .checked_add(length)
                .and_then(|value| value.checked_sub(1))
            else {
                return Err("The HLS byte range exceeds the addressable resource size".into());
            };
            request = request.header(reqwest::header::RANGE, format!("bytes={start}-{end}"));
        }
        match request.send().await {
            Ok(response) if response.status().is_success() => {
                if let Some((start, length)) = byte_range {
                    let valid_range = response.status() == reqwest::StatusCode::PARTIAL_CONTENT
                        && content_range(&response)
                            .map(|(actual_start, actual_end, _)| {
                                actual_start == start
                                    && actual_end == start.saturating_add(length).saturating_sub(1)
                            })
                            .unwrap_or(false);
                    if !valid_range {
                        last_error = "The server returned an invalid HLS byte range".into();
                        continue;
                    }
                }
                let mut bytes = Vec::new();
                let mut stream = response.bytes_stream();
                while let Some(chunk) = stream.next().await {
                    match chunk {
                        Ok(chunk) => {
                            if !throttle(app, id, chunk.len(), generation).await {
                                return Err("paused".to_string());
                            }
                            bytes.extend_from_slice(&chunk);
                            if expected_length
                                .map(|length| bytes.len() as u64 > length)
                                .unwrap_or(false)
                            {
                                last_error =
                                    "The server returned an overlong HLS byte range".into();
                                bytes.clear();
                                break;
                            }
                        }
                        Err(error) => {
                            last_error = error.to_string();
                            bytes.clear();
                            break;
                        }
                    }
                }
                if expected_length
                    .map(|length| bytes.len() as u64 == length)
                    .unwrap_or(!bytes.is_empty())
                {
                    return Ok(bytes);
                }
                if expected_length.is_some() {
                    last_error = "The server returned an incomplete HLS byte range".into();
                }
            }
            Ok(response) if terminal_source_status(response.status()) => {
                return Err(terminal_source_error(response.status()));
            }
            Ok(response) => {
                pacing = retry_after(&response);
                last_error = format!("source returned {}", response.status());
            }
            Err(error) => last_error = error.to_string()
        }
    }
    Err(last_error)
}

/// Stream one media fragment into `part_path`, decrypting on the way when
/// the segment carries an AES-128 key, and make it durable. Only one network
/// chunk (plus at most one cipher block) is held at a time, so a segment of
/// any size costs no more memory than a small one (F13). Returns the number
/// of bytes written.
async fn fragment_to_file(
    client: &reqwest::Client,
    app: &AppHandle,
    id: &str,
    source: &str,
    byte_range: Option<(u64, u64)>,
    key: Option<([u8; 16], [u8; 16])>,
    part_path: &Path,
    retries: u32,
    generation: u64
) -> Result<u64, String> {
    let attempts = retries.saturating_add(1).max(1);
    let mut last_error = String::from("fragment request failed");
    let expected_length = byte_range.map(|(_, length)| length);
    let mut pacing = None;
    for attempt in 0..attempts {
        if attempt > 0 && !wait_before_retry(app, id, generation, attempt, pacing.take()).await {
            return Err("paused".to_string());
        }
        let mut request = acquisition_request(client, app, id, source);
        if let Some((start, length)) = byte_range {
            let Some(end) = start
                .checked_add(length)
                .and_then(|value| value.checked_sub(1))
            else {
                return Err("The HLS byte range exceeds the addressable resource size".into());
            };
            request = request.header(reqwest::header::RANGE, format!("bytes={start}-{end}"));
        }
        match request.send().await {
            Ok(response) if response.status().is_success() => {
                if let Some((start, length)) = byte_range {
                    let valid_range = response.status() == reqwest::StatusCode::PARTIAL_CONTENT
                        && content_range(&response)
                            .map(|(actual_start, actual_end, _)| {
                                actual_start == start
                                    && actual_end == start.saturating_add(length).saturating_sub(1)
                            })
                            .unwrap_or(false);
                    if !valid_range {
                        last_error = "The server returned an invalid HLS byte range".into();
                        continue;
                    }
                }
                let mut part = File::create(part_path).await.map_err(|error| error.to_string())?;
                let mut decryptor = key.map(|(key, iv)| media::SegmentDecryptor::new(&key, iv));
                let mut received = 0u64;
                let mut written = 0u64;
                let mut interrupted = false;
                let mut stream = response.bytes_stream();
                while let Some(chunk) = stream.next().await {
                    match chunk {
                        Ok(chunk) => {
                            if !throttle(app, id, chunk.len(), generation).await {
                                return Err("paused".to_string());
                            }
                            received += chunk.len() as u64;
                            if expected_length.is_some_and(|length| received > length) {
                                last_error = "The server returned an overlong HLS byte range".into();
                                interrupted = true;
                                break;
                            }
                            let plain = match decryptor.as_mut() {
                                Some(decryptor) => decryptor.update(&chunk),
                                None => chunk.to_vec(),
                            };
                            part.write_all(&plain).await.map_err(|error| error.to_string())?;
                            written += plain.len() as u64;
                        }
                        Err(error) => {
                            last_error = error.to_string();
                            interrupted = true;
                            break;
                        }
                    }
                }
                if !interrupted && expected_length.map(|length| received == length).unwrap_or(received > 0) {
                    if let Some(decryptor) = decryptor {
                        let tail = decryptor.finish()?;
                        part.write_all(&tail).await.map_err(|error| error.to_string())?;
                        written += tail.len() as u64;
                    }
                    // Durability barrier: a renamed part file must hold
                    // complete bytes, so a power loss can never leave a
                    // trusted-but-torn segment (F10).
                    part.sync_all().await.map_err(|error| error.to_string())?;
                    return Ok(written);
                }
                if !interrupted && expected_length.is_some() {
                    last_error = "The server returned an incomplete HLS byte range".into();
                }
            }
            Ok(response) if terminal_source_status(response.status()) => {
                return Err(terminal_source_error(response.status()));
            }
            Ok(response) => {
                pacing = retry_after(&response);
                last_error = format!("source returned {}", response.status());
            }
            Err(error) => last_error = error.to_string()
        }
    }
    Err(last_error)
}

/// Read a response body, refusing to hold more than `limit` bytes: at most
/// `limit` bytes come back, so a caller can tell an oversized body apart.
async fn bounded_body(response: reqwest::Response, limit: usize) -> Result<Vec<u8>, String> {
    let mut body = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|error| error.to_string())?;
        let room = limit - body.len();
        body.extend_from_slice(&chunk[..chunk.len().min(room)]);
        if body.len() == limit {
            break;
        }
    }
    Ok(body)
}

/// Largest manifest (HLS playlist or DASH MPD) read into memory. Long VOD
/// timelines run to a few megabytes; this leaves ample room.
const MANIFEST_LIMIT: usize = 32 * 1024 * 1024;

/// A manifest body as text, refusing one larger than `MANIFEST_LIMIT` (F13).
async fn manifest_text(response: reqwest::Response) -> Result<String, String> {
    let body = bounded_body(response, MANIFEST_LIMIT + 1).await?;
    if body.len() > MANIFEST_LIMIT {
        return Err("The media manifest is larger than 32 MiB".into());
    }
    Ok(String::from_utf8_lossy(&body).into_owned())
}

async fn fetch_hls_key_bytes(client: &reqwest::Client, app: &AppHandle, id: &str, uri: &str) -> Result<[u8; 16], String> {
    let response = acquisition_request(client, app, id, uri).send().await.map_err(|error| error.to_string())?.error_for_status().map_err(|error| error.to_string())?;
    // A key is 16 bytes; reading stops just past that, whatever the server sends.
    let bytes = bounded_body(response, 17).await?;
    if bytes.len() != 16 { return Err(format!("The HLS AES-128 key did not contain 16 bytes (got {}{})", bytes.len().min(16), if bytes.len() > 16 { " or more" } else { "" })); }
    let mut key = [0u8; 16];
    key.copy_from_slice(&bytes);
    Ok(key)
}

async fn acquire_media_segment(
    client: &reqwest::Client,
    app: &AppHandle,
    id: &str,
    fragment: &media::Segment,
    segment_path: &Path,
    segment_temp_path: &Path,
    retry_count: u32,
    generation: u64,
    completed: &AtomicU64,
    downloaded: &AtomicU64,
    started: std::time::Instant,
    existing_bytes: u64,
    total_segments: u32,
    existing_count: u64,
    connection_cap: usize,
) -> Result<(), String> {
    if !transfer_can_continue(app, id, generation) { return Err("paused".to_string()); }
    // RFC 8216 4.4.2.4: full-segment AES-128 arrives encrypted; the 16-byte
    // key is fetched first through the same acquisition context (Referer
    // flows via acquisition_request) so the segment is decrypted as it
    // streams to disk. Byte-range segments never carry a key (the parser
    // leaves key None), so no partial decryption path exists.
    let key = match fragment.key.as_ref() {
        Some(key) => Some((fetch_hls_key_bytes(client, app, id, &key.uri).await?, media::hls_key_iv(key))),
        None => None,
    };
    if let Some(parent) = segment_path.parent() { tokio::fs::create_dir_all(parent).await.map_err(|error| error.to_string())?; }
    if !transfer_can_continue(app, id, generation) { return Err("paused".to_string()); }
    let written = match fragment_to_file(client, app, id, &fragment.url, fragment.range, key, segment_temp_path, retry_count, generation).await {
        Ok(written) => written,
        Err(error) => {
            let _ = tokio::fs::remove_file(segment_temp_path).await;
            return Err(error);
        }
    };
    if !transfer_can_continue(app, id, generation) { return Err("paused".to_string()); }
    tokio::fs::rename(segment_temp_path, segment_path).await.map_err(|error| error.to_string())?;
    if !transfer_can_continue(app, id, generation) { return Err("paused".to_string()); }
    let done = completed.fetch_add(1, Ordering::Relaxed) + 1;
    let size = downloaded.fetch_add(written, Ordering::Relaxed) + written;
    let elapsed = started.elapsed().as_secs_f64().max(0.1);
    let speed = ((size.saturating_sub(existing_bytes)) as f64 / elapsed) as u64;
    let remaining = (total_segments as u64).saturating_sub(done);
    let eta = if speed > 0 && remaining > 0 {
        let average_fragment = size / done.max(1);
        Some(format!("{}s left", (average_fragment.saturating_mul(remaining) / speed).max(1)))
    } else {
        None
    };
    let state = app.state::<CoreState>();
    let finished_missing = done.saturating_sub(existing_count);
    if !transfer_can_continue(app, id, generation) { return Err("paused".to_string()); }
    emit_job(&state, id, |job| {
        job.downloaded = size;
        job.progress = done as f64 / total_segments as f64 * 100.0;
        job.speed = speed;
        job.eta = eta.clone();
        job.segments = Some(SegmentState { completed: done as u32, total: total_segments, identity: job.segments.as_ref().and_then(|segments| segments.identity.clone()) });
        job.connections = connection_cap.min((total_segments as u64).saturating_sub(finished_missing) as usize) as u32;
    });
    emit_progress(app, &state, id);
    Ok(())
}

fn content_range(response: &reqwest::Response) -> Option<(u64, u64, u64)> {
    let value = response.headers().get(reqwest::header::CONTENT_RANGE)?.to_str().ok()?;
    let (range, total) = value.strip_prefix("bytes ")?.split_once('/')?;
    let (start, end) = range.split_once('-')?;
    Some((start.parse().ok()?, end.parse().ok()?, total.parse().ok()?))
}

/// Longest wait a server's Retry-After can impose on one retry.
const RETRY_AFTER_CAP: Duration = Duration::from_secs(30);

/// A response's Retry-After, as delta-seconds or an IMF-fixdate
/// ("Sun, 06 Nov 1994 08:49:37 GMT"); other forms are ignored.
fn retry_after(response: &reqwest::Response) -> Option<Duration> {
    let value = response.headers().get(reqwest::header::RETRY_AFTER)?.to_str().ok()?.trim();
    if let Ok(seconds) = value.parse::<u64>() {
        return Some(Duration::from_secs(seconds));
    }
    let parts: Vec<&str> = value.split_whitespace().collect();
    let [_, day, month, year, time, "GMT"] = parts.as_slice() else { return None; };
    let month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"].iter().position(|name| name == month)? as i64 + 1;
    let (day, year): (i64, i64) = (day.parse().ok()?, year.parse().ok()?);
    let clock: Vec<i64> = time.split(':').map(|part| part.parse().ok()).collect::<Option<_>>()?;
    let [hours, minutes, seconds] = clock.as_slice() else { return None; };
    // Days since 1970-01-01 for a proleptic Gregorian date (Howard Hinnant's days_from_civil).
    let shifted = if month <= 2 { year - 1 } else { year };
    let era = shifted.div_euclid(400);
    let year_of_era = shifted - era * 400;
    let day_of_year = (153 * (month + if month > 2 { -3 } else { 9 }) + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let at = (era * 146_097 + day_of_era - 719_468) * 86_400 + hours * 3600 + minutes * 60 + seconds;
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).ok()?.as_secs() as i64;
    Some(Duration::from_secs(at.saturating_sub(now).max(0) as u64))
}

/// Wait before retry number `attempt` (1-based) of a transfer request (F22):
/// the server's Retry-After when it sent one, otherwise exponential backoff
/// from 250 ms, both capped. The wait is sliced so pause and cancel stay
/// responsive; false means the transfer may no longer continue.
async fn wait_before_retry(app: &AppHandle, id: &str, generation: u64, attempt: u32, server_pacing: Option<Duration>) -> bool {
    let backoff = Duration::from_millis(250u64 << attempt.saturating_sub(1).min(4));
    let deadline = std::time::Instant::now() + server_pacing.unwrap_or(backoff).min(RETRY_AFTER_CAP);
    loop {
        if !transfer_can_continue(app, id, generation) {
            return false;
        }
        let now = std::time::Instant::now();
        if now >= deadline {
            return true;
        }
        sleep((deadline - now).min(Duration::from_millis(100))).await;
    }
}

async fn range_bytes(
    client: &reqwest::Client,
    app: &AppHandle,
    id: &str,
    source: &str,
    start: u64,
    end: u64,
    retries: u32,
    expected: &ResourceIdentity,
    generation: u64
) -> Result<Vec<u8>, String> {
    let attempts = retries.saturating_add(1).max(1);
    let mut last_error = String::from("range request failed");
    let total_len = end.saturating_sub(start).saturating_add(1);
    let mut pacing = None;
    for attempt in 0..attempts {
        if attempt > 0 && !wait_before_retry(app, id, generation, attempt, pacing.take()).await {
            return Err("paused".to_string());
        }
        match acquisition_request(client, app, id, source)
            .header(reqwest::header::RANGE, format!("bytes={start}-{end}"))
            .send()
            .await
        {
            Ok(response) if response.status() == reqwest::StatusCode::PARTIAL_CONTENT => {
                let valid_range = content_range(&response)
                    .map(|(actual_start, actual_end, actual_total)| {
                        actual_start == start
                            && actual_end == end
                            && actual_total == expected.length
                    })
                    .unwrap_or(false);
                if !valid_range {
                    last_error = "The server returned an invalid byte range".into();
                    continue;
                }
                if !valid_range_identity(&response, expected) {
                    // A different resource will not turn back into the
                    // verified one: retrying only spends requests.
                    return Err("The resource changed while it was being acquired".into());
                }
                let mut buf = Vec::new();
                let mut stream = response.bytes_stream();
                let mut overflow = false;
                while let Some(chunk) = stream.next().await {
                    match chunk {
                        Ok(bytes) => {
                            buf.extend_from_slice(&bytes);
                            if buf.len() as u64 > total_len {
                                overflow = true;
                                break;
                            }
                            // Pace each received chunk. The byte counter is
                            // deliberately updated only after the complete
                            // range is durably written by the caller. That
                            // keeps displayed progress and persisted ranges
                            // truthful if pause or a retry interrupts here.
                            if !throttle(app, id, bytes.len(), generation).await {
                                return Err("paused".to_string());
                            }
                        }
                        Err(error) => {
                            last_error = error.to_string();
                            buf.clear();
                            break;
                        }
                    }
                }
                if overflow {
                    last_error = "The server returned an overlong byte range".into();
                    continue;
                }
                if buf.len() as u64 == total_len {
                    return Ok(buf);
                }
                if buf.is_empty() {
                    continue;
                }
                last_error = "The server returned an incomplete byte range".into();
            }
            Ok(response) if terminal_source_status(response.status()) => {
                return Err(terminal_source_error(response.status()));
            }
            Ok(response) => {
                pacing = retry_after(&response);
                last_error = format!("range request returned {}", response.status());
            }
            Err(error) => last_error = error.to_string()
        }
    }
    Err(last_error)
}

async fn acquire_ranges(
    app: AppHandle,
    id: String,
    source: String,
    response: reqwest::Response,
    total: u64,
    generation: u64
) -> Result<(), String> {
    if !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }
    let state = app.state::<CoreState>();
    let (temp_path, max_connections, retry_count, replace_existing) = state
        .snapshot
        .lock()
        .map_err(|_| "State unavailable".to_string())
        .and_then(|snapshot| {
            snapshot
                .jobs
                .iter()
                .find(|job| job.id == id)
                .map(|job| {
                    (
                        job.temp_path.clone(),
                        job.max_connections,
                        if snapshot.settings.retry_automatically {
                            snapshot.settings.max_retries
                        } else {
                            0
                        },
                        snapshot.settings.collision_behavior == "replace"
                    )
                })
                .ok_or_else(|| "Acquisition no longer exists".to_string())
        })?;
    let identity = identity_from_response(&response, total);
    let first_target = total.min(1024 * 1024) as usize;
    let mut first = Vec::with_capacity(first_target);
    let mut first_stream = response.bytes_stream();
    while first.len() < first_target {
        let Some(chunk) = first_stream.next().await else { break; };
        let chunk = chunk.map_err(|error| error.to_string())?;
        let remaining = first_target.saturating_sub(first.len());
        first.extend_from_slice(&chunk[..chunk.len().min(remaining)]);
        if chunk.is_empty() {
            break;
        }
    }
    drop(first_stream);
    if !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }
    if first.len() as u64 != first_target as u64 {
        return Err("The initial range response ended before the advertised object length".into());
    }
    let Some(parent) = PathBuf::from(&temp_path).parent().map(PathBuf::from) else {
        return Err("Temporary path is invalid".into());
    };
    tokio::fs::create_dir_all(parent)
        .await
        .map_err(|error| error.to_string())?;
    if !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }
    let existing = state.snapshot.lock().ok().and_then(|snapshot| {
        snapshot
            .jobs
            .iter()
            .find(|job| job.id == id)
            .map(|job| (job.resource_identity.clone(), job.completed_ranges.clone()))
    });
    let evidence = resume_evidence(
        existing.as_ref().and_then(|(stored_identity, _)| stored_identity.as_ref()),
        &identity,
    );
    let mut completed_ranges = existing
        .as_ref()
        .filter(|(_, ranges)| evidence != ResumeEvidence::Rejected && !ranges.is_empty())
        .map(|(_, ranges)| {
            ranges
                .iter()
                .filter(|range| range.start <= range.end && range.end < total)
                .cloned()
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    completed_ranges = completed_ranges
        .into_iter()
        .fold(Vec::new(), |ranges, range| merge_range(&ranges, range));
    let mut can_resume = !completed_ranges.is_empty()
        && tokio::fs::metadata(&temp_path)
            .await
            .map(|metadata| metadata.len() == total)
            .unwrap_or(false);
    if !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }
    if can_resume && evidence == ResumeEvidence::Sampled {
        can_resume = temp_prefix_matches(&temp_path, &first).await;
        if !transfer_can_continue(&app, &id, generation) {
            return Ok(());
        }
    }
    if !can_resume {
        completed_ranges = vec![ByteRange { start: 0, end: first.len() as u64 - 1 }];
        if !transfer_can_continue(&app, &id, generation) {
            return Ok(());
        }
        let mut initial = File::create(&temp_path)
            .await
            .map_err(|error| error.to_string())?;
        if !transfer_can_continue(&app, &id, generation) {
            return Ok(());
        }
        initial
            .write_all(&first)
            .await
            .map_err(|error| error.to_string())?;
        if !transfer_can_continue(&app, &id, generation) {
            return Ok(());
        }
        initial
            .set_len(total)
            .await
            .map_err(|error| error.to_string())?;
        // The initial useful response is claimed as a completed range only
        // after the bytes and preallocation are durable (F10).
        initial
            .sync_all()
            .await
            .map_err(|error| error.to_string())?;
    } else if !completed_ranges.iter().any(|range| range.start == 0 && range.end >= first.len() as u64 - 1) {
        let mut initial = OpenOptions::new()
            .write(true)
            .open(&temp_path)
            .await
            .map_err(|error| error.to_string())?;
        if !transfer_can_continue(&app, &id, generation) {
            return Ok(());
        }
        initial
            .seek(SeekFrom::Start(0))
            .await
            .map_err(|error| error.to_string())?;
        if !transfer_can_continue(&app, &id, generation) {
            return Ok(());
        }
        initial
            .write_all(&first)
            .await
            .map_err(|error| error.to_string())?;
        if !transfer_can_continue(&app, &id, generation) {
            return Ok(());
        }
        // Same barrier before claiming the initial range on the resume path.
        initial
            .sync_all()
            .await
            .map_err(|error| error.to_string())?;
        completed_ranges = merge_range(
            &completed_ranges,
            ByteRange { start: 0, end: first.len() as u64 - 1 },
        );
    }
    let ranges = missing_ranges(total, &completed_ranges, max_connections);
    let worker_count = max_connections.clamp(1, 32).min(ranges.len().max(1) as u32) as usize;
    let initial_downloaded = covered_bytes(&completed_ranges).min(total);
    if !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }
    emit_job(&state, &id, |job| {
        job.state = "downloading".into();
        job.total = Some(total);
        job.downloaded = initial_downloaded;
        job.progress = initial_downloaded as f64 / total as f64 * 100.0;
        job.resumable = true;
        job.mode = "whole-object".into();
        job.resource_identity = Some(identity.clone());
        job.completed_ranges = completed_ranges.clone();
        job.connections = if ranges.is_empty() {
            0
        } else {
            worker_count as u32
        };
        job.events.insert(
            0,
            job_event(
                &format!("Range support verified; {worker_count} workers started"),
                Some("success")
            )
        );
    });
    emit_snapshot(&app, &state);
    let downloaded = std::sync::Arc::new(AtomicU64::new(initial_downloaded));
    let started = std::time::Instant::now();
    let completed_workers = std::sync::Arc::new(AtomicU64::new(0));
    let total_ranges = ranges.len() as u64;
    // One client per job: connection-pool and TLS-session reuse across every
    // chunk instead of a fresh handshake per worker (F08).
    let shared_client = std::sync::Arc::new(http_client());
    let mut transfers = futures_util::stream::iter(ranges.into_iter().map(|(start, end)| {
        let client = shared_client.clone();
        let source = source.clone();
        let temp_path = temp_path.clone();
        let app = app.clone();
        let id = id.clone();
        let identity = identity.clone();
        let downloaded = downloaded.clone();
        let completed_workers = completed_workers.clone();
        async move {
            if !transfer_is_downloading(&app, &id, generation) {
                return Err("paused".to_string());
            }
            let bytes = range_bytes(
                &client,
                &app,
                &id,
                &source,
                start,
                end,
                retry_count,
                &identity,
                generation
            )
            .await?;
            if !transfer_is_downloading(&app, &id, generation) {
                return Err("paused".to_string());
            }
            let mut file = OpenOptions::new()
                .write(true)
                .open(&temp_path)
                .await
                .map_err(|error| error.to_string())?;
            if !transfer_is_downloading(&app, &id, generation) {
                return Err("paused".to_string());
            }
            file.seek(SeekFrom::Start(start))
                .await
                .map_err(|error| error.to_string())?;
            if !transfer_is_downloading(&app, &id, generation) {
                return Err("paused".to_string());
            }
            file.write_all(&bytes)
                .await
                .map_err(|error| error.to_string())?;
            // Durability barrier: completed ranges are trusted after reboot,
            // so bytes must be durable before the range is claimed (F10).
            file.sync_all()
                .await
                .map_err(|error| error.to_string())?;
            if !transfer_is_downloading(&app, &id, generation) {
                return Err("paused".to_string());
            }
            let total_downloaded =
                downloaded.fetch_add(bytes.len() as u64, Ordering::Relaxed) + bytes.len() as u64;
            let speed = ((total_downloaded.saturating_sub(initial_downloaded)) as f64
                / started.elapsed().as_secs_f64().max(0.1)) as u64;
            let finished = completed_workers.fetch_add(1, Ordering::Relaxed) + 1;
            let state = app.state::<CoreState>();
            emit_job(&state, &id, |job| {
                job.downloaded = total_downloaded;
                job.speed = speed;
                job.eta = if speed > 0 {
                    Some(format!(
                        "{}s left",
                        total.saturating_sub(total_downloaded) / speed
                    ))
                } else {
                    None
                };
                job.progress = total_downloaded as f64 / total as f64 * 100.0;
                job.completed_ranges = merge_range(&job.completed_ranges, ByteRange { start, end });
                job.connections =
                    worker_count.min(total_ranges.saturating_sub(finished) as usize) as u32;
            });
            emit_progress(&app, &state, &id);
            // No throttle here: intake was already paced piece-by-piece inside
            // range_bytes; charging the whole chunk again would halve the rate.
            Ok::<(), String>(())
        }
    }))
    .buffer_unordered(worker_count);
    let mut transfer_error = None;
    while let Some(result) = transfers.next().await {
        if let Err(error) = result {
            transfer_error = Some(error);
        }
    }
    if let Some(initial_error) = transfer_error {
        if !transfer_is_current(&app, &id, generation) {
            return Ok(());
        }
        if job_state(&app, &id).as_deref() == Some("paused") {
            emit_job(&state, &id, |job| {
                job.connections = 0;
                job.speed = 0;
                job.eta = Some("Paused".into());
                job.events.insert(
                    0,
                    job_event(
                        "Paused with verified byte ranges preserved",
                        Some("warning")
                    )
                );
            });
            emit_snapshot(&app, &state);
            return Ok(());
        }
        // A dead URL cannot be revived by fewer connections or a new stream:
        // report it instead of grinding through the fallback stages (F05).
        if is_terminal_source_error(&initial_error) {
            return Err(initial_error);
        }
        let mut fallback_completed = state
            .snapshot
            .lock()
            .ok()
            .and_then(|snapshot| {
                snapshot
                    .jobs
                    .iter()
                    .find(|job| job.id == id)
                    .map(|job| job.completed_ranges.clone())
            })
            .unwrap_or_default();
        fallback_completed.sort_by_key(|range| (range.start, range.end));
        fallback_completed = fallback_completed
            .into_iter()
            .fold(Vec::new(), |ranges, range| merge_range(&ranges, range));
        let fallback_ranges = missing_ranges(total, &fallback_completed, 1);
        emit_job(&state, &id, |job| {
            job.connections = if fallback_ranges.is_empty() { 0 } else { 1 };
            job.speed = 0;
            job.eta = Some("Retrying with one connection".into());
            job.events.insert(
                0,
                job_event(
                    "Parallel range acquisition was rejected; retrying with one connection",
                    Some("warning")
                )
            );
        });
        emit_snapshot(&app, &state);
        let fallback_client = http_client();
        let mut fallback_error = None;
        // Give a rate-limiting server a short quiet period before switching to
        // one connection. The range probe and the failed workers have already
        // consumed the server's burst allowance.
        sleep(Duration::from_millis(500)).await;
        for (start, end) in fallback_ranges {
            if !transfer_can_continue(&app, &id, generation) {
                return Ok(());
            }
            let bytes = match range_bytes(
                &fallback_client,
                &app,
                &id,
                &source,
                start,
                end,
                retry_count.max(1),
                &identity,
                generation
            )
            .await
            {
                Ok(bytes) => bytes,
                Err(error) => {
                    if is_terminal_source_error(&error) {
                        return Err(error);
                    }
                    fallback_error = Some(error);
                    break;
                }
            };
            if !transfer_can_continue(&app, &id, generation) {
                return Ok(());
            }
            let mut file = match OpenOptions::new().write(true).open(&temp_path).await {
                Ok(file) => file,
                Err(error) => {
                    fallback_error = Some(error.to_string());
                    break;
                }
            };
            if !transfer_can_continue(&app, &id, generation) {
                return Ok(());
            }
            if let Err(error) = file.seek(SeekFrom::Start(start)).await {
                fallback_error = Some(error.to_string());
                break;
            }
            if let Err(error) = file.write_all(&bytes).await {
                fallback_error = Some(error.to_string());
                break;
            }
            // Same durability barrier as the parallel path (F10).
            if let Err(error) = file.sync_all().await {
                fallback_error = Some(error.to_string());
                break;
            }
            let total_downloaded =
                downloaded.fetch_add(bytes.len() as u64, Ordering::Relaxed) + bytes.len() as u64;
            emit_job(&state, &id, |job| {
                job.downloaded = total_downloaded;
                job.speed = 0;
                job.eta = Some("Retrying with one connection".into());
                job.progress = total_downloaded as f64 / total as f64 * 100.0;
                job.completed_ranges = merge_range(&job.completed_ranges, ByteRange { start, end });
                job.connections = 1;
            });
            emit_progress(&app, &state, &id);
        }
        if let Some(_error) = fallback_error {
            emit_job(&state, &id, |job| {
                job.connections = 1;
                job.speed = 0;
                job.eta = Some("Retrying as one stream".into());
                job.events.insert(
                    0,
                    job_event(
                        "Range requests remained unavailable; retrying as one stream",
                        Some("warning")
                    )
                );
            });
            emit_snapshot(&app, &state);
            sleep(Duration::from_secs(3)).await;
            // The fallback is a new request for the object whose ranges were
            // verified: it must prove it is still that object and still a
            // file before it may replace them. It streams into a staging file,
            // so a rejected fallback leaves the verified ranges on disk.
            let stream_client = http_client();
            let rejected = |reason: String| format!("{initial_error}; one-stream fallback rejected: {reason}");
            let response = match acquisition_request(&stream_client, &app, &id, &source)
                .send()
                .await
            {
                Ok(response) if response.status().is_success() => response,
                Ok(response) => return Err(rejected(format!("source returned {}", response.status()))),
                Err(stream_error) => return Err(rejected(stream_error.to_string())),
            };
            match partial_response_is_complete(&response) {
                Ok(Some(length)) if length != total => {
                    return Err(rejected(format!("the source now reports {length} bytes, not {total}")))
                }
                Ok(_) => {}
                Err(reason) => return Err(rejected(reason)),
            }
            if !valid_range_identity(&response, &identity) {
                return Err(rejected("the resource changed since its ranges were verified".into()));
            }
            let fallback_mime = header_string(&response, reqwest::header::CONTENT_TYPE);
            let fallback_disposition = header_string(&response, reqwest::header::CONTENT_DISPOSITION);
            let job_name = state
                .snapshot
                .lock()
                .ok()
                .and_then(|snapshot| snapshot.jobs.iter().find(|job| job.id == id).map(|job| job.name.clone()))
                .unwrap_or_default();
            if page_instead_of_file(fallback_mime.as_deref(), fallback_disposition.as_deref(), &job_name) {
                return Err(rejected("the source returned a web page instead of the file".into()));
            }
            let staging = format!("{temp_path}.stream");
            let mut file = File::create(&staging).await.map_err(|error| rejected(error.to_string()))?;
            let mut full_downloaded = 0u64;
            let mut prefix: Vec<u8> = Vec::new();
            let mut sniffed = false;
            let mut stream = response.bytes_stream();
            let outcome: Result<(), String> = async {
                while let Some(chunk) = stream.next().await {
                    if !transfer_is_downloading(&app, &id, generation) {
                        return Err(String::new());
                    }
                    let bytes = chunk.map_err(|error| rejected(error.to_string()))?;
                    if !sniffed {
                        prefix.extend_from_slice(&bytes[..bytes.len().min(BODY_SNIFF_LIMIT.saturating_sub(prefix.len()))]);
                        if prefix.len() >= BODY_SNIFF_LIMIT {
                            sniffed = true;
                            if response_is_page(fallback_mime.as_deref(), fallback_disposition.as_deref(), &job_name, &prefix) {
                                return Err(rejected("the source returned a web page instead of the file".into()));
                            }
                        }
                    }
                    file.write_all(&bytes).await.map_err(|error| rejected(error.to_string()))?;
                    full_downloaded = full_downloaded.saturating_add(bytes.len() as u64);
                    if !throttle(&app, &id, bytes.len(), generation).await {
                        return Err(String::new());
                    }
                    let progress = full_downloaded as f64 / total as f64 * 100.0;
                    emit_job(&state, &id, |job| {
                        job.downloaded = full_downloaded;
                        job.progress = progress;
                        job.speed = 0;
                        job.eta = Some("Retrying as one stream".into());
                        job.connections = 1;
                    });
                    // Same progress bookkeeping as every other streaming path: a
                    // full snapshot here would rewrite the whole jobs table and
                    // re-render every row once per network chunk.
                    emit_progress(&app, &state, &id);
                }
                if !sniffed && response_is_page(fallback_mime.as_deref(), fallback_disposition.as_deref(), &job_name, &prefix) {
                    return Err(rejected("the source returned a web page instead of the file".into()));
                }
                if full_downloaded != total {
                    return Err(rejected(format!("it returned {full_downloaded} bytes, expected {total}")));
                }
                file.sync_all().await.map_err(|error| rejected(error.to_string()))
            }
            .await;
            drop(file);
            if let Err(reason) = outcome {
                let _ = tokio::fs::remove_file(&staging).await;
                // An empty reason is a pause/cancel: the caller decides.
                return if reason.is_empty() { Ok(()) } else { Err(reason) };
            }
            tokio::fs::rename(&staging, &temp_path)
                .await
                .map_err(|error| rejected(error.to_string()))?;
            downloaded.store(full_downloaded, Ordering::Relaxed);
            emit_job(&state, &id, |job| {
                job.downloaded = full_downloaded;
                job.progress = 100.0;
                job.speed = 0;
                job.connections = 1;
                job.resumable = false;
                job.mode = "single-stream".into();
                job.completed_ranges = Vec::new();
                job.mime = fallback_mime.clone();
                job.eta = Some("Finalizing".into());
            });
            emit_snapshot(&app, &state);
        }
    }
    if !transfer_is_current(&app, &id, generation) {
        return Ok(());
    }
    if job_state(&app, &id).as_deref() != Some("downloading") {
        if job_state(&app, &id).as_deref() == Some("paused") {
            emit_job(&state, &id, |job| {
                job.connections = 0;
                job.speed = 0;
                job.eta = Some("Paused".into());
            });
            emit_snapshot(&app, &state);
        }
        return Ok(());
    }
    let committed = state
        .snapshot
        .lock()
        .ok()
        .and_then(|snapshot| {
            snapshot
                .jobs
                .iter()
                .find(|job| job.id == id)
                .map(|job| (job.provisional != Some(true), job.destination.clone()))
        })
        .unwrap_or((false, String::new()));
    if !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }
    // Held until the completion below is recorded (F08).
    let publishing;
    if committed.0 && !committed.1.is_empty() {
        if let Some(parent) = PathBuf::from(&committed.1).parent() {
            let _ = tokio::fs::create_dir_all(parent).await;
            if !transfer_can_continue(&app, &id, generation) {
                return Ok(());
            }
        }
        if !transfer_can_continue(&app, &id, generation) {
            return Ok(());
        }
        let (destination, reserved, reservation) =
            managed_destination(&committed.1, replace_existing)?;
        if destination != committed.1 || reservation.is_some() {
            let reservation_marker = reservation.clone();
            emit_job(&state, &id, |job| {
                job.destination = destination.clone();
                job.destination_reservation = reservation_marker;
                if let Some(file_name) = PathBuf::from(&destination)
                    .file_name()
                    .and_then(|value| value.to_str())
                {
                    if destination != committed.1 {
                        job.name = file_name.to_string();
                    }
                }
                if destination != committed.1 {
                    job.events.insert(
                        0,
                        job_event("Destination renamed to avoid a collision", Some("warning"))
                    );
                }
            });
            emit_snapshot(&app, &state);
        }
        publishing = begin_publishing(&state, &id, || transfer_can_continue(&app, &id, generation));
        if publishing.is_none() {
            if reserved {
                cleanup_reserved_destination(&destination, reservation.as_deref()).await;
            }
            clear_destination_reservation(&app, &state, &id);
            return Ok(());
        }
        if let Err(error) = move_completed_file(
            &temp_path,
            &destination,
            replace_existing,
            reservation.as_deref()
        )
        .await
        {
            clear_destination_reservation(&app, &state, &id);
            return Err(error);
        }
        clear_destination_reservation(&app, &state, &id);
    }
    if !committed.0 && !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }
    emit_job(&state, &id, |job| {
        if committed.0 {
            complete_job(job);
        } else {
            mark_ready_for_confirmation(job, "Download ready; waiting for destination");
        }
    });
    emit_snapshot(&app, &state);
    if committed.0 {
        add_notification(&app, &state, &id, "completed");
    }
    Ok(())
}

fn manifest_segment_path(directory: &Path, track: usize, index: usize, track_count: usize) -> PathBuf {
    if track_count == 1 { directory.join(format!("{index:08}.part")) } else { directory.join(format!("{track:02}")).join(format!("{index:08}.part")) }
}

// Identity of a segmented resource: track kinds, every segment URL and byte
// range, plus each segment's encryption identity (key URI + effective IV).
// Rotated keys at unchanged URLs must not mix old ciphertext with a new key.
// Positional part-files are only reusable when this matches; otherwise the
// directory is wiped and refetched rather than stitching a new manifest onto
// old bytes (SPEC §8.8: restart when identity cannot be established safely).
fn segment_identity(tracks: &[media::MediaTrack]) -> String {
    let mut hash = 0xcbf29ce484222325u64;
    for track in tracks {
        for byte in track.kind.bytes().chain([0xff]) {
            hash ^= u64::from(byte);
            hash = hash.wrapping_mul(0x100000001b3);
        }
        for segment in &track.segments {
            for byte in segment.url.bytes().chain([0xfe]) {
                hash ^= u64::from(byte);
                hash = hash.wrapping_mul(0x100000001b3);
            }
            for byte in segment
                .range
                .iter()
                .flat_map(|(start, length)| {
                    start.to_le_bytes().into_iter().chain(length.to_le_bytes())
                })
                .chain([0xfd])
            {
                hash ^= u64::from(byte);
                hash = hash.wrapping_mul(0x100000001b3);
            }
            if let Some(key) = segment.key.as_ref() {
                for byte in key.uri.bytes().chain(media::hls_key_iv(key).into_iter()).chain([0xfc]) {
                    hash ^= u64::from(byte);
                    hash = hash.wrapping_mul(0x100000001b3);
                }
            }
        }
    }
    format!("{hash:016x}")
}

async fn resource_length(client: &reqwest::Client, app: &AppHandle, id: &str, url: &str) -> Result<u64, String> {
    let response = acquisition_request(client, app, id, url).header(reqwest::header::RANGE, "bytes=0-0").send().await.map_err(|error| format!("Could not probe DASH resource length: {error}"))?;
    let status = response.status();
    if !status.is_success() && status != reqwest::StatusCode::PARTIAL_CONTENT { return Err(format!("DASH resource length probe returned HTTP {status}")); }
    if let Some(value) = response.headers().get(reqwest::header::CONTENT_RANGE).and_then(|value| value.to_str().ok()) {
        if let Some(total) = value.rsplit('/').next().and_then(|value| value.parse::<u64>().ok()) { return Ok(total); }
    }
    response.content_length().ok_or_else(|| "DASH resource length probe returned no total size".into())
}

const DASH_HEADER_LIMIT: u64 = 16 * 1024 * 1024;

async fn materialize_dash_segment_bases(client: &reqwest::Client, app: &AppHandle, id: &str, mut tracks: Vec<media::MediaTrack>, retries: u32, generation: u64) -> Result<Vec<media::MediaTrack>, String> {
    for track in &mut tracks {
        let Some(base) = track.segment_base.clone() else { continue; };
        let total_length = resource_length(client, app, id, &base.url).await?;
        // The index and initialization are parsed in memory; real ones are
        // kilobytes, so a manifest claiming more is refused (F13).
        if base.index_range.1 > DASH_HEADER_LIMIT || base.initialization_range.is_some_and(|(_, length)| length > DASH_HEADER_LIMIT) {
            return Err("The DASH index or initialization range is larger than 16 MiB".into());
        }
        let index = media::Segment { url: base.url.clone(), range: Some(base.index_range), key: None };
        let index_data = fragment_bytes(client, app, id, &index.url, index.range, retries, generation).await?;
        let initialization_data = if let Some(range) = base.initialization_range {
            let initialization = media::Segment { url: base.url.clone(), range: Some(range), key: None };
            fragment_bytes(client, app, id, &initialization.url, initialization.range, retries, generation).await?
        } else { Vec::new() };
        track.segments = media::expand_dash_segment_base(&base, &index_data, total_length, &initialization_data)?;
        track.segment_base = None;
    }
    Ok(tracks)
}

async fn acquire_manifest(
    app: AppHandle,
    id: String,
    source: String,
    body: String,
    _mime: Option<String>,
    expected_kind: Option<String>,
    selected_segments: Vec<String>,
    generation: u64
) -> Result<(), String> {
    if !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }
    let client = http_client();
    let mut manifest_source = source;
    let mut manifest_body = body;
    let mut hls_track_sources = None;
    let mut hls_all_mpeg_ts = false;
    for _ in 0..4 {
        let is_hls = manifest_source.to_ascii_lowercase().contains(".m3u8")
            || manifest_body.contains("#EXTM3U");
        if !is_hls {
            break;
        }
        if let Some(sources) =
            media::hls_variant_tracks(&manifest_source, &manifest_body, &selected_segments)
        {
            hls_track_sources = Some(sources);
            break;
        }
        let Some(variant) = (if is_hls {
            media::hls_variant(&manifest_source, &manifest_body)
        } else {
            None
        }) else {
            break;
        };
        let fetch = acquisition_request(&client, &app, &id, &variant)
            .send()
            .await
            .map_err(|error| error.to_string())?
            .error_for_status()
            .map_err(|error| error.to_string())?;
        // Redirects change the resolution base: relative segment references
        // belong to the manifest's effective URL, not the requested one (F04).
        manifest_source = fetch.url().to_string();
        manifest_body = manifest_text(fetch).await?;
        if !transfer_can_continue(&app, &id, generation) {
            return Ok(());
        }
    }
    let is_hls =
        manifest_source.to_ascii_lowercase().contains(".m3u8") || manifest_body.contains("#EXTM3U");
    let tracks = if let Some(sources) = hls_track_sources {
        let mut tracks = Vec::with_capacity(sources.len());
        let mut track_has_map = Vec::with_capacity(sources.len());
        for (kind, track_source) in sources {
            let mut source = track_source;
            let fetch = acquisition_request(&client, &app, &id, &source)
                .send()
                .await
                .map_err(|error| error.to_string())?
                .error_for_status()
                .map_err(|error| error.to_string())?;
            // Same redirect rule as the variant loop above (F04).
            source = fetch.url().to_string();
            let mut body = manifest_text(fetch).await?;
            if !transfer_can_continue(&app, &id, generation) {
                return Ok(());
            }
            for _ in 0..4 {
                let Some(variant) = media::hls_variant(&source, &body) else {
                    break;
                };
                let fetch = acquisition_request(&client, &app, &id, &variant)
                    .send()
                    .await
                    .map_err(|error| error.to_string())?
                    .error_for_status()
                    .map_err(|error| error.to_string())?;
                source = fetch.url().to_string();
                body = manifest_text(fetch).await?;
                if !transfer_can_continue(&app, &id, generation) {
                    return Ok(());
                }
            }
            track_has_map.push(body.contains("#EXT-X-MAP"));
            tracks.push(media::MediaTrack {
                kind,
                segments: media::parse_hls(&source, &body)?,
                segment_base: None
            });
        }
        let all_fmp4 = track_has_map.iter().all(|has_map| *has_map);
        let all_mpeg_ts = track_has_map.iter().all(|has_map| !*has_map);
        if !all_fmp4 && !all_mpeg_ts {
            return Err("Mixed HLS MPEG-TS and fragmented-MP4 tracks are not supported".into());
        }
        hls_all_mpeg_ts = all_mpeg_ts;
        tracks
    } else if is_hls {
        hls_all_mpeg_ts = !manifest_body.contains("#EXT-X-MAP");
        vec![media::MediaTrack {
            kind: expected_kind.unwrap_or_else(|| "video".into()),
            segments: media::parse_hls(&manifest_source, &manifest_body)?,
            segment_base: None
        }]
    } else {
        media::parse_dash_tracks_for_segments(&manifest_source, &manifest_body, &selected_segments)?
    };
    let range_retry_count = app
        .state::<CoreState>()
        .snapshot
        .lock()
        .map(|snapshot| {
            if snapshot.settings.retry_automatically {
                snapshot.settings.max_retries
            } else {
                0
            }
        })
        .unwrap_or(0);
    let tracks =
        materialize_dash_segment_bases(&client, &app, &id, tracks, range_retry_count, generation)
            .await?;
    // Name the acquisition after its container, not the manifest: HLS without
    // an EXT-X-MAP carries MPEG-TS; everything else assembles to fragmented MP4.
    let single_webm = tracks.len() == 1
        && tracks[0]
            .segments
            .first()
            .map(|segment| {
                segment
                    .url
                    .split(['?', '#'])
                    .next()
                    .unwrap_or_default()
                    .to_ascii_lowercase()
                    .ends_with(".webm")
            })
            .unwrap_or(false);
    let (container_ext, container_mime) = if single_webm {
        ("webm", "video/webm")
    } else if is_hls && hls_all_mpeg_ts {
        ("ts", "video/mp2t")
    } else {
        ("mp4", "video/mp4")
    };
    let track_count = tracks.len();
    let track_lengths = tracks
        .iter()
        .map(|track| track.segments.len())
        .collect::<Vec<_>>();
    let track_kinds = tracks
        .iter()
        .map(|track| {
            if track.kind.is_empty() {
                "media"
            } else {
                track.kind.as_str()
            }
        })
        .collect::<Vec<_>>()
        .join(" + ");
    let total_segments = track_lengths.iter().sum::<usize>();
    if total_segments == 0 {
        return Err("The manifest did not contain any downloadable segments".into());
    }
    if !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }
    let state = app.state::<CoreState>();
    let (temp_path, max_connections, retry_count, replace_existing, stored_identity) = state
        .snapshot
        .lock()
        .map_err(|_| "State unavailable".to_string())
        .and_then(|snapshot| {
            snapshot
                .jobs
                .iter()
                .find(|job| job.id == id)
                .map(|job| {
                    (
                        job.temp_path.clone(),
                        job.max_connections,
                        if snapshot.settings.retry_automatically {
                            snapshot.settings.max_retries
                        } else {
                            0
                        },
                        snapshot.settings.collision_behavior == "replace",
                        job.segments
                            .as_ref()
                            .and_then(|segments| segments.identity.clone())
                    )
                })
                .ok_or_else(|| "Acquisition no longer exists".to_string())
        })?;
    let segment_dir = PathBuf::from(format!("{temp_path}.segments"));
    let identity = segment_identity(&tracks);
    if stored_identity.as_deref() != Some(identity.as_str()) {
        let _ = tokio::fs::remove_dir_all(&segment_dir).await;
        if !transfer_can_continue(&app, &id, generation) {
            return Ok(());
        }
        cleanup_media_track_files(&temp_path);
    }
    tokio::fs::create_dir_all(&segment_dir)
        .await
        .map_err(|error| error.to_string())?;
    if !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }
    let total_segments = total_segments as u32;
    let concurrency = max_connections.clamp(1, total_segments) as usize;
    let mut existing_segments: Vec<(usize, usize)> = Vec::new();
    let mut existing_bytes = 0u64;
    for (track, length) in track_lengths.iter().enumerate() {
        if track_count > 1 {
            tokio::fs::create_dir_all(segment_dir.join(format!("{track:02}")))
                .await
                .map_err(|error| error.to_string())?;
            if !transfer_can_continue(&app, &id, generation) {
                return Ok(());
            }
        }
        for index in 0..*length {
            let path = manifest_segment_path(&segment_dir, track, index, track_count);
            if let Ok(metadata) = tokio::fs::metadata(path).await {
                if !transfer_can_continue(&app, &id, generation) {
                    return Ok(());
                }
                if metadata.is_file() && metadata.len() > 0 {
                    existing_segments.push((track, index));
                    existing_bytes = existing_bytes.saturating_add(metadata.len());
                }
            }
        }
    }
    let missing_count = total_segments as usize - existing_segments.len();
    let existing_count = existing_segments.len() as u64;
    if !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }
    emit_job(&state, &id, |job| {
        job.state = "downloading".into();
        job.mode = "segments".into();
        job.media = true;
        job.mime = Some(container_mime.to_string());
        job.media_tracks = Some(track_count as u32);
        job.resumable = true;
        job.downloaded = existing_bytes;
        job.progress = existing_segments.len() as f64 / total_segments as f64 * 100.0;
        job.speed = 0;
        job.eta = None;
        job.connections = concurrency.min(missing_count) as u32;
        job.segments = Some(SegmentState {
            completed: existing_segments.len() as u32,
            total: total_segments,
            identity: Some(identity.clone())
        });
        let corrected = manifest_output_name(&job.name, container_ext);
        if corrected != job.name {
            job.name = corrected.clone();
            let mut destination = PathBuf::from(&job.destination);
            destination.set_file_name(&corrected);
            job.destination = destination.to_string_lossy().into_owned();
        }
        job.events.insert(
            0,
            job_event(
                &format!("Manifest parsed: {total_segments} fragments across {track_kinds}"),
                Some("success")
            )
        );
    });
    emit_snapshot(&app, &state);
    let started = std::time::Instant::now();
    let completed = std::sync::Arc::new(AtomicU64::new(existing_segments.len() as u64));
    let downloaded = std::sync::Arc::new(AtomicU64::new(existing_bytes));
    let existing_segments = std::sync::Arc::new(existing_segments);
    let work: Vec<_> = tracks
        .into_iter()
        .enumerate()
        .flat_map(|(track, media_track)| {
            media_track
                .segments
                .into_iter()
                .enumerate()
                .map(move |(index, fragment)| (track, index, fragment))
        })
        .filter(|(track, index, _)| !existing_segments.contains(&(*track, *index)))
        .collect();
    let results = futures_util::stream::iter(work.clone())
        .map(|(track, index, fragment)| {
            let client = client.clone();
            let app = app.clone();
            let id = id.clone();
            let segment_path = manifest_segment_path(&segment_dir, track, index, track_count);
            let segment_temp_path = segment_path.with_extension("part.tmp");
            let completed = completed.clone();
            let downloaded = downloaded.clone();
            async move {
                acquire_media_segment(
                    &client,
                    &app,
                    &id,
                    &fragment,
                    &segment_path,
                    &segment_temp_path,
                    retry_count,
                    generation,
                    &completed,
                    &downloaded,
                    started,
                    existing_bytes,
                    total_segments,
                    existing_count,
                    concurrency
                )
                .await
            }
        })
        .buffer_unordered(concurrency)
        .collect::<Vec<_>>()
        .await;
    let concurrent_error = results
        .iter()
        .find_map(|result| result.as_ref().err().cloned());
    if let Some(initial_error) = concurrent_error {
        if !transfer_is_current(&app, &id, generation) {
            return Ok(());
        }
        if matches!(job_state(&app, &id).as_deref(), Some("paused")) {
            emit_job(&state, &id, |job| {
                job.connections = 0;
                job.speed = 0;
                job.eta = Some("Paused".into());
                job.events.insert(
                    0,
                    job_event("Paused with completed fragments preserved", Some("warning"))
                );
            });
            emit_snapshot(&app, &state);
            return Ok(());
        }
        // Dead segment URLs cannot be revived sequentially either (F05).
        if is_terminal_source_error(&initial_error) {
            emit_job(&state, &id, |job| {
                job.state = "failed".into();
                job.error = Some(initial_error.clone());
                job.connections = 0;
                job.speed = 0;
                job.events.insert(
                    0,
                    job_event("Media fragment acquisition failed", Some("error"))
                );
            });
            emit_snapshot(&app, &state);
            return Err(initial_error);
        }
        emit_job(&state, &id, |job| {
            job.connections = 1;
            job.speed = 0;
            job.eta = Some("Retrying sequentially".into());
            job.events.insert(
                0,
                job_event(
                    "Concurrent fragment acquisition failed; retrying sequentially",
                    Some("warning")
                )
            );
        });
        emit_snapshot(&app, &state);
        let mut sequential_error = None;
        for (track, index, fragment) in &work {
            if !transfer_can_continue(&app, &id, generation) {
                return Ok(());
            }
            let segment_path = manifest_segment_path(&segment_dir, *track, *index, track_count);
            let present = tokio::fs::metadata(&segment_path)
                .await
                .map(|metadata| metadata.is_file() && metadata.len() > 0)
                .unwrap_or(false);
            if present {
                continue;
            }
            let segment_temp_path = segment_path.with_extension("part.tmp");
            if let Err(error) = acquire_media_segment(
                &client,
                &app,
                &id,
                fragment,
                &segment_path,
                &segment_temp_path,
                retry_count.max(1),
                generation,
                &completed,
                &downloaded,
                started,
                existing_bytes,
                total_segments,
                existing_count,
                1
            )
            .await
            {
                sequential_error = Some(error);
                break;
            }
        }
        if let Some(error) = sequential_error {
            let error = format!("{initial_error}; sequential fallback failed: {error}");
            emit_job(&state, &id, |job| {
                job.state = "failed".into();
                job.error = Some(error.clone());
                job.connections = 0;
                job.speed = 0;
                job.events.insert(
                    0,
                    job_event("Media fragment acquisition failed", Some("error"))
                );
            });
            emit_snapshot(&app, &state);
            return Err(error);
        }
    }
    finish_segmented(app.clone(), id.clone(), generation, segment_dir, temp_path, track_lengths, replace_existing).await
}

/// Join a segmented download's part files into track files, mux them when
/// there are several, and publish the result. Shared by a live acquisition
/// and by recovery of one whose fragments were all on disk when the resident
/// stopped (1.10).
async fn finish_segmented(
    app: AppHandle,
    id: String,
    generation: u64,
    segment_dir: PathBuf,
    temp_path: String,
    track_lengths: Vec<usize>,
    replace_existing: bool
) -> Result<(), String> {
    let state = app.state::<CoreState>();
    let track_count = track_lengths.len();
    if !transfer_is_current(&app, &id, generation) {
        return Ok(());
    }
    if job_state(&app, &id).as_deref() != Some("downloading") {
        if job_state(&app, &id).as_deref() == Some("paused") {
            emit_job(&state, &id, |job| {
                job.connections = 0;
                job.speed = 0;
                job.eta = Some("Paused".into());
            });
            emit_snapshot(&app, &state);
        }
        return Ok(());
    }
    emit_job(&state, &id, |job| {
        job.state = "finalizing".into();
        job.total = Some(job.downloaded);
        job.connections = 0;
        job.events.insert(
            0,
            job_event("Assembling ordered media fragments", Some("warning"))
        );
    });
    emit_snapshot(&app, &state);
    let mut track_paths = Vec::with_capacity(track_count);
    for (track, length) in track_lengths.iter().enumerate() {
        if !transfer_can_continue(&app, &id, generation) {
            return Ok(());
        }
        let output_path = if track_count == 1 {
            temp_path.clone()
        } else {
            format!("{temp_path}.track-{track:02}")
        };
        let mut output = File::create(&output_path)
            .await
            .map_err(|error| error.to_string())?;
        if !transfer_can_continue(&app, &id, generation) {
            return Ok(());
        }
        for index in 0..*length {
            let path = manifest_segment_path(&segment_dir, track, index, track_count);
            let mut segment = File::open(&path)
                .await
                .map_err(|error| error.to_string())?;
            if !transfer_can_continue(&app, &id, generation) {
                return Ok(());
            }
            tokio::io::copy(&mut segment, &mut output)
                .await
                .map_err(|error| error.to_string())?;
            if !transfer_can_continue(&app, &id, generation) {
                return Ok(());
            }
        }
        // The muxer reads the track from another thread: every write must
        // have landed before the handle goes.
        output.flush().await.map_err(|error| error.to_string())?;
        drop(output);
        track_paths.push(output_path);
    }
    if !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }
    let committed = state
        .snapshot
        .lock()
        .ok()
        .and_then(|snapshot| {
            snapshot
                .jobs
                .iter()
                .find(|job| job.id == id)
                .map(|job| (job.provisional != Some(true), job.destination.clone()))
        })
        .unwrap_or((false, String::new()));
    // Held until the completion below is recorded (F08).
    let publishing;
    if committed.0 && !committed.1.is_empty() {
        let final_path = if track_count > 1 {
            let mux_path = format!("{temp_path}.mux.{}", media_extension(&committed.1));
            let muxed_path = mux_media_tracks(&track_paths, &mux_path).await?;
            if !transfer_can_continue(&app, &id, generation) {
                return Ok(());
            }
            muxed_path
        } else {
            finalize_media(&temp_path, &committed.1).await?;
            if !transfer_can_continue(&app, &id, generation) {
                return Ok(());
            }
            temp_path.clone()
        };
        let requested_destination = if track_count > 1 {
            destination_with_output_extension(&committed.1, &final_path)
        } else {
            committed.1.clone()
        };
        if let Some(parent) = PathBuf::from(&requested_destination).parent() {
            let _ = tokio::fs::create_dir_all(parent).await;
            if !transfer_can_continue(&app, &id, generation) {
                return Ok(());
            }
        }
        if !transfer_can_continue(&app, &id, generation) {
            return Ok(());
        }
        let (destination, reserved, reservation) =
            managed_destination(&requested_destination, replace_existing)?;
        if destination != committed.1 || reservation.is_some() {
            let reservation_marker = reservation.clone();
            emit_job(&state, &id, |job| {
                job.destination = destination.clone();
                job.destination_reservation = reservation_marker;
                if let Some(file_name) = PathBuf::from(&destination)
                    .file_name()
                    .and_then(|value| value.to_str())
                {
                    if destination != committed.1 {
                        job.name = file_name.to_string();
                    }
                }
                if destination != committed.1 {
                    job.events.insert(
                        0,
                        job_event("Destination renamed to avoid a collision", Some("warning"))
                    );
                }
            });
            emit_snapshot(&app, &state);
        }
        publishing = begin_publishing(&state, &id, || transfer_can_continue(&app, &id, generation));
        if publishing.is_none() {
            if reserved {
                cleanup_reserved_destination(&destination, reservation.as_deref()).await;
            }
            clear_destination_reservation(&app, &state, &id);
            return Ok(());
        }
        if let Err(error) = move_completed_file(
            &final_path,
            &destination,
            replace_existing,
            reservation.as_deref()
        )
        .await
        {
            clear_destination_reservation(&app, &state, &id);
            return Err(error);
        }
        clear_destination_reservation(&app, &state, &id);
        let _ = tokio::fs::remove_dir_all(&segment_dir).await;
        cleanup_media_track_files(&temp_path);
    }
    if !committed.0 && !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }
    emit_job(&state, &id, |job| {
        if committed.0 {
            complete_job(job);
        } else {
            mark_ready_for_confirmation(
                job,
                if track_count > 1 {
                    "Tracks assembled; waiting for destination"
                } else {
                    "Fragments assembled; waiting for destination"
                }
            );
        }
    });
    emit_snapshot(&app, &state);
    if committed.0 {
        add_notification(&app, &state, &id, "completed");
    }
    Ok(())
}

async fn download_track_to_file(
    app: &AppHandle,
    id: &str,
    target_path: &Path,
    generation: u64,
    downloaded_atomic: &std::sync::Arc<AtomicU64>,
    combined_total: Option<u64>,
    started: std::time::Instant,
    response: reqwest::Response,
    expected_kind: Option<&str>,
) -> Result<(), String> {
    let state = app.state::<CoreState>();
    if !response.status().is_success() {
        return Err(format!("track source returned {}", response.status()));
    }
    let expected = partial_response_is_complete(&response)?;
    let track_mime = header_string(&response, reqwest::header::CONTENT_TYPE);
    let track_disposition = header_string(&response, reqwest::header::CONTENT_DISPOSITION);

    let mut stream = response.bytes_stream();
    let mut prefix_chunks = Vec::new();
    let mut prefix_len = 0usize;
    while prefix_len < BODY_SNIFF_LIMIT {
        match stream.next().await {
            Some(Ok(bytes)) => {
                prefix_len = prefix_len.saturating_add(bytes.len());
                let empty = bytes.is_empty();
                prefix_chunks.push(bytes);
                if empty {
                    break;
                }
            }
            Some(Err(error)) => return Err(error.to_string()),
            None => break,
        }
    }
    let mut sniff_prefix = Vec::with_capacity(prefix_len.min(BODY_SNIFF_LIMIT));
    for chunk in &prefix_chunks {
        let remaining = BODY_SNIFF_LIMIT.saturating_sub(sniff_prefix.len());
        if remaining == 0 {
            break;
        }
        sniff_prefix.extend_from_slice(&chunk[..chunk.len().min(remaining)]);
    }
    if expected_kind == Some("video") && body_looks_like_audio(&sniff_prefix) {
        return Err("The source returned audio data for the requested video track".into());
    }
    if response_is_page(track_mime.as_deref(), track_disposition.as_deref(), "", &sniff_prefix) {
        return Err("A media track returned a web page instead of media; the site may need its login session".into());
    }
    let mut stream = futures_util::stream::iter(
        prefix_chunks
            .into_iter()
            .map(Ok::<_, reqwest::Error>)
    )
    .chain(stream);
    let mut file = File::create(target_path).await.map_err(|e| e.to_string())?;
    let mut track_downloaded = 0u64;
    while let Some(chunk) = stream.next().await {
        if !transfer_can_continue(app, id, generation) {
            return Err("paused".into());
        }
        let bytes = chunk.map_err(|e| e.to_string())?;
        file.write_all(&bytes).await.map_err(|e| e.to_string())?;
        track_downloaded = track_downloaded.saturating_add(bytes.len() as u64);
        downloaded_atomic.fetch_add(bytes.len() as u64, Ordering::Relaxed);
        let so_far = downloaded_atomic.load(Ordering::Relaxed);
        let speed = (so_far as f64 / started.elapsed().as_secs_f64().max(0.1)) as u64;
        emit_job(&state, id, |job| {
            job.downloaded = so_far;
            job.speed = speed;
            if let Some(total) = combined_total {
                job.progress = (so_far as f64 / total as f64 * 100.0).min(100.0);
                if speed > 0 {
                    job.eta = Some(format!("{}s left", (total.saturating_sub(so_far) / speed).max(1)));
                }
            }
        });
        emit_progress(app, &state, id);
        if !throttle(app, id, bytes.len(), generation).await {
            return Err("paused".into());
        }
    }
    if expected.is_some_and(|total| track_downloaded != total) {
        return Err(format!("track stream ended after {track_downloaded} bytes; expected {expected:?}"));
    }
    file.sync_all().await.map_err(|e| e.to_string())?;
    Ok(())
}

async fn acquire_dual_track(
    app: AppHandle,
    id: String,
    video_response: reqwest::Response,
    audio_source: String,
    generation: u64,
) -> Result<(), String> {
    if !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }
    let state = app.state::<CoreState>();
    let client = http_client();
    let (temp_path, replace_existing) = state
        .snapshot
        .lock()
        .map_err(|_| "State unavailable".to_string())
        .and_then(|snapshot| {
            snapshot
                .jobs
                .iter()
                .find(|job| job.id == id)
                .map(|job| (job.temp_path.clone(), snapshot.settings.collision_behavior == "replace"))
                .ok_or_else(|| "Acquisition no longer exists".to_string())
        })?;

    if let Some(parent) = PathBuf::from(&temp_path).parent() {
        tokio::fs::create_dir_all(parent).await.map_err(|e| e.to_string())?;
    }

    let video_total = partial_response_is_complete(&video_response)?;
    let audio_response = acquisition_request(&client, &app, &id, &audio_source)
        .send()
        .await
        .map_err(|error| redact_url_credentials(&error.to_string()))?;
    if !audio_response.status().is_success() {
        return Err(format!("audio source returned {}", audio_response.status()));
    }
    if media_mime_kind(header_string(&audio_response, reqwest::header::CONTENT_TYPE).as_deref()) == Some("video") {
        return Err("The companion source returned video data instead of audio".into());
    }
    let audio_total = partial_response_is_complete(&audio_response)?;

    let combined_total = match (video_total, audio_total) {
        (Some(v), Some(a)) => v.checked_add(a),
        _ => None,
    };

    emit_job(&state, &id, |job| {
        job.state = "downloading".into();
        job.mode = "dual-track".into();
        job.media = true;
        job.media_tracks = Some(2);
        job.total = combined_total;
        job.downloaded = 0;
        job.progress = 0.0;
        job.speed = 0;
        job.connections = 2;
        job.events.insert(
            0,
            job_event("Acquiring separate video and audio streams", Some("success")),
        );
    });
    emit_snapshot(&app, &state);

    let started = std::time::Instant::now();
    let downloaded = std::sync::Arc::new(AtomicU64::new(0));

    let track0_path = format!("{temp_path}.track-00");
    let track1_path = format!("{temp_path}.track-01");

    let video_task = download_track_to_file(
        &app,
        &id,
        Path::new(&track0_path),
        generation,
        &downloaded,
        combined_total,
        started,
        video_response,
        Some("video"),
    );

    let audio_task = download_track_to_file(
        &app,
        &id,
        Path::new(&track1_path),
        generation,
        &downloaded,
        combined_total,
        started,
        audio_response,
        Some("audio"),
    );

    futures_util::try_join!(video_task, audio_task)?;

    if !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }

    emit_job(&state, &id, |job| {
        job.state = "finalizing".into();
        job.total = Some(job.downloaded);
        job.connections = 0;
        job.speed = 0;
        job.eta = Some("Assembling media".into());
        job.events.insert(
            0,
            job_event("Muxing video and audio tracks", Some("warning")),
        );
    });
    emit_snapshot(&app, &state);

    let committed = state
        .snapshot
        .lock()
        .ok()
        .and_then(|snapshot| {
            snapshot
                .jobs
                .iter()
                .find(|job| job.id == id)
                .map(|job| (job.provisional != Some(true), job.destination.clone()))
        })
        .unwrap_or((false, String::new()));

    // Held until the completion below is recorded (F08).
    let publishing;
    if committed.0 && !committed.1.is_empty() {
        let track_paths = vec![track0_path, track1_path];
        let mux_path = format!("{temp_path}.mux.{}", media_extension(&committed.1));
        let muxed_path = mux_media_tracks(&track_paths, &mux_path).await?;
        if !transfer_can_continue(&app, &id, generation) {
            return Ok(());
        }
        let requested_destination = destination_with_output_extension(&committed.1, &muxed_path);
        if let Some(parent) = PathBuf::from(&requested_destination).parent() {
            let _ = tokio::fs::create_dir_all(parent).await;
            if !transfer_can_continue(&app, &id, generation) {
                return Ok(());
            }
        }
        let (destination, reserved, reservation) =
            managed_destination(&requested_destination, replace_existing)?;
        if destination != committed.1 || reservation.is_some() {
            let reservation_marker = reservation.clone();
            emit_job(&state, &id, |job| {
                job.destination = destination.clone();
                job.destination_reservation = reservation_marker;
                if let Some(file_name) = PathBuf::from(&destination)
                    .file_name()
                    .and_then(|value| value.to_str())
                {
                    if destination != committed.1 {
                        job.name = file_name.to_string();
                    }
                }
                if destination != committed.1 {
                    job.events.insert(
                        0,
                        job_event("Destination renamed to avoid a collision", Some("warning")),
                    );
                }
            });
            emit_snapshot(&app, &state);
        }
        publishing = begin_publishing(&state, &id, || transfer_can_continue(&app, &id, generation));
        if publishing.is_none() {
            if reserved {
                cleanup_reserved_destination(&destination, reservation.as_deref()).await;
            }
            clear_destination_reservation(&app, &state, &id);
            return Ok(());
        }
        if let Err(error) = move_completed_file(
            &muxed_path,
            &destination,
            replace_existing,
            reservation.as_deref(),
        )
        .await
        {
            clear_destination_reservation(&app, &state, &id);
            return Err(error);
        }
        clear_destination_reservation(&app, &state, &id);
        cleanup_media_track_files(&temp_path);
    }

    if !committed.0 && !transfer_can_continue(&app, &id, generation) {
        return Ok(());
    }

    emit_job(&state, &id, |job| {
        if committed.0 {
            complete_job(job);
        } else {
            mark_ready_for_confirmation(job, "Tracks downloaded; waiting for destination");
        }
    });
    emit_snapshot(&app, &state);
    if committed.0 {
        add_notification(&app, &state, &id, "completed");
    }
    Ok(())
}

async fn acquire_once(app: AppHandle, id: String, source: String, generation: u64) -> bool {
    let state = app.state::<CoreState>();
    let client = http_client();
    let selected_segments = state
        .snapshot
        .lock()
        .ok()
        .and_then(|snapshot| {
            snapshot
                .jobs
                .iter()
                .find(|job| job.id == id)
                .map(|job| job.selected_segments.clone())
        })
        .unwrap_or_default();
    // A present body means the browser POSTed (the extension only records
    // bodies it observed). That POST is the request, and its outcome is final:
    // falling back to GET asks for something else and used to complete jobs
    // with a landing page, and replaying it again can repeat the submission's
    // side effects, so a failed POST is never retried automatically. A GET is
    // sent once here; the caller's retry loop owns retries.
    let is_post = state
        .snapshot
        .lock()
        .ok()
        .and_then(|snapshot| {
            snapshot
                .jobs
                .iter()
                .find(|job| job.id == id)
                .map(|job| job.post_body.is_some())
        })
        .unwrap_or(false);
    let request = if is_post {
        post_replay_request(&client, &app, &id, &source)
    } else {
        Some(acquisition_request(&client, &app, &id, &source))
    };
    let sent = match request {
        Some(request) => request.send().await.map_err(|error| error.to_string()),
        None => Err("The form submission for this download is no longer available".into()),
    };
    let response = match sent {
        Ok(response) if response.status().is_success() => response,
        outcome => {
            if !transfer_can_continue(&app, &id, generation) {
                return false;
            }
            let (error, event, retryable) = match outcome {
                Ok(response) => (
                    format!("Source returned {}", response.status()),
                    if is_post { "Source rejected the form submission" } else { "Source rejected the acquisition" },
                    !is_post && retryable_status(response.status()),
                ),
                Err(error) => (error, "Could not connect to source", !is_post),
            };
            mark_acquisition_failed(&app, &state, &id, error, event);
            return retryable;
        }
    };
    if !transfer_can_continue(&app, &id, generation) {
        return false;
    }
    let manifest_source = response.url().to_string();
    let response_mime = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .map(str::to_string);
    let page_disposition = response
        .headers()
        .get(reqwest::header::CONTENT_DISPOSITION)
        .and_then(|value| value.to_str().ok())
        .map(str::to_string);
    let (expected_kind, companion_audio_url, job_name) = state
        .snapshot
        .lock()
        .ok()
        .and_then(|snapshot| {
            snapshot.jobs.iter().find(|job| job.id == id).map(|job| (
                job.media.then(|| job.player_kind.clone().unwrap_or_else(|| "video".into())),
                job.companion_audio.clone(),
                job.name.clone(),
            ))
        })
        .unwrap_or((None, None, String::new()));
    let total = match partial_response_is_complete(&response) {
        Ok(total) => total,
        Err(error) => {
            if transfer_can_continue(&app, &id, generation) {
                mark_acquisition_failed(&app, &state, &id, error, "Source returned an incomplete object");
            }
            return false;
        }
    };
    if mime_conflicts_player_kind(expected_kind.as_deref(), response_mime.as_deref()) {
        mark_acquisition_failed(
            &app,
            &state,
            &id,
            format!("The source MIME type is not compatible with the expected {} track", expected_kind.as_deref().unwrap_or("media")),
            "Source media type did not match the requested player track",
        );
        return false;
    }
    if page_instead_of_file(response_mime.as_deref(), page_disposition.as_deref(), &job_name) {
        mark_acquisition_failed(
            &app,
            &state,
            &id,
            "The source returned a web page instead of a file; the site may need its login session".into(),
            "Source returned a web page instead of a file",
        );
        return false;
    }
    let known_manifest = media::is_manifest_source(&manifest_source, response_mime.as_deref())
        || manifest_mime(response_mime.as_deref());
    if known_manifest {
        report_viability(&app, &id, Ok(()));
        let body = manifest_text(response).await;
        let result = match body {
            Ok(body) => acquire_manifest(
                app.clone(),
                id.clone(),
                manifest_source.clone(),
                body,
                response_mime.clone(),
                expected_kind.clone(),
                selected_segments.clone(),
                generation,
            )
            .await,
            Err(error) => Err(error),
        };
        if let Err(error) = result {
            if transfer_is_current(&app, &id, generation) {
                if job_state(&app, &id).as_deref() == Some("paused") {
                    emit_job(&state, &id, |job| {
                        job.connections = 0;
                        job.speed = 0;
                        job.eta = Some("Paused".into());
                    });
                    emit_snapshot(&app, &state);
                } else if job_state(&app, &id).as_deref() != Some("failed") {
                    mark_acquisition_failed(&app, &state, &id, error, "Manifest acquisition failed");
                }
            }
        }
        return false;
    }
    if let Some(audio_source) = companion_audio_url {
        report_viability(&app, &id, Ok(()));
        if expected_kind.as_deref() != Some("video") {
            mark_acquisition_failed(&app, &state, &id, "Companion audio requires a video source".into(), "Invalid companion audio metadata");
            return false;
        }
        if let Err(error) = acquire_dual_track(
            app.clone(),
            id.clone(),
            response,
            audio_source,
            generation,
        )
        .await
        {
            if transfer_is_current(&app, &id, generation) {
                if job_state(&app, &id).as_deref() == Some("paused") {
                    emit_job(&state, &id, |job| {
                        job.connections = 0;
                        job.speed = 0;
                        job.eta = Some("Paused".into());
                    });
                    emit_snapshot(&app, &state);
                } else if job_state(&app, &id).as_deref() != Some("failed") {
                    mark_acquisition_failed(&app, &state, &id, error, "Dual-track media acquisition failed");
                }
            }
        }
        return false;
    }
    // Ranged workers re-request the source with GET, which a POST response
    // cannot be rebuilt from: a form capture stays one stream.
    let safe_ranges = !is_post && supports_safe_initial_ranges(&response, total, response_mime.as_deref());
    let mut stream = response.bytes_stream();
    // Keep a bounded prefix together while sniffing so a manifest marker
    // split across response chunks is still recognized without losing bytes.
    let mut prefix_chunks = Vec::new();
    let mut prefix_len = 0usize;
    while prefix_len < BODY_SNIFF_LIMIT {
        match stream.next().await {
            Some(Ok(bytes)) => {
                prefix_len = prefix_len.saturating_add(bytes.len());
                let empty = bytes.is_empty();
                prefix_chunks.push(bytes);
                if empty {
                    break;
                }
            }
            Some(Err(error)) => {
                mark_acquisition_failed(&app, &state, &id, error.to_string(), "Network stream interrupted");
                return false;
            }
            None => break,
        }
    }
    let mut sniff_prefix = Vec::with_capacity(prefix_len.min(BODY_SNIFF_LIMIT));
    for chunk in &prefix_chunks {
        let remaining = BODY_SNIFF_LIMIT.saturating_sub(sniff_prefix.len());
        if remaining == 0 {
            break;
        }
        sniff_prefix.extend_from_slice(&chunk[..chunk.len().min(remaining)]);
    }
    if media::is_manifest_body(&sniff_prefix) {
        report_viability(&app, &id, Ok(()));
        let mut body = prefix_chunks
            .iter()
            .flat_map(|chunk| chunk.iter().copied())
            .collect::<Vec<_>>();
        while let Some(chunk) = stream.next().await {
            match chunk {
                Ok(bytes) => body.extend_from_slice(&bytes),
                Err(error) => {
                    mark_acquisition_failed(&app, &state, &id, error.to_string(), "Manifest stream interrupted");
                    return false;
                }
            }
        }
        let result = acquire_manifest(
            app.clone(),
            id.clone(),
            manifest_source,
            String::from_utf8_lossy(&body).into_owned(),
            response_mime.clone(),
            expected_kind.clone(),
            selected_segments.clone(),
            generation,
        )
        .await;
        if let Err(error) = result {
            if transfer_is_current(&app, &id, generation) {
                mark_acquisition_failed(&app, &state, &id, error, "Manifest acquisition failed");
            }
        }
        return false;
    }
    if response_is_page(response_mime.as_deref(), page_disposition.as_deref(), &job_name, &sniff_prefix) {
        mark_acquisition_failed(
            &app,
            &state,
            &id,
            "The source returned a web page instead of a file; the site may need its login session".into(),
            "Source returned a web page instead of a file",
        );
        return false;
    }
    if expected_kind.as_deref() == Some("video") && body_looks_like_audio(&sniff_prefix) {
        mark_acquisition_failed(
            &app,
            &state,
            &id,
            "The source returned audio data for the requested video track".into(),
            "Source media bytes did not match the requested video track",
        );
        return false;
    }
    adopt_response_name(&app, &state, &id, page_disposition.as_deref());
    // Headers and first bytes say this is the file: the browser may let go.
    report_viability(&app, &id, Ok(()));
    if safe_ranges {
        let probe_end = total
            .expect("safe range response has a total")
            .min(1024 * 1024)
            .saturating_sub(1);
        let probe = acquisition_request(&client, &app, &id, &manifest_source)
            .header(reqwest::header::RANGE, format!("bytes=0-{probe_end}"))
            .send()
            .await;
        let valid_probe = match probe {
            Ok(probe) if probe.status() == reqwest::StatusCode::PARTIAL_CONTENT => {
                content_range(&probe)
                    .map(|(start, end, advertised)| {
                        start == 0
                            && end == probe_end
                            && advertised == total.expect("safe range response has a total")
                            && probe.content_length() == Some(probe_end + 1)
                    })
                    .unwrap_or(false)
                    .then_some(probe)
            }
            _ => None,
        };
        if let Some(probe) = valid_probe {
            drop(stream);
            if let Err(error) = acquire_ranges(
                app.clone(),
                id.clone(),
                source.clone(),
                probe,
                total.expect("safe range response has a total"),
                generation,
            )
            .await
            {
                if transfer_is_current(&app, &id, generation) {
                    if job_state(&app, &id).as_deref() == Some("paused") {
                        emit_job(&state, &id, |job| {
                            job.connections = 0;
                            job.speed = 0;
                            job.eta = Some("Paused".into());
                        });
                        emit_snapshot(&app, &state);
                    } else if job_state(&app, &id).as_deref() != Some("failed") {
                        mark_acquisition_failed(&app, &state, &id, error, "Range acquisition failed");
                    }
                }
            }
            return false;
        }
    }
    let temp_path = state.snapshot.lock().ok().and_then(|snapshot| {
        snapshot
            .jobs
            .iter()
            .find(|job| job.id == id)
            .map(|job| job.temp_path.clone())
    });
    let Some(temp_path) = temp_path else {
        return false;
    };
    if let Some(parent) = PathBuf::from(&temp_path).parent() {
        if let Err(error) = tokio::fs::create_dir_all(parent).await {
            if !transfer_can_continue(&app, &id, generation) {
                return false;
            }
            emit_job(&state, &id, |job| {
                job.state = "failed".into();
                job.error = Some(format!("Could not create the temporary folder: {error}"));
                job.events.insert(
                    0,
                    job_event("Could not create the temporary folder", Some("error"))
                );
            });
            emit_snapshot(&app, &state);
            add_notification(&app, &state, &id, "failed");
            return false;
        }
    }
    if !transfer_can_continue(&app, &id, generation) {
        return false;
    }
    let Ok(mut file) = File::create(&temp_path).await else {
        if !transfer_can_continue(&app, &id, generation) {
            return false;
        }
        emit_job(&state, &id, |job| {
            job.state = "failed".into();
            job.error = Some("Could not open the temporary file".into());
            job.events.insert(
                0,
                job_event("Could not open the temporary file", Some("error"))
            );
        });
        emit_snapshot(&app, &state);
        add_notification(&app, &state, &id, "failed");
        return false;
    };
    if !transfer_can_continue(&app, &id, generation) {
        return false;
    }
    emit_job(&state, &id, |job| {
        job.state = "downloading".into();
        job.total = total;
        job.resumable = false;
        job.connections = 1;
        job.mode = "single-stream".into();
        job.mime = response_mime.clone();
        job.events.insert(
            0,
            job_event(
                "First native acquisition is receiving data",
                Some("success")
            )
        );
    });
    emit_snapshot(&app, &state);
    let started = std::time::Instant::now();
    let mut downloaded = 0u64;
    let mut stream = futures_util::stream::iter(
        prefix_chunks
            .into_iter()
            .map(Ok::<_, reqwest::Error>)
    )
        .chain(stream);
    while let Some(chunk) = stream.next().await {
        if !transfer_is_downloading(&app, &id, generation) {
            drop(stream);
            drop(file);
            return false;
        }
        match chunk {
            Ok(bytes) => {
                if let Err(error) = file.write_all(&bytes).await {
                    if !transfer_can_continue(&app, &id, generation) {
                        return false;
                    }
                    emit_job(&state, &id, |job| {
                        job.state = "failed".into();
                        job.error = Some(redact_url_credentials(&error.to_string()));
                        job.speed = 0;
                        job.connections = 0;
                        job.events.insert(
                            0,
                            job_event("Could not write temporary data", Some("error"))
                        );
                    });
                    emit_snapshot(&app, &state);
                    add_notification(&app, &state, &id, "failed");
                    return false;
                }
                if !transfer_can_continue(&app, &id, generation) {
                    return false;
                }
                downloaded += bytes.len() as u64;
                let speed = (downloaded as f64 / started.elapsed().as_secs_f64().max(0.1)) as u64;
                emit_job(&state, &id, |job| {
                    job.downloaded = downloaded;
                    job.speed = speed;
                    job.progress = total
                        .map(|value| downloaded as f64 / value as f64 * 100.0)
                        .unwrap_or(0.0);
                    job.eta = total.and_then(|value| {
                        if speed > 0 {
                            Some(format!(
                                "{}s left",
                                (value.saturating_sub(downloaded) / speed).max(1)
                            ))
                        } else {
                            None
                        }
                    });
                });
                emit_progress(&app, &state, &id);
                if !throttle(&app, &id, bytes.len(), generation).await {
                    drop(stream);
                    drop(file);
                    return false;
                }
            }
            Err(error) => {
                if !transfer_can_continue(&app, &id, generation) {
                    return false;
                }
                emit_job(&state, &id, |job| {
                    job.state = "failed".into();
                    job.error = Some(redact_url_credentials(&error.to_string()));
                    job.speed = 0;
                    job.connections = 0;
                    job.events
                        .insert(0, job_event("Network stream interrupted", Some("error")));
                });
                emit_snapshot(&app, &state);
                add_notification(&app, &state, &id, "failed");
                return true;
            }
        }
    }
    drop(file);
    if total.is_some_and(|expected| downloaded != expected) {
        mark_acquisition_failed(
            &app,
            &state,
            &id,
            format!("The source ended after {downloaded} bytes; expected {total:?}"),
            "Network stream ended before the complete object",
        );
        return false;
    }
    if !transfer_can_continue(&app, &id, generation) {
        return false;
    }
    let replace_existing = state
        .snapshot
        .lock()
        .ok()
        .map(|snapshot| snapshot.settings.collision_behavior == "replace")
        .unwrap_or(false);
    let committed = state
        .snapshot
        .lock()
        .ok()
        .and_then(|snapshot| {
            snapshot
                .jobs
                .iter()
                .find(|job| job.id == id)
                .map(|job| (job.provisional != Some(true), job.destination.clone()))
        })
        .unwrap_or((false, String::new()));
    // Held until the completion below is recorded (F08).
    let publishing;
    if committed.0 && !committed.1.is_empty() {
        if let Some(parent) = PathBuf::from(&committed.1).parent() {
            let _ = std::fs::create_dir_all(parent);
            if !transfer_can_continue(&app, &id, generation) {
                return false;
            }
        }
        if !transfer_can_continue(&app, &id, generation) {
            return false;
        }
        let (destination, reserved, reservation) =
            match managed_destination(&committed.1, replace_existing) {
                Ok(value) => value,
                Err(error) => {
                    emit_job(&state, &id, |job| {
                        job.state = "failed".into();
                        job.error = Some(error.clone());
                        job.events.insert(
                            0,
                            job_event("Could not reserve a unique destination", Some("error"))
                        );
                    });
                    emit_snapshot(&app, &state);
                    add_notification(&app, &state, &id, "failed");
                    return false;
                }
            };
        if destination != committed.1 || reservation.is_some() {
            let reservation_marker = reservation.clone();
            emit_job(&state, &id, |job| {
                job.destination = destination.clone();
                job.destination_reservation = reservation_marker;
                if let Some(file_name) = PathBuf::from(&destination)
                    .file_name()
                    .and_then(|value| value.to_str())
                {
                    if destination != committed.1 {
                        job.name = file_name.to_string();
                    }
                }
                if destination != committed.1 {
                    job.events.insert(
                        0,
                        job_event("Destination renamed to avoid a collision", Some("warning"))
                    );
                }
            });
            emit_snapshot(&app, &state);
        }
        publishing = begin_publishing(&state, &id, || transfer_can_continue(&app, &id, generation));
        if publishing.is_none() {
            if reserved {
                cleanup_reserved_destination(&destination, reservation.as_deref()).await;
            }
            clear_destination_reservation(&app, &state, &id);
            return false;
        }
        if let Err(error) = move_completed_file(
            &temp_path,
            &destination,
            replace_existing,
            reservation.as_deref()
        )
        .await
        {
            clear_destination_reservation(&app, &state, &id);
            if !transfer_can_continue(&app, &id, generation) {
                return false;
            }
            emit_job(&state, &id, |job| {
                job.state = "failed".into();
                job.error = Some(redact_url_credentials(&error.to_string()));
                job.events.insert(
                    0,
                    job_event("Could not move the completed file", Some("error"))
                );
            });
            emit_snapshot(&app, &state);
            add_notification(&app, &state, &id, "failed");
            return false;
        }
        clear_destination_reservation(&app, &state, &id);
    }
    if !committed.0 && !transfer_can_continue(&app, &id, generation) {
        return false;
    }
    emit_job(&state, &id, |job| {
        if committed.0 {
            complete_job(job);
        } else {
            mark_ready_for_confirmation(job, "Download ready; waiting for destination");
        }
    });
    emit_snapshot(&app, &state);
    if committed.0 {
        add_notification(&app, &state, &id, "completed");
    }
    false
}

/// How many capture sources may be probed. Every probe is a real request, so the
/// bound is what keeps a capture the page could not resolve cheap.
const MAX_SOURCE_PROBES: usize = 4;
/// Prefix size for classification: enough for a manifest, a container header,
/// and the content type that arrives with the first chunk.
const SOURCE_PROBE_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CandidateVerdict {
    Media,
    NotMedia,
    Unknown,
}

/// URL-level evidence that a source is media, cheap enough to skip a probe.
fn url_implies_media(url: &str) -> bool {
    if media::is_manifest_source(url, None) {
        return true;
    }
    const MEDIA_EXTENSIONS: [&str; 14] = [
        ".mp4", ".m4s", ".m4a", ".m4v", ".ts", ".webm", ".mkv", ".mov", ".mp3", ".aac", ".ogg",
        ".oga", ".opus", ".flac",
    ];
    let path = reqwest::Url::parse(url)
        .map(|parsed| parsed.path().to_ascii_lowercase())
        .unwrap_or_default();
    MEDIA_EXTENSIONS
        .iter()
        .any(|extension| path.ends_with(extension))
}

/// A URL whose bytes are one signed slice of an object: a `/range/` path segment
/// (Vimeo's `/v2/range/prot/<b64>/…`) or a `range=` parameter the signature
/// covers (`sparams` lists it, as YouTube's URLs do).
///
/// Such a URL can never be acquired as a file: the slice is a few kilobytes and
/// the signature expires within seconds (measured live — the same Vimeo
/// fragment answered 206 at +30 ms and 403 at +4.4 s; YouTube captures built on
/// ranged URLs produced 62 B–62 KB "downloads"). The job must rebuild from a
/// manifest or fail honestly; downloading a slice and calling it a file is the
/// one outcome that is never acceptable.
const FRAGMENT_SOURCE_ERROR: &str = "The observed media URL is a single byte-range fragment: its signature expires within seconds and its bytes are not a file. No manifest was captured for this player — reopen it (or play it briefly) and capture again.";

fn url_is_byte_range_fragment(url: &str) -> bool {
    let Ok(parsed) = reqwest::Url::parse(url) else {
        return false;
    };
    if parsed
        .path()
        .to_ascii_lowercase()
        .split('/')
        .any(|segment| segment == "range")
    {
        return true;
    }
    let mut has_range = false;
    let mut signed = false;
    for (name, value) in parsed.query_pairs() {
        if name.eq_ignore_ascii_case("range") {
            has_range = true;
        } else if name.eq_ignore_ascii_case("sparams")
            && value.split(',').any(|item| item.trim().eq_ignore_ascii_case("range"))
        {
            signed = true;
        }
    }
    has_range && signed
}

/// Classify one candidate source by what it actually returns. A page that could
/// not tell which URL feeds its player usually still observed several plausible
/// ones; this is what turns that list into one answer.
async fn classify_candidate(
    client: &reqwest::Client,
    url: &str,
    referrer: Option<&str>,
    user_agent: Option<&str>,
) -> CandidateVerdict {
    if url_implies_media(url) {
        return CandidateVerdict::Media;
    }
    let Ok(parsed) = reqwest::Url::parse(url) else {
        return CandidateVerdict::NotMedia;
    };
    let mut request = client
        .get(parsed)
        .header(
            reqwest::header::RANGE,
            format!("bytes=0-{}", SOURCE_PROBE_BYTES - 1),
        )
        .timeout(Duration::from_secs(8));
    if let Some(agent) = user_agent {
        request = request.header(reqwest::header::USER_AGENT, agent);
    }
    if let Some(value) = referrer.and_then(|value| referer_value(value, url)) {
        request = request.header(reqwest::header::REFERER, value);
    }
    let Ok(response) = request.send().await else {
        return CandidateVerdict::Unknown;
    };
    if !response.status().is_success() {
        return CandidateVerdict::NotMedia;
    }
    let content_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    let mut body = Vec::new();
    let mut stream = response.bytes_stream();
    while body.len() < 8192 {
        let Some(chunk) = stream.next().await else { break };
        let Ok(chunk) = chunk else { break };
        let take = chunk.len().min(8192 - body.len());
        body.extend_from_slice(&chunk[..take]);
    }
    if media::is_manifest_body(&body) {
        return CandidateVerdict::Media;
    }
    if content_type.starts_with("video/") || content_type.starts_with("audio/") {
        return CandidateVerdict::Media;
    }
    if content_type.is_empty() || content_type == "application/octet-stream" {
        // Neither the URL nor the response says what this is. Not evidence.
        return CandidateVerdict::Unknown;
    }
    CandidateVerdict::NotMedia
}

/// A capture can hand over alternates when the page could not prove which
/// resource feeds the player (SPEC §6.2): blob/MSE players whose bytes arrive
/// from a realm nothing can instrument. Deciding is the resident's job, because
/// deciding means fetching. The first source that behaves like finite media
/// becomes the job's source, the job's log records the swap, and the alternates
/// are consumed either way so nothing lingers in the store.
async fn resolve_job_source(app: &AppHandle, id: &str, source: String) -> Result<String, String> {
    let candidates = app
        .state::<CoreState>()
        .snapshot
        .lock()
        .ok()
        .and_then(|snapshot| {
            snapshot
                .jobs
                .iter()
                .find(|job| job.id == id)
                .map(|job| job.candidates.clone())
        })
        .unwrap_or_default();
    // A signed byte-range URL cannot become a file, whatever it looks like: its
    // bytes are a slice and its signature dies within seconds. Only a manifest
    // (or a non-fragment alternate) may be acquired.
    let fragment_source =
        url_is_byte_range_fragment(&source) && !media::is_manifest_source(&source, None);
    if candidates.is_empty() {
        return if fragment_source {
            Err(FRAGMENT_SOURCE_ERROR.into())
        } else {
            Ok(source)
        };
    }
    let (referrer, _, user_agent) = job_context(app, id);
    let mut chosen = source.clone();
    if !url_implies_media(&source) || fragment_source {
        let client = http_client();
        let pool = std::iter::once(source.clone()).chain(candidates.iter().cloned());
        for url in pool.take(MAX_SOURCE_PROBES) {
            // A fragment is never a verdict, only a hint that something else
            // must exist; skip it and keep probing.
            if url_is_byte_range_fragment(&url) && !media::is_manifest_source(&url, None) {
                continue;
            }
            if classify_candidate(&client, &url, referrer.as_deref(), user_agent.as_deref()).await
                == CandidateVerdict::Media
            {
                chosen = url;
                break;
            }
        }
    }
    if chosen == source && fragment_source {
        return Err(FRAGMENT_SOURCE_ERROR.into());
    }
    let state = app.state::<CoreState>();
    emit_job(&state, id, |job| {
        job.candidates.clear();
        if job.source != chosen {
            job.domain = domain(&chosen);
            job.source = chosen.clone();
            job.events.insert(
                0,
                job_event(
                    "Capture alternates checked; acquiring the verified source",
                    Some("success"),
                ),
            );
        }
    });
    emit_snapshot(app, &state);
    Ok(chosen)
}

/// Per-track fragment counts of a segmented download whose part files are
/// all present: `NNNNNNNN.part` files directly in the directory for one
/// track, or in `00`, `01`, ... for several. None unless they add up to the
/// expected total with no gap or empty part.
fn complete_segment_layout(segment_dir: &Path, expected_total: u32) -> Option<Vec<usize>> {
    fn contiguous(directory: &Path) -> usize {
        let mut count = 0;
        while std::fs::metadata(directory.join(format!("{count:08}.part"))).is_ok_and(|meta| meta.is_file() && meta.len() > 0) {
            count += 1;
        }
        count
    }
    let single = contiguous(segment_dir);
    let lengths = if single > 0 {
        vec![single]
    } else {
        let mut lengths = Vec::new();
        loop {
            let count = contiguous(&segment_dir.join(format!("{:02}", lengths.len())));
            if count == 0 {
                break;
            }
            lengths.push(count);
        }
        if lengths.len() < 2 {
            return None;
        }
        lengths
    };
    (expected_total > 0 && lengths.iter().sum::<usize>() == expected_total as usize).then_some(lengths)
}

/// A committed segmented job that stopped while finalizing already had every
/// fragment on disk: finish it from them rather than asking the source again,
/// which may have expired since (1.10). Returns false when the job is not in
/// that position and must be acquired normally.
async fn finish_from_disk(app: &AppHandle, id: &str, generation: u64) -> bool {
    let state = app.state::<CoreState>();
    let found = state.snapshot.lock().ok().and_then(|snapshot| {
        let replace_existing = snapshot.settings.collision_behavior == "replace";
        snapshot.jobs.iter().find(|job| job.id == id).and_then(|job| {
            let segments = job.segments.as_ref()?;
            (job.state == "finalizing" && job.provisional != Some(true) && segments.completed == segments.total)
                .then(|| (job.temp_path.clone(), segments.total, replace_existing))
        })
    });
    let Some((temp_path, total, replace_existing)) = found else { return false; };
    let segment_dir = PathBuf::from(format!("{temp_path}.segments"));
    let Some(track_lengths) = complete_segment_layout(&segment_dir, total) else { return false; };
    emit_job(&state, id, |job| {
        job.state = "downloading".into();
        job.events.insert(0, job_event("Resuming assembly from the fragments already downloaded", Some("warning")));
    });
    emit_snapshot(app, &state);
    if let Err(error) = finish_segmented(app.clone(), id.to_string(), generation, segment_dir, temp_path, track_lengths, replace_existing).await {
        if transfer_is_current(app, id, generation) && job_state(app, id).as_deref() != Some("failed") {
            mark_acquisition_failed(app, &state, id, error, "Media finalization failed");
        }
    }
    true
}

async fn acquire(app: AppHandle, id: String, source: String, generation: u64) {
    if !transfer_can_continue(&app, &id, generation) {
        return;
    }
    if finish_from_disk(&app, &id, generation).await {
        return;
    }
    let source = match resolve_job_source(&app, &id, source).await {
        Ok(source) => source,
        Err(error) => {
            if transfer_can_continue(&app, &id, generation) {
                let state = app.state::<CoreState>();
                mark_acquisition_failed(
                    &app,
                    state.inner(),
                    &id,
                    error,
                    "Source was a byte-range fragment, not a file",
                );
            }
            return;
        }
    };
    if !transfer_can_continue(&app, &id, generation) {
        return;
    }
    let (automatic, retries) = {
        let state = app.state::<CoreState>();
        state
            .snapshot
            .lock()
            .ok()
            .map(|snapshot| {
                (
                    snapshot.settings.retry_automatically,
                    snapshot.settings.max_retries
                )
            })
            .unwrap_or((false, 0))
    };
    for attempt in 0..=retries {
        if attempt == 0 && !transfer_can_continue(&app, &id, generation) {
            return;
        }
        if attempt > 0 {
            let state = app.state::<CoreState>();
            if !transfer_is_current(&app, &id, generation)
                || job_state(&app, &id).as_deref() != Some("failed")
            {
                return;
            }
            emit_job(&state, &id, |job| {
                job.state = "connecting".into();
                job.error = None;
                job.connections = 0;
                job.events.insert(
                    0,
                    job_event(
                        &format!("Automatic retry {attempt} of {retries}"),
                        Some("warning")
                    )
                );
            });
            emit_snapshot(&app, &state);
            sleep(Duration::from_millis((attempt as u64 * 500).min(5000))).await;
            if !transfer_can_continue(&app, &id, generation) {
                return;
            }
        }
        let retryable = acquire_once(app.clone(), id.clone(), source.clone(), generation).await;
        report_viability_failure(&app, &id);
        if !transfer_is_current(&app, &id, generation)
            || !automatic
            || !retryable
            || job_state(&app, &id).as_deref() != Some("failed")
            || attempt == retries
        {
            return;
        }
    }
}

#[tauri::command]
fn get_snapshot(state: State<'_, CoreState>) -> AppSnapshot {
    state
        .snapshot
        .lock()
        .map(|snapshot| snapshot.clone())
        .unwrap_or_else(|_| AppSnapshot {
            jobs: vec![],
            settings: default_settings(),
            connected: false,
            aggregate_speed: 0,
            notifications: vec![],
            bridge_available: false,
        })
}

#[tauri::command]
fn open_path(path: String) -> Result<(), String> {
    let path = path.trim();
    if path.is_empty() {
        return Err("Path is empty".into());
    }
    #[cfg(windows)]
    {
        use std::{
            ffi::{c_void, OsStr},
            os::windows::ffi::OsStrExt,
            ptr,
        };
        fn wide(value: &OsStr) -> Vec<u16> {
            value.encode_wide().chain(std::iter::once(0)).collect()
        }
        #[link(name = "shell32")]
        extern "system" {
            fn ShellExecuteW(
                hwnd: *mut c_void,
                operation: *const u16,
                file: *const u16,
                parameters: *const u16,
                directory: *const u16,
                show: i32,
            ) -> isize;
        }
        let operation = wide(OsStr::new("open"));
        let target = wide(OsStr::new(path));
        let result = unsafe {
            ShellExecuteW(
                ptr::null_mut(),
                operation.as_ptr(),
                target.as_ptr(),
                ptr::null(),
                ptr::null(),
                1,
            )
        };
        if result > 32 {
            Ok(())
        } else {
            Err(format!(
                "Windows could not open the path (ShellExecuteW code {result})"
            ))
        }
    }
    #[cfg(not(windows))]
    {
        Err("Opening paths is only available in the Windows desktop build".into())
    }
}

#[tauri::command]
fn pause_job(app: AppHandle, state: State<'_, CoreState>, id: String) {
    let _lifecycle = state.lifecycle.lock().ok();
    let Ok(publishing) = state.publishing.lock() else { return };
    if decline_while_publishing(state.inner(), &publishing, &id, "Pause") {
        drop(publishing);
        emit_snapshot(&app, &state);
        return;
    }
    let mut paused = false;
    emit_job(&state, &id, |job| paused = pause_in_place(job, "Paused by user"));
    if paused {
        abort_transfer(state.inner(), &id);
    }
    emit_snapshot(&app, &state);
}

fn pause_in_place(job: &mut DownloadJob, event: &str) -> bool {
    if !PAUSABLE_STATES.contains(&job.state.as_str()) {
        return false;
    }
    job.state = "paused".into();
    job.speed = 0;
    job.connections = 0;
    job.eta = Some("Paused".into());
    job.events.insert(0, job_event(event, Some("warning")));
    true
}

/// Pause every running transfer. Shared by the manager's Pause All and the
/// tray's, like `resume_all_jobs`.
fn pause_all_jobs(app: &AppHandle, event: &str) {
    let state = app.state::<CoreState>();
    let _lifecycle = state.lifecycle.lock().ok();
    let Ok(publishing) = state.publishing.lock() else { return };
    let mut ids = Vec::new();
    if let Ok(mut snapshot) = state.snapshot.lock() {
        for job in snapshot.jobs.iter_mut() {
            if !publishing.contains(&job.id) && pause_in_place(job, event) {
                ids.push(job.id.clone());
            }
        }
    }
    drop(publishing);
    for id in ids {
        abort_transfer(state.inner(), &id);
    }
    emit_snapshot(app, &state);
}

fn resume_all_jobs(app: &AppHandle, event: &str) {
    let state = app.state::<CoreState>();
    let _lifecycle = state.lifecycle.lock().ok();
    let sources = state
        .snapshot
        .lock()
        .ok()
        .map(|mut snapshot| plan_resume_all(&mut snapshot, event, |id| transfer_is_active(state.inner(), id)))
        .unwrap_or_default();
    emit_snapshot(app, &state);
    for (id, source) in sources {
        let _ = spawn_transfer(app, state.inner(), id, source);
    }
}

/// Flip every paused/pending job back to transferring and return the
/// (id, source) pairs that need a transfer task. Shared by the manager's
/// Resume All and the tray's, so both surfaces behave identically.
/// A provisional waiting for the user's confirmation is not paused and is not
/// resumed here: accepting it is the user's decision, not a resume.
fn plan_resume_all(
    snapshot: &mut AppSnapshot,
    event: &str,
    transfer_active: impl Fn(&str) -> bool,
) -> Vec<(String, String)> {
    let mut sources = Vec::new();
    for job in snapshot.jobs.iter_mut() {
        if !["paused", "pending"].contains(&job.state.as_str()) || transfer_active(&job.id) {
            continue;
        }
        job.state = "downloading".into();
        job.connections = 1;
        job.eta = Some("Resuming".into());
        job.events.insert(0, job_event(event, Some("success")));
        sources.push((job.id.clone(), job.source.clone()));
    }
    sources
}

#[tauri::command]
fn resume_job(app: AppHandle, state: State<'_, CoreState>, id: String) {
    let _lifecycle = state.lifecycle.lock().ok();
    if transfer_is_active(state.inner(), &id) {
        return;
    }
    let Some(source) = state.snapshot.lock().ok().and_then(|snapshot| {
        snapshot
            .jobs
            .iter()
            .find(|job| job.id == id && ["paused", "pending"].contains(&job.state.as_str()))
            .map(|job| job.source.clone())
    })
    else {
        return;
    };
    emit_job(&state, &id, |job| {
        if ["paused", "pending"].contains(&job.state.as_str()) {
            job.state = "downloading".into();
            job.connections = 1;
            job.eta = Some("Resuming".into());
            job.events.insert(0, job_event("Resumed", Some("success")));
        }
    });
    emit_snapshot(&app, &state);
    if !spawn_transfer(&app, state.inner(), id, source) {
        return;
    }
}

#[tauri::command]
fn retry_job(app: AppHandle, state: State<'_, CoreState>, id: String) {
    let _lifecycle = state.lifecycle.lock().ok();
    if transfer_is_active(state.inner(), &id) {
        return;
    }
    let source = state.snapshot.lock().ok().and_then(|snapshot| {
        snapshot
            .jobs
            .iter()
            .find(|job| job.id == id)
            .map(|job| job.source.clone())
    });
    emit_job(&state, &id, |job| {
        job.state = "connecting".into();
        job.error = None;
        job.speed = 0;
        job.connections = 0;
        job.events.insert(0, job_event("Retrying source", None));
    });
    emit_snapshot(&app, &state);
    if let Some(source) = source {
        let _ = spawn_transfer(&app, state.inner(), id, source);
    }
}

#[tauri::command]
fn cancel_job_internal(app: &AppHandle, state: &CoreState, id: &str) {
    let _lifecycle = state.lifecycle.lock().ok();
    let Ok(publishing) = state.publishing.lock() else { return };
    if decline_while_publishing(state, &publishing, id, "Cancel") {
        drop(publishing);
        emit_snapshot(app, state);
        return;
    }
    abort_transfer(state, id);
    let mut temp_path = None;
    if let Ok(mut snapshot) = state.snapshot.lock() {
        if let Some(job) = snapshot.jobs.iter().find(|job| job.id == id && job.provisional == Some(true)) { temp_path = Some(job.temp_path.clone()); }
        snapshot.jobs.retain(|job| !(job.id == id && job.provisional == Some(true)));
        snapshot.notifications.retain(|item| item.job_id != id);
        if let Some(job) = snapshot.jobs.iter_mut().find(|job| job.id == id) { job.state = "failed".into(); job.error = Some("Cancelled by user".into()); job.speed = 0; job.connections = 0; job.events.insert(0, job_event("Cancelled by user", Some("warning"))); }
    }
    if let Some(path) = temp_path { let _ = std::fs::remove_file(&path); let _ = std::fs::remove_dir_all(format!("{path}.segments")); cleanup_media_track_files(&path); }
    if let Ok(mut buckets) = state.job_bandwidth.lock() { buckets.remove(id); }
    emit_snapshot(app, state);
}

fn close_add_window(app: &AppHandle, id: &str) {
    let Some(window) = app.get_webview_window(&format!("add-{id}")) else { return; };
    tauri::async_runtime::spawn(async move {
        sleep(Duration::from_millis(10)).await;
        let _ = window.destroy();
    });
}

#[tauri::command]
fn cancel_job(app: AppHandle, state: State<'_, CoreState>, id: String) {
    cancel_job_internal(&app, &state, &id);
    close_add_window(&app, &id);
}

fn apply_main_window_close(app: &AppHandle, state: &CoreState) -> Result<(), String> {
    let close_to_tray = state
        .snapshot
        .lock()
        .map(|snapshot| snapshot.settings.close_behavior == "tray")
        .unwrap_or(true);
    if close_to_tray {
        app.get_webview_window("main")
            .ok_or_else(|| "Main window unavailable".to_string())?
            .hide()
            .map_err(|error| error.to_string())
    } else {
        app.exit(0);
        Ok(())
    }
}

#[tauri::command]
fn main_window_action(app: AppHandle, state: State<'_, CoreState>, action: String) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "Main window unavailable".to_string())?;
    match action.as_str() {
        "minimize" => window.minimize().map_err(|error| error.to_string()),
        "maximize" => {
            if window.is_maximized().map_err(|error| error.to_string())? {
                window.unmaximize().map_err(|error| error.to_string())
            } else {
                window.maximize().map_err(|error| error.to_string())
            }
        }
        "close" => apply_main_window_close(&app, state.inner()),
        _ => Err("Unsupported window action".into()),
    }
}

#[tauri::command]
fn start_window_drag(app: AppHandle, label: String) -> Result<(), String> {
    if label != "main" && !label.starts_with("add-") {
        return Err("Unsupported window".into());
    }
    app.get_webview_window(&label)
        .ok_or_else(|| "Window unavailable".to_string())?
        .start_dragging()
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn remove_job(app: AppHandle, state: State<'_, CoreState>, id: String, delete_file: Option<bool>) {
    let _lifecycle = state.lifecycle.lock().ok();
    let Ok(publishing) = state.publishing.lock() else { return };
    if decline_while_publishing(state.inner(), &publishing, &id, "Remove") {
        drop(publishing);
        emit_snapshot(&app, &state);
        return;
    }
    abort_transfer(state.inner(), &id);
    let mut temporary = None;
    let mut track_cleanup = None;
    let mut destination_to_delete = None;
    if let Ok(mut snapshot) = state.snapshot.lock() {
        if let Some(job) = snapshot.jobs.iter().find(|job| job.id == id) {
            track_cleanup = Some(job.temp_path.clone());
            if job.state != "completed" {
                temporary = Some(job.temp_path.clone());
            }
            // Only a completed job owns the file at its destination: an
            // unfinished job's destination is a plan, and the reservation that
            // creates that file only happens at completion. Deleting it for a
            // failed or paused job would remove whatever the user already had
            // under that name.
            if delete_file.unwrap_or(false) && job.state == "completed" {
                destination_to_delete = Some(job.destination.clone());
            }
        }
        snapshot.jobs.retain(|job| job.id != id);
        snapshot.notifications.retain(|item| item.job_id != id);
    }
    if let Ok(mut buckets) = state.inner().job_bandwidth.lock() {
        buckets.remove(&id);
    }
    // Per-job upserts replaced the full-table rewrite, so removals need an
    // explicit delete (F09).
    if let Ok(database) = state.inner().database.lock() {
        let _ = database.execute("DELETE FROM jobs WHERE id = ?1", params![id]);
    }
    if let Some(path) = temporary {
        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir_all(format!("{path}.segments"));
    }
    if let Some(path) = track_cleanup {
        cleanup_media_track_files(&path);
    }
    if let Some(dest) = destination_to_delete {
        let _ = std::fs::remove_file(&dest);
    }
    emit_snapshot(&app, &state);
}

#[tauri::command]
fn pause_all(app: AppHandle) {
    pause_all_jobs(&app, "Paused by user");
}

#[tauri::command]
fn resume_all(app: AppHandle) {
    resume_all_jobs(&app, "Resumed");
}

fn start_provisional(
    app: AppHandle,
    state: &CoreState,
    input: ProvisionalInput,
    show_window: bool,
    mut viability: Option<Viability>
) -> Result<String, String> {
    let _lifecycle = state
        .lifecycle
        .lock()
        .map_err(|_| "Lifecycle unavailable")?;
    let parsed =
        reqwest::Url::parse(&input.source).map_err(|_| "Use an HTTP or HTTPS URL".to_string())?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err("Use an HTTP or HTTPS URL".into());
    }
    let media = input.media.unwrap_or(false);
    let player_kind = if media {
        Some(
            player_kind_value(input.player_kind.as_deref().unwrap_or("video"))
                .ok_or_else(|| "Player kind must be video or audio".to_string())?,
        )
    } else {
        if input.player_kind.is_some() || input.companion_audio.is_some() {
            return Err("Player metadata requires a media acquisition".into());
        }
        None
    };
    let companion_audio = input.companion_audio.as_deref().map(|value| {
        validated_http_url(value).ok_or_else(|| "Companion audio must be an HTTP or HTTPS URL".to_string())
    }).transpose()?;
    if companion_audio.is_some() && player_kind.as_deref() != Some("video") {
        return Err("Companion audio requires a video acquisition".into());
    }
    if companion_audio.as_deref() == Some(input.source.as_str()) {
        return Err("Companion audio must be different from the video source".into());
    }
    if show_window {
        let target = state
            .reattach_target
            .lock()
            .ok()
            .and_then(|mut value| value.take());
        if let Some(target_id) = target {
            let accepted = !transfer_is_active(state, &target_id)
                && state
                    .snapshot
                    .lock()
                    .ok()
                    .map(|mut snapshot| {
                        let Some(job) = snapshot.jobs.iter_mut().find(|job| {
                            job.id == target_id
                                && job.provisional != Some(true)
                                && source_compatible(&job.source, &input.source)
                        }) else {
                            return false;
                        };
                        job.source = input.source.clone();
                        job.selected_segments = input.selected_segments.clone();
                        job.player_kind = player_kind.clone();
                        job.companion_audio = companion_audio.clone();
                        job.referrer = input.referrer.clone();
                        job.post_body = input.post_body.clone();
                        job.user_agent = input
                            .user_agent
                            .as_deref()
                            .and_then(user_agent_value)
                            .map(str::to_string);
                        job.domain = domain(&input.source);
                        job.state = "connecting".into();
                        job.error = None;
                        job.speed = 0;
                        job.connections = 0;
                        job.started = Some(now_label());
                        job.events
                            .insert(0, job_event("Source reattached by user", Some("success")));
                        true
                    })
                    .unwrap_or(false);
            if accepted {
                emit_snapshot(&app, state);
                if let (Some(sender), Ok(mut pending)) = (viability.take(), state.viability.lock()) {
                    pending.insert(target_id.clone(), sender);
                }
                let _ = spawn_transfer(&app, state, target_id.clone(), input.source);
                return Ok(target_id);
            }
            if let Ok(mut value) = state.reattach_target.lock() {
                if value.is_none() {
                    *value = Some(target_id);
                }
            }
        }
    }
    let id = format!("provisional-{}", Uuid::new_v4());
    // A link's download attribute or the URL only suggest a name; the
    // server's own filename outranks both, as it does in Chromium.
    // Manual Add may name the destination up front; it is the user's choice
    // and outranks both the default folder and a server-suggested name (F11).
    let chosen_destination = input
        .destination
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string);
    let adopt_response_name = chosen_destination.is_none()
        && (input.name_is_hint || input.name.as_deref().map_or(true, |value| value.trim().is_empty()));
    let (name, destination, temp_folder, max_connections, bandwidth_limit) = {
        let snapshot = state.snapshot.lock().map_err(|_| "State unavailable")?;
        let name = input
            .name
            .filter(|value| !value.trim().is_empty())
            .map(|value| safe_filename(&value))
            .unwrap_or_else(|| safe_filename(&source_name(&input.source)));
        let (name, destination) = match chosen_destination {
            Some(chosen) if Path::new(&chosen).is_dir() => {
                let destination = destination_for_filename(&chosen, &name);
                (name, destination)
            }
            Some(chosen) => {
                let leaf = Path::new(&chosen).file_name().and_then(|value| value.to_str()).map(safe_filename).unwrap_or(name);
                let destination = Path::new(&chosen).with_file_name(&leaf).to_string_lossy().into_owned();
                (leaf, destination)
            }
            None => {
                let destination = destination_for_filename(&snapshot.settings.default_folder, &name);
                (name, destination)
            }
        };
        // SPEC §11.4: a capture may carry a per-download override, and it
        // only applies while Settings allows overrides at all.
        let overrides = snapshot.settings.per_download_overrides;
        let max_connections = clamp_connections(
            if overrides {
                input.max_connections
            } else {
                None
            }
            .unwrap_or(snapshot.settings.max_connections)
        );
        (
            name,
            destination,
            temp_root().to_string_lossy().into_owned(),
            max_connections,
            if overrides { input.bandwidth_limit } else { None }
        )
    };
    let job = DownloadJob {
        id: id.clone(),
        name,
        source: input.source.clone(),
        domain: domain(&input.source),
        state: "connecting".into(),
        progress: 0.0,
        downloaded: 0,
        total: None,
        speed: 0,
        eta: Some("Connecting…".into()),
        connections: 0,
        max_connections,
        bandwidth_limit,
        mode: "single-stream".into(),
        media,
        media_tracks: None,
        destination,
        temp_path: Path::new(&temp_folder)
            .join(format!("{id}.part"))
            .to_string_lossy()
            .into_owned(),
        resumable: false,
        mime: None,
        error: None,
        created: now_label(),
        started: Some(now_label()),
        completed: None,
        provisional: Some(true),
        segments: None,
        completed_ranges: vec![],
        resource_identity: None,
        destination_reservation: None,
        selected_segments: input.selected_segments,
        player_kind,
        companion_audio,
        referrer: input.referrer,
        post_body: input.post_body,
        user_agent: input
            .user_agent
            .as_deref()
            .and_then(user_agent_value)
            .map(str::to_string),
        candidates: input.candidates,
        events: vec![job_event("Provisional acquisition created", None)]
    };
    {
        let mut snapshot = state.snapshot.lock().map_err(|_| "State unavailable")?;
        snapshot.jobs.insert(0, job);
    }
    emit_snapshot(&app, state);
    if let (Some(sender), Ok(mut pending)) = (viability.take(), state.viability.lock()) {
        pending.insert(id.clone(), sender);
    }
    if adopt_response_name {
        if let Ok(mut adoptable) = state.adoptable_names.lock() {
            adoptable.insert(id.clone());
        }
    }
    let _ = spawn_transfer(&app, state, id.clone(), input.source);
    if show_window {
        if let Err(error) = open_add_window(&app, &id) {
            drop(_lifecycle);
            cancel_job_internal(&app, state, &id);
            return Err(error);
        }
    }
    Ok(id)
}

/// The Add Download window for one provisional acquisition. Closing it is
/// Cancel while the acquisition is still provisional (SPEC §7.3).
fn open_add_window(app: &AppHandle, id: &str) -> Result<(), String> {
    let label = format!("add-{id}");
    let url = format!("index.html?window=add&id={id}");
    let mut add_window = WebviewWindowBuilder::new(app, label, WebviewUrl::App(url.into()))
        .title("Add Download")
        .inner_size(440.0, 500.0)
        .resizable(false)
        .decorations(false)
        .shadow(true)
        .visible(false)
        .on_page_load(|window, payload| {
            if payload.event() == PageLoadEvent::Finished {
                let _ = window.show().and_then(|_| window.set_focus());
            }
        })
        .center();
    #[cfg(windows)]
    {
        add_window = add_window.data_directory(app_data_root().join("webview"));
    }
    let window = add_window
        .build()
        .map_err(|error| format!("Could not open Add Download window: {error}"))?;
    let close_handle = app.clone();
    let close_id = id.to_string();
    window.on_window_event(move |event| {
        if let WindowEvent::CloseRequested { .. } = event {
            let state = close_handle.state::<CoreState>();
            let is_provisional = state
                .snapshot
                .lock()
                .ok()
                .and_then(|snapshot| {
                    snapshot
                        .jobs
                        .iter()
                        .find(|job| job.id == close_id)
                        .map(|job| job.provisional == Some(true))
                })
                .unwrap_or(false);
            if is_provisional {
                cancel_job_internal(&close_handle, &state, &close_id);
            }
        }
    });
    Ok(())
}

#[tauri::command]
fn create_provisional(
    app: AppHandle,
    state: State<'_, CoreState>,
    input: ProvisionalInput,
) -> Result<String, String> {
    start_provisional(app, state.inner(), input, false, None)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CommitDecision {
    Accept,
    WaitForIdle,
    Reject,
}

fn commit_is_ready(
    provisional: Option<bool>,
    state: Option<&str>,
    progress: f64,
    transfer_active: bool,
) -> bool {
    provisional == Some(true)
        && !transfer_active
        && state == Some("ready")
        && progress >= 100.0
}

/// States a provisional acquisition can be accepted from. Accepting a paused or
/// waiting acquisition is legitimate — the user is turning it into a managed job
/// — which is why only failure blocks the decision.
const COMMITTABLE_STATES: [&str; 6] = [
    "connecting",
    "downloading",
    "paused",
    "pending",
    "finalizing",
    "ready",
];

fn commit_decision(
    provisional: Option<bool>,
    state: Option<&str>,
    progress: f64,
    transfer_active: bool,
) -> CommitDecision {
    let Some(state) = state else {
        return CommitDecision::Reject;
    };
    if provisional != Some(true) || !COMMITTABLE_STATES.contains(&state) {
        return CommitDecision::Reject;
    }
    // A settled acquisition (finished, or assembling its containers) accepts as
    // soon as it stops moving bytes, because this command then owns putting the
    // file in place. A job still moving bytes accepts immediately and keeps
    // transferring straight into the destination.
    let settled = state == "ready" || progress >= 100.0;
    if settled && transfer_active {
        return CommitDecision::WaitForIdle;
    }
    CommitDecision::Accept
}

fn commit_still_owned(state: &CoreState, id: &str) -> bool {
    state
        .snapshot
        .lock()
        .ok()
        .and_then(|snapshot| {
            snapshot.jobs.iter().find(|job| job.id == id).map(|job| {
                job.provisional == Some(false) && job.state == "finalizing" && job.progress >= 100.0
            })
        })
        .unwrap_or(false)
}

async fn commit_wait_for_transfer_idle(state: &CoreState, id: &str) -> bool {
    for _ in 0..300 {
        if !transfer_is_active(state, id) {
            return true;
        }
        sleep(Duration::from_millis(10)).await;
    }
    false
}

#[tauri::command]
async fn commit_provisional(
    app: AppHandle,
    state: State<'_, CoreState>,
    id: String,
    input: CommitInput
) -> Result<(), String> {
    // The user's committed name is final.
    if let Ok(mut adoptable) = state.adoptable_names.lock() {
        adoptable.remove(&id);
    }
    let (decision, ready_at_start) = {
        let snapshot = state.snapshot.lock().map_err(|_| "State unavailable")?;
        let job = snapshot
            .jobs
            .iter()
            .find(|job| job.id == id)
            .ok_or_else(|| "Acquisition no longer exists".to_string())?;
        let active = transfer_is_active(state.inner(), &id);
        let ready = job.state == "ready" || job.progress >= 100.0;
        (
            commit_decision(
                job.provisional,
                Some(job.state.as_str()),
                job.progress,
                active
            ),
            ready
        )
    };
    if decision == CommitDecision::Reject {
        return Err("Acquisition is no longer available for commit".into());
    }
    if decision == CommitDecision::WaitForIdle {
        if !commit_wait_for_transfer_idle(state.inner(), &id).await {
            let still_exists = state
                .snapshot
                .lock()
                .ok()
                .map(|snapshot| snapshot.jobs.iter().any(|job| job.id == id))
                .unwrap_or(false);
            if !still_exists {
                return Err("Acquisition was cancelled before it became ready".into());
            }
        }
    }
    let accepted = {
        let _lifecycle = state
            .lifecycle
            .lock()
            .map_err(|_| "Lifecycle unavailable")?;
        let mut snapshot = state.snapshot.lock().map_err(|_| "State unavailable")?;
        let collision = snapshot.settings.collision_behavior.clone();
        let overrides = snapshot.settings.per_download_overrides;
        let active = transfer_is_active(state.inner(), &id);
        let job = snapshot
            .jobs
            .iter_mut()
            .find(|job| job.id == id)
            .ok_or_else(|| "Acquisition was cancelled before it could be committed".to_string())?;
        let decision = commit_decision(
            job.provisional,
            Some(job.state.as_str()),
            job.progress,
            active
        );
        if ready_at_start
            && !active
            && (decision != CommitDecision::Accept
                || !commit_is_ready(
                    job.provisional,
                    Some(job.state.as_str()),
                    job.progress,
                    active
                ))
        {
            return Err("Acquisition is no longer ready to save".into());
        }
        if decision == CommitDecision::Reject {
            return Err("Acquisition is no longer available for commit".into());
        }
        let before = (job.name.clone(), job.destination.clone(), job.max_connections, job.bandwidth_limit, job.state.clone(), job.eta.clone());
        let name = if input.name.trim().is_empty() {
            job.name.clone()
        } else {
            safe_filename(&input.name)
        };
        let requested_destination = if input.destination.trim().is_empty() {
            job.destination.clone()
        } else {
            input.destination.trim().to_string()
        };
        job.name = name;
        job.destination = collision_destination(&requested_destination, &collision);
        if job.destination != requested_destination {
            // Keep the row title coherent with the actual file on disk.
            if let Some(file_name) = PathBuf::from(&job.destination)
                .file_name()
                .and_then(|value| value.to_str())
            {
                job.name = file_name.to_string();
            }
            job.events.insert(
                0,
                job_event(
                    "Destination renamed to avoid an existing file",
                    Some("warning")
                )
            );
        }
        if let Some(max_connections) = input.max_connections.filter(|_| overrides) {
            job.max_connections = clamp_connections(max_connections);
        }
        // None (absent) = keep the existing cap; Some(None) (explicit null)
        // = clear back to the global setting; Some(n) = set. With overrides
        // disallowed the capture's caps are ignored either way (SPEC §11.4).
        if let Some(cap) = input.bandwidth_limit.filter(|_| overrides) {
            job.bandwidth_limit = cap.filter(|value| *value > 0);
        }
        job.provisional = Some(false);
        // The job is a managed download now. A transfer that had already
        // finished its bytes re-enters finalization, because moving the file
        // into place is exactly the work that is left.
        if job.state == "ready" {
            job.state = "finalizing".into();
            job.eta = None;
        }
        // Keep the acquisition mode's verified resumability. Single-stream
        // fallback is intentionally non-resumable until range resume exists.
        job.events.insert(
            0,
            job_event("Accepted as managed download", Some("success"))
        );
        (
            ready_at_start && !active,
            job.temp_path.clone(),
            job.destination.clone(),
            collision == "replace",
            job.mode == "segments" || job.mode == "dual-track",
            job.media_tracks.unwrap_or(1),
            before
        )
    };
    // Save is acknowledged only once the acceptance is on disk: boot drops
    // provisional rows, so an unrecorded Save would silently lose the
    // download after a restart (F21). On failure the job stays provisional
    // and the Add window stays open with the storage error.
    if let Err(error) = persist_job(&state, &id) {
        let (name, destination, max_connections, bandwidth_limit, job_state, eta) = accepted.6.clone();
        emit_job(&state, &id, |job| {
            if job.state == "completed" {
                return;
            }
            job.provisional = Some(true);
            job.name = name;
            job.destination = destination;
            job.max_connections = max_connections;
            job.bandwidth_limit = bandwidth_limit;
            if job.state == "finalizing" && job_state == "ready" {
                job.state = job_state;
                job.eta = eta;
            }
            job.events.insert(0, job_event("Save could not be recorded; the download is still waiting", Some("error")));
        });
        emit_snapshot(&app, &state);
        return Err(format!("Could not record the Save: {error}"));
    }
    emit_snapshot(&app, &state);
    if !accepted.0 {
        close_add_window(&app, &id);
        return Ok(());
    }
    if !commit_still_owned(state.inner(), &id) {
        emit_snapshot(&app, &state);
        return Err("Acquisition was paused or cancelled before finalization".into());
    }
    let final_path = if accepted.4 && accepted.5 > 1 {
        let track_paths = (0..accepted.5 as usize)
            .map(|track| format!("{}.track-{track:02}", accepted.1))
            .collect::<Vec<_>>();
        let mux_path = format!("{}.mux.{}", accepted.1, media_extension(&accepted.2));
        let muxed_path = match mux_media_tracks(&track_paths, &mux_path).await {
            Ok(path) => path,
            Err(error) => {
                if !commit_still_owned(state.inner(), &id) {
                    emit_snapshot(&app, &state);
                    return Err("Acquisition was paused or cancelled during finalization".into());
                }
                emit_job(&state, &id, |job| {
                    job.state = "failed".into();
                    job.error = Some(error.clone());
                    job.events.insert(
                        0,
                        job_event(
                            "Media finalization failed; downloaded parts were preserved",
                            Some("error")
                        )
                    );
                });
                emit_snapshot(&app, &state);
                return Err(error);
            }
        };
        if !commit_still_owned(state.inner(), &id) {
            emit_snapshot(&app, &state);
            return Err("Acquisition was paused or cancelled during finalization".into());
        }
        muxed_path
    } else {
        if accepted.4 {
            if let Err(error) = finalize_media(&accepted.1, &accepted.2).await {
                if !commit_still_owned(state.inner(), &id) {
                    emit_snapshot(&app, &state);
                    return Err("Acquisition was paused or cancelled during finalization".into());
                }
                emit_job(&state, &id, |job| {
                    job.state = "failed".into();
                    job.error = Some(error.clone());
                    job.events.insert(
                        0,
                        job_event(
                            "Media finalization failed; downloaded parts were preserved",
                            Some("error")
                        )
                    );
                });
                emit_snapshot(&app, &state);
                return Err(error);
            }
            if !commit_still_owned(state.inner(), &id) {
                emit_snapshot(&app, &state);
                return Err("Acquisition was paused or cancelled during finalization".into());
            }
        }
        accepted.1.clone()
    };
    if !commit_still_owned(state.inner(), &id) {
        emit_snapshot(&app, &state);
        return Err("Acquisition was paused or cancelled before the file move".into());
    }
    let mut destination = if accepted.4 && accepted.5 > 1 {
        destination_with_output_extension(&accepted.2, &final_path)
    } else {
        accepted.2.clone()
    };
    if let Some(parent) = PathBuf::from(&destination).parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if !commit_still_owned(state.inner(), &id) {
        emit_snapshot(&app, &state);
        return Err("Acquisition was paused or cancelled before the file move".into());
    }
    let (reserved, reservation) = if accepted.3 {
        (false, None)
    } else {
        match reserve_collision_destination_with_marker(&destination) {
            Ok((path, marker)) => {
                destination = path;
                (true, Some(marker))
            }
            Err(error) => {
                emit_job(&state, &id, |job| {
                    job.state = "failed".into();
                    job.error = Some(error.clone());
                    job.events.insert(
                        0,
                        job_event("Could not reserve a unique destination", Some("error"))
                    );
                });
                emit_snapshot(&app, &state);
                return Err(error);
            }
        }
    };
    if destination != accepted.2 || reservation.is_some() {
        let reservation_marker = reservation.clone();
        emit_job(&state, &id, |job| {
            job.destination = destination.clone();
            job.destination_reservation = reservation_marker;
            if let Some(file_name) = PathBuf::from(&destination)
                .file_name()
                .and_then(|value| value.to_str())
            {
                if destination != accepted.2 {
                    job.name = file_name.to_string();
                }
            }
            if destination != accepted.2 {
                job.events.insert(
                    0,
                    job_event(
                        "Destination renamed to avoid a concurrent collision",
                        Some("warning")
                    )
                );
            }
        });
        emit_snapshot(&app, &state);
    }
    let publishing = begin_publishing(&state, &id, || commit_still_owned(state.inner(), &id));
    if publishing.is_none() {
        if reserved {
            cleanup_reserved_destination(&destination, reservation.as_deref()).await;
        }
        clear_destination_reservation(&app, &state, &id);
        emit_snapshot(&app, &state);
        return Err("Acquisition was paused or cancelled before the file move".into());
    }
    match move_completed_file(
        &final_path,
        &destination,
        accepted.3,
        reservation.as_deref()
    )
    .await
    {
        Ok(()) => {
            if accepted.4 {
                let _ = std::fs::remove_dir_all(format!("{}.segments", accepted.1));
            }
            cleanup_media_track_files(&accepted.1);
            let mut completed = false;
            emit_job(&state, &id, |job| {
                if job.provisional == Some(false) && job.progress >= 100.0 {
                    job.destination_reservation = None;
                    complete_job(job);
                    job.eta = None;
                    completed = true;
                }
            });
            if !completed {
                emit_snapshot(&app, &state);
                return Err("Acquisition was paused or cancelled before completion".into());
            }
            add_notification(&app, &state, &id, "completed");
        }
        Err(error) => {
            if reserved {
                cleanup_reserved_destination(&destination, reservation.as_deref()).await;
            }
            if !commit_still_owned(state.inner(), &id) {
                emit_snapshot(&app, &state);
                return Err("Acquisition was paused or cancelled during the file move".into());
            }
            emit_job(&state, &id, |job| {
                job.state = "failed".into();
                job.error = Some(redact_url_credentials(&error.to_string()));
                job.events.insert(
                    0,
                    job_event("Could not move the completed file", Some("error"))
                );
            });
            emit_snapshot(&app, &state);
            return Err(error.to_string());
        }
    }
    emit_snapshot(&app, &state);
    close_add_window(&app, &id);
    Ok(())
}

fn update_settings_snapshot(state: &CoreState, patch: &Value) -> Result<Option<(bool, (bool, bool, bool))>, String> {
    let _lifecycle = state.lifecycle.lock().map_err(|_| "Lifecycle unavailable".to_string())?;
    let (previous, checks) = {
        let mut snapshot = state.snapshot.lock().map_err(|_| "State unavailable".to_string())?;
        let payload = patch.get("patch").unwrap_or(patch);
        let Value::Object(entries) = payload else { return Ok(None); };
        let previous = snapshot.clone();
        snapshot.settings = apply_settings_patch(&snapshot.settings, &Value::Object(entries.clone()));
        (
            previous,
            (snapshot.settings.intercept_downloads, snapshot.settings.show_media_buttons, snapshot.settings.start_at_sign_in),
        )
    };
    if let Err(error) = save_snapshot(state) {
        if let Ok(mut snapshot) = state.snapshot.lock() { *snapshot = previous; }
        return Err(error);
    }
    Ok(Some((previous.settings.start_at_sign_in, checks)))
}

#[tauri::command]
fn update_settings(
    app: AppHandle,
    state: State<'_, CoreState>,
    patch: Value,
) -> Result<(), String> {
    let Some((previous_startup, (intercept_downloads, show_media_buttons, start_at_sign_in))) =
        update_settings_snapshot(state.inner(), &patch)?
    else {
        return Ok(());
    };
    write_browser_policy(
        &browser_policy_root(),
        &settings_policy(
            &state
                .snapshot
                .lock()
                .map_err(|_| "State unavailable".to_string())?
                .settings,
        ),
    )?;
    sync_tray_checks(&app, intercept_downloads, show_media_buttons);
    // Touch the OS startup entry only when its setting changed: theme tweaks
    // and folder keystrokes must not rewrite system state (F14).
    if previous_startup != start_at_sign_in {
        startup::sync(start_at_sign_in)?;
    }
    emit_snapshot_event(&app, &state)
}

#[tauri::command]
fn reattach_job(app: AppHandle, state: State<'_, CoreState>, id: String) {
    let exists = state
        .snapshot
        .lock()
        .ok()
        .map(|snapshot| {
            snapshot
                .jobs
                .iter()
                .any(|job| job.id == id && job.provisional != Some(true))
        })
        .unwrap_or(false);
    if exists {
        if let Ok(mut target) = state.reattach_target.lock() {
            *target = Some(id.clone());
        }
        emit_job(&state, &id, |job| {
            job.state = "pending".into();
            job.eta = Some("Waiting for renewed source".into());
            job.events.insert(
                0,
                job_event("Waiting for a renewed browser source", Some("warning"))
            );
        });
        emit_snapshot(&app, &state);
    }
}

fn provisional_input_from_message(message: &Value) -> Option<ProvisionalInput> {
    let message_type = message.get("type").and_then(Value::as_str)?;
    if !matches!(message_type, "capture-acquisition" | "media-capture") {
        return None;
    }
    let payload = message.get("payload").unwrap_or(&message);
    let source = payload
        .get("source")
        .or_else(|| payload.get("url"))
        .and_then(Value::as_str)?
        .trim()
        .to_string();
    let parsed = reqwest::Url::parse(&source).ok()?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return None;
    }
    let name = payload
        .get("name")
        .and_then(Value::as_str)
        .map(str::to_string);
    let media = message_type == "media-capture"
        || payload
            .get("media")
            .and_then(Value::as_bool)
            .unwrap_or(false);
    let player_kind = match payload.get("playerKind") {
        None => None,
        Some(Value::String(value)) => Some(player_kind_value(value)?),
        Some(_) => return None,
    };
    let companion_audio = match payload.get("companionAudio") {
        None => None,
        Some(Value::String(value)) => Some(validated_http_url(value)?),
        Some(_) => return None,
    };
    if companion_audio.is_some() && player_kind.as_deref().unwrap_or("video") != "video" {
        return None;
    }
    if companion_audio.as_deref() == Some(source.as_str()) {
        return None;
    }
    // Bytes/sec; absent, zero, or non-numeric means no per-job cap.
    let bandwidth_limit = payload
        .get("bandwidthLimit")
        .and_then(Value::as_u64)
        .filter(|value| *value > 0);
    let selected_segments = payload
        .get("selectedSegments")
        .and_then(Value::as_array)
        .map(|values| {
            values
                .iter()
                .filter_map(Value::as_str)
                .filter_map(|value| {
                    let parsed = reqwest::Url::parse(value).ok()?;
                    matches!(parsed.scheme(), "http" | "https").then(|| value.to_string())
                })
                .take(8)
                .collect()
        })
        .unwrap_or_default();
    // Alternates for a capture that could not prove which resource feeds the
    // player: same validation as selected segments, minus the primary.
    let candidates = payload
        .get("candidates")
        .and_then(Value::as_array)
        .map(|values| {
            let mut urls: Vec<String> = Vec::new();
            for value in values.iter().filter_map(Value::as_str) {
                let Ok(parsed) = reqwest::Url::parse(value) else { continue };
                if !matches!(parsed.scheme(), "http" | "https") { continue }
                let url = parsed.to_string();
                if url != source && !urls.contains(&url) { urls.push(url); }
                if urls.len() >= 6 { break; }
            }
            urls
        })
        .unwrap_or_default();
    // Request-context replay (SPEC §5.1/§16): the extension forwards the
    // capture page as `referrer` (ordinary-capture and media-capture send
    // `pageUrl`; the downloads-API fallback sends the item referrer).
    // Accept explicit `referrer` first, then `pageUrl`; keep only HTTP(S).
    let referrer = payload
        .get("referrer")
        .or_else(|| payload.get("pageUrl"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .and_then(|value| reqwest::Url::parse(value).ok())
        .filter(|parsed| matches!(parsed.scheme(), "http" | "https"))
        .map(|parsed| parsed.to_string());
    // POST replay (SPEC §5.1 method/body): keep a small urlencoded body only.
    // Multipart, raw, empty, and oversized bodies fall back to a safe GET.
    const POST_BODY_MAX: usize = 64 * 1024;
    let post_body = payload
        .get("postBody")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= POST_BODY_MAX)
        .map(str::to_string);
    let user_agent = payload
        .get("userAgent")
        .and_then(Value::as_str)
        .and_then(user_agent_value)
        .map(str::to_string);
    Some(ProvisionalInput {
        source,
        name,
        media: Some(media),
        max_connections: None,
        bandwidth_limit,
        selected_segments,
        candidates,
        player_kind,
        companion_audio,
        referrer,
        post_body,
        user_agent,
        name_is_hint: payload.get("nameIsHint").and_then(Value::as_bool).unwrap_or(false),
        destination: None
    })
}

fn capture_input_from_args(args: &[String]) -> Option<ProvisionalInput> {
    let index = args.iter().position(|value| value == "--capture")?;
    let raw = args.get(index + 1)?;
    let message: Value = serde_json::from_str(raw).ok()?;
    provisional_input_from_message(&message)
}

fn policy_from_args(args: &[String]) -> Option<BrowserPolicy> {
    let index = args.iter().position(|value| value == "--policy")?;
    let raw = args.get(index + 1)?;
    browser_policy_from_value(&serde_json::from_str(raw).ok()?)
}

fn commit_from_args(args: &[String]) -> Option<(String, CommitInput)> {
    let index = args.iter().position(|value| value == "--commit")?;
    let raw = args.get(index + 1)?;
    let message: Value = serde_json::from_str(raw).ok()?;
    let payload = message.get("payload").unwrap_or(&message);
    let id = payload.get("id").and_then(Value::as_str)?.to_string();
    let input = serde_json::from_value(payload.get("input")?.clone()).ok()?;
    Some((id, input))
}

fn spawn_commit(app: AppHandle, id: String, input: CommitInput) {
    let state_app = app.clone();
    tauri::async_runtime::spawn(async move {
        let state = state_app.state::<CoreState>();
        let _ = commit_provisional(app, state, id, input).await;
    });
}

/// Loopback hosts the bridge accepts. The listener binds 127.0.0.1, but the
/// Host header is caller-controlled (DNS rebinding), so it is validated
/// instead of trusted (F01).
fn bridge_host_allowed(host: &Option<String>) -> bool {
    let Some(raw) = host.as_deref() else { return true };
    let host = raw.trim();
    let bare = if let Some(stripped) = host.strip_prefix('[') {
        stripped.split(']').next().unwrap_or(stripped)
    } else if host == "::1" {
        "::1"
    } else {
        host.split(':').next().unwrap_or(host)
    };
    matches!(bare.to_ascii_lowercase().as_str(), "127.0.0.1" | "localhost" | "::1")
}

/// Browsers always attach Origin to cross-origin fetches and pages cannot
/// suppress it, so a present non-extension Origin proves a web caller (F01).
/// Absent Origin means a non-browser local client (fixtures, harnesses);
/// those stay allowed behind the loopback bind.
fn bridge_origin_allowed(origin: &Option<String>) -> bool {
    match origin.as_deref() {
        None => true,
        Some(origin) => origin.starts_with("chrome-extension://"),
    }
}

fn bridge_caller_allowed(request: &ipc::Request) -> bool {
    bridge_host_allowed(&request.host) && bridge_origin_allowed(&request.origin)
}

/// Validated extension origin to reflect in CORS headers, if any (F01).
fn bridge_allow_origin(request: &ipc::Request) -> Option<String> {
    match request.origin.as_deref() {
        Some(origin) if origin.starts_with("chrome-extension://") => Some(origin.to_string()),
        _ => None,
    }
}

fn bridge_json_content(request: &ipc::Request) -> bool {
    request.content_type.as_deref().is_some_and(|value| {
        value.split(';').next().unwrap_or(value).trim().eq_ignore_ascii_case("application/json")
    })
}

fn bridge_reject(request: &ipc::Request, status: u16, error: &str) -> ipc::Response {
    bridge_json(status, json!({ "ok": false, "error": error })).with_origin(bridge_allow_origin(request))
}

fn bridge_json(status: u16, value: Value) -> ipc::Response {
    let body = if status == 204 {
        Vec::new()
    } else {
        serde_json::to_vec(&value).unwrap_or_else(|_| {
            b"{\"ok\":false,\"error\":\"response serialization failed\"}".to_vec()
        })
    };
    ipc::Response::new(status, body)
}

fn bridge_respond(request: &ipc::Request, status: u16, value: Value) -> ipc::Response {
    bridge_json(status, value).with_origin(bridge_allow_origin(request))
}

fn capture_id_of(message: &Value) -> Option<String> {
    message
        .get("payload")
        .and_then(|payload| payload.get("captureId"))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= 128)
        .map(str::to_string)
}

/// Cancel an acquisition the extension owns the decision for. Only a
/// provisional is discarded: a capture that reattached to a managed job must
/// not turn that job into a user cancellation.
fn cancel_capture_job(app: &AppHandle, id: &str) {
    let state = app.state::<CoreState>();
    let provisional = state
        .snapshot
        .lock()
        .ok()
        .and_then(|snapshot| snapshot.jobs.iter().find(|job| job.id == id).map(|job| job.provisional == Some(true)))
        .unwrap_or(false);
    if provisional {
        cancel_job_internal(app, state.inner(), id);
        close_add_window(app, id);
    }
}

/// Browser captures (SPEC §5.1, §7.1). With `requireViable`, the bridge answers
/// only once the resident's own first response is the file: the extension
/// keeps the browser's copy until then and resumes it on a hand-back, so a
/// source the resident cannot fetch (session cookies, one-use URL, login page)
/// never costs the user their download (§5.1.1). `captureId` makes creation
/// idempotent and lets the extension cancel a capture whose answer it lost.
async fn bridge_capture(app: AppHandle, request: &ipc::Request, message: Value) -> ipc::Response {
    let capture_id = capture_id_of(&message);
    if message.get("type").and_then(Value::as_str) == Some("cancel-acquisition") {
        let id = message
            .get("payload")
            .and_then(|payload| payload.get("id"))
            .and_then(Value::as_str)
            .map(str::to_string);
        if id.is_none() && capture_id.is_none() {
            return bridge_reject(request, 400, "invalid cancellation");
        }
        let mut targets: Vec<String> = id.into_iter().collect();
        if let Some(capture_id) = capture_id {
            let state = app.state::<CoreState>();
            if let Ok(mut ledger) = state.captures.lock() {
                match ledger.iter().find(|(key, _)| *key == capture_id).map(|(_, record)| record.clone()) {
                    Some(CaptureRecord::Job(job_id)) => targets.push(job_id),
                    Some(CaptureRecord::Cancelled) => {}
                    None => {
                        ledger.push_back((capture_id, CaptureRecord::Cancelled));
                        while ledger.len() > CAPTURE_LEDGER_MAX {
                            ledger.pop_front();
                        }
                    }
                }
            };
        }
        for target in targets {
            cancel_capture_job(&app, &target);
        }
        return bridge_respond(request, 200, json!({ "ok": true }));
    }
    let Some(input) = provisional_input_from_message(&message) else {
        return bridge_reject(request, 400, "invalid acquisition");
    };
    let require_viable = message
        .get("payload")
        .and_then(|payload| payload.get("requireViable"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let (sender, receiver) = if require_viable {
        let (sender, receiver) = tokio::sync::oneshot::channel();
        (Some(sender), Some(receiver))
    } else {
        (None, None)
    };
    let created = {
        let state = app.state::<CoreState>();
        let Ok(mut ledger) = state.captures.lock() else {
            return bridge_respond(request, 500, json!({ "ok": false, "error": "capture ledger unavailable" }));
        };
        let known = capture_id
            .as_ref()
            .and_then(|capture_id| ledger.iter().find(|(key, _)| key == capture_id).map(|(_, record)| record.clone()));
        match known {
            Some(CaptureRecord::Cancelled) => {
                return bridge_respond(request, 200, json!({ "ok": false, "error": "capture was cancelled" }));
            }
            Some(CaptureRecord::Job(id)) => {
                return bridge_respond(request, 200, json!({ "ok": true, "id": id, "duplicate": true }));
            }
            None => {}
        }
        let created = start_provisional(app.clone(), state.inner(), input, !require_viable, sender);
        if let (Ok(id), Some(capture_id)) = (&created, capture_id) {
            ledger.push_back((capture_id, CaptureRecord::Job(id.clone())));
            while ledger.len() > CAPTURE_LEDGER_MAX {
                ledger.pop_front();
            }
        }
        created
    };
    let id = match created {
        Ok(id) => id,
        Err(error) => return bridge_respond(request, 500, json!({ "ok": false, "error": error })),
    };
    if let Some(receiver) = receiver {
        let reason = match tokio::time::timeout(Duration::from_secs(20), receiver).await {
            Ok(Ok(Ok(()))) => None,
            Ok(Ok(Err(error))) => Some(error),
            Ok(Err(_)) => Some("The acquisition stopped before it received the file".to_string()),
            Err(_) => Some("The source did not answer in time".to_string()),
        };
        if let Some(reason) = reason {
            cancel_capture_job(&app, &id);
            return bridge_respond(request, 200, json!({ "ok": false, "handback": true, "error": redact_url_credentials(&reason) }));
        }
        let still_provisional = app
            .state::<CoreState>()
            .snapshot
            .lock()
            .ok()
            .and_then(|snapshot| snapshot.jobs.iter().find(|job| job.id == id).map(|job| job.provisional == Some(true)))
            .unwrap_or(false);
        if !still_provisional {
            return bridge_respond(request, 200, json!({ "ok": false, "error": "capture was cancelled" }));
        }
        if let Err(error) = open_add_window(&app, &id) {
            cancel_capture_job(&app, &id);
            return bridge_respond(request, 500, json!({ "ok": false, "error": error }));
        }
    }
    bridge_respond(request, 200, json!({ "ok": true, "id": id }))
}

async fn bridge_request(app: AppHandle, request: ipc::Request) -> ipc::Response {
    if request.method == "OPTIONS" {
        if !bridge_origin_allowed(&request.origin) {
            return bridge_reject(&request, 403, "bridge preflight not from the browser extension");
        }
        return bridge_json(204, json!({})).with_origin(bridge_allow_origin(&request));
    }
    if !bridge_host_allowed(&request.host) {
        return bridge_reject(&request, 403, "bridge request host is not loopback");
    }
    let mutation = request.method == "POST";
    if mutation && !bridge_caller_allowed(&request) {
        return bridge_reject(&request, 403, "bridge request not from the browser extension");
    }
    if mutation && !bridge_json_content(&request) {
        return bridge_reject(&request, 415, "bridge request must be JSON");
    }
    match (request.method.as_str(), request.path.as_str()) {
        ("GET", "/v1/health") => bridge_respond(&request, 200, json!({ "ok": true })),
        ("GET", "/v1/policy") => {
            // The SQLite-backed snapshot is the single authority while
            // running; the JSON cache is only a fallback (F14).
            let policy = app.state::<CoreState>()
                .snapshot
                .lock()
                .map(|snapshot| settings_policy(&snapshot.settings))
                .unwrap_or_else(|_| {
                    load_browser_policy(&browser_policy_root()).unwrap_or((true, true, Vec::new()))
                });
            bridge_respond(
                &request,
                200,
                json!({ "ok": true, "policy": browser_policy_value(&policy) })
            )
        }
        ("POST", "/v1/policy") => {
            let Ok(message) = serde_json::from_slice::<Value>(&request.body) else {
                return bridge_reject(&request, 400, "invalid policy");
            };
            let Some(policy) = browser_policy_from_value(&message) else {
                return bridge_reject(&request, 400, "invalid policy");
            };
            let state = app.state::<CoreState>();
            if let Err(error) = apply_browser_policy(&app, state.inner(), policy.clone()) {
                return bridge_reject(&request, 500, &format!("could not persist policy: {error}"));
            }
            bridge_respond(
                &request,
                200,
                json!({ "ok": true, "policy": browser_policy_value(&policy) })
            )
        }
        ("POST", "/v1/capture") => {
            let Ok(message) = serde_json::from_slice::<Value>(&request.body) else {
                return bridge_reject(&request, 400, "invalid acquisition");
            };
            bridge_capture(app, &request, message).await
        }
        ("POST", "/v1/manager") => {
            let Some(window) = app.get_webview_window("main") else {
                return bridge_respond(
                    &request,
                    500,
                    json!({ "ok": false, "error": "manager window unavailable" })
                );
            };
            if let Err(error) = window.show().and_then(|_| window.set_focus()) {
                return bridge_respond(&request, 500, json!({ "ok": false, "error": error.to_string() }));
            }
            bridge_respond(&request, 200, json!({ "ok": true }))
        }
        _ => bridge_reject(&request, 404, "unknown bridge route"),
    }
}

fn start_bridge(app: &AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let listener = match tokio::net::TcpListener::bind(ipc::BRIDGE_ADDR).await {
            Ok(listener) => listener,
            Err(error) => {
                eprintln!("Local browser bridge unavailable: {error}");
                // A second per-user instance (or another local holder of the
                // fixed port) leaves this manager running without a bridge.
                // Say so in the snapshot instead of showing normal
                // "Browser integration on" state (F01).
                let state = app.state::<CoreState>();
                if let Ok(mut snapshot) = state.snapshot.lock() {
                    snapshot.bridge_available = false;
                }
                let _ = emit_snapshot_event(&app, &state);
                return;
            }
        };
        let handler = move |request| {
            let app = app.clone();
            async move { bridge_request(app, request).await }
        };
        if let Err(error) = ipc::serve(listener, handler).await {
            eprintln!("Local browser bridge stopped: {error}");
        }
    });
}

fn background_launch(args: &[String]) -> bool {
    args.iter().any(|value| {
        matches!(
            value.as_str(),
            "--startup" | "--capture" | "--policy" | "--commit"
        )
    })
}

fn tray_image() -> Image<'static> {
    let mut pixels = vec![0u8; 32 * 32 * 4];
    for y in 0..32u32 {
        for x in 0..32u32 {
            let dx = x as i32 - 16;
            let dy = y as i32 - 16;
            let distance = dx * dx + dy * dy;
            let index = ((y * 32 + x) * 4) as usize;
            if distance <= 225 {
                pixels[index..index + 4].copy_from_slice(&[8, 120, 237, 255]);
            }
            if (x == 15 || x == 16) && (y >= 8 && y <= 20)
                || (y >= 19
                    && y <= 21
                    && x >= 11
                    && x <= 20
                    && (x as i32 - 16).abs() <= (y as i32 - 19))
            {
                pixels[index..index + 4].copy_from_slice(&[255, 255, 255, 255]);
            }
        }
    }
    Image::new_owned(pixels, 32, 32)
}

// Pure toggle decision shared by both tray check arms (SPEC §12): each arm
// flips exactly its own flag and keeps the other, so tray and Settings can
// never diverge into two policies. Unit-covered; the visual check direction
// stays source-verified because GUI event injection is blocked (XTEST).
fn tray_toggle_next(current: (bool, bool), which: &str) -> (bool, bool) {
    match which {
        "browser-integration" => (!current.0, current.1),
        "media-buttons" => (current.0, !current.1),
        _ => current,
    }
}

// Keeps the native tray checkmarks coherent with Settings (SPEC §12: one
// policy, not two copies). Called from every writer of the two flags — the
// tray toggle arms, update_settings, apply_browser_policy — because neither
// direction propagates on its own: Tauri check items keep whatever checked
// state they were built or last set with, and Settings-panel changes never
// reach the tray menu. Handles are stored at install time; before that (or
// in tests, where no tray exists) this is a silent no-op.
fn sync_tray_checks(app: &tauri::AppHandle, intercept_downloads: bool, show_media_buttons: bool) {
    let state = app.state::<CoreState>();
    {
        if let Ok(checks) = state.tray_checks.lock() {
            if let Some((browser, media)) = checks.as_ref() {
                let _ = browser.set_checked(intercept_downloads);
                let _ = media.set_checked(show_media_buttons);
            }
        };
    }
}

fn install_tray(
    app: &tauri::AppHandle,
    intercept_downloads: bool,
    show_media_buttons: bool,
) -> tauri::Result<()> {
    let open_manager =
        MenuItemBuilder::with_id("open-manager", "Open Download Manager").build(app)?;
    let pause_all = MenuItemBuilder::with_id("pause-all", "Pause All").build(app)?;
    let resume_all = MenuItemBuilder::with_id("resume-all", "Resume All").build(app)?;
    let browser_integration =
        CheckMenuItemBuilder::with_id("browser-integration", "Intercept browser downloads")
            .checked(intercept_downloads)
            .build(app)?;
    let media_buttons = CheckMenuItemBuilder::with_id("media-buttons", "Media Buttons")
        .checked(show_media_buttons)
        .build(app)?;
    let bandwidth = MenuItemBuilder::with_id("bandwidth", "Set Bandwidth Limit").build(app)?;
    let exit = MenuItemBuilder::with_id("exit-manager", "Exit Manager").build(app)?;
    let menu = MenuBuilder::new(app)
        .items(&[&open_manager, &pause_all, &resume_all])
        .separator()
        .items(&[&browser_integration, &media_buttons, &bandwidth])
        .separator()
        .item(&exit)
        .build()?;
    TrayIconBuilder::with_id("main-tray")
        .icon(tray_image())
        .menu(&menu)
        .tooltip("Download Manager")
        .on_menu_event(|app, event| {
            let id = event.id().as_ref();
            match id {
                "open-manager" => {
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.show();
                        let _ = window.set_focus();
                    }
                }
                "pause-all" => pause_all_jobs(app, "Paused from the system tray"),
                "resume-all" => resume_all_jobs(app, "Resumed from the system tray"),
                "browser-integration" => {
                    let state = app.state::<CoreState>();
                    let checks = if let Ok(mut snapshot) = state.snapshot.lock() {
                        let previous = (
                            snapshot.settings.intercept_downloads,
                            snapshot.settings.show_media_buttons,
                        );
                        let next = tray_toggle_next(previous, "browser-integration");
                        snapshot.settings.intercept_downloads = next.0;
                        snapshot.settings.show_media_buttons = next.1;
                        // A cache write failure rolls the toggle back: the menu
                        // must never disagree with durable state (F14).
                        if let Err(error) = write_browser_policy(
                            &browser_policy_root(),
                            &settings_policy(&snapshot.settings),
                        ) {
                            eprintln!("Browser policy cache unavailable: {error}");
                            snapshot.settings.intercept_downloads = previous.0;
                            snapshot.settings.show_media_buttons = previous.1;
                            None
                        } else {
                            Some(next)
                        }
                    } else {
                        None
                    };
                    if let Some((intercept, media)) = checks {
                        sync_tray_checks(app, intercept, media);
                    }
                    emit_snapshot(app, &state);
                }
                "media-buttons" => {
                    let state = app.state::<CoreState>();
                    let checks = if let Ok(mut snapshot) = state.snapshot.lock() {
                        let previous = (
                            snapshot.settings.intercept_downloads,
                            snapshot.settings.show_media_buttons,
                        );
                        let next = tray_toggle_next(previous, "media-buttons");
                        snapshot.settings.intercept_downloads = next.0;
                        snapshot.settings.show_media_buttons = next.1;
                        if let Err(error) = write_browser_policy(
                            &browser_policy_root(),
                            &settings_policy(&snapshot.settings),
                        ) {
                            eprintln!("Browser policy cache unavailable: {error}");
                            snapshot.settings.intercept_downloads = previous.0;
                            snapshot.settings.show_media_buttons = previous.1;
                            None
                        } else {
                            Some(next)
                        }
                    } else {
                        None
                    };
                    if let Some((intercept, media)) = checks {
                        sync_tray_checks(app, intercept, media);
                    }
                    emit_snapshot(app, &state);
                }
                "bandwidth" => {
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.show();
                        let _ = window.set_focus();
                        let _ = window.eval(
                            "window.location.href = window.location.pathname + '?settings=network'",
                        );
                    }
                }
                "exit-manager" => app.exit(0),
                _ => {}
            }
        })
        .build(app)?;
    {
        let state = app.state::<CoreState>();
        if let Ok(mut checks) = state.tray_checks.lock() {
            *checks = Some((browser_integration.clone(), media_buttons.clone()));
        };
    }
    Ok(())
}

fn main() {
    configure_portable_webview2();
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            if let Some(policy) = policy_from_args(&argv) {
                let state = app.state::<CoreState>();
                if let Err(error) = apply_browser_policy(app, state.inner(), policy) {
                    eprintln!("Browser policy update unavailable: {error}");
                }
            } else if let Some((id, input)) = commit_from_args(&argv) {
                spawn_commit(app.clone(), id, input);
            } else if let Some(input) = capture_input_from_args(&argv) {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.hide();
                }
                let state = app.state::<CoreState>();
                let _ = start_provisional(app.clone(), state.inner(), input, true, None);
            } else if argv.iter().any(|value| value == "--startup") {
                let show_manager = app
                    .state::<CoreState>()
                    .snapshot
                    .lock()
                    .map(|snapshot| snapshot.settings.show_manager_at_sign_in)
                    .unwrap_or(true);
                if show_manager {
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.show();
                        let _ = window.set_focus();
                    }
                }
            } else if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }));
    #[cfg(not(windows))]
    let builder = builder.plugin(tauri_plugin_notification::init());
    builder
        .setup(|app| {
            let launch_args = std::env::args().collect::<Vec<_>>();
            let launch_in_background = background_launch(&launch_args);
            let root = app_data_root();
            std::fs::create_dir_all(&root).map_err(|error| error.to_string())?;
            restrict_data_dir(&root);
            let main_config = app.config().app.windows.iter().find(|window| window.label == "main").ok_or_else(|| "Main window configuration is missing".to_string())?;
            let mut main_window = WebviewWindowBuilder::from_config(app.handle(), main_config).map_err(|error| error.to_string())?;
            if launch_in_background {
                main_window = main_window.visible(false);
            }
            #[cfg(windows)]
            {
                main_window = main_window.data_directory(app_data_root().join("webview"));
            }
            main_window.build().map_err(|error| error.to_string())?;
            let database = Connection::open(root.join("download-manager.db")).map_err(|error| error.to_string())?;
            restrict_file(&root.join("download-manager.db"));
            database.execute_batch("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL); CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL); CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, payload TEXT);").map_err(|error| error.to_string())?;
            let stored_settings: Result<String, _> = database.query_row("SELECT payload FROM settings WHERE id = 1", [], |row| row.get::<_, String>(0));
            let mut settings = stored_settings.as_deref().map(settings_from_stored).unwrap_or_else(|_| default_settings());
            if stored_settings.is_err() {
                // Fresh database (first boot, portable move): the JSON cache
                // seeds policy once. Afterwards SQLite is the authority and
                // the cache is rewritten from it, never the reverse (F14).
                if let Some(policy) = load_browser_policy(&root) {
                    settings.intercept_downloads = policy.0;
                    settings.show_media_buttons = policy.1;
                    settings.excluded_sites = policy.2;
                }
            } else if let Err(error) = write_browser_policy(&root, &settings_policy(&settings)) {
                eprintln!("Browser policy cache unavailable: {error}");
            }
            let show_manager_at_startup = settings.show_manager_at_sign_in;
            if let Err(error) = startup::sync(settings.start_at_sign_in) { eprintln!("Startup registration unavailable: {error}"); }
            let initial_snapshot = snapshot_from_database(&database, settings);
            let tray_intercept_downloads = initial_snapshot.settings.intercept_downloads;
            let tray_show_media_buttons = initial_snapshot.settings.show_media_buttons;
            let recovered = initial_snapshot.jobs.iter().filter(|job| job.provisional != Some(true) && TRANSFER_STATES.contains(&job.state.as_str())).map(|job| (job.id.clone(), job.source.clone())).collect::<Vec<_>>();
            app.manage(CoreState { snapshot: Mutex::new(initial_snapshot), database: Mutex::new(database), reattach_target: Mutex::new(None), bandwidth: Mutex::new(BandwidthBucket { tokens: 0.0, updated: std::time::Instant::now() }), transfer_controls: TransferRegistry::default(), job_bandwidth: Mutex::new(std::collections::HashMap::new()), lifecycle: Mutex::new(()), tray_checks: Mutex::new(None), progress: Mutex::new(ProgressThrottle::default()), viability: Mutex::new(std::collections::HashMap::new()), captures: Mutex::new(std::collections::VecDeque::new()), adoptable_names: Mutex::new(std::collections::HashSet::new()), publishing: Mutex::new(std::collections::HashSet::new()) });
            save_snapshot(&app.state::<CoreState>()).map_err(|error| error.to_string())?;
            install_tray(app.handle(), tray_intercept_downloads, tray_show_media_buttons)?;
            start_bridge(app.handle());
            if let Some(window) = app.get_webview_window("main") {
                let close_handle = app.handle().clone();
                window.on_window_event(move |event| {
                    if let WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let state = close_handle.state::<CoreState>();
                        let _ = apply_main_window_close(&close_handle, state.inner());
                    }
                });
            }
            if let Some(policy) = policy_from_args(&launch_args) {
                let state = app.state::<CoreState>();
                if let Err(error) = apply_browser_policy(app.handle(), state.inner(), policy) {
                    eprintln!("Browser policy update unavailable: {error}");
                }
            } else if let Some((id, input)) = commit_from_args(&launch_args) {
                if let Some(window) = app.get_webview_window("main") { let _ = window.hide(); }
                spawn_commit(app.handle().clone(), id, input);
            } else if let Some(input) = capture_input_from_args(&launch_args) {
                if let Some(window) = app.get_webview_window("main") { let _ = window.hide(); }
                let state = app.state::<CoreState>();
                let _ = start_provisional(app.handle().clone(), state.inner(), input, true, None);
            } else if launch_args.iter().any(|value| value == "--startup") && !show_manager_at_startup {
                if let Some(window) = app.get_webview_window("main") { let _ = window.hide(); }
            } else if launch_args.iter().any(|value| value == "--startup") {
                if let Some(window) = app.get_webview_window("main") { let _ = window.show(); }
            }
            for (id, source) in recovered { let _ = spawn_transfer(app.handle(), app.state::<CoreState>().inner(), id, source); }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_snapshot, open_path, pause_job, resume_job, retry_job, cancel_job, remove_job, pause_all, resume_all, create_provisional, commit_provisional, update_settings, reattach_job, main_window_action, start_window_drag])
        .run(tauri::generate_context!())
        .expect("error while running Download Manager");
}

fn configure_portable_webview2() {
    #[cfg(windows)]
    if let Ok(executable) = std::env::current_exe() {
        if let Some(parent) = executable.parent() {
            let runtime = parent.join("webview2");
            if runtime.join("msedgewebview2.exe").is_file() {
                std::env::set_var("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", runtime);
            }
        }
    }
}

