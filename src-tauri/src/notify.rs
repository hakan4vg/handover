//! Native toast actions (SPEC §14).
//!
//! The pinned `tauri-plugin-notification` desktop backend forwards
//! title/body/icon/sound only; its action types are mobile-only (official
//! docs: "The Actions API is only available on mobile platforms"). The
//! action-capable path therefore lives here: on Linux via `notify-rust`
//! (the same crate the plugin itself uses for desktop delivery), and on
//! Windows via its unpackaged-app fallback. Other platforms keep the plain
//! plugin builder.

use tauri::AppHandle;
#[cfg(any(target_os = "linux", windows))]
use tauri::{Emitter, Manager};

/// (action id, button label) pairs for a job toast. Pure: unit-covered.
#[cfg(any(target_os = "linux", windows, test))]
pub fn actions_for(kind: &str) -> Vec<(&'static str, &'static str)> {
    match kind {
        "completed" => vec![("open", "Open"), ("folder", "Show in folder")],
        _ => vec![("details", "View details")]
    }
}

/// Show the job toast, with actions where the OS backend supports them.
/// Never panics and never blocks the caller: delivery runs on a spawned
/// thread and every failure path is silent, because the in-app notification
/// center is the durable record and the toast is only a shortcut.
pub fn show_job_notification(
    app: &AppHandle,
    title: &str,
    body: &str,
    _kind: &str,
    _job_id: &str,
    _destination: &str
) {
    #[cfg(target_os = "linux")]
    {
        show_linux(
            app.clone(),
            title.to_string(),
            body.to_string(),
            _kind.to_string(),
            _job_id.to_string(),
            _destination.to_string()
        );
        return;
    }
    #[cfg(windows)]
    {
        show_windows(app, title, body, _kind, _job_id, _destination);
        return;
    }
    #[cfg(all(not(target_os = "linux"), not(windows)))]
    {
        // The in-app notification center carries the durable actions.
        use tauri_plugin_notification::NotificationExt;
        let _ = app.notification().builder().title(title).body(body).show();
    }
}

#[cfg(windows)]
fn show_windows(
    app: &AppHandle,
    title: &str,
    body: &str,
    kind: &str,
    job_id: &str,
    destination: &str
) {
    let app = app.clone();
    let title = title.to_string();
    let body = body.to_string();
    let kind = kind.to_string();
    let job_id = job_id.to_string();
    let destination = destination.to_string();
    std::thread::spawn(move || {
        let mut note = notify_rust::Notification::new();
        note.summary(&title).body(&body);
        for (id, label) in actions_for(&kind) {
            note.action(id, label);
        }
        let Ok(handle) = note.show() else { return };
        handle.wait_for_action(|action| {
            handle_notification_action(&app, action, &job_id, &destination);
        });
    });
}

#[cfg(target_os = "linux")]
fn show_linux(
    app: AppHandle,
    title: String,
    body: String,
    kind: String,
    job_id: String,
    destination: String,
) {
    std::thread::spawn(move || {
        let mut note = notify_rust::Notification::new();
        note.summary(&title).body(&body).appname("Download Manager");
        for (id, label) in actions_for(&kind) {
            note.action(id, label);
        }
        // No notification daemon on headless boxes: stay silent, the in-app
        // center already recorded this notification.
        let Ok(handle) = note.show() else { return };
        handle.wait_for_action(|action| {
            handle_notification_action(&app, action, &job_id, &destination);
        });
    });
}

/// Route one toasted action id to the same behavior as the matching in-app
/// notification control. `""`/`"default"` is the body click: the primary
/// action. `"__closed"` is dismissal: nothing to do.
#[cfg(any(target_os = "linux", windows))]
fn handle_notification_action(app: &AppHandle, action: &str, job_id: &str, destination: &str) {
    match action {
        "open" | "" | "default" => {
            let _ = crate::open_path(destination.to_string());
        }
        "folder" => {
            let folder = std::path::Path::new(destination)
                .parent()
                .map(|parent| parent.to_string_lossy().into_owned())
                .filter(|parent| !parent.is_empty());
            if let Some(folder) = folder {
                let _ = crate::open_path(folder);
            }
        }
        "details" => {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
            let _ = app.emit(
                "notification-action",
                serde_json::json!({ "jobId": job_id })
            );
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::actions_for;

    #[test]
    fn completed_toast_offers_open_and_folder() {
        assert_eq!(
            actions_for("completed"),
            vec![("open", "Open"), ("folder", "Show in folder")]
        );
    }

    #[test]
    fn failed_toast_offers_view_details() {
        assert_eq!(actions_for("failed"), vec![("details", "View details")]);
    }
}
