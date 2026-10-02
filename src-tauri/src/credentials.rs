//! Browser cookies a download needs (SPEC §16).
//!
//! The extension sends only the cookies Chromium holds for the download's own
//! URLs. Here they are cut down to what Chromium would actually send for this
//! request (SameSite, expiry), checked for sanity, and loaded into a cookie jar
//! owned by that one job. The jar then picks cookies per request and per
//! redirect hop by domain, path and Secure, exactly like a browser, so a
//! redirect to another site carries none of them. Cookies a server sets during
//! the download (a confirmation step, say) stay in that job's jar.
//!
//! Cookies never enter the job record, the UI snapshot or a log line. At rest
//! they are DPAPI-protected and live exactly as long as an unfinished job.

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use reqwest::cookie::Jar;
use serde::{Deserialize, Serialize};

/// More than any real site sets for one URL; a larger capture is cut here.
pub const MAX_COOKIES: usize = 150;
const MAX_TOTAL_BYTES: usize = 64 * 1024;
const MAX_VALUE_BYTES: usize = 4096;

/// One cookie as `chrome.cookies` describes it.
#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CapturedCookie {
    pub name: String,
    pub value: String,
    pub domain: String,
    #[serde(default)]
    pub host_only: bool,
    #[serde(default = "root_path")]
    pub path: String,
    #[serde(default)]
    pub secure: bool,
    #[serde(default)]
    pub http_only: bool,
    /// "no_restriction", "lax", "strict" or "unspecified" (Lax by default).
    #[serde(default)]
    pub same_site: Option<String>,
    /// Unix seconds; absent for a session cookie.
    #[serde(default)]
    pub expiration_date: Option<f64>,
}

fn root_path() -> String {
    "/".into()
}

/// A job's cookies: what was captured (for storage) and the live jar.
pub struct Credentials {
    pub cookies: Vec<CapturedCookie>,
    pub jar: Arc<Jar>,
}

impl Credentials {
    pub fn new(cookies: Vec<CapturedCookie>) -> Self {
        let jar = jar(&cookies);
        Self { cookies, jar }
    }
}

fn now_seconds() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |elapsed| elapsed.as_secs_f64())
}

fn bare_domain(domain: &str) -> String {
    domain.trim_start_matches('.').to_ascii_lowercase()
}

/// Whether the page that started the download belongs to the cookie's site.
/// A cookie's domain is never a public suffix, so a page inside that domain is
/// same-site with the cookie; this needs no public-suffix list and can only
/// err towards sending less.
fn page_within(page_host: Option<&str>, domain: &str) -> bool {
    let Some(host) = page_host else { return false };
    let domain = bare_domain(domain);
    let host = host.to_ascii_lowercase();
    host == domain || host.ends_with(&format!(".{domain}"))
}

/// The captured cookies Chromium would send for this download. `navigation`
/// is a top-level download (Lax cookies go along); otherwise the request is a
/// media subresource of `page`.
pub fn admit(cookies: Vec<CapturedCookie>, page: Option<&str>, navigation: bool) -> Vec<CapturedCookie> {
    let page_host = page.and_then(|page| reqwest::Url::parse(page).ok()).and_then(|url| url.host_str().map(str::to_string));
    let now = now_seconds();
    let mut total = 0;
    let mut admitted = Vec::new();
    for cookie in cookies {
        let sane = !cookie.name.is_empty()
            && !cookie.domain.trim_start_matches('.').is_empty()
            && cookie.value.len() <= MAX_VALUE_BYTES
            && cookie.path.starts_with('/')
            && !cookie.name.contains(['=', ';', ' ', '\t'])
            && ![&cookie.name, &cookie.value, &cookie.domain, &cookie.path].iter().any(|text| text.contains(['\r', '\n', ';']) || text.chars().any(char::is_control));
        if !sane || cookie.expiration_date.is_some_and(|expires| expires <= now) {
            continue;
        }
        let same_site = page_within(page_host.as_deref(), &cookie.domain);
        let sent = match cookie.same_site.as_deref().unwrap_or("unspecified") {
            "no_restriction" => true,
            "strict" => same_site,
            _ => navigation || same_site,
        };
        if !sent {
            continue;
        }
        total += cookie.name.len() + cookie.value.len();
        if admitted.len() == MAX_COOKIES || total > MAX_TOTAL_BYTES {
            break;
        }
        admitted.push(cookie);
    }
    admitted
}

/// A jar holding `cookies`, each scoped as Chromium scoped it.
fn jar(cookies: &[CapturedCookie]) -> Arc<Jar> {
    let jar = Jar::default();
    let now = now_seconds();
    for cookie in cookies {
        let domain = bare_domain(&cookie.domain);
        // Set from an https URL inside the cookie's own scope, so Secure
        // cookies are accepted; the jar still sends them only over https.
        let Ok(origin) = reqwest::Url::parse(&format!("https://{domain}{}", cookie.path)) else { continue };
        let mut line = format!("{}={}; Path={}", cookie.name, cookie.value, cookie.path);
        if !cookie.host_only {
            line.push_str(&format!("; Domain={domain}"));
        }
        if cookie.secure {
            line.push_str("; Secure");
        }
        if cookie.http_only {
            line.push_str("; HttpOnly");
        }
        if let Some(expires) = cookie.expiration_date {
            line.push_str(&format!("; Max-Age={}", (expires - now).ceil().max(1.0) as u64));
        }
        jar.add_cookie_str(&line, &origin);
    }
    Arc::new(jar)
}
