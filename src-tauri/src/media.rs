use quick_xml::events::Event;
use quick_xml::Reader;
use reqwest::Url;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Segment {
    pub url: String,
    /// Optional HTTP byte range: (start offset, length).
    pub range: Option<(u64, u64)>,
    /// HLS full-segment encryption for this segment, if any. Only the open
    /// `METHOD=AES-128` form is represented; anything else fails parsing
    /// honestly so encrypted bytes never land in an output silently.
    pub key: Option<HlsKey>,
}

/// Open AES-128 segment encryption (RFC 8216 4.4.2.4): fetch `uri` for the
/// 16-byte key, decrypt CBC with `iv` (explicit, else the media sequence
/// number as 128-bit big-endian), strip PKCS#7.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HlsKey {
    pub uri: String,
    pub iv: Option<[u8; 16]>,
    pub sequence: u64,
}

/// Effective IV for a segment key: explicit IV when present, otherwise the
/// media sequence number as a 128-bit big-endian integer (RFC 8216 4.4.2.4).
pub fn hls_key_iv(key: &HlsKey) -> [u8; 16] {
    if let Some(iv) = key.iv { return iv; }
    let mut iv = [0u8; 16];
    iv[8..].copy_from_slice(&key.sequence.to_be_bytes());
    iv
}

/// Decrypt one full-segment AES-128-CBC body and strip PKCS#7. Ciphertext
/// must be non-empty and block-aligned; anything else is an honest error so
/// undecryptable bytes never land in an output silently.
pub fn decrypt_aes128_segment(ciphertext: &[u8], key_bytes: &[u8; 16], iv: [u8; 16]) -> Result<Vec<u8>, String> {
    use aes::Aes128;
    use cbc::Decryptor;
    use cipher::{BlockDecryptMut, KeyIvInit};
    if ciphertext.is_empty() { return Err("The AES-128 segment is empty".into()); }
    if ciphertext.len() % 16 != 0 { return Err("The AES-128 segment is not block-aligned".into()); }
    let mut buf = ciphertext.to_vec();
    let decrypted = Decryptor::<Aes128>::new(key_bytes.into(), &iv.into()).decrypt_padded_mut::<cipher::block_padding::Pkcs7>(&mut buf).map_err(|_| "The AES-128 segment failed PKCS#7 validation".to_string())?;
    Ok(decrypted.to_vec())
}

#[derive(Clone, Debug)]
pub struct MediaTrack {
    pub kind: String,
    pub segments: Vec<Segment>,
}

pub fn is_manifest_source(source: &str, mime: Option<&str>) -> bool {
    let path = Url::parse(source).ok().map(|url| url.path().to_ascii_lowercase()).unwrap_or_default();
    path.ends_with(".m3u8") || path.ends_with(".mpd") || mime.map(|value| value.to_ascii_lowercase().contains("mpegurl") || value.to_ascii_lowercase().contains("dash+xml")).unwrap_or(false)
}

pub fn hls_variant(source: &str, body: &str) -> Option<String> {
    let mut variant = false;
    let mut selected = None;
    for line in body.lines().map(str::trim) {
        if line.starts_with("#EXT-X-STREAM-INF") { variant = true; continue; }
        if variant && !line.is_empty() && !line.starts_with('#') {
            selected = resolve(source, line);
            variant = false;
        }
    }
    selected
}

pub fn hls_variant_tracks(source: &str, body: &str) -> Option<Vec<(String, String)>> {
    let mut audio = None;
    let mut pending_variant = false;
    let mut video = None;
    for line in body.lines().map(str::trim) {
        if line.starts_with("#EXT-X-MEDIA") && line.to_ascii_uppercase().contains("TYPE=AUDIO") {
            if let Some(uri) = hls_attribute(line, "URI").and_then(|value| resolve(source, &value)) { audio = Some(uri); }
        } else if line.starts_with("#EXT-X-STREAM-INF") {
            pending_variant = true;
        } else if pending_variant && !line.is_empty() && !line.starts_with('#') {
            video = resolve(source, line);
            break;
        }
    }
    let video = video?;
    let mut tracks = vec![("video".into(), video)];
    if let Some(audio) = audio { tracks.push(("audio".into(), audio)); }
    Some(tracks)
}

