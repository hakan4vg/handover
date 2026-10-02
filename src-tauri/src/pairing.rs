//! Pairing with the browser extension, and the sealed bridge channel.
//!
//! The bridge listens on loopback, where any local program could pose as
//! either side; a program holding the port while the app is closed would
//! otherwise receive whatever the extension sends, cookies included. Pairing,
//! approved once by the user in the app, gives the extension a 256-bit key.
//! Every message after that is AES-256-GCM: the request carries a timestamp
//! and a fresh nonce (replays are refused), and the answer is sealed under the
//! same key and bound to its request's nonce, so the extension acts only on an
//! answer from the paired app.

use std::collections::{HashMap, VecDeque};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use serde_json::{json, Value};

pub type Key = [u8; 32];

const REQUEST_AAD: &[u8] = b"dm-bridge-1 request";
const RESPONSE_AAD: &str = "dm-bridge-1 response ";
/// A sealed request older (or newer) than this is refused.
const MAX_SKEW_MS: u64 = 60_000;
/// A pairing request the user has not answered expires.
const PAIRING_TTL: Duration = Duration::from_secs(120);

pub struct Pending {
    pub request: String,
    pub code: String,
    key_id: String,
    key: Key,
    decision: Option<bool>,
    created: Instant,
}

pub enum Status {
    Unknown,
    Waiting,
    Denied,
    Approved { key_id: String, key: String },
}

pub enum OpenError {
    /// No such key: the extension must pair (again).
    NotPaired,
    /// Malformed, forged, stale or replayed.
    Rejected(&'static str),
}

pub struct Opened {
    pub key: Key,
    pub nonce: String,
    pub message: Value,
}

#[derive(Default)]
pub struct Pairings {
    keys: HashMap<String, Key>,
    pending: Option<Pending>,
    seen: VecDeque<(String, u64)>,
}

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |elapsed| elapsed.as_millis() as u64)
}

fn random<const N: usize>() -> [u8; N] {
    let mut bytes = [0u8; N];
    getrandom::getrandom(&mut bytes).expect("the operating system's random source is unavailable");
    bytes
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

impl Pairings {
    pub fn with_keys(keys: HashMap<String, Key>) -> Self {
        Self { keys, ..Self::default() }
    }

    pub fn count(&self) -> usize {
        self.keys.len()
    }

    pub fn forget(&mut self, key_id: &str) {
        self.keys.remove(key_id);
    }

    pub fn forget_all(&mut self) {
        self.keys.clear();
    }

    /// Start a pairing for `request` (the extension's random id) and return
    /// the code both sides show. A newer request replaces an unanswered one.
    pub fn begin(&mut self, request: &str) -> String {
        let number = u32::from_le_bytes(random::<4>()) % 1_000_000;
        let code = format!("{:03} {:03}", number / 1000, number % 1000);
        self.pending = Some(Pending {
            request: request.to_string(),
            code: code.clone(),
            key_id: hex(&random::<8>()),
            key: random::<32>(),
            decision: None,
            created: Instant::now(),
        });
        code
    }

    pub fn pending(&mut self) -> Option<&Pending> {
        if self.pending.as_ref().is_some_and(|pending| pending.created.elapsed() > PAIRING_TTL) {
            self.pending = None;
        }
        self.pending.as_ref()
    }

    /// The user's answer. On Allow, returns the new key for the caller to
    /// store; it is handed to the extension once, by `status`. Err when the
    /// request expired or a newer one replaced it: nothing was recorded.
    pub fn answer(&mut self, request: &str, allow: bool) -> Result<Option<(String, Key)>, ()> {
        self.pending();
        let pending = self.pending.as_mut().filter(|pending| pending.request == request && pending.decision.is_none()).ok_or(())?;
        pending.decision = Some(allow);
        if !allow {
            return Ok(None);
        }
        self.keys.insert(pending.key_id.clone(), pending.key);
        Ok(Some((pending.key_id.clone(), pending.key)))
    }

    pub fn status(&mut self, request: &str) -> Status {
        let Some(pending) = self.pending().filter(|pending| pending.request == request) else {
            return Status::Unknown;
        };
        let status = match pending.decision {
            None => return Status::Waiting,
            Some(false) => Status::Denied,
            Some(true) => Status::Approved { key_id: pending.key_id.clone(), key: B64.encode(pending.key) },
        };
        self.pending = None;
        status
    }

    /// Open a sealed request: `{ k, iv, data }` whose plaintext is
    /// `{ t, message }`.
    pub fn open(&mut self, body: &[u8]) -> Result<Opened, OpenError> {
        let envelope: Value = serde_json::from_slice(body).map_err(|_| OpenError::Rejected("not a sealed message"))?;
        let field = |name: &str| envelope.get(name).and_then(Value::as_str);
        let key_id = field("k").ok_or(OpenError::Rejected("not a sealed message"))?;
        let nonce = field("iv").ok_or(OpenError::Rejected("not a sealed message"))?;
        let data = field("data").and_then(|data| B64.decode(data).ok()).ok_or(OpenError::Rejected("not a sealed message"))?;
        let nonce_bytes = B64.decode(nonce).ok().filter(|bytes| bytes.len() == 12).ok_or(OpenError::Rejected("not a sealed message"))?;
        let key = *self.keys.get(key_id).ok_or(OpenError::NotPaired)?;
        let plain = Aes256Gcm::new(&key.into())
            .decrypt(Nonce::from_slice(&nonce_bytes), Payload { msg: &data, aad: REQUEST_AAD })
            .map_err(|_| OpenError::Rejected("the message did not open"))?;
        let inner: Value = serde_json::from_slice(&plain).map_err(|_| OpenError::Rejected("the message did not open"))?;
        let sent = inner.get("t").and_then(Value::as_u64).ok_or(OpenError::Rejected("the message has no time"))?;
        let now = now_ms();
        if now.abs_diff(sent) > MAX_SKEW_MS {
            return Err(OpenError::Rejected("the message is too old"));
        }
        while self.seen.front().is_some_and(|(_, at)| now.saturating_sub(*at) > MAX_SKEW_MS * 2) {
            self.seen.pop_front();
        }
        if self.seen.iter().any(|(seen, _)| seen == nonce) {
            return Err(OpenError::Rejected("the message was already received"));
        }
        self.seen.push_back((nonce.to_string(), now));
        let message = inner.get("message").cloned().ok_or(OpenError::Rejected("the message is empty"))?;
        Ok(Opened { key, nonce: nonce.to_string(), message })
    }
}

/// Seal an answer to the request whose nonce was `request_nonce`.
pub fn seal(key: &Key, request_nonce: &str, value: &Value) -> Value {
    let nonce = random::<12>();
    let aad = format!("{RESPONSE_AAD}{request_nonce}");
    let plain = serde_json::to_vec(value).unwrap_or_default();
    let data = Aes256Gcm::new(key.into())
        .encrypt(Nonce::from_slice(&nonce), Payload { msg: &plain, aad: aad.as_bytes() })
        .unwrap_or_default();
    json!({ "iv": B64.encode(nonce), "data": B64.encode(data) })
}

pub fn encode_key(key: &Key) -> String {
    B64.encode(key)
}

pub fn decode_key(text: &str) -> Option<Key> {
    B64.decode(text).ok()?.try_into().ok()
}
