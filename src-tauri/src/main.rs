#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod media;

use futures_util::StreamExt;
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{io::{Read, SeekFrom, Write}, path::{Path, PathBuf}, process::{Command, Stdio}, sync::{atomic::{AtomicU64, Ordering}, Mutex}};
use tauri::{AppHandle, Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri::image::Image;
use tauri::menu::{CheckMenuItemBuilder, MenuBuilder, MenuItemBuilder};
use tauri::tray::TrayIconBuilder;
use tauri_plugin_notification::NotificationExt;
use tokio::{fs::{File, OpenOptions}, io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt}};
use tokio::time::{sleep, Duration};
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
    kind: String,
    state: String,
    progress: f64,
    downloaded: u64,
    total: Option<u64>,
    speed: u64,
    eta: Option<String>,
    connections: u32,
    max_connections: u32,
    mode: String,
    media: bool,
    media_details: Option<String>,
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
    events: Vec<JobEvent>,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct SegmentState { completed: u32, total: u32 }

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
    temp_folder: String,
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
struct AppSnapshot { jobs: Vec<DownloadJob>, settings: AppSettings, connected: bool, aggregate_speed: u64, notifications: Vec<NotificationItem> }

struct CoreState { snapshot: Mutex<AppSnapshot>, database: Mutex<Connection>, reattach_target: Mutex<Option<String>> }

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ProvisionalInput { source: String, name: Option<String>, media: Option<bool>, max_connections: Option<u32> }

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct CommitInput { name: String, destination: String, max_connections: Option<u32> }

fn now_label() -> String { "Just now".to_string() }

fn default_collision_behavior() -> String { "rename".into() }

fn clamp_connections(value: u32) -> u32 { value.clamp(1, 32) }

const EXTENSION_ID: &str = "mfdaoipoffnpeijnjminkdhecpnoemel";

fn sync_startup(enabled: bool) {
    #[cfg(windows)]
    {
        let key = "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run";
        if enabled {
            if let Ok(executable) = std::env::current_exe() { let value = format!("\"{}\" --startup", executable.display()); let _ = Command::new("reg.exe").args(["ADD", key, "/v", "Download Manager", "/t", "REG_SZ", "/d", &value, "/f"]).stdout(Stdio::null()).stderr(Stdio::null()).status(); }
        } else { let _ = Command::new("reg.exe").args(["DELETE", key, "/v", "Download Manager", "/f"]).stdout(Stdio::null()).stderr(Stdio::null()).status(); }
    }
    #[cfg(not(windows))]
    let _ = enabled;
}

fn register_native_host(root: &PathBuf) {
    #[cfg(windows)]
    {
        let Ok(executable) = std::env::current_exe() else { return; };
        let manifest_path = root.join("com.downloadmanager.host.json");
        let manifest = json!({ "name": "com.downloadmanager.host", "description": "Download Manager browser bridge", "path": executable, "type": "stdio", "allowed_origins": [format!("chrome-extension://{EXTENSION_ID}/")] });
        let Ok(contents) = serde_json::to_string_pretty(&manifest) else { return; };
        if std::fs::write(&manifest_path, contents).is_err() { return; }
        let manifest = manifest_path.to_string_lossy().into_owned();
        let keys = [
            "HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\com.downloadmanager.host",
            "HKCU\\Software\\Microsoft\\Edge\\NativeMessagingHosts\\com.downloadmanager.host",
            "HKCU\\Software\\BraveSoftware\\Brave-Browser\\NativeMessagingHosts\\com.downloadmanager.host",
            "HKCU\\Software\\Vivaldi\\NativeMessagingHosts\\com.downloadmanager.host",
        ];
        for key in keys { let _ = Command::new("reg.exe").arg("ADD").arg(key).arg("/ve").arg("/t").arg("REG_SZ").arg("/d").arg(&manifest).arg("/f").stdout(Stdio::null()).stderr(Stdio::null()).status(); }
    }
    #[cfg(not(windows))]
    let _ = root;
}

fn default_settings() -> AppSettings {
    let profile = std::env::var("USERPROFILE").unwrap_or_else(|_| ".".into());
    let local_app_data = std::env::var("LOCALAPPDATA").unwrap_or_else(|_| profile.clone());
    AppSettings { start_at_sign_in: true, show_manager_at_sign_in: true, close_behavior: "tray".into(), default_folder: format!("{profile}\\Downloads"), temp_folder: format!("{local_app_data}\\Download Manager\\Temp"), collision_behavior: default_collision_behavior(), intercept_downloads: true, show_media_buttons: true, excluded_sites: vec![], bandwidth_limit: None, bandwidth_unit: "MB/s".into(), max_connections: 8, per_download_overrides: true, retry_automatically: true, max_retries: 5, completion_notifications: true, failure_notifications: true, theme: "system".into(), accent: "#0878ed".into(), density: "comfortable".into() }
}

type BrowserPolicy = (bool, bool, Vec<String>);

fn browser_policy_root() -> PathBuf {
    std::env::var_os("APPDATA").map(PathBuf::from).map(|path| path.join("com.downloadmanager.app")).unwrap_or_else(|| PathBuf::from("."))
}

fn browser_policy_value(policy: &BrowserPolicy) -> Value {
    json!({ "interceptDownloads": policy.0, "showMediaButtons": policy.1, "excludedSites": policy.2 })
}

fn browser_policy_from_value(value: &Value) -> Option<BrowserPolicy> {
    let payload = value.get("payload").unwrap_or(value);
    let intercept = payload.get("interceptDownloads").and_then(Value::as_bool)?;
    let media = payload.get("showMediaButtons").and_then(Value::as_bool)?;
    let excluded = payload.get("excludedSites").and_then(Value::as_array)?.iter().filter_map(Value::as_str).map(str::to_ascii_lowercase).filter(|site| !site.is_empty()).collect::<Vec<_>>();
    Some((intercept, media, excluded))
}

fn write_browser_policy(root: &Path, policy: &BrowserPolicy) {
    let _ = std::fs::create_dir_all(root);
    if let Ok(contents) = serde_json::to_string(&browser_policy_value(policy)) { let _ = std::fs::write(root.join("browser-policy.json"), contents); }
}

fn load_browser_policy(root: &Path) -> Option<BrowserPolicy> {
    let contents = std::fs::read_to_string(root.join("browser-policy.json")).ok()?;
    browser_policy_from_value(&serde_json::from_str(&contents).ok()?)
}

fn settings_policy(settings: &AppSettings) -> BrowserPolicy {
    (settings.intercept_downloads, settings.show_media_buttons, settings.excluded_sites.clone())
}

fn apply_browser_policy(app: &AppHandle, state: &CoreState, policy: BrowserPolicy) {
    if let Ok(mut snapshot) = state.snapshot.lock() {
        snapshot.settings.intercept_downloads = policy.0;
        snapshot.settings.show_media_buttons = policy.1;
        snapshot.settings.excluded_sites = policy.2;
        write_browser_policy(&browser_policy_root(), &settings_policy(&snapshot.settings));
    }
    emit_snapshot(app, state);
}

fn snapshot_from_database(database: &Connection, settings: AppSettings) -> AppSnapshot {
    let mut jobs = Vec::new();
    if let Ok(mut statement) = database.prepare("SELECT payload FROM jobs ORDER BY created_at DESC") {
        if let Ok(rows) = statement.query_map([], |row| row.get::<_, String>(0)) {
            for payload in rows.flatten() {
                if let Ok(job) = serde_json::from_str::<DownloadJob>(&payload) {
                    if job.provisional == Some(true) {
                        let _ = std::fs::remove_file(&job.temp_path);
                        let _ = std::fs::remove_dir_all(format!("{}.segments", job.temp_path));
                        continue;
                    }
                    jobs.push(job);
                }
            }
        }
    }
    AppSnapshot { jobs, settings, connected: true, aggregate_speed: 0, notifications: vec![] }
}

fn save_snapshot(state: &CoreState) {
    if let Ok(snapshot) = state.snapshot.lock() {
        if let Ok(database) = state.database.lock() {
            let _ = database.execute("INSERT OR REPLACE INTO settings (id, payload) VALUES (1, ?1)", params![serde_json::to_string(&snapshot.settings).unwrap_or_default()]);
            let _ = database.execute("DELETE FROM jobs", []);
            for job in &snapshot.jobs {
                let _ = database.execute("INSERT INTO jobs (id, created_at, payload) VALUES (?1, ?2, ?3)", params![job.id, job.created, serde_json::to_string(job).unwrap_or_default()]);
            }
        }
    }
}

fn emit_snapshot(app: &AppHandle, state: &CoreState) {
    if let Ok(snapshot) = state.snapshot.lock() { let _ = app.emit("state-changed", snapshot.clone()); }
    save_snapshot(state);
}

fn job_event(message: &str, tone: Option<&str>) -> JobEvent { JobEvent { at: now_label(), message: message.into(), tone: tone.map(str::to_string) } }