pub fn parse_hls(source: &str, body: &str) -> Result<Vec<Segment>, String> {
    if !body.lines().any(|line| line.trim().eq_ignore_ascii_case("#EXT-X-ENDLIST")) { return Err("Live media is not supported; a finite VOD playlist is required".into()); }
    let mut segments = Vec::new();
    let mut pending_range = None;
    let mut previous_range: Option<(String, u64)> = None;
    // RFC 8216 4.4.2.4: EXT-X-KEY applies to subsequent segments until
    // replaced or cleared; EXT-X-MEDIA-SEQUENCE anchors sequence numbers.
    let mut base_sequence: u64 = 0;
    let mut segment_index: u64 = 0;
    let mut pending_key: Option<HlsKeyTemplate> = None;
    for line in body.lines().map(str::trim) {
        if let Some(value) = line.strip_prefix("#EXT-X-BYTERANGE:") {
            pending_range = Some(parse_hls_byterange(value)?);
            continue;
        }
        if let Some(value) = line.strip_prefix("#EXT-X-MEDIA-SEQUENCE:") {
            base_sequence = value.trim().parse::<u64>().map_err(|_| "Invalid HLS media sequence number".to_string())?;
            continue;
        }
        if line.starts_with("#EXT-X-KEY:") {
            pending_key = parse_hls_key(source, line)?;
            continue;
        }
        if line.is_empty() || line.starts_with('#') { continue; }
        let Some(url) = resolve(source, line) else { continue; };
        let range = if let Some((length, offset)) = pending_range.take() {
            let start = match offset {
                Some(start) => start,
                None => previous_range.as_ref().filter(|(previous_url, _)| previous_url == &url).map(|(_, end)| *end).ok_or_else(|| "An HLS byte range omitted its offset without a preceding range on the same resource".to_string())?,
            };
            let end = start.checked_add(length).ok_or_else(|| "An HLS byte range exceeds the addressable resource size".to_string())?;
            previous_range = Some((url.clone(), end));
            Some((start, length))
        } else {
            previous_range = None;
            None
        };
        let sequence = base_sequence.saturating_add(segment_index);
        segment_index = segment_index.saturating_add(1);
        let key = pending_key.clone().map(|template| HlsKey { uri: template.uri, iv: template.iv, sequence });
        segments.push(Segment { url, range, key });
    }
    if segments.is_empty() { return Err("The VOD playlist did not contain any media fragments".into()); }
    if let Some(line) = body.lines().map(str::trim).find(|line| line.starts_with("#EXT-X-MAP")) {
        let Some(map) = hls_map_segment(line)? else { return Err("The HLS initialization map is missing its URI".into()); };
        if let Some(url) = resolve(source, &map.0) {
            segments.insert(0, Segment { url, range: map.1, key: None });
        }
    }
    Ok(segments)
}

/// Flattened single-track view; the engine uses parse_dash_tracks, tests use this.
#[allow(dead_code)]
pub fn parse_dash(source: &str, body: &str) -> Result<Vec<Segment>, String> {
    Ok(parse_dash_tracks(source, body)?.into_iter().flat_map(|track| track.segments).collect())
}

pub fn parse_dash_tracks(source: &str, body: &str) -> Result<Vec<MediaTrack>, String> {
    parse_dash_tracks_for_segments(source, body, &[])
}

