use std::path::PathBuf;

pub fn sync(enabled: bool) -> Result<(), String> {
    #[cfg(windows)]
    {
        let entry = startup_entry_path();
        if !enabled {
            return match std::fs::remove_file(entry) {
                Ok(()) => Ok(()),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
                Err(error) => Err(error.to_string()),
            };
        }
        let executable = std::env::current_exe().map_err(|error| error.to_string())?;
        let parent = entry
            .parent()
            .ok_or_else(|| "Startup folder is unavailable".to_string())?;
        std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
        let contents = shell_link(&executable, "--startup")?;
        std::fs::write(entry, contents).map_err(|error| error.to_string())
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let entry = startup_entry_path();
        if enabled {
            let executable = std::env::current_exe().map_err(|error| error.to_string())?;
            let parent = entry
                .parent()
                .ok_or_else(|| "Autostart folder is unavailable".to_string())?;
            std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
            let escaped = executable.to_string_lossy().replace('"', "\\\"");
            let contents = format!("[Desktop Entry]\nType=Application\nName=Download Manager\nExec=\"{escaped}\" --startup\nHidden=false\nX-GNOME-Autostart-enabled=true\n");
            std::fs::write(entry, contents).map_err(|error| error.to_string())
        } else {
            match std::fs::remove_file(entry) {
                Ok(()) => Ok(()),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
                Err(error) => Err(error.to_string()),
            }
        }
    }
    #[cfg(target_os = "macos")]
    {
        let _ = enabled;
        Ok(())
    }
}

fn startup_entry_path() -> PathBuf {
    #[cfg(windows)]
    {
        let roaming = std::env::var_os("APPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                std::env::var_os("USERPROFILE")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| PathBuf::from("."))
                    .join("AppData")
                    .join("Roaming")
            });
        return roaming
            .join("Microsoft")
            .join("Windows")
            .join("Start Menu")
            .join("Programs")
            .join("Startup")
            .join("Download Manager.lnk");
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let config = std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
            .unwrap_or_else(|| {
                std::env::var_os("HOME")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| PathBuf::from("."))
                    .join(".config")
            });
        return config.join("autostart").join("download-manager.desktop");
    }
    #[cfg(target_os = "macos")]
    {
        PathBuf::from(".")
    }
}

#[cfg(windows)]
use std::path::Path;

