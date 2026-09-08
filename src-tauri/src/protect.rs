//! Current-user protection for durable sensitive acquisition context (F11,
//! SPEC §16): replayable sources, form bodies, and segment URLs are opaque at
//! rest on Windows through DPAPI with no new dependencies (hand-rolled
//! crypt32 FFI). Other platforms keep the existing file-permission story.
//!
//! Envelopes are tagged so readers distinguish protected, legacy-plaintext,
//! and foreign-machine blobs: a portable folder moved to another machine or
//! user yields an honest needs-reattach job, never a crash or a silent leak.
//! Plaintext already in memory (local UI, live transfer) is unaffected; only
//! the database payload is enveloped.

pub const ENVELOPE_PREFIX: &str = "dpapi1:";
pub const LEGACY_PREFIX: &str = "plain1:";

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    out
}

fn hex_decode(text: &str) -> Option<Vec<u8>> {
    if text.len() % 2 != 0 {
        return None;
    }
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len() / 2);
    let mut index = 0;
    while index < bytes.len() {
        let high = hex_value(bytes[index])?;
        let low = hex_value(bytes[index + 1])?;
        out.push(high << 4 | low);
        index += 2;
    }
    Some(out)
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

#[cfg(windows)]
mod platform {
    use std::ffi::c_void;

    #[repr(C)]
    struct Blob {
        len: u32,
        data: *mut u8,
    }

    #[link(name = "crypt32")]
    unsafe extern "system" {
        fn CryptProtectData(
            data_in: *const Blob,
            descr: *const u16,
            entropy: *const Blob,
            reserved: *mut c_void,
            prompt: *mut c_void,
            flags: u32,
            data_out: *mut Blob,
        ) -> i32;
        fn CryptUnprotectData(
            data_in: *const Blob,
            descr: *mut *mut u16,
            entropy: *const Blob,
            reserved: *mut c_void,
            prompt: *mut c_void,
            flags: u32,
            data_out: *mut Blob,
        ) -> i32;
        fn LocalFree(mem: *mut c_void) -> *mut c_void;
    }

    const UI_FORBIDDEN: u32 = 0x1;