pub fn parse_dash_tracks_for_segments(source: &str, body: &str, selected_segments: &[String]) -> Result<Vec<MediaTrack>, String> {
    let lower = body.to_ascii_lowercase();
    if lower.contains("type=\"dynamic\"") || lower.contains("type='dynamic'") || lower.contains("minimumupdateperiod=") || lower.contains("timeshiftbufferdepth=") { return Err("Live media is not supported; a static MPD is required".into()); }
    let mut reader = Reader::from_str(body);
    reader.config_mut().trim_text(true);
    let mut stack: Vec<Vec<u8>> = Vec::new();
    let mut global_base_urls = Vec::new();
    let mut global_base_text_depth = None;
    let mut current_track: Option<DashTrackBuilder> = None;
    let mut presentation_duration = None;
    let mut tracks = Vec::new();
    loop {
        match reader.read_event() {
            Ok(Event::Start(element)) => {
                let name = element.name().as_ref().to_ascii_lowercase();
                if name.as_slice() == b"mpd" { presentation_duration = attribute(&element, b"mediaPresentationDuration").and_then(|value| parse_duration(&value)); }
                if name.as_slice() == b"adaptationset" && current_track.is_none() {
                    current_track = Some(DashTrackBuilder::new(attribute(&element, b"contentType").or_else(|| attribute(&element, b"mimeType"))));
                } else if name.as_slice() == b"representation" {
                    if let Some(track) = current_track.as_mut() {
                        let representation_id = attribute(&element, b"id").unwrap_or_default();
                        track.representation_ids.push(representation_id.clone());
                        if !track.selected_representation {
                            track.selected_representation = true;
                            track.representation_open = true;
                            track.representation_id = representation_id;
                            track.bandwidth = attribute(&element, b"bandwidth").unwrap_or_default();
                            if let Some(kind) = attribute(&element, b"contentType").or_else(|| attribute(&element, b"mimeType")).and_then(|value| track_kind(&value)) { track.kind = kind; }
                        }
                    }
                }
                if let Some(track) = current_track.as_mut() {
                    if name.as_slice() == b"baseurl" { track.base_text_depth = Some(stack.len() + 1); }
                    if name.as_slice() == b"segmenttemplate" && track.template.is_none() { track.template = Some(DashTemplate::from_element(&element)); }
                    if name.as_slice() == b"s" && (track.representation_open || !track.selected_representation) { if let Some(template) = track.template.as_mut() { template.timeline.push(DashTimeline::from_element(&element)); } }
                    if (name.as_slice() == b"initialization" || name.as_slice() == b"segmenturl")
                        && (track.representation_open || !track.selected_representation)
                    {
                        let attribute_name = if name.as_slice() == b"initialization" {
                            b"sourceURL".as_slice()
                        } else {
                            b"media".as_slice()
                        };
                        let range_name = if name.as_slice() == b"initialization" {
                            b"range".as_slice()
                        } else {
                            b"mediaRange".as_slice()
                        };
                        if let Some(value) = attribute(&element, attribute_name) {
                            track.segment_refs.push((value, dash_range(&element, range_name)?));
                        }
                    }
                } else if name.as_slice() == b"baseurl" { global_base_text_depth = Some(stack.len() + 1); }
                stack.push(name.to_vec());
            }
            Ok(Event::Empty(element)) => {
                let name = element.name().as_ref().to_ascii_lowercase();
                if name.as_slice() == b"mpd" { presentation_duration = attribute(&element, b"mediaPresentationDuration").and_then(|value| parse_duration(&value)); }
                if name.as_slice() == b"adaptationset" && current_track.is_none() { current_track = Some(DashTrackBuilder::new(attribute(&element, b"contentType").or_else(|| attribute(&element, b"mimeType")))); }
                if name.as_slice() == b"representation" {
                    if let Some(track) = current_track.as_mut() {
                        let representation_id = attribute(&element, b"id").unwrap_or_default();
                        track.representation_ids.push(representation_id.clone());
                        if !track.selected_representation {
                            track.selected_representation = true;
                            track.representation_id = representation_id;
                            track.bandwidth = attribute(&element, b"bandwidth").unwrap_or_default();
                            if let Some(kind) = attribute(&element, b"contentType").or_else(|| attribute(&element, b"mimeType")).and_then(|value| track_kind(&value)) { track.kind = kind; }
                        }
                    }
                }
                if let Some(track) = current_track.as_mut() {
                    if name.as_slice() == b"segmenttemplate" && track.template.is_none() { track.template = Some(DashTemplate::from_element(&element)); }
                    if (name.as_slice() == b"initialization" || name.as_slice() == b"segmenturl")
                        && (track.representation_open || !track.selected_representation)
                    {
                        let attribute_name = if name.as_slice() == b"initialization" {
                            b"sourceURL".as_slice()
                        } else {
                            b"media".as_slice()
                        };
                        let range_name = if name.as_slice() == b"initialization" {
                            b"range".as_slice()
                        } else {
                            b"mediaRange".as_slice()
                        };
                        if let Some(value) = attribute(&element, attribute_name) {
                            track.segment_refs.push((value, dash_range(&element, range_name)?));
                        }
                    }
                    if name.as_slice() == b"s" && (track.representation_open || !track.selected_representation) { if let Some(template) = track.template.as_mut() { template.timeline.push(DashTimeline::from_element(&element)); } }
                }
            }
            Ok(Event::Text(text)) => {
                if let Ok(value) = text.decode() {
                    if let Some(track) = current_track.as_mut() {
                        if track.base_text_depth == Some(stack.len()) { if let Some(url) = resolve(source, value.trim()) { track.base_urls.push(url); } }
                    } else if global_base_text_depth == Some(stack.len()) { if let Some(url) = resolve(source, value.trim()) { global_base_urls.push(url); } }
                }
            }
            Ok(Event::End(element)) => {
                let name = element.name().as_ref().to_ascii_lowercase();
                if let Some(track) = current_track.as_mut() {
                    if name.as_slice() == b"baseurl" { track.base_text_depth = None; }
                    if name.as_slice() == b"representation" && track.representation_open { track.representation_open = false; }
                    if name.as_slice() == b"adaptationset" {
                        let finished = current_track.take().and_then(|track| track.finish(source, global_base_urls.first().map(String::as_str), presentation_duration, selected_segments));
                        if let Some(track) = finished { tracks.push(track); }
                    }
                } else if name.as_slice() == b"baseurl" { global_base_text_depth = None; }
                stack.pop();
            }
            Ok(Event::Eof) => break,
            Err(error) => return Err(format!("Could not parse MPD: {error}")),
            _ => {}
        }
    }
    if let Some(track) = current_track.take().and_then(|track| track.finish(source, global_base_urls.first().map(String::as_str), presentation_duration, selected_segments)) { tracks.push(track); }
    if tracks.is_empty() { return Err("The static MPD did not contain downloadable segments".into()); }
    Ok(tracks)
}

struct DashTrackBuilder {
    kind: String,
    base_urls: Vec<String>,
    segment_refs: Vec<(String, Option<(u64, u64)>)>,
    template: Option<DashTemplate>,
    representation_id: String,
    bandwidth: String,
    representation_ids: Vec<String>,
    selected_representation: bool,
    representation_open: bool,
    base_text_depth: Option<usize>,
}

