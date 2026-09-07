//! Native toast actions (SPEC §14).
//!
//! The pinned `tauri-plugin-notification` desktop backend forwards
//! title/body/icon/sound only; its action types are mobile-only (official
//! docs: "The Actions API is only available on mobile platforms"). The
//! action-capable path therefore lives here: on Linux via `notify-rust`
//! (the same crate the plugin itself uses for desktop delivery), everywhere
//! else via the plain plugin builder until the Windows delivery pass wires
//! native WinRT actions.

use tauri::{AppHandle, Emitter, Manager};

/// (action id, button label) pairs for a job toast. Pure: unit-covered.
pub fn actions_for(kind: &str) -> Vec<(&'static str, &'static str)> {
    match kind {
        "completed" => vec![("open", "Open"), ("folder", "Show in folder")],
        _ => vec![("details", "View details")],
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
    kind: &str,
    job_id: &str,
    destination: &str,
) {
    #[cfg(target_os = "linux")]
    {
        show_linux(
            app.clone(),
            title.to_string(),
            body.to_string(),
            kind.to_string(),
            job_id.to_string(),
            destination.to_string(),
        );
        return;
    }
    #[cfg(not(target_os = "linux"))]
    {
        // Plain toast until the Windows delivery pass wires native actions.
        // The in-app notification center already carries Open / Show in
        // folder / View details there.
        use tauri_plugin_notification::NotificationExt;
        let _ = app.notification().builder().title(title).body(body).show();
    }
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
#[cfg(target_os = "linux")]
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
                serde_json::json!({ "jobId": job_id }),
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