fn domain(source: &str) -> String { reqwest::Url::parse(source).ok().and_then(|url| url.host_str().map(str::to_string)).unwrap_or_else(|| "source unavailable".into()) }

fn source_compatible(existing: &str, candidate: &str) -> bool {
    let Some(existing) = reqwest::Url::parse(existing).ok() else { return false; };
    let Some(candidate) = reqwest::Url::parse(candidate).ok() else { return false; };
    existing.scheme() == candidate.scheme() && existing.host() == candidate.host() && existing.path() == candidate.path()
}

fn source_name(source: &str) -> String { reqwest::Url::parse(source).ok().and_then(|url| url.path_segments().and_then(|segments| segments.last()).map(str::to_string)).filter(|name| !name.is_empty()).unwrap_or_else(|| "download.bin".into()) }

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

fn identities_match(existing: Option<&ResourceIdentity>, current: &ResourceIdentity) -> bool {
    let Some(existing) = existing else { return false; };
    existing.length == current.length && existing.etag == current.etag && existing.last_modified == current.last_modified
}

fn valid_range_identity(response: &reqwest::Response, expected: &ResourceIdentity) -> bool {
    let etag = header_string(response, reqwest::header::ETAG);
    let last_modified = header_string(response, reqwest::header::LAST_MODIFIED);
    expected.etag.as_ref().map_or(true, |value| etag.as_ref() == Some(value)) && expected.last_modified.as_ref().map_or(true, |value| last_modified.as_ref() == Some(value))
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

fn emit_job(state: &CoreState, id: &str, update: impl FnOnce(&mut DownloadJob)) {
    if let Ok(mut snapshot) = state.snapshot.lock() { if let Some(job) = snapshot.jobs.iter_mut().find(|job| job.id == id) { update(job); snapshot.aggregate_speed = snapshot.jobs.iter().filter(|item| item.state == "downloading").map(|item| item.speed).sum(); } }
}

fn add_notification(app: &AppHandle, state: &CoreState, id: &str, kind: &str) {
    let (item, enabled) = match state.snapshot.lock() {
        Ok(mut snapshot) => {
            let Some(job) = snapshot.jobs.iter().find(|job| job.id == id).cloned() else { return; };
            let enabled = if kind == "completed" { snapshot.settings.completion_notifications } else { snapshot.settings.failure_notifications };
            let item = NotificationItem { id: format!("{kind}-{id}"), notification_type: kind.into(), title: if kind == "completed" { "Download completed".into() } else { "Download failed".into() }, detail: if kind == "completed" { format!("{} · {}", job.name, format_bytes(job.total)) } else { format!("{} · {}", job.name, job.error.unwrap_or_else(|| "The source could not be acquired".into())) }, time: now_label(), job_id: id.into() };
            if enabled && !snapshot.notifications.iter().any(|current| current.id == item.id) { snapshot.notifications.insert(0, item.clone()); snapshot.notifications.truncate(40); }
            (item, enabled)
        }
        Err(_) => return,
    };
    emit_snapshot(app, state);
    if enabled { let _ = app.notification().builder().title(item.title).body(item.detail).show(); }
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

fn retryable_status(status: reqwest::StatusCode) -> bool {
    status == reqwest::StatusCode::REQUEST_TIMEOUT || status == reqwest::StatusCode::TOO_MANY_REQUESTS || status.is_server_error()
}

async fn finalize_media(temp_path: &str, destination: &str) -> Result<(), String> {
    let extension = PathBuf::from(destination).extension().and_then(|value| value.to_str()).map(str::to_ascii_lowercase);
    if matches!(extension.as_deref(), None | Some("ts") | Some("m4s")) { return Ok(()); }
    let output_path = format!("{temp_path}.final.{}", extension.as_deref().unwrap_or("mkv"));
    let input = temp_path.to_string();
    let output = output_path.clone();
    let result = tokio::task::spawn_blocking(move || Command::new("ffmpeg").args(["-hide_banner", "-loglevel", "error", "-y", "-i", &input, "-map", "0", "-c", "copy", &output]).status()).await.map_err(|error| error.to_string())?;
    match result {
        Ok(status) if status.success() => { tokio::fs::rename(&output_path, temp_path).await.map_err(|error| error.to_string())?; Ok(()) }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.to_string()),
        Ok(_) => { let _ = tokio::fs::remove_file(&output_path).await; Err("Media finalization failed; downloaded parts were preserved".into()) }
    }
}

async fn move_completed_file(source: &str, destination: &str) -> Result<(), String> {
    if let Some(parent) = PathBuf::from(destination).parent() { tokio::fs::create_dir_all(parent).await.map_err(|error| error.to_string())?; }
    match tokio::fs::rename(source, destination).await {
        Ok(()) => Ok(()),
        Err(error) if matches!(error.raw_os_error(), Some(17) | Some(18)) => {
            match tokio::fs::copy(source, destination).await {
                Ok(_) => match tokio::fs::remove_file(source).await {
                    Ok(()) => Ok(()),
                    Err(remove_error) => { let _ = tokio::fs::remove_file(destination).await; Err(remove_error.to_string()) }
                },
                Err(copy_error) => { let _ = tokio::fs::remove_file(destination).await; Err(format!("{error}; fallback copy failed: {copy_error}")) }
            }
        }
        Err(error) => Err(error.to_string()),
    }
}

fn media_extension(destination: &str) -> String {
    PathBuf::from(destination).extension().and_then(|value| value.to_str()).map(str::to_ascii_lowercase).unwrap_or_else(|| "mkv".into())
}

async fn mux_media_tracks(track_paths: &[String], output_path: &str) -> Result<(), String> {
    if track_paths.len() < 2 { return Err("Separate media tracks require at least two inputs".into()); }
    let paths = track_paths.to_vec();
    let output = output_path.to_string();
    let result = tokio::task::spawn_blocking(move || {
        let mut command = Command::new("ffmpeg");
        command.args(["-hide_banner", "-loglevel", "error", "-y"]);
        for path in &paths { command.arg("-i").arg(path); }
        for index in 0..paths.len() { command.args(["-map", &format!("{index}:0")]); }
        command.args(["-c", "copy", &output]).status()
    }).await.map_err(|error| error.to_string())?;
    match result {
        Ok(status) if status.success() => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Err("FFmpeg is required to combine separate audio and video tracks".into()),
        Err(error) => Err(error.to_string()),
        Ok(_) => { let _ = tokio::fs::remove_file(output_path).await; Err("Media track finalization failed; downloaded parts were preserved".into()) }
    }
}

fn job_state(app: &AppHandle, id: &str) -> Option<String> {
    let state = app.state::<CoreState>();
    state.snapshot.lock().ok().and_then(|snapshot| snapshot.jobs.iter().find(|job| job.id == id).map(|job| job.state.clone()))
}

async fn throttle(app: &AppHandle, bytes: usize) {
    let state = app.state::<CoreState>();
    let limit = state.snapshot.lock().ok().and_then(|snapshot| snapshot.settings.bandwidth_limit.map(|value| (value, snapshot.settings.bandwidth_unit.clone(), snapshot.jobs.iter().filter(|job| ["connecting", "downloading", "finalizing"].contains(&job.state.as_str())).count().max(1) as u64)));
    let Some((value, unit, active)) = limit else { return; };
    let multiplier = match unit.as_str() { "GB/s" => 1024f64 * 1024f64 * 1024f64, "MB/s" => 1024f64 * 1024f64, _ => 1024f64 };
    let bytes_per_second = value as f64 * multiplier / active as f64;
    if bytes_per_second > 0.0 { sleep(Duration::from_secs_f64(bytes as f64 / bytes_per_second)).await; }
}

async fn fragment_bytes(client: &reqwest::Client, source: &str, retries: u32) -> Result<Vec<u8>, String> {
    let attempts = retries.saturating_add(1).max(1);
    let mut last_error = String::from("fragment request failed");
    for _ in 0..attempts {
        match client.get(source).send().await {
            Ok(response) if response.status().is_success() => match response.bytes().await { Ok(bytes) => return Ok(bytes.to_vec()), Err(error) => last_error = error.to_string() },
            Ok(response) => last_error = format!("source returned {}", response.status()),
            Err(error) => last_error = error.to_string(),
        }
    }
    Err(last_error)
}

fn content_range(response: &reqwest::Response) -> Option<(u64, u64, u64)> {
    let value = response.headers().get(reqwest::header::CONTENT_RANGE)?.to_str().ok()?;
    let (range, total) = value.strip_prefix("bytes ")?.split_once('/')?;
    let (start, end) = range.split_once('-')?;
    Some((start.parse().ok()?, end.parse().ok()?, total.parse().ok()?))
}

async fn range_bytes(client: &reqwest::Client, source: &str, start: u64, end: u64, retries: u32, expected: &ResourceIdentity) -> Result<Vec<u8>, String> {
    let attempts = retries.saturating_add(1).max(1);
    let mut last_error = String::from("range request failed");
    for _ in 0..attempts {
        match client.get(source).header(reqwest::header::RANGE, format!("bytes={start}-{end}")).send().await {
            Ok(response) if response.status() == reqwest::StatusCode::PARTIAL_CONTENT => {
                let valid_range = content_range(&response).map(|(actual_start, actual_end, actual_total)| actual_start == start && actual_end == end && actual_total == expected.length).unwrap_or(false);
                if !valid_range { last_error = "The server returned an invalid byte range".into(); continue; }
                if !valid_range_identity(&response, expected) { last_error = "The resource changed while it was being acquired".into(); continue; }
                match response.bytes().await { Ok(bytes) if bytes.len() as u64 == end - start + 1 => return Ok(bytes.to_vec()), Ok(_) => last_error = "The server returned an incomplete byte range".into(), Err(error) => last_error = error.to_string() }
            }
            Ok(response) => last_error = format!("range request returned {}", response.status()),
            Err(error) => last_error = error.to_string(),
        }
    }
    Err(last_error)
}

async fn acquire_ranges(app: AppHandle, id: String, source: String, response: reqwest::Response, total: u64) -> Result<(), String> {
    let state = app.state::<CoreState>();
    let (temp_path, max_connections, retry_count, replace_existing) = state.snapshot.lock().map_err(|_| "State unavailable".to_string()).and_then(|snapshot| snapshot.jobs.iter().find(|job| job.id == id).map(|job| (job.temp_path.clone(), job.max_connections, if snapshot.settings.retry_automatically { snapshot.settings.max_retries } else { 0 }, snapshot.settings.collision_behavior == "replace")).ok_or_else(|| "Acquisition no longer exists".to_string()))?;
    let identity = identity_from_response(&response, total);
    let first = response.bytes().await.map_err(|error| error.to_string())?;
    if first.len() != 1 { return Err("The range probe returned an unexpected payload".into()); }
    let Some(parent) = PathBuf::from(&temp_path).parent().map(PathBuf::from) else { return Err("Temporary path is invalid".into()); };
    tokio::fs::create_dir_all(parent).await.map_err(|error| error.to_string())?;
    let existing = state.snapshot.lock().ok().and_then(|snapshot| snapshot.jobs.iter().find(|job| job.id == id).map(|job| (job.resource_identity.clone(), job.completed_ranges.clone())));
    let mut completed_ranges = existing.as_ref().filter(|(stored_identity, ranges)| identities_match(stored_identity.as_ref(), &identity) && !ranges.is_empty()).map(|(_, ranges)| ranges.iter().filter(|range| range.start <= range.end && range.end < total).cloned().collect::<Vec<_>>()).unwrap_or_default();
    completed_ranges = completed_ranges.into_iter().fold(Vec::new(), |ranges, range| merge_range(&ranges, range));
    let mut can_resume = !completed_ranges.is_empty() && tokio::fs::metadata(&temp_path).await.map(|metadata| metadata.len() == total).unwrap_or(false);
    if can_resume {
        let mut existing_file = File::open(&temp_path).await.map_err(|error| error.to_string())?;
        let mut existing_first = [0u8; 1];
        can_resume = existing_file.read_exact(&mut existing_first).await.is_ok() && existing_first[0] == first[0];
    }
    if !can_resume {
        completed_ranges = vec![ByteRange { start: 0, end: 0 }];
        let mut initial = File::create(&temp_path).await.map_err(|error| error.to_string())?;
        initial.write_all(&first).await.map_err(|error| error.to_string())?;
        initial.set_len(total).await.map_err(|error| error.to_string())?;
    } else if !completed_ranges.iter().any(|range| range.start == 0) {
        let mut initial = OpenOptions::new().write(true).open(&temp_path).await.map_err(|error| error.to_string())?;
        initial.seek(SeekFrom::Start(0)).await.map_err(|error| error.to_string())?;
        initial.write_all(&first).await.map_err(|error| error.to_string())?;
        completed_ranges = merge_range(&completed_ranges, ByteRange { start: 0, end: 0 });
    }
    let ranges = missing_ranges(total, &completed_ranges, max_connections);
    let worker_count = max_connections.clamp(1, 32).min(ranges.len().max(1) as u32) as usize;
    let initial_downloaded = covered_bytes(&completed_ranges).min(total);
    emit_job(&state, &id, |job| { job.state = "downloading".into(); job.total = Some(total); job.downloaded = initial_downloaded; job.progress = initial_downloaded as f64 / total as f64 * 100.0; job.resumable = true; job.mode = "whole-object".into(); job.resource_identity = Some(identity.clone()); job.completed_ranges = completed_ranges.clone(); job.connections = if ranges.is_empty() { 0 } else { worker_count as u32 }; job.events.insert(0, job_event(&format!("Range support verified; {worker_count} workers started"), Some("success"))); });
    emit_snapshot(&app, &state);
    let downloaded = std::sync::Arc::new(AtomicU64::new(initial_downloaded));
    let completed_workers = std::sync::Arc::new(AtomicU64::new(0));
    let total_ranges = ranges.len() as u64;
    let mut transfers = futures_util::stream::iter(ranges.into_iter().map(|(start, end)| {
        let client = http_client();
        let source = source.clone();
        let temp_path = temp_path.clone();
        let app = app.clone();
        let id = id.clone();
        let identity = identity.clone();
        let downloaded = downloaded.clone();
        let completed_workers = completed_workers.clone();
        async move {
            if job_state(&app, &id).as_deref() != Some("downloading") { return Err("paused".to_string()); }
            let bytes = range_bytes(&client, &source, start, end, retry_count, &identity).await?;
            if job_state(&app, &id).as_deref() != Some("downloading") { return Err("paused".to_string()); }
            let mut file = OpenOptions::new().write(true).open(&temp_path).await.map_err(|error| error.to_string())?;
            file.seek(SeekFrom::Start(start)).await.map_err(|error| error.to_string())?;
            file.write_all(&bytes).await.map_err(|error| error.to_string())?;
            throttle(&app, bytes.len()).await;
            let total_downloaded = downloaded.fetch_add(bytes.len() as u64, Ordering::Relaxed) + bytes.len() as u64;
            let finished = completed_workers.fetch_add(1, Ordering::Relaxed) + 1;
            let state = app.state::<CoreState>();
            emit_job(&state, &id, |job| { job.downloaded = total_downloaded; job.progress = total_downloaded as f64 / total as f64 * 100.0; job.completed_ranges = merge_range(&job.completed_ranges, ByteRange { start, end }); job.connections = worker_count.min(total_ranges.saturating_sub(finished) as usize) as u32; });
            emit_snapshot(&app, &state);
            Ok::<(), String>(())
        }
    })).buffer_unordered(worker_count);
    let mut transfer_error = None;
    while let Some(result) = transfers.next().await { if let Err(error) = result { transfer_error = Some(error); } }
    if let Some(error) = transfer_error {
        if job_state(&app, &id).as_deref() == Some("paused") { emit_job(&state, &id, |job| { job.connections = 0; job.speed = 0; job.events.insert(0, job_event("Paused with verified byte ranges preserved", Some("warning"))); }); emit_snapshot(&app, &state); return Ok(()); }
        return Err(error);
    }
    let committed = state.snapshot.lock().ok().and_then(|snapshot| snapshot.jobs.iter().find(|job| job.id == id).map(|job| (job.provisional != Some(true), job.destination.clone()))).unwrap_or((false, String::new()));
    if committed.0 && !committed.1.is_empty() { if let Some(parent) = PathBuf::from(&committed.1).parent() { let _ = tokio::fs::create_dir_all(parent).await; } if replace_existing { let _ = tokio::fs::remove_file(&committed.1).await; } move_completed_file(&temp_path, &committed.1).await?; }
    emit_job(&state, &id, |job| { job.speed = 0; job.connections = 0; if committed.0 { job.state = "completed".into(); job.progress = 100.0; job.completed = Some(now_label()); job.events.insert(0, job_event("Download completed", Some("success"))); } else { job.state = "finalizing".into(); job.progress = 100.0; job.eta = Some("Ready to save".into()); job.events.insert(0, job_event("Download ready; waiting for destination", Some("warning"))); } });
    emit_snapshot(&app, &state);
    if committed.0 { add_notification(&app, &state, &id, "completed"); }
    Ok(())
}

fn manifest_segment_path(directory: &Path, track: usize, index: usize, track_count: usize) -> PathBuf {
    if track_count == 1 { directory.join(format!("{index:08}.part")) } else { directory.join(format!("{track:02}")).join(format!("{index:08}.part")) }
}

async fn acquire_manifest(app: AppHandle, id: String, source: String, body: String, mime: Option<String>) -> Result<(), String> {
    let client = http_client();
    let mut manifest_source = source;
    let mut manifest_body = body;
    let mut hls_track_sources = None;
    for _ in 0..4 {
        let is_hls = manifest_source.to_ascii_lowercase().contains(".m3u8") || manifest_body.contains("#EXTM3U");
        if !is_hls { break; }
        if let Some(sources) = media::hls_variant_tracks(&manifest_source, &manifest_body) { hls_track_sources = Some(sources); break; }
        let Some(variant) = (if is_hls { media::hls_variant(&manifest_source, &manifest_body) } else { None }) else { break };
        manifest_body = client.get(&variant).send().await.map_err(|error| error.to_string())?.error_for_status().map_err(|error| error.to_string())?.text().await.map_err(|error| error.to_string())?;
        manifest_source = variant;
    }
    let is_hls = manifest_source.to_ascii_lowercase().contains(".m3u8") || manifest_body.contains("#EXTM3U");
    let tracks = if let Some(sources) = hls_track_sources {
        let mut tracks = Vec::with_capacity(sources.len());
        for (kind, track_source) in sources {
            let mut source = track_source;
            let mut body = client.get(&source).send().await.map_err(|error| error.to_string())?.error_for_status().map_err(|error| error.to_string())?.text().await.map_err(|error| error.to_string())?;
            for _ in 0..4 {
                let Some(variant) = media::hls_variant(&source, &body) else { break; };
                body = client.get(&variant).send().await.map_err(|error| error.to_string())?.error_for_status().map_err(|error| error.to_string())?.text().await.map_err(|error| error.to_string())?;
                source = variant;
            }
            tracks.push(media::MediaTrack { kind, segments: media::parse_hls(&source, &body)? });
        }
        tracks
    } else if is_hls { vec![media::MediaTrack { kind: "video".into(), segments: media::parse_hls(&manifest_source, &manifest_body)? }] } else { media::parse_dash_tracks(&manifest_source, &manifest_body)? };
    let track_count = tracks.len();
    let track_lengths = tracks.iter().map(|track| track.segments.len()).collect::<Vec<_>>();
    let track_kinds = tracks.iter().map(|track| if track.kind.is_empty() { "media" } else { track.kind.as_str() }).collect::<Vec<_>>().join(" + ");
    let total_segments = track_lengths.iter().sum::<usize>();
    if total_segments == 0 { return Err("The manifest did not contain any downloadable segments".into()); }
    let state = app.state::<CoreState>();
    let (temp_path, max_connections, retry_count, replace_existing) = state.snapshot.lock().map_err(|_| "State unavailable".to_string()).and_then(|snapshot| snapshot.jobs.iter().find(|job| job.id == id).map(|job| (job.temp_path.clone(), job.max_connections, if snapshot.settings.retry_automatically { snapshot.settings.max_retries } else { 0 }, snapshot.settings.collision_behavior == "replace")).ok_or_else(|| "Acquisition no longer exists".to_string()))?;
    let segment_dir = PathBuf::from(format!("{temp_path}.segments"));
    tokio::fs::create_dir_all(&segment_dir).await.map_err(|error| error.to_string())?;
    let total_segments = total_segments as u32;
    let concurrency = max_connections.clamp(1, total_segments) as usize;
    let mut existing_segments: Vec<(usize, usize)> = Vec::new();
    let mut existing_bytes = 0u64;
    for (track, length) in track_lengths.iter().enumerate() {
        if track_count > 1 { tokio::fs::create_dir_all(segment_dir.join(format!("{track:02}"))).await.map_err(|error| error.to_string())?; }
        for index in 0..*length {
            let path = manifest_segment_path(&segment_dir, track, index, track_count);
            if let Ok(metadata) = tokio::fs::metadata(path).await {
                if metadata.is_file() && metadata.len() > 0 { existing_segments.push((track, index)); existing_bytes = existing_bytes.saturating_add(metadata.len()); }
            }
        }
    }
    let missing_count = total_segments as usize - existing_segments.len();
    let existing_count = existing_segments.len() as u64;
    emit_job(&state, &id, |job| { job.state = "downloading".into(); job.mode = "segments".into(); job.media = true; job.mime = mime.clone(); job.media_tracks = Some(track_count as u32); job.resumable = true; job.downloaded = existing_bytes; job.progress = existing_segments.len() as f64 / total_segments as f64 * 100.0; job.connections = concurrency.min(missing_count) as u32; job.segments = Some(SegmentState { completed: existing_segments.len() as u32, total: total_segments }); job.events.insert(0, job_event(&format!("Manifest parsed: {total_segments} fragments across {track_kinds}"), Some("success"))); });
    emit_snapshot(&app, &state);
    let completed = std::sync::Arc::new(AtomicU64::new(existing_segments.len() as u64));
    let downloaded = std::sync::Arc::new(AtomicU64::new(existing_bytes));
    let existing_segments = std::sync::Arc::new(existing_segments);
    let work = tracks.into_iter().enumerate().flat_map(|(track, media_track)| media_track.segments.into_iter().enumerate().map(move |(index, fragment)| (track, index, fragment))).filter(|(track, index, _)| !existing_segments.contains(&(*track, *index)));
    let results = futures_util::stream::iter(work).map(|(track, index, fragment)| {
        let client = client.clone();
        let app = app.clone();
        let id = id.clone();
        let segment_path = manifest_segment_path(&segment_dir, track, index, track_count);
        let segment_temp_path = segment_path.with_extension("part.tmp");
        let completed = completed.clone();
        let downloaded = downloaded.clone();
        async move {
            if !matches!(job_state(&app, &id).as_deref(), Some("connecting") | Some("downloading") | Some("finalizing")) { return Err("paused".to_string()); }
            let bytes = fragment_bytes(&client, &fragment.url, retry_count).await?;
            if let Some(parent) = segment_path.parent() { tokio::fs::create_dir_all(parent).await.map_err(|error| error.to_string())?; }
            tokio::fs::write(&segment_temp_path, &bytes).await.map_err(|error| error.to_string())?;
            tokio::fs::rename(&segment_temp_path, &segment_path).await.map_err(|error| error.to_string())?;
            throttle(&app, bytes.len()).await;
            let done = completed.fetch_add(1, Ordering::Relaxed) + 1;
            let size = downloaded.fetch_add(bytes.len() as u64, Ordering::Relaxed) + bytes.len() as u64;
            let state = app.state::<CoreState>();
            let finished_missing = done.saturating_sub(existing_count);
            emit_job(&state, &id, |job| { job.downloaded = size; job.progress = done as f64 / total_segments as f64 * 100.0; job.segments = Some(SegmentState { completed: done as u32, total: total_segments }); job.connections = concurrency.min((missing_count as u64).saturating_sub(finished_missing) as usize) as u32; });
            emit_snapshot(&app, &state);
            Ok::<(), String>(())
        }
    }).buffer_unordered(concurrency).collect::<Vec<_>>().await;
    if results.iter().any(Result::is_err) {
        if matches!(job_state(&app, &id).as_deref(), Some("paused")) { emit_job(&state, &id, |job| { job.connections = 0; job.speed = 0; job.events.insert(0, job_event("Paused with completed fragments preserved", Some("warning"))); }); emit_snapshot(&app, &state); return Ok(()); }
        let error = results.into_iter().find_map(Result::err).unwrap_or_else(|| "A media fragment failed".into());
        emit_job(&state, &id, |job| { job.state = "failed".into(); job.error = Some(error.clone()); job.connections = 0; job.speed = 0; job.events.insert(0, job_event("Media fragment acquisition failed", Some("error"))); });
        emit_snapshot(&app, &state);
        return Err(error);
    }
    emit_job(&state, &id, |job| { job.state = "finalizing".into(); job.connections = 0; job.events.insert(0, job_event("Assembling ordered media fragments", Some("warning"))); });
    emit_snapshot(&app, &state);
    let mut track_paths = Vec::with_capacity(track_count);
    for (track, length) in track_lengths.iter().enumerate() {
        let output_path = if track_count == 1 { temp_path.clone() } else { format!("{temp_path}.track-{track:02}") };
        let mut output = File::create(&output_path).await.map_err(|error| error.to_string())?;
        for index in 0..*length {
            let path = manifest_segment_path(&segment_dir, track, index, track_count);
            let bytes = tokio::fs::read(&path).await.map_err(|error| error.to_string())?;
            output.write_all(&bytes).await.map_err(|error| error.to_string())?;
        }
        drop(output);
        track_paths.push(output_path);
    }
    let committed = state.snapshot.lock().ok().and_then(|snapshot| snapshot.jobs.iter().find(|job| job.id == id).map(|job| (job.provisional != Some(true), job.destination.clone()))).unwrap_or((false, String::new()));
    if committed.0 && !committed.1.is_empty() {
        let final_path = if track_count > 1 {
            let mux_path = format!("{temp_path}.mux.{}", media_extension(&committed.1));
            mux_media_tracks(&track_paths, &mux_path).await?;
            mux_path
        } else {
            finalize_media(&temp_path, &committed.1).await?;
            temp_path.clone()
        };
        if let Some(parent) = PathBuf::from(&committed.1).parent() { let _ = tokio::fs::create_dir_all(parent).await; }
        if replace_existing { let _ = tokio::fs::remove_file(&committed.1).await; }
        move_completed_file(&final_path, &committed.1).await?;
        let _ = tokio::fs::remove_dir_all(&segment_dir).await;
    }
    emit_job(&state, &id, |job| { job.speed = 0; job.connections = 0; if committed.0 { job.state = "completed".into(); job.progress = 100.0; job.completed = Some(now_label()); job.events.insert(0, job_event("Download completed", Some("success"))); } else { job.state = "finalizing".into(); job.progress = 100.0; job.eta = Some("Ready to save".into()); job.events.insert(0, job_event(if track_count > 1 { "Tracks assembled; waiting for destination" } else { "Fragments assembled; waiting for destination" }, Some("warning"))); } });
    emit_snapshot(&app, &state);
    if committed.0 { add_notification(&app, &state, &id, "completed"); }
    Ok(())
}

async fn acquire_once(app: AppHandle, id: String, source: String) -> bool {
    let state = app.state::<CoreState>();
    let client = http_client();
    let mut response = match client.get(&source).header(reqwest::header::RANGE, "bytes=0-0").send().await {
        Ok(response) if response.status().is_success() => response,
        _ => match client.get(&source).send().await {
            Ok(response) if response.status().is_success() => response,
            Ok(response) => { let retryable = retryable_status(response.status()); emit_job(&state, &id, |job| { job.state = "failed".into(); job.error = Some(format!("Source returned {}", response.status())); job.eta = None; job.events.insert(0, job_event("Source rejected the acquisition", Some("error"))); }); emit_snapshot(&app, &state); add_notification(&app, &state, &id, "failed"); return retryable; }
            Err(error) => { emit_job(&state, &id, |job| { job.state = "failed".into(); job.error = Some(error.to_string()); job.eta = None; job.events.insert(0, job_event("Could not connect to source", Some("error"))); }); emit_snapshot(&app, &state); add_notification(&app, &state, &id, "failed"); return true; }
        }
    };
    let valid_probe = response.status() != reqwest::StatusCode::PARTIAL_CONTENT || content_range(&response).map(|(start, end, total)| start == 0 && end == 0 && total > 0).unwrap_or(false);
    if !valid_probe {
        response = match client.get(&source).send().await {
            Ok(response) if response.status().is_success() && response.status() != reqwest::StatusCode::PARTIAL_CONTENT => response,
            Ok(_response) => { emit_job(&state, &id, |job| { job.state = "failed".into(); job.error = Some("The source returned an invalid partial response".into()); job.eta = None; job.events.insert(0, job_event("Source returned an invalid partial response", Some("error"))); }); emit_snapshot(&app, &state); add_notification(&app, &state, &id, "failed"); return false; }
            Err(error) => { emit_job(&state, &id, |job| { job.state = "failed".into(); job.error = Some(error.to_string()); job.eta = None; job.events.insert(0, job_event("Could not connect to source", Some("error"))); }); emit_snapshot(&app, &state); add_notification(&app, &state, &id, "failed"); return true; }
        };
    }
    let response_mime = response.headers().get(reqwest::header::CONTENT_TYPE).and_then(|value| value.to_str().ok()).map(str::to_string);
    if media::is_manifest_source(&source, response_mime.as_deref()) {
        let result = match response.text().await { Ok(body) => acquire_manifest(app.clone(), id.clone(), source.clone(), body, response_mime.clone()).await, Err(error) => Err(error.to_string()) };
        if let Err(error) = result { if job_state(&app, &id).as_deref() != Some("failed") { emit_job(&state, &id, |job| { job.state = "failed".into(); job.error = Some(error.clone()); job.connections = 0; job.events.insert(0, job_event("Manifest acquisition failed", Some("error"))); }); emit_snapshot(&app, &state); } add_notification(&app, &state, &id, "failed"); }
        return false;
    }
    if let Some((start, end, ranged_total)) = content_range(&response) { if response.status() == reqwest::StatusCode::PARTIAL_CONTENT && start == 0 && end == 0 && ranged_total > 1 { if let Err(error) = acquire_ranges(app.clone(), id.clone(), source.clone(), response, ranged_total).await { if job_state(&app, &id).as_deref() != Some("failed") { emit_job(&state, &id, |job| { job.state = "failed".into(); job.error = Some(error.clone()); job.connections = 0; job.events.insert(0, job_event("Range acquisition failed", Some("error"))); }); emit_snapshot(&app, &state); add_notification(&app, &state, &id, "failed"); } } return false; } }
    let total = response.content_length();
    let temp_path = state.snapshot.lock().ok().and_then(|snapshot| snapshot.jobs.iter().find(|job| job.id == id).map(|job| job.temp_path.clone()));
    let Some(temp_path) = temp_path else { return false; };
    if let Some(parent) = PathBuf::from(&temp_path).parent() {
        if let Err(error) = tokio::fs::create_dir_all(parent).await {
            emit_job(&state, &id, |job| { job.state = "failed".into(); job.error = Some(format!("Could not create the temporary folder: {error}")); job.events.insert(0, job_event("Could not create the temporary folder", Some("error"))); });
            emit_snapshot(&app, &state);
            add_notification(&app, &state, &id, "failed");
            return false;
        }
    }
    let Ok(mut file) = File::create(&temp_path).await else {
        emit_job(&state, &id, |job| { job.state = "failed".into(); job.error = Some("Could not open the temporary file".into()); job.events.insert(0, job_event("Could not open the temporary file", Some("error"))); });
        emit_snapshot(&app, &state);
        add_notification(&app, &state, &id, "failed");
        return false;
    };
    emit_job(&state, &id, |job| { job.state = "downloading".into(); job.total = total; job.resumable = total.is_some(); job.connections = 1; job.mode = "single-stream".into(); job.mime = response.headers().get(reqwest::header::CONTENT_TYPE).and_then(|value| value.to_str().ok()).map(str::to_string); job.events.insert(0, job_event("First native acquisition is receiving data", Some("success"))); });
    emit_snapshot(&app, &state);
    let started = std::time::Instant::now();
    let mut downloaded = 0u64;
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        if job_state(&app, &id).as_deref() != Some("downloading") { drop(stream); drop(file); emit_job(&state, &id, |job| { job.speed = 0; job.connections = 0; job.eta = Some("Paused".into()); }); emit_snapshot(&app, &state); return false; }
        match chunk {
            Ok(bytes) => {
                if let Err(error) = file.write_all(&bytes).await { emit_job(&state, &id, |job| { job.state = "failed".into(); job.error = Some(error.to_string()); job.speed = 0; job.connections = 0; job.events.insert(0, job_event("Could not write temporary data", Some("error"))); }); emit_snapshot(&app, &state); add_notification(&app, &state, &id, "failed"); return false; }
                downloaded += bytes.len() as u64;
                throttle(&app, bytes.len()).await;
                let speed = (downloaded as f64 / started.elapsed().as_secs_f64().max(0.1)) as u64;
                emit_job(&state, &id, |job| { job.downloaded = downloaded; job.speed = speed; job.progress = total.map(|value| downloaded as f64 / value as f64 * 100.0).unwrap_or(0.0); job.eta = total.and_then(|value| if speed > 0 { Some(format!("{}s left", (value.saturating_sub(downloaded) / speed).max(1))) } else { None }); });
                emit_snapshot(&app, &state);
            }
            Err(error) => { emit_job(&state, &id, |job| { job.state = "failed".into(); job.error = Some(error.to_string()); job.speed = 0; job.connections = 0; job.events.insert(0, job_event("Network stream interrupted", Some("error"))); }); emit_snapshot(&app, &state); add_notification(&app, &state, &id, "failed"); return true; }
        }
    }
    drop(file);
    let replace_existing = state.snapshot.lock().ok().map(|snapshot| snapshot.settings.collision_behavior == "replace").unwrap_or(false);
    let committed = state.snapshot.lock().ok().and_then(|snapshot| snapshot.jobs.iter().find(|job| job.id == id).map(|job| (job.provisional != Some(true), job.destination.clone()))).unwrap_or((false, String::new()));
    if committed.0 && !committed.1.is_empty() {
        if let Some(parent) = PathBuf::from(&committed.1).parent() { let _ = std::fs::create_dir_all(parent); }
        if replace_existing { let _ = std::fs::remove_file(&committed.1); }
        if let Err(error) = move_completed_file(&temp_path, &committed.1).await {
            emit_job(&state, &id, |job| { job.state = "failed".into(); job.error = Some(error.to_string()); job.events.insert(0, job_event("Could not move the completed file", Some("error"))); });
            emit_snapshot(&app, &state);
            add_notification(&app, &state, &id, "failed");
            return false;
        }
    }
    emit_job(&state, &id, |job| { job.speed = 0; job.connections = 0; if committed.0 { job.state = "completed".into(); job.progress = 100.0; job.completed = Some(now_label()); job.events.insert(0, job_event("Download completed", Some("success"))); } else { job.state = "finalizing".into(); job.progress = 100.0; job.eta = Some("Ready to save".into()); job.events.insert(0, job_event("Download ready; waiting for destination", Some("warning"))); } });
    emit_snapshot(&app, &state);
    if committed.0 { add_notification(&app, &state, &id, "completed"); }
    false
}