impl DashTrackBuilder {
    fn new(kind: Option<String>) -> Self { Self { kind: kind.as_deref().and_then(track_kind).unwrap_or_default(), base_urls: Vec::new(), segment_refs: Vec::new(), template: None, representation_id: String::new(), bandwidth: String::new(), representation_ids: Vec::new(), selected_representation: false, representation_open: false, base_text_depth: None } }

    fn finish(self, source: &str, inherited_base: Option<&str>, presentation_duration: Option<u64>, selected_segments: &[String]) -> Option<MediaTrack> {
        let base = self.base_urls.first().map(String::as_str).or(inherited_base).unwrap_or(source);
        let representation_ids = if self.representation_ids.is_empty() { vec![self.representation_id.clone()] } else { self.representation_ids.clone() };
        let mut segments = self.segment_refs.iter().filter_map(|(value, range)| resolve(base, value).map(|url| Segment { url, range: *range, key: None })).collect::<Vec<_>>();
        if segments.is_empty() {
            let Some(template) = self.template.as_ref() else { return None; };
            let mut chosen_id = self.representation_id.as_str();
            for representation_id in &representation_ids {
                if let Ok(candidate) = expand_dash_template(template, base, representation_id, &self.bandwidth, presentation_duration) {
                    if selected_segments.iter().any(|selected| candidate.iter().any(|segment| &segment.url == selected)) {
                        chosen_id = representation_id;
                        break;
                    }
                }
            }
            segments = expand_dash_template(template, base, chosen_id, &self.bandwidth, presentation_duration).ok()?;
        }
        if segments.is_empty() { return None; }
        Some(MediaTrack { kind: self.kind, segments })
    }
}

fn track_kind(value: &str) -> Option<String> {
    let lower = value.to_ascii_lowercase();
    if lower.contains("video") { Some("video".into()) } else if lower.contains("audio") { Some("audio".into()) } else { None }
}

#[derive(Clone, Debug)]
struct DashTemplate {
    media: Option<String>,
    initialization: Option<String>,
    timescale: u64,
    duration: Option<u64>,
    start_number: u64,
    timeline: Vec<DashTimeline>,
}

impl DashTemplate {
    fn from_element(element: &quick_xml::events::BytesStart<'_>) -> Self {
        Self {
            media: attribute(element, b"media"),
            initialization: attribute(element, b"initialization"),
            timescale: attribute(element, b"timescale").and_then(|value| value.parse().ok()).unwrap_or(1),
            duration: attribute(element, b"duration").and_then(|value| value.parse().ok()),
            start_number: attribute(element, b"startNumber").and_then(|value| value.parse().ok()).unwrap_or(1),
            timeline: Vec::new(),
        }
    }
}

#[derive(Clone, Debug)]
struct DashTimeline {
    time: Option<u64>,
    duration: u64,
    repeat: i64,
}

impl DashTimeline {
    fn from_element(element: &quick_xml::events::BytesStart<'_>) -> Self {
        Self {
            time: attribute(element, b"t").and_then(|value| value.parse().ok()),
            duration: attribute(element, b"d").and_then(|value| value.parse().ok()).unwrap_or(0),
            repeat: attribute(element, b"r").and_then(|value| value.parse().ok()).unwrap_or(0),
        }
    }
}

fn expand_dash_template(template: &DashTemplate, base: &str, representation_id: &str, bandwidth: &str, presentation_duration: Option<u64>) -> Result<Vec<Segment>, String> {
    let Some(media) = template.media.as_deref() else { return Err("The static MPD did not define media URLs".into()); };
    let mut segments = Vec::new();
    if let Some(initialization) = template.initialization.as_deref() {
        let value = expand_template(initialization, template.start_number, 0, representation_id, bandwidth);
        if let Some(url) = resolve(base, &value) { segments.push(Segment { url, range: None, key: None }); }
    }
    let mut number = template.start_number;
    let mut current_time = 0u64;
    if !template.timeline.is_empty() {
        for (index, item) in template.timeline.iter().enumerate() {
            let start = item.time.unwrap_or(current_time);
            let next_time = template.timeline.get(index + 1).and_then(|next| next.time);
            let repeat = if item.repeat >= 0 { item.repeat as u64 + 1 } else if let Some(next) = next_time { ((next.saturating_sub(start) + item.duration.max(1) - 1) / item.duration.max(1)).max(1) } else if let Some(duration) = presentation_duration { ((duration.saturating_mul(template.timescale).saturating_sub(start) + item.duration.max(1) - 1) / item.duration.max(1)).max(1) } else { 1 };
            for offset in 0..repeat.min(100_000) {
                let time = start.saturating_add(offset.saturating_mul(item.duration));
                let value = expand_template(media, number, time, representation_id, bandwidth);
                if let Some(url) = resolve(base, &value) { segments.push(Segment { url, range: None, key: None }); }
                number = number.saturating_add(1);
            }
            current_time = start.saturating_add(repeat.saturating_mul(item.duration));
        }
    } else if let (Some(duration), Some(segment_duration)) = (presentation_duration, template.duration) {
        let count = ((duration.saturating_mul(template.timescale) + segment_duration.saturating_sub(1)) / segment_duration).min(100_000);
        for index in 0..count {
            let time = index.saturating_mul(segment_duration);
            let value = expand_template(media, number, time, representation_id, bandwidth);
            if let Some(url) = resolve(base, &value) { segments.push(Segment { url, range: None, key: None }); }
            number = number.saturating_add(1);
        }
    }
    if segments.len() <= usize::from(template.initialization.is_some()) { return Err("The static MPD did not contain a finite segment timeline".into()); }
    Ok(segments)
}