#[cfg(windows)]
fn shell_link(target: &Path, arguments: &str) -> Result<Vec<u8>, String> {
    fn utf16_z(value: &str) -> Vec<u8> {
        let mut bytes = Vec::new();
        for unit in value.encode_utf16() {
            bytes.extend_from_slice(&unit.to_le_bytes());
        }
        bytes.extend_from_slice(&0u16.to_le_bytes());
        bytes
    }

    fn utf16(value: &str) -> Vec<u8> {
        value.encode_utf16().flat_map(u16::to_le_bytes).collect()
    }

    fn push_u16(bytes: &mut Vec<u8>, value: u16) {
        bytes.extend_from_slice(&value.to_le_bytes());
    }
    fn push_u32(bytes: &mut Vec<u8>, value: u32) {
        bytes.extend_from_slice(&value.to_le_bytes());
    }
    fn push_u64(bytes: &mut Vec<u8>, value: u64) {
        bytes.extend_from_slice(&value.to_le_bytes());
    }

    fn filetime(value: Option<std::time::SystemTime>) -> u64 {
        const WINDOWS_EPOCH_OFFSET_SECONDS: u64 = 11_644_473_600;
        value
            .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|duration| {
                duration
                    .as_secs()
                    .saturating_add(WINDOWS_EPOCH_OFFSET_SECONDS)
                    .saturating_mul(10_000_000)
                    .saturating_add(u64::from(duration.subsec_nanos() / 100))
            })
            .unwrap_or(0)
    }

    fn volume_details(target: &str) -> (u32, u32, Vec<u8>) {
        let Some(drive) = target.get(..2).filter(|value| value.as_bytes().get(1) == Some(&b':')) else {
            return (0, 3, vec![0]);
        };
        let root = format!("{drive}\\");
        let root_wide = root.encode_utf16().chain(std::iter::once(0)).collect::<Vec<_>>();
        let mut serial = 0u32;
        let drive_type = unsafe { GetDriveTypeW(root_wide.as_ptr()) };
        let ok = unsafe {
            GetVolumeInformationW(
                root_wide.as_ptr(),
                std::ptr::null_mut(),
                0,
                &mut serial,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                0,
            )
        };
        let serial = if ok == 0 { 0 } else { serial };
        (serial, if drive_type == 0 { 3 } else { drive_type }, vec![0])
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetDriveTypeW(root_path_name: *const u16) -> u32;
        fn GetVolumeInformationW(
            root_path_name: *const u16,
            volume_name_buffer: *mut u16,
            volume_name_size: u32,
            volume_serial_number: *mut u32,
            maximum_component_length: *mut u32,
            file_system_flags: *mut u32,
            file_system_name_buffer: *mut u16,
            file_system_name_size: u32,
        ) -> i32;
    }

    let target = target.to_string_lossy().replace('/', "\\");
    let metadata = std::fs::metadata(&target).ok();
    let file_size = metadata
        .as_ref()
        .map(|value| value.len())
        .unwrap_or(0)
        .min(u64::from(u32::MAX)) as u32;
    let creation_time = filetime(metadata.as_ref().and_then(|value| value.created().ok()));
    let access_time = filetime(metadata.as_ref().and_then(|value| value.accessed().ok()));
    let write_time = filetime(metadata.as_ref().and_then(|value| value.modified().ok()));
    let (volume_serial, drive_type, volume_label) = volume_details(&target);
    let parent = target
        .rsplit_once('\\')
        .map(|(parent, _)| {
            if parent.ends_with(':') {
                format!("{parent}\\")
            } else {
                parent.to_string()
            }
        })
        .ok_or_else(|| "Executable path has no parent".to_string())?;
    let suffix = target
        .rsplit_once('\\')
        .map(|(_, name)| name)
        .ok_or_else(|| "Executable path has no filename".to_string())?;
    let base_ansi = parent
        .as_bytes()
        .iter()
        .copied()
        .chain([0])
        .collect::<Vec<_>>();
    let suffix_ansi = suffix
        .as_bytes()
        .iter()
        .copied()
        .chain([0])
        .collect::<Vec<_>>();
    let base_unicode = utf16_z(&parent);
    let suffix_unicode = utf16_z(suffix);
    let volume_size = 0x10u32 + volume_label.len() as u32;
    let link_info_header_size = 0x24u32;
    let volume_offset = link_info_header_size;
    let local_base_offset = volume_offset + volume_size;
    let common_suffix_offset = local_base_offset + base_ansi.len() as u32;
    let local_base_unicode_offset = common_suffix_offset + suffix_ansi.len() as u32;
    let common_suffix_unicode_offset = local_base_unicode_offset + base_unicode.len() as u32;
    let link_info_size = common_suffix_unicode_offset + suffix_unicode.len() as u32;

    let mut link_info = Vec::with_capacity(link_info_size as usize);
    push_u32(&mut link_info, link_info_size);
    push_u32(&mut link_info, link_info_header_size);
    push_u32(&mut link_info, 1);
    push_u32(&mut link_info, volume_offset);
    push_u32(&mut link_info, local_base_offset);
    push_u32(&mut link_info, 0);
    push_u32(&mut link_info, common_suffix_offset);
    push_u32(&mut link_info, local_base_unicode_offset);
    push_u32(&mut link_info, common_suffix_unicode_offset);
    push_u32(&mut link_info, volume_size);
    push_u32(&mut link_info, drive_type);
    push_u32(&mut link_info, volume_serial);
    push_u32(&mut link_info, 0x10);
    link_info.extend_from_slice(&volume_label);
    link_info.extend_from_slice(&base_ansi);
    link_info.extend_from_slice(&suffix_ansi);
    link_info.extend_from_slice(&base_unicode);
    link_info.extend_from_slice(&suffix_unicode);

    let mut link = Vec::with_capacity(0x4c + link_info.len() + arguments.len() * 2 + 6);
    push_u32(&mut link, 0x4c);
    link.extend_from_slice(&[
        0x01, 0x14, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x46,
    ]);
    push_u32(&mut link, 0x000000a2);
    push_u32(&mut link, 0x00000020);
    push_u64(&mut link, creation_time);
    push_u64(&mut link, access_time);
    push_u64(&mut link, write_time);
    push_u32(&mut link, file_size);
    push_u32(&mut link, 0);
    push_u32(&mut link, 1);
    push_u16(&mut link, 0);
    link.extend_from_slice(&[0u8; 10]);
    link.extend_from_slice(&link_info);
    let argument_units = arguments.encode_utf16().count();
    push_u16(&mut link, argument_units as u16);
    link.extend_from_slice(&utf16(arguments));
    push_u32(&mut link, 0);
    Ok(link)
}

#[cfg(test)]
mod tests {
    use super::startup_entry_path;

    #[test]
    fn startup_path_is_user_scoped() {
        let path = startup_entry_path();
        assert!(path.to_string_lossy().contains("Download Manager"));
    }

    #[cfg(windows)]
    #[test]
    fn shell_link_has_terminal_block() {
        let link = super::shell_link(
            std::path::Path::new(r"C:\portable\Download Manager.exe"),
            "--startup",
        )
        .expect("valid shell link");
        assert_eq!(&link[..4], &[0x4c, 0x00, 0x00, 0x00]);
        assert_eq!(&link[link.len() - 4..], &[0x00, 0x00, 0x00, 0x00]);
    }

    #[cfg(windows)]
    #[test]
    fn shell_link_keeps_network_offset_empty_and_volume_serial_in_volume_id() {
        let link = super::shell_link(
            std::path::Path::new(r"C:\portable\Download Manager.exe"),
            "--startup",
        )
        .expect("valid shell link");
        let read_u32 = |offset: usize| u32::from_le_bytes(link[offset..offset + 4].try_into().unwrap());
        let link_info = 0x4c;
        let volume_offset = read_u32(link_info + 12) as usize;
        assert_eq!(read_u32(link_info + 20), 0);
        assert_eq!(read_u32(link_info + volume_offset + 12), 0x10);
    }
}