async fn acquire(app: AppHandle, id: String, source: String) {
    let (automatic, retries) = {
        let state = app.state::<CoreState>();
        state.snapshot.lock().ok().map(|snapshot| (snapshot.settings.retry_automatically, snapshot.settings.max_retries)).unwrap_or((false, 0))
    };
    for attempt in 0..=retries {
        if attempt > 0 {
            let state = app.state::<CoreState>();
            if job_state(&app, &id).as_deref() != Some("failed") { return; }
            emit_job(&state, &id, |job| { job.state = "connecting".into(); job.error = None; job.connections = 0; job.events.insert(0, job_event(&format!("Automatic retry {attempt} of {retries}"), Some("warning"))); });
            emit_snapshot(&app, &state);
            sleep(Duration::from_millis((attempt as u64 * 500).min(5000))).await;
        }
        let retryable = acquire_once(app.clone(), id.clone(), source.clone()).await;
        if !automatic || !retryable || job_state(&app, &id).as_deref() != Some("failed") || attempt == retries { return; }
    }
}

#[tauri::command]
fn get_snapshot(state: State<'_, CoreState>) -> AppSnapshot { state.snapshot.lock().map(|snapshot| snapshot.clone()).unwrap_or_else(|_| AppSnapshot { jobs: vec![], settings: default_settings(), connected: false, aggregate_speed: 0, notifications: vec![] }) }

#[tauri::command]
fn open_path(path: String) -> Result<(), String> {
    let path = path.trim();
    if path.is_empty() { return Err("Path is empty".into()); }
    #[cfg(windows)]
    { Command::new("explorer.exe").arg(path).spawn().map(|_| ()).map_err(|error| error.to_string()) }
    #[cfg(target_os = "macos")]
    { Command::new("open").arg(path).spawn().map(|_| ()).map_err(|error| error.to_string()) }
    #[cfg(all(unix, not(target_os = "macos")))]
    { Command::new("xdg-open").arg(path).spawn().map(|_| ()).map_err(|error| error.to_string()) }
}

#[tauri::command]
fn pause_job(app: AppHandle, state: State<'_, CoreState>, id: String) { emit_job(&state, &id, |job| { if ["downloading", "connecting", "finalizing"].contains(&job.state.as_str()) { job.state = "paused".into(); job.speed = 0; job.connections = 0; job.events.insert(0, job_event("Paused by user", Some("warning"))); } }); emit_snapshot(&app, &state); }

#[tauri::command]
fn resume_job(app: AppHandle, state: State<'_, CoreState>, id: String) {
    let Some((source, ready)) = state.snapshot.lock().ok().and_then(|snapshot| snapshot.jobs.iter().find(|job| job.id == id && ["paused", "pending"].contains(&job.state.as_str())).map(|job| (job.source.clone(), job.provisional == Some(true) && job.progress >= 100.0))) else { return; };
    emit_job(&state, &id, |job| { if ["paused", "pending"].contains(&job.state.as_str()) { job.state = if ready { "finalizing" } else { "downloading" }.into(); job.connections = 0; job.eta = Some(if ready { "Ready to save" } else { "Resuming" }.into()); job.events.insert(0, job_event("Resumed", Some("success"))); } });
    emit_snapshot(&app, &state);
    if !ready { tauri::async_runtime::spawn(acquire(app.clone(), id, source)); }
}

#[tauri::command]
fn retry_job(app: AppHandle, state: State<'_, CoreState>, id: String) { let source = state.snapshot.lock().ok().and_then(|snapshot| snapshot.jobs.iter().find(|job| job.id == id).map(|job| job.source.clone())); emit_job(&state, &id, |job| { job.state = "connecting".into(); job.error = None; job.speed = 0; job.connections = 0; job.events.insert(0, job_event("Retrying source", None)); }); emit_snapshot(&app, &state); if let Some(source) = source { tauri::async_runtime::spawn(acquire(app.clone(), id, source)); } }

#[tauri::command]
fn cancel_job_internal(app: &AppHandle, state: &CoreState, id: &str) {
    let mut temp_path = None;
    if let Ok(mut snapshot) = state.snapshot.lock() {
        if let Some(job) = snapshot.jobs.iter().find(|job| job.id == id && job.provisional == Some(true)) { temp_path = Some(job.temp_path.clone()); }
        snapshot.jobs.retain(|job| !(job.id == id && job.provisional == Some(true)));
        if let Some(job) = snapshot.jobs.iter_mut().find(|job| job.id == id) { job.state = "failed".into(); job.error = Some("Cancelled by user".into()); job.speed = 0; job.connections = 0; job.events.insert(0, job_event("Cancelled by user", Some("warning"))); }
    }
    if let Some(path) = temp_path { let _ = std::fs::remove_file(&path); let _ = std::fs::remove_dir_all(format!("{path}.segments")); }
    emit_snapshot(app, state);
}

#[tauri::command]
fn cancel_job(app: AppHandle, state: State<'_, CoreState>, id: String) { cancel_job_internal(&app, &state, &id); }

#[tauri::command]
fn remove_job(app: AppHandle, state: State<'_, CoreState>, id: String) { let mut temporary = None; if let Ok(mut snapshot) = state.snapshot.lock() { temporary = snapshot.jobs.iter().find(|job| job.id == id && job.state != "completed").map(|job| job.temp_path.clone()); snapshot.jobs.retain(|job| job.id != id); } if let Some(path) = temporary { let _ = std::fs::remove_file(&path); let _ = std::fs::remove_dir_all(format!("{path}.segments")); } emit_snapshot(&app, &state); }

#[tauri::command]
fn pause_all(app: AppHandle, state: State<'_, CoreState>) { if let Ok(mut snapshot) = state.snapshot.lock() { for job in snapshot.jobs.iter_mut() { if ["downloading", "connecting", "finalizing"].contains(&job.state.as_str()) { job.state = "paused".into(); job.speed = 0; job.connections = 0; } } } emit_snapshot(&app, &state); }

#[tauri::command]
fn resume_all(app: AppHandle, state: State<'_, CoreState>) { let mut sources = Vec::new(); if let Ok(mut snapshot) = state.snapshot.lock() { for job in snapshot.jobs.iter_mut() { if ["paused", "pending"].contains(&job.state.as_str()) { job.state = "downloading".into(); job.connections = 1; sources.push((job.id.clone(), job.source.clone())); } } } emit_snapshot(&app, &state); for (id, source) in sources { tauri::async_runtime::spawn(acquire(app.clone(), id, source)); } }

fn start_provisional(app: AppHandle, state: &CoreState, input: ProvisionalInput, show_window: bool) -> Result<String, String> {
    let parsed = reqwest::Url::parse(&input.source).map_err(|_| "Use an HTTP or HTTPS URL".to_string())?;
    if !matches!(parsed.scheme(), "http" | "https") { return Err("Use an HTTP or HTTPS URL".into()); }
    if show_window {
        let target = state.reattach_target.lock().ok().and_then(|mut value| value.take());
        if let Some(target_id) = target {
            let accepted = state.snapshot.lock().ok().map(|mut snapshot| {
                let Some(job) = snapshot.jobs.iter_mut().find(|job| job.id == target_id && job.provisional != Some(true) && source_compatible(&job.source, &input.source)) else { return false; };
                job.source = input.source.clone();
                job.domain = domain(&input.source);
                job.state = "connecting".into();
                job.error = None;
                job.speed = 0;
                job.connections = 0;
                job.started = Some(now_label());
                job.events.insert(0, job_event("Source reattached by user", Some("success")));
                true
            }).unwrap_or(false);
            if accepted {
                emit_snapshot(&app, state);
                tauri::async_runtime::spawn(acquire(app.clone(), target_id.clone(), input.source));
                return Ok(target_id);
            }
            if let Ok(mut value) = state.reattach_target.lock() { if value.is_none() { *value = Some(target_id); } }
        }
    }
    let id = format!("provisional-{}", Uuid::new_v4());
    let (name, destination, temp_folder, max_connections) = { let snapshot = state.snapshot.lock().map_err(|_| "State unavailable")?; let name = input.name.filter(|value| !value.trim().is_empty()).unwrap_or_else(|| source_name(&input.source)); let destination = format!("{}\\{}", snapshot.settings.default_folder, name); let max_connections = clamp_connections(input.max_connections.unwrap_or(snapshot.settings.max_connections)); (name, destination, snapshot.settings.temp_folder.clone(), max_connections) };
    let job = DownloadJob { id: id.clone(), name, source: input.source.clone(), domain: domain(&input.source), kind: if input.media.unwrap_or(false) { "video".into() } else { "document".into() }, state: "connecting".into(), progress: 0.0, downloaded: 0, total: None, speed: 0, eta: Some("Connecting…".into()), connections: 0, max_connections, mode: "single-stream".into(), media: input.media.unwrap_or(false), media_details: None, media_tracks: None, destination, temp_path: format!("{}\\{}.part", temp_folder, id), resumable: false, mime: None, error: None, created: now_label(), started: Some(now_label()), completed: None, provisional: Some(true), segments: None, completed_ranges: vec![], resource_identity: None, events: vec![job_event("Provisional acquisition created", None)] };
    { let mut snapshot = state.snapshot.lock().map_err(|_| "State unavailable")?; snapshot.jobs.insert(0, job); }
    emit_snapshot(&app, &state);
    tauri::async_runtime::spawn(acquire(app.clone(), id.clone(), input.source));
    if show_window {
        let label = format!("add-{}", id);
        let url = format!("index.html?window=add&id={id}");
        if let Ok(window) = WebviewWindowBuilder::new(&app, label, WebviewUrl::App(url.into())).title("Add Download").inner_size(410.0, 560.0).resizable(false).decorations(false).center().build() {
            let close_handle = app.clone();
            let close_id = id.clone();
            let close_window = window.clone();
            window.on_window_event(move |event| {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let state = close_handle.state::<CoreState>();
                    let is_provisional = state.snapshot.lock().ok().and_then(|snapshot| snapshot.jobs.iter().find(|job| job.id == close_id).map(|job| job.provisional == Some(true))).unwrap_or(false);
                    if is_provisional { cancel_job_internal(&close_handle, &state, &close_id); }
                    let _ = close_window.destroy();
                }
            });
        }
    }
    Ok(id)
}

#[tauri::command]
fn create_provisional(app: AppHandle, state: State<'_, CoreState>, input: ProvisionalInput) -> Result<String, String> {
    start_provisional(app, state.inner(), input, false)
}

#[tauri::command]
async fn commit_provisional(app: AppHandle, state: State<'_, CoreState>, id: String, input: CommitInput) -> Result<(), String> {
    let ready = {
        let mut snapshot = state.snapshot.lock().map_err(|_| "State unavailable")?;
        let collision = snapshot.settings.collision_behavior.clone();
        let job = snapshot.jobs.iter_mut().find(|job| job.id == id).ok_or_else(|| "Acquisition no longer exists".to_string())?;
        let name = if input.name.trim().is_empty() { job.name.clone() } else { input.name.trim().to_string() };
        let requested_destination = if input.destination.trim().is_empty() { job.destination.clone() } else { input.destination.trim().to_string() };
        job.name = name;
        job.destination = collision_destination(&requested_destination, &collision);
        if job.destination != requested_destination { job.events.insert(0, job_event("Destination renamed to avoid an existing file", Some("warning"))); }
        if let Some(max_connections) = input.max_connections { job.max_connections = clamp_connections(max_connections); }
        job.provisional = Some(false);
        job.resumable = true;
        job.events.insert(0, job_event("Accepted as managed download", Some("success")));
        (job.state == "finalizing" || job.progress >= 100.0, job.temp_path.clone(), job.destination.clone(), collision == "replace", job.mode == "segments", job.media_tracks.unwrap_or(1))
    };
    if ready.0 {
        let final_path = if ready.4 && ready.5 > 1 {
            let track_paths = (0..ready.5 as usize).map(|track| format!("{}.track-{track:02}", ready.1)).collect::<Vec<_>>();
            let mux_path = format!("{}.mux.{}", ready.1, media_extension(&ready.2));
            if let Err(error) = mux_media_tracks(&track_paths, &mux_path).await {
                emit_job(&state, &id, |job| { job.state = "failed".into(); job.error = Some(error.clone()); job.events.insert(0, job_event("Media finalization failed; downloaded parts were preserved", Some("error"))); });
                emit_snapshot(&app, &state);
                return Err(error);
            }
            mux_path
        } else {
            if ready.4 {
                if let Err(error) = finalize_media(&ready.1, &ready.2).await {
                    emit_job(&state, &id, |job| { job.state = "failed".into(); job.error = Some(error.clone()); job.events.insert(0, job_event("Media finalization failed; downloaded parts were preserved", Some("error"))); });
                    emit_snapshot(&app, &state);
                    return Err(error);
                }
            }
            ready.1.clone()
        };
        if let Some(parent) = PathBuf::from(&ready.2).parent() { let _ = std::fs::create_dir_all(parent); }
        if ready.3 { let _ = std::fs::remove_file(&ready.2); }
        match move_completed_file(&final_path, &ready.2).await {
            Ok(()) => { if ready.4 { let _ = std::fs::remove_dir_all(format!("{}.segments", ready.1)); } emit_job(&state, &id, |job| { job.state = "completed".into(); job.progress = 100.0; job.eta = None; job.completed = Some(now_label()); job.events.insert(0, job_event("Download completed", Some("success"))); }); add_notification(&app, &state, &id, "completed"); },
            Err(error) => emit_job(&state, &id, |job| { job.state = "failed".into(); job.error = Some(error.to_string()); job.events.insert(0, job_event("Could not move the completed file", Some("error"))); }),
        }
    }
    emit_snapshot(&app, &state);
    Ok(())
}

#[tauri::command]
fn update_settings(app: AppHandle, state: State<'_, CoreState>, patch: Value) {
    if let Ok(mut snapshot) = state.snapshot.lock() {
        if let Value::Object(patch) = patch.get("patch").cloned().unwrap_or(patch) {
            let mut current = serde_json::to_value(&snapshot.settings).unwrap_or_else(|_| json!({}));
            if let Value::Object(ref mut object) = current { for (key, value) in patch { object.insert(key, value); } }
            if let Ok(settings) = serde_json::from_value(current) { snapshot.settings = settings; }
            write_browser_policy(&browser_policy_root(), &settings_policy(&snapshot.settings));
        }
    }
    let enabled = state.snapshot.lock().map(|snapshot| snapshot.settings.start_at_sign_in).unwrap_or(true);
    sync_startup(enabled);
    emit_snapshot(&app, &state);
}

#[tauri::command]
fn reattach_job(app: AppHandle, state: State<'_, CoreState>, id: String) { let exists = state.snapshot.lock().ok().map(|snapshot| snapshot.jobs.iter().any(|job| job.id == id && job.provisional != Some(true))).unwrap_or(false); if exists { if let Ok(mut target) = state.reattach_target.lock() { *target = Some(id.clone()); } emit_job(&state, &id, |job| { job.state = "pending".into(); job.eta = Some("Waiting for renewed source".into()); job.events.insert(0, job_event("Waiting for a renewed browser source", Some("warning"))); }); emit_snapshot(&app, &state); } }

fn provisional_input_from_message(message: &Value) -> Option<ProvisionalInput> {
    let message_type = message.get("type").and_then(Value::as_str)?;
    if !matches!(message_type, "capture-acquisition" | "media-capture") { return None; }
    let payload = message.get("payload").unwrap_or(&message);
    let source = payload.get("source").or_else(|| payload.get("url")).and_then(Value::as_str)?.trim().to_string();
    let parsed = reqwest::Url::parse(&source).ok()?;
    if !matches!(parsed.scheme(), "http" | "https") { return None; }
    let name = payload.get("name").and_then(Value::as_str).map(str::to_string);
    let media = message_type == "media-capture" || payload.get("media").and_then(Value::as_bool).unwrap_or(false);
    Some(ProvisionalInput { source, name, media: Some(media), max_connections: None })
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

fn native_host() {
    let mut input = std::io::stdin().lock();
    let mut output = std::io::stdout().lock();
    loop {
        let mut length = [0u8; 4];
        if input.read_exact(&mut length).is_err() { break; }
        let size = u32::from_le_bytes(length) as usize;
        if size == 0 || size > 1024 * 1024 { break; }
        let mut bytes = vec![0u8; size];
        if input.read_exact(&mut bytes).is_err() { break; }
        let message = match serde_json::from_slice::<Value>(&bytes) { Ok(message) => message, Err(_) => { write_native_response(&mut output, json!({ "ok": false, "error": "invalid message" })); continue; } };
        let message_type = message.get("type").and_then(Value::as_str);
        if message_type == Some("get-policy") {
            let policy = load_browser_policy(&browser_policy_root()).unwrap_or((true, true, Vec::new()));
            write_native_response(&mut output, json!({ "ok": true, "policy": browser_policy_value(&policy) }));
            continue;
        }
        if message_type == Some("update-policy") {
            let Some(policy) = browser_policy_from_value(&message) else { write_native_response(&mut output, json!({ "ok": false, "error": "invalid policy" })); continue; };
            write_browser_policy(&browser_policy_root(), &policy);
            let forwarded = std::env::current_exe().ok().and_then(|executable| serde_json::to_string(&message).ok().and_then(|raw| Command::new(executable).arg("--policy").arg(raw).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn().ok())).is_some();
            write_native_response(&mut output, json!({ "ok": forwarded }));
            continue;
        }
        if !matches!(message_type, Some("open-manager") | Some("capture-acquisition") | Some("media-capture")) { write_native_response(&mut output, json!({ "ok": false, "error": "unsupported message" })); continue; }
        if matches!(message_type, Some("capture-acquisition") | Some("media-capture")) && provisional_input_from_message(&message).is_none() { write_native_response(&mut output, json!({ "ok": false, "error": "invalid acquisition" })); continue; }
        let forwarded = std::env::current_exe().ok().and_then(|executable| serde_json::to_string(&message).ok().and_then(|raw| Command::new(executable).arg("--capture").arg(raw).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn().ok())).is_some();
        write_native_response(&mut output, json!({ "ok": forwarded }));
    }
}

fn write_native_response(output: &mut impl Write, message: Value) {
    if let Ok(bytes) = serde_json::to_vec(&message) {
        let _ = output.write_all(&(bytes.len() as u32).to_le_bytes());
        let _ = output.write_all(&bytes);
        let _ = output.flush();
    }
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
            if (x == 15 || x == 16) && (y >= 8 && y <= 20) || (y >= 19 && y <= 21 && x >= 11 && x <= 20 && (x as i32 - 16).abs() <= (y as i32 - 19)) {
                pixels[index..index + 4].copy_from_slice(&[255, 255, 255, 255]);
            }
        }
    }
    Image::new_owned(pixels, 32, 32)
}

fn install_tray(app: &tauri::AppHandle, intercept_downloads: bool, show_media_buttons: bool) -> tauri::Result<()> {
    let open_manager = MenuItemBuilder::with_id("open-manager", "Open Download Manager").build(app)?;
    let pause_all = MenuItemBuilder::with_id("pause-all", "Pause All").build(app)?;
    let resume_all = MenuItemBuilder::with_id("resume-all", "Resume All").build(app)?;
    let browser_integration = CheckMenuItemBuilder::with_id("browser-integration", "Browser Integration").checked(intercept_downloads).build(app)?;
    let media_buttons = CheckMenuItemBuilder::with_id("media-buttons", "Media Buttons").checked(show_media_buttons).build(app)?;
    let bandwidth = MenuItemBuilder::with_id("bandwidth", "Set Bandwidth Limit").build(app)?;
    let exit = MenuItemBuilder::with_id("exit-manager", "Exit Manager").build(app)?;
    let menu = MenuBuilder::new(app).items(&[&open_manager, &pause_all, &resume_all]).separator().items(&[&browser_integration, &media_buttons, &bandwidth]).separator().item(&exit).build()?;
    TrayIconBuilder::with_id("main-tray").icon(tray_image()).menu(&menu).tooltip("Download Manager").on_menu_event(|app, event| {
        let id = event.id().as_ref();
        match id {
            "open-manager" => { if let Some(window) = app.get_webview_window("main") { let _ = window.show(); let _ = window.set_focus(); } }
            "pause-all" => { let state = app.state::<CoreState>(); if let Ok(mut snapshot) = state.snapshot.lock() { for job in snapshot.jobs.iter_mut() { if ["downloading", "connecting", "finalizing"].contains(&job.state.as_str()) { job.state = "paused".into(); job.speed = 0; job.connections = 0; job.events.insert(0, job_event("Paused from the system tray", Some("warning"))); } } } emit_snapshot(app, &state); }
            "resume-all" => { let state = app.state::<CoreState>(); let mut sources = Vec::new(); if let Ok(mut snapshot) = state.snapshot.lock() { for job in snapshot.jobs.iter_mut() { if ["paused", "pending"].contains(&job.state.as_str()) { job.state = "downloading".into(); job.connections = 1; job.events.insert(0, job_event("Resumed from the system tray", Some("success"))); sources.push((job.id.clone(), job.source.clone())); } } } emit_snapshot(app, &state); for (id, source) in sources { tauri::async_runtime::spawn(acquire(app.clone(), id, source)); } }
            "browser-integration" => { let state = app.state::<CoreState>(); if let Ok(mut snapshot) = state.snapshot.lock() { snapshot.settings.intercept_downloads = !snapshot.settings.intercept_downloads; write_browser_policy(&browser_policy_root(), &settings_policy(&snapshot.settings)); } emit_snapshot(app, &state); }
            "media-buttons" => { let state = app.state::<CoreState>(); if let Ok(mut snapshot) = state.snapshot.lock() { snapshot.settings.show_media_buttons = !snapshot.settings.show_media_buttons; write_browser_policy(&browser_policy_root(), &settings_policy(&snapshot.settings)); } emit_snapshot(app, &state); }
            "bandwidth" => { if let Some(window) = app.get_webview_window("main") { let _ = window.show(); let _ = window.set_focus(); let _ = window.eval("window.location.href = window.location.pathname + '?settings=network'"); } }
            "exit-manager" => app.exit(0),
            _ => {}
        }
    }).build(app)?;
    Ok(())
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|value| value == "--native-host") { native_host(); return; }
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| { if let Some(policy) = policy_from_args(&argv) { let state = app.state::<CoreState>(); apply_browser_policy(app, state.inner(), policy); } else if let Some(input) = capture_input_from_args(&argv) { if let Some(window) = app.get_webview_window("main") { let _ = window.hide(); } let state = app.state::<CoreState>(); let _ = start_provisional(app.clone(), state.inner(), input, true); } else if let Some(window) = app.get_webview_window("main") { let _ = window.show(); let _ = window.set_focus(); } }))
        .plugin(tauri_plugin_notification::init())
        .setup(|app| {
            let root = app.path().app_data_dir().unwrap_or_else(|_| PathBuf::from("."));
            std::fs::create_dir_all(&root).ok();
            register_native_host(&root);
            let database = Connection::open(root.join("download-manager.db")).map_err(|error| error.to_string())?;
            database.execute_batch("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL); CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);").map_err(|error| error.to_string())?;
            let mut settings = database.query_row("SELECT payload FROM settings WHERE id = 1", [], |row| row.get::<_, String>(0)).ok().and_then(|payload| serde_json::from_str(&payload).ok()).unwrap_or_else(default_settings);
            if let Some(policy) = load_browser_policy(&root) { settings.intercept_downloads = policy.0; settings.show_media_buttons = policy.1; settings.excluded_sites = policy.2; } else { write_browser_policy(&root, &settings_policy(&settings)); }
            let show_manager_at_startup = settings.show_manager_at_sign_in;
            sync_startup(settings.start_at_sign_in);
            let initial_snapshot = snapshot_from_database(&database, settings);
            let tray_intercept_downloads = initial_snapshot.settings.intercept_downloads;
            let tray_show_media_buttons = initial_snapshot.settings.show_media_buttons;
            let recovered = initial_snapshot.jobs.iter().filter(|job| job.provisional != Some(true) && ["connecting", "downloading", "finalizing"].contains(&job.state.as_str())).map(|job| (job.id.clone(), job.source.clone())).collect::<Vec<_>>();
            app.manage(CoreState { snapshot: Mutex::new(initial_snapshot), database: Mutex::new(database), reattach_target: Mutex::new(None) });
            save_snapshot(&app.state::<CoreState>());
            install_tray(app.handle(), tray_intercept_downloads, tray_show_media_buttons)?;
            if let Some(window) = app.get_webview_window("main") {
                let close_handle = app.handle().clone();
                window.on_window_event(move |event| {
                    if let WindowEvent::CloseRequested { api, .. } = event {
                        let close_to_tray = close_handle.state::<CoreState>().snapshot.lock().map(|snapshot| snapshot.settings.close_behavior == "tray").unwrap_or(true);
                        if close_to_tray { api.prevent_close(); if let Some(main) = close_handle.get_webview_window("main") { let _ = main.hide(); } }
                    }
                });
            }
            if let Some(policy) = policy_from_args(&std::env::args().collect::<Vec<_>>()) {
                if let Some(window) = app.get_webview_window("main") { let _ = window.hide(); }
                let state = app.state::<CoreState>();
                apply_browser_policy(app.handle(), state.inner(), policy);
            } else if let Some(input) = capture_input_from_args(&std::env::args().collect::<Vec<_>>()) {
                if let Some(window) = app.get_webview_window("main") { let _ = window.hide(); }
                let state = app.state::<CoreState>();
                let _ = start_provisional(app.handle().clone(), state.inner(), input, true);
            } else if std::env::args().any(|value| value == "--startup") && !show_manager_at_startup {
                if let Some(window) = app.get_webview_window("main") { let _ = window.hide(); }
            }
            for (id, source) in recovered { tauri::async_runtime::spawn(acquire(app.handle().clone(), id, source)); }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_snapshot, open_path, pause_job, resume_job, retry_job, cancel_job, remove_job, pause_all, resume_all, create_provisional, commit_provisional, update_settings, reattach_job])
        .run(tauri::generate_context!())
        .expect("error while running Download Manager");
}