fn expand_template(template: &str, number: u64, time: u64, representation_id: &str, bandwidth: &str) -> String {
    let mut value = template.replace("$Number$", &number.to_string()).replace("$Time$", &time.to_string()).replace("$RepresentationID$", representation_id).replace("$Bandwidth$", bandwidth);
    while let Some(start) = value.find("$Number%") {
        let Some(end_offset) = value[start..].find("d$") else { break; };
        let width_end = start + end_offset;
        let end = width_end + 2;
        let width = value[start + 8..width_end].parse::<usize>().unwrap_or(0);
        let formatted = if width > 0 { format!("{number:0width$}") } else { number.to_string() };
        value.replace_range(start..end, &formatted);
    }
    value
}

fn parse_duration(value: &str) -> Option<u64> {
    let value = value.strip_prefix("PT")?;
    let mut number = String::new();
    let mut seconds = 0.0;
    for character in value.chars() {
        if character.is_ascii_digit() || character == '.' { number.push(character); continue; }
        let amount = number.parse::<f64>().ok()?;
        seconds += match character { 'H' => amount * 3600.0, 'M' => amount * 60.0, 'S' => amount, _ => return None };
        number.clear();
    }
    if !number.is_empty() { return None; }
    Some(seconds.ceil() as u64)
}

fn hls_map_segment(line: &str) -> Result<Option<(String, Option<(u64, u64)>)>, String> {
    if !line.starts_with("#EXT-X-MAP") { return Ok(None); }
    let Some(uri) = hls_attribute(line, "URI") else { return Ok(None); };
    let range = hls_attribute(line, "BYTERANGE").map(|value| parse_hls_byterange(&value).map(|(length, offset)| (offset.unwrap_or(0), length))).transpose()?;
    Ok(Some((uri, range)))
}

/// Parsed EXT-X-KEY state before sequence anchoring: EXT-X-KEY applies to
/// subsequent segments, so the per-segment sequence is attached at push time.
#[derive(Clone, Debug, PartialEq, Eq)]
struct HlsKeyTemplate {
    uri: String,
    iv: Option<[u8; 16]>,
}

/// Parse an `#EXT-X-KEY:` line. Returns `Ok(None)` for `METHOD=NONE`
/// (clears encryption); any method other than open `AES-128` is an honest
/// error so encrypted bytes never land in an output silently.
fn parse_hls_key(source: &str, line: &str) -> Result<Option<HlsKeyTemplate>, String> {
    let body = line.strip_prefix("#EXT-X-KEY:").unwrap_or("");
    let method = hls_attr_bare(body, "METHOD").unwrap_or_default();
    if method.eq_ignore_ascii_case("NONE") { return Ok(None); }
    if !method.eq_ignore_ascii_case("AES-128") { return Err(format!("Unsupported HLS encryption method: {method}")); }
    if let Some(format) = hls_attribute(line, "KEYFORMAT").or_else(|| hls_attr_bare(body, "KEYFORMAT")) {
        if !format.eq_ignore_ascii_case("identity") { return Err(format!("Unsupported HLS key format: {format}")); }
    }
    let Some(uri_value) = hls_attribute(line, "URI") else { return Err("HLS AES-128 key is missing its URI".into()); };
    let Some(uri) = resolve(source, &uri_value) else { return Err("HLS AES-128 key URI does not resolve".into()); };
    let iv = hls_attr_bare(body, "IV").as_deref().map(parse_hls_iv).transpose()?;
    Ok(Some(HlsKeyTemplate { uri, iv }))
}

/// Bare (unquoted) attribute value: `KEY=value` up to the next comma.
fn hls_attr_bare(body: &str, key: &str) -> Option<String> {
    let marker = format!("{key}=");
    let start = body.find(&marker)? + marker.len();
    let rest = &body[start..];
    if rest.starts_with('"') { return hls_attribute(body, key); }
    Some(rest.split(',').next()?.trim().to_string())
}