    pub fn protect(plaintext: &[u8]) -> Result<Vec<u8>, String> {
        let len = u32::try_from(plaintext.len()).map_err(|_| "Sensitive value is too large".to_string())?;
        let input = Blob { len, data: plaintext.as_ptr() as *mut u8 };
        let mut output = Blob { len: 0, data: std::ptr::null_mut() };
        // SAFETY: input borrows live plaintext; output is allocated by DPAPI
        // and freed below; UI_FORBIDDEN forbids any prompt.
        let ok = unsafe {
            CryptProtectData(
                &input,
                std::ptr::null(),
                std::ptr::null(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                UI_FORBIDDEN,
                &mut output,
            )
        };
        if ok == 0 || output.data.is_null() {
            return Err("Could not protect sensitive data for the current user".into());
        }
        // SAFETY: DPAPI wrote `len` bytes to a LocalAlloc buffer on success.
        let protected = unsafe { std::slice::from_raw_parts(output.data, output.len as usize).to_vec() };
        unsafe {
            LocalFree(output.data as *mut c_void);
        }
        Ok(protected)
    }

    pub fn unprotect(protected: &[u8]) -> Result<Vec<u8>, String> {
        let len = u32::try_from(protected.len()).map_err(|_| "Protected value is too large".to_string())?;
        let input = Blob { len, data: protected.as_ptr() as *mut u8 };
        let mut output = Blob { len: 0, data: std::ptr::null_mut() };
        let mut description: *mut u16 = std::ptr::null_mut();
        // SAFETY: same contract as protect; a failure means foreign machine,
        // foreign user, or a corrupt blob — all surface as unavailable here.
        let ok = unsafe {
            CryptUnprotectData(
                &input,
                &mut description,
                std::ptr::null(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                UI_FORBIDDEN,
                &mut output,
            )
        };
        if !description.is_null() {
            unsafe {
                LocalFree(description as *mut c_void);
            }
        }
        if ok == 0 || output.data.is_null() {
            return Err("Saved credentials are unavailable on this machine or user".into());
        }
        // SAFETY: DPAPI wrote `len` bytes to a LocalAlloc buffer on success.
        let plaintext = unsafe { std::slice::from_raw_parts(output.data, output.len as usize).to_vec() };
        unsafe {
            LocalFree(output.data as *mut c_void);
        }
        Ok(plaintext)
    }
}

#[cfg(not(windows))]
mod platform {
    pub fn protect(plaintext: &[u8]) -> Result<Vec<u8>, String> {
        Ok(plaintext.to_vec())
    }

    pub fn unprotect(protected: &[u8]) -> Result<Vec<u8>, String> {
        Ok(protected.to_vec())
    }
}

/// Envelope one sensitive value for database storage. Empty values pass
/// through so blank fields never spend a DPAPI call.
pub fn protect_field(plaintext: &str) -> String {
    if plaintext.is_empty() {
        return String::new();
    }
    match platform::protect(plaintext.as_bytes()) {
        Ok(protected) => format!("{ENVELOPE_PREFIX}{}", hex_encode(&protected)),
        Err(_) => format!("{LEGACY_PREFIX}{}", hex_encode(plaintext.as_bytes())),
    }
}

/// Restore one stored value. Untagged values are pre-envelope database rows
/// and pass through. `dpapi1:` blobs that will not open here (moved folder,
/// other user, corruption) are an error the caller must handle honestly.
pub fn unprotect_field(stored: &str) -> Result<String, String> {
    if stored.is_empty() {
        return Ok(String::new());
    }
    if let Some(payload) = stored.strip_prefix(ENVELOPE_PREFIX) {
        let protected = hex_decode(payload).ok_or_else(|| "Stored credential is corrupt".to_string())?;
        let plaintext = platform::unprotect(&protected)?;
        return String::from_utf8(plaintext).map_err(|_| "Stored credential is corrupt".to_string());
    }
    if let Some(payload) = stored.strip_prefix(LEGACY_PREFIX) {
        let plaintext = hex_decode(payload).ok_or_else(|| "Stored credential is corrupt".to_string())?;
        return String::from_utf8(plaintext).map_err(|_| "Stored credential is corrupt".to_string());
    }
    Ok(stored.to_string())
}

#[cfg(test)]
mod tests {
    use super::{hex_decode, hex_encode, protect_field, unprotect_field, ENVELOPE_PREFIX};

    #[test]
    fn hex_round_trip() {
        assert_eq!(hex_encode(b""), "");
        assert_eq!(hex_decode(""), Some(Vec::new()));
        assert_eq!(hex_decode("00ffab"), Some(vec![0x00, 0xff, 0xab]));
        assert_eq!(hex_decode("zz"), None);
        assert_eq!(hex_decode("abc"), None);
    }

    #[test]
    fn empty_fields_pass_through() {
        assert_eq!(protect_field(""), "");
        assert_eq!(unprotect_field("").expect("empty"), "");
    }

    #[test]
    fn legacy_rows_pass_through() {
        assert_eq!(
            unprotect_field("https://cdn.example.test/file.bin?token=abc").expect("legacy"),
            "https://cdn.example.test/file.bin?token=abc"
        );
    }

    #[test]
    fn corrupt_envelopes_fail() {
        assert!(unprotect_field("dpapi1:zz").is_err());
        assert!(unprotect_field("plain1:zz").is_err());
    }

    #[cfg(windows)]
    #[test]
    fn dpapi_envelope_round_trip() {
        let secret = "https://cdn.example.test/export?token=single-use-secret&user=alice";
        let stored = protect_field(secret);
        assert!(stored.starts_with(ENVELOPE_PREFIX), "unexpected envelope: {stored}");
        assert!(!stored.contains("single-use-secret"));
        assert_eq!(unprotect_field(&stored).expect("round trip"), secret);
    }

    #[cfg(windows)]
    #[test]
    fn tampered_envelope_fails() {
        let stored = protect_field("https://cdn.example.test/file.bin?token=abc");
        let mut tampered = stored.clone();
        tampered.pop();
        tampered.push('0');
        assert!(unprotect_field(&tampered).is_err());
    }
}