fn parse_hls_iv(value: &str) -> Result<[u8; 16], String> {
    let hex = value.strip_prefix("0x").or_else(|| value.strip_prefix("0X")).unwrap_or(value);
    if hex.len() != 32 || !hex.chars().all(|c| c.is_ascii_hexdigit()) { return Err("Invalid HLS key IV".into()); }
    let mut iv = [0u8; 16];
    for (index, chunk) in hex.as_bytes().chunks(2).enumerate() {
        iv[index] = u8::from_str_radix(std::str::from_utf8(chunk).map_err(|_| "Invalid HLS key IV".to_string())?, 16).map_err(|_| "Invalid HLS key IV".to_string())?;
    }
    Ok(iv)
}

fn hls_attribute(line: &str, key: &str) -> Option<String> {
    let marker = format!("{key}=\"");
    let start = line.find(&marker)? + marker.len();
    Some(line[start..].split('\"').next()?.to_string())
}

fn parse_hls_byterange(value: &str) -> Result<(u64, Option<u64>), String> {
    let (length, offset) = value.split_once('@').map_or((value, None), |(length, offset)| (length, Some(offset)));
    let length = length.parse::<u64>().map_err(|_| "Invalid HLS byte-range length".to_string())?;
    if length == 0 { return Err("An HLS byte range must have a positive length".into()); }
    let offset = offset.map(|value| value.parse::<u64>().map_err(|_| "Invalid HLS byte-range offset".to_string())).transpose()?;
    Ok((length, offset))
}


fn dash_range(element: &quick_xml::events::BytesStart<'_>, key: &[u8]) -> Result<Option<(u64, u64)>, String> {
    let Some(value) = attribute(element, key) else { return Ok(None); };
    let Some((start, end)) = value.split_once('-') else { return Err(format!("Invalid DASH byte range: {value}")); };
    let start = start.parse::<u64>().map_err(|_| format!("Invalid DASH byte range start: {value}"))?;
    let end = end.parse::<u64>().map_err(|_| format!("Invalid DASH byte range end: {value}"))?;
    let length = end.checked_sub(start).and_then(|length| length.checked_add(1)).ok_or_else(|| format!("Invalid DASH byte range: {value}"))?;
    Ok(Some((start, length)))
}

fn attribute(element: &quick_xml::events::BytesStart<'_>, key: &[u8]) -> Option<String> {
    element.attributes().flatten().find(|attribute| attribute.key.as_ref().eq_ignore_ascii_case(key)).and_then(|attribute| attribute.normalized_value(quick_xml::XmlVersion::Implicit1_0).ok().map(|value| value.into_owned()))
}

fn resolve(source: &str, value: &str) -> Option<String> {
    Url::parse(value).ok().or_else(|| Url::parse(source).ok()?.join(value).ok()).map(|url| url.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_finite_hls_in_order() {
        let body = "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:2,\none.m4s\n#EXTINF:2,\ntwo.m4s\n#EXT-X-ENDLIST";
        let segments = parse_hls("https://cdn.example.test/vod/index.m3u8", body).expect("finite playlist");
        assert_eq!(segments.iter().map(|segment| segment.url.as_str()).collect::<Vec<_>>(), ["https://cdn.example.test/vod/init.mp4", "https://cdn.example.test/vod/one.m4s", "https://cdn.example.test/vod/two.m4s"]);
    }

    #[test]
    fn parses_hls_byte_ranges_and_rejects_unsafe_implicit_offsets() {
        let body = "#EXTM3U\n#EXT-X-VERSION:4\n#EXT-X-MAP:URI=\"shared.mp4\",BYTERANGE=\"2@30\"\n#EXTINF:2,\n#EXT-X-BYTERANGE:4@0\nshared.mp4\n#EXTINF:2,\n#EXT-X-BYTERANGE:3\nshared.mp4\n#EXT-X-ENDLIST";
        let segments = parse_hls("https://cdn.example.test/vod/index.m3u8", body).expect("byte-range playlist");
        assert_eq!(segments[0].url, "https://cdn.example.test/vod/shared.mp4");
        assert_eq!(segments[0].range, Some((30, 2)));
        assert_eq!(segments[1].range, Some((0, 4)));
        assert_eq!(segments[2].range, Some((4, 3)));
        assert!(parse_hls("https://cdn.example.test/vod/index.m3u8", "#EXTM3U\n#EXT-X-BYTERANGE:3\nshared.mp4\n#EXT-X-ENDLIST").is_err());
    }

    #[test]
    fn rejects_live_hls() {
        assert!(parse_hls("https://cdn.example.test/live.m3u8", "#EXTM3U\n#EXTINF:2,\none.ts").is_err());
    }

    #[test]
    fn parses_static_dash_segment_urls() {
        let body = "<MPD type=\"static\"><Period><AdaptationSet><Representation><BaseURL>https://cdn.example.test/vod/</BaseURL><SegmentList><Initialization sourceURL=\"init.mp4\"/><SegmentURL media=\"one.m4s\"/><SegmentURL media=\"two.m4s\"/></SegmentList></Representation></AdaptationSet></Period></MPD>";
        let segments = parse_dash("https://cdn.example.test/manifest.mpd", body).expect("static MPD");
        assert_eq!(segments.iter().map(|segment| segment.url.as_str()).collect::<Vec<_>>(), ["https://cdn.example.test/vod/init.mp4", "https://cdn.example.test/vod/one.m4s", "https://cdn.example.test/vod/two.m4s"]);
    }

    #[test]
    fn expands_static_dash_segment_template() {
        let body = "<MPD type=\"static\" mediaPresentationDuration=\"PT6S\"><Period><AdaptationSet><Representation id=\"video\"><BaseURL>https://cdn.example.test/vod/</BaseURL><SegmentTemplate timescale=\"1\" media=\"seg-$Number$.m4s\" initialization=\"init.mp4\" startNumber=\"1\"><SegmentTimeline><S t=\"0\" d=\"2\" r=\"2\"/></SegmentTimeline></SegmentTemplate></Representation></AdaptationSet></Period></MPD>";
        let segments = parse_dash("https://cdn.example.test/manifest.mpd", body).expect("static template MPD");
        assert_eq!(segments.iter().map(|segment| segment.url.as_str()).collect::<Vec<_>>(), ["https://cdn.example.test/vod/init.mp4", "https://cdn.example.test/vod/seg-1.m4s", "https://cdn.example.test/vod/seg-2.m4s", "https://cdn.example.test/vod/seg-3.m4s"]);
    }

    #[test]
    fn expands_dash_number_padding_and_iso_duration() {
        let body = "<MPD type=\"static\" mediaPresentationDuration=\"PT1M2.2S\"><Period><AdaptationSet><Representation id=\"video\"><BaseURL>https://cdn.example.test/vod/</BaseURL><SegmentTemplate timescale=\"1\" duration=\"10\" media=\"seg-$Number%05d$.m4s\" initialization=\"init.mp4\"/></Representation></AdaptationSet></Period></MPD>";
        let segments = parse_dash("https://cdn.example.test/manifest.mpd", body).expect("duration template MPD");
        assert_eq!(segments.first().map(|segment| segment.url.as_str()), Some("https://cdn.example.test/vod/init.mp4"));
        assert_eq!(segments.get(1).map(|segment| segment.url.as_str()), Some("https://cdn.example.test/vod/seg-00001.m4s"));
        assert_eq!(segments.len(), 8);
    }

    #[test]
    fn keeps_static_dash_audio_and_video_tracks_separate() {
        let body = "<MPD type=\"static\"><Period><AdaptationSet contentType=\"video\"><Representation id=\"v\"><BaseURL>https://cdn.example.test/v/</BaseURL><SegmentList><Initialization sourceURL=\"init.mp4\"/><SegmentURL media=\"one.m4s\"/></SegmentList></Representation></AdaptationSet><AdaptationSet contentType=\"audio\"><Representation id=\"a\"><BaseURL>https://cdn.example.test/a/</BaseURL><SegmentList><Initialization sourceURL=\"init.mp4\"/><SegmentURL media=\"one.m4s\"/></SegmentList></Representation></AdaptationSet></Period></MPD>";
        let tracks = parse_dash_tracks("https://cdn.example.test/manifest.mpd", body).expect("separate tracks");
        assert_eq!(tracks.iter().map(|track| track.kind.as_str()).collect::<Vec<_>>(), ["video", "audio"]);
        assert_eq!(tracks[0].segments[1].url, "https://cdn.example.test/v/one.m4s");
        assert_eq!(tracks[1].segments[1].url, "https://cdn.example.test/a/one.m4s");
    }

    #[test]
    fn parses_adaptation_level_dash_segment_list() {
        let body = "<MPD type=\"static\"><Period><AdaptationSet contentType=\"video\"><SegmentList><Initialization sourceURL=\"init.mp4\" range=\"30-31\"/><SegmentURL media=\"one.m4s\" mediaRange=\"100-109\"/><SegmentURL media=\"two.m4s\" mediaRange=\"110-119\"/></SegmentList><Representation id=\"video\"><BaseURL>https://cdn.example.test/vod/</BaseURL></Representation></AdaptationSet></Period></MPD>";
        let segments = parse_dash("https://cdn.example.test/manifest.mpd", body).expect("adaptation-level segment list");
        assert_eq!(segments.iter().map(|segment| segment.url.as_str()).collect::<Vec<_>>(), ["https://cdn.example.test/vod/init.mp4", "https://cdn.example.test/vod/one.m4s", "https://cdn.example.test/vod/two.m4s"]);
        assert_eq!(segments[0].range, Some((30, 2)));
        assert_eq!(segments[1].range, Some((100, 10)));
        assert_eq!(segments[2].range, Some((110, 10)));
        assert!(parse_dash("https://cdn.example.test/manifest.mpd", "<MPD type=\"static\"><Period><AdaptationSet><Representation><SegmentList><SegmentURL media=\"one.m4s\" mediaRange=\"10-2\"/></SegmentList></Representation></AdaptationSet></Period></MPD>").is_err());
    }

    #[test]
    fn selects_dash_representation_matching_browser_segment() {
        let body = "<MPD type=\"static\" mediaPresentationDuration=\"PT4S\"><Period><AdaptationSet contentType=\"video\"><SegmentTemplate timescale=\"1\" duration=\"2\" media=\"$RepresentationID$-$Number$.m4s\" initialization=\"$RepresentationID$-init.m4s\"/><Representation id=\"low\"/><Representation id=\"high\"/></AdaptationSet><AdaptationSet contentType=\"audio\"><Representation id=\"audio\"><SegmentList><Initialization sourceURL=\"audio-init.m4s\"/><SegmentURL media=\"audio-1.m4s\"/><SegmentURL media=\"audio-2.m4s\"/></SegmentList></Representation></AdaptationSet></Period></MPD>";
        let tracks = parse_dash_tracks_for_segments("https://cdn.example.test/vod/manifest.mpd", body, &["https://cdn.example.test/vod/high-1.m4s".into()]).expect("selected DASH representation");
        assert_eq!(tracks[0].segments[0].url, "https://cdn.example.test/vod/high-init.m4s");
        assert_eq!(tracks[0].segments[1].url, "https://cdn.example.test/vod/high-1.m4s");
        assert_eq!(tracks[0].segments[2].url, "https://cdn.example.test/vod/high-2.m4s");
    }

    #[test]
    fn finds_hls_video_and_alternate_audio_playlists() {
        let body = "#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"audio\",URI=\"audio/index.m3u8\"\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,AUDIO=\"audio\"\nvideo/index.m3u8";
        let tracks = hls_variant_tracks("https://cdn.example.test/vod/master.m3u8", body).expect("HLS master");
        assert_eq!(tracks, [("video".into(), "https://cdn.example.test/vod/video/index.m3u8".into()), ("audio".into(), "https://cdn.example.test/vod/audio/index.m3u8".into())]);
    }

    #[test]
    fn parses_aes128_key_with_sequence_iv_and_transition() {
        let body = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:7\n#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n#EXTINF:2,\none.ts\n#EXTINF:2,\ntwo.ts\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:2,\nthree.ts\n#EXT-X-ENDLIST";
        let segments = parse_hls("https://cdn.example.test/vod/index.m3u8", body).expect("aes playlist");
        assert_eq!(segments.len(), 3);
        let first = segments[0].key.as_ref().expect("first encrypted");
        assert_eq!(first.uri, "https://cdn.example.test/vod/key.bin");
        assert_eq!(first.sequence, 7);
        assert_eq!(hls_key_iv(first)[8..], 7u64.to_be_bytes());
        assert_eq!(segments[1].key.as_ref().expect("second encrypted").sequence, 8);
        assert!(segments[2].key.is_none());
    }

    #[test]
    fn parses_aes128_explicit_iv_and_rejects_other_methods() {
        let body = "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\",IV=0x0000000000000000000000000000002a\n#EXTINF:2,\none.ts\n#EXT-X-ENDLIST";
        let segments = parse_hls("https://cdn.example.test/vod/index.m3u8", body).expect("explicit iv");
        let key = segments[0].key.as_ref().expect("key");
        assert_eq!(hls_key_iv(key)[15], 0x2a);
        assert!(parse_hls("https://cdn.example.test/vod/index.m3u8", "#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI=\"key.bin\"\n#EXTINF:2,\none.ts\n#EXT-X-ENDLIST").is_err());
    }

    #[test]
    fn decrypts_aes128_cbc_pkcs7_roundtrip_and_rejects_misaligned() {
        let key = [0x11u8; 16];
        let iv = [0x22u8; 16];
        let plaintext = b"hello aes-128 hls slice";
        // openssl enc -aes-128-cbc -K 11*16 -iv 22*16 of the plaintext above.
        let ciphertext: [u8; 32] = [0xdb, 0x65, 0x64, 0x3d, 0x41, 0x77, 0x4f, 0x88, 0xbf, 0xbd, 0x33, 0x07, 0x80, 0x24, 0xb6, 0xc0, 0xb5, 0x18, 0x95, 0x3b, 0x30, 0x6d, 0xf8, 0xb9, 0x2b, 0xd1, 0xf5, 0x9b, 0x94, 0xb7, 0xe7, 0x4d];
        let decrypted = decrypt_aes128_segment(&ciphertext, &key, iv).expect("known vector");
        assert_eq!(decrypted, plaintext);
        assert!(decrypt_aes128_segment(&ciphertext[..15], &key, iv).is_err());
        let mut tampered = ciphertext;
        let last = tampered.len() - 1;
        tampered[last] ^= 0xff;
        assert!(decrypt_aes128_segment(&tampered, &key, iv).is_err());
    }
}
