use quick_xml::events::Event;
use quick_xml::Reader;
use reqwest::Url;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

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

/// Full-segment AES-128-CBC decryption fed in arbitrary pieces, so a segment
/// is decrypted as it streams to disk (F13). Whole blocks are released as
/// they arrive; the last block is held back until `finish`, which checks and
/// strips the PKCS#7 padding. Empty, unaligned or badly padded ciphertext is
/// an honest error, so undecryptable bytes never land in an output silently.
pub struct SegmentDecryptor {
    cipher: cbc::Decryptor<aes::Aes128>,
    pending: Vec<u8>,
    seen: u64,
}

impl SegmentDecryptor {
    pub fn new(key_bytes: &[u8; 16], iv: [u8; 16]) -> Self {
        use cipher::KeyIvInit;
        Self { cipher: cbc::Decryptor::new(key_bytes.into(), &iv.into()), pending: Vec::new(), seen: 0 }
    }

    /// Decrypt what can be released of `input`; the result may be empty.
    pub fn update(&mut self, input: &[u8]) -> Vec<u8> {
        use cipher::BlockDecryptMut;
        self.seen += input.len() as u64;
        self.pending.extend_from_slice(input);
        let partial = self.pending.len() % 16;
        let release = if partial == 0 { self.pending.len().saturating_sub(16) } else { self.pending.len() - partial };
        let mut plain: Vec<u8> = self.pending.drain(..release).collect();
        for block in plain.chunks_exact_mut(16) {
            self.cipher.decrypt_block_mut(cipher::generic_array::GenericArray::from_mut_slice(block));
        }
        plain
    }

    /// The final block with its padding removed.
    pub fn finish(mut self) -> Result<Vec<u8>, String> {
        use cipher::BlockDecryptMut;
        if self.seen == 0 { return Err("The AES-128 segment is empty".into()); }
        if self.pending.len() != 16 { return Err("The AES-128 segment is not block-aligned".into()); }
        self.cipher.decrypt_block_mut(cipher::generic_array::GenericArray::from_mut_slice(&mut self.pending));
        let padding = usize::from(self.pending[15]);
        if padding == 0 || padding > 16 || self.pending[16 - padding..].iter().any(|byte| usize::from(*byte) != padding) {
            return Err("The AES-128 segment failed PKCS#7 validation".into());
        }
        self.pending.truncate(16 - padding);
        Ok(self.pending)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DashSegmentBase {
    pub url: String,
    pub initialization_range: Option<(u64, u64)>,
    pub index_range: (u64, u64),
    pub container: String,
}

#[derive(Clone, Debug)]
pub struct MediaTrack {
    pub kind: String,
    pub segments: Vec<Segment>,
    pub segment_base: Option<DashSegmentBase>,
}

pub fn is_manifest_source(source: &str, mime: Option<&str>) -> bool {
    let path = Url::parse(source)
        .ok()
        .map(|url| url.path().to_ascii_lowercase())
        .unwrap_or_default();
    path.ends_with(".m3u8")
        || path.ends_with(".mpd")
        || mime
            .map(|value| {
                value.to_ascii_lowercase().contains("mpegurl")
                    || value.to_ascii_lowercase().contains("dash+xml")
            })
            .unwrap_or(false)
}

pub fn is_manifest_body(body: &[u8]) -> bool {
    let body = String::from_utf8_lossy(body);
    let body = body.trim_start_matches('\u{feff}').trim_start();
    let lower = body.get(..body.len().min(8192)).unwrap_or(body).to_ascii_lowercase();
    lower.starts_with("#extm3u")
        || lower.contains("#ext-x-stream-inf")
        || lower.contains("#ext-x-targetduration")
        || lower.contains("<mpd")
        || lower.contains("<?xml") && lower.contains("<mpd")
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

/// Every URI an HLS playlist references, resolved: a multivariant playlist's
/// variant and rendition playlists, a media playlist's segments and maps.
pub fn hls_references(source: &str, body: &str) -> Vec<String> {
    let mut references = Vec::new();
    for line in body.lines().map(str::trim).filter(|line| !line.is_empty()) {
        let uri = if line.starts_with("#EXT-X-MEDIA") || line.starts_with("#EXT-X-MAP") {
            hls_attribute(line, "URI")
        } else if line.starts_with('#') {
            None
        } else {
            Some(line.to_string())
        };
        if let Some(uri) = uri.and_then(|value| resolve(source, &value)) {
            references.push(uri);
        }
    }
    references
}

pub fn hls_variant_tracks(
    source: &str,
    body: &str,
    selected_segments: &[String]
) -> Option<Vec<(String, String)>> {
    let mut audio_options: Vec<(Option<String>, String, bool)> = Vec::new();
    let mut variants: Vec<(Option<String>, String)> = Vec::new();
    let mut pending_variant: Option<String> = None;
    for line in body.lines().map(str::trim) {
        if line.starts_with("#EXT-X-MEDIA") && line.to_ascii_uppercase().contains("TYPE=AUDIO") {
            let group = hls_attr_bare(line, "GROUP-ID");
            let is_default = hls_attr_bare(line, "DEFAULT")
                .is_some_and(|value| value.eq_ignore_ascii_case("YES"));
            if let Some(uri) = hls_attribute(line, "URI").and_then(|value| resolve(source, &value))
            {
                audio_options.push((group, uri, is_default));
            }
        } else if line.starts_with("#EXT-X-STREAM-INF") {
            pending_variant = Some(line.to_string());
        } else if pending_variant.is_some() && !line.is_empty() && !line.starts_with('#') {
            let attributes = pending_variant.take().expect("pending HLS variant");
            let group = hls_attr_bare(&attributes, "AUDIO");
            if let Some(uri) = resolve(source, line) {
                variants.push((group, uri));
            }
        }
    }
    let selected_video = selected_segments
        .iter()
        .find_map(|selected| {
            variants
                .iter()
                .find(|(_, uri)| selected_hls_url(uri, std::slice::from_ref(selected)))
        })
        .or_else(|| variants.first())?;
    let mut tracks = vec![("video".into(), selected_video.1.clone())];
    let selected_audio = selected_segments
        .iter()
        .find_map(|selected| {
            audio_options.iter().find(|(group, uri, _)| {
                group == &selected_video.0 && selected_hls_url(uri, std::slice::from_ref(selected))
            })
        })
        .or_else(|| {
            audio_options
                .iter()
                .find(|(group, _, is_default)| group == &selected_video.0 && *is_default)
        })
        .or_else(|| {
            audio_options
                .iter()
                .find(|(group, _, _)| group == &selected_video.0)
        })
        .or_else(|| audio_options.first());
    if let Some((_, audio, _)) = selected_audio {
        tracks.push(("audio".into(), audio.clone()));
    }
    Some(tracks)
}

fn selected_hls_url(candidate: &str, selected_segments: &[String]) -> bool {
    let Ok(candidate_url) = Url::parse(candidate) else {
        return false;
    };
    let Some(leaf) = candidate_url.path().rsplit('/').next() else {
        return false;
    };
    let stem = leaf.strip_suffix(".m3u8").unwrap_or(leaf);
    if stem.is_empty() {
        return false;
    }
    let mut identities = vec![stem];
    if let Some((_, remainder)) = stem.split_once('-').or_else(|| stem.split_once('_')) {
        if !remainder.is_empty() {
            identities.push(remainder);
        }
    }
    selected_segments.iter().any(|selected| {
        let Ok(selected_url) = Url::parse(selected) else {
            return false;
        };
        if selected_url.scheme() != candidate_url.scheme()
            || selected_url.host_str() != candidate_url.host_str()
            || selected_url.port_or_known_default() != candidate_url.port_or_known_default()
        {
            return false;
        }
        let selected_path = selected_url.path();
        identities.iter().any(|identity| {
            selected_path.match_indices(identity).any(|(index, _)| {
                let before = selected_path
                    .as_bytes()
                    .get(index.saturating_sub(1))
                    .copied();
                let after = selected_path
                    .as_bytes()
                    .get(index + identity.len())
                    .copied();
                let boundary = |byte: Option<u8>| {
                    byte.is_none_or(|value| matches!(value, b'/' | b'_' | b'-' | b'.'))
                };
                boundary(before) && boundary(after)
            })
        })
    })
}

pub fn parse_hls(source: &str, body: &str) -> Result<Vec<Segment>, String> {
    if !body
        .lines()
        .any(|line| line.trim().eq_ignore_ascii_case("#EXT-X-ENDLIST"))
    {
        return Err("Live media is not supported; a finite VOD playlist is required".into());
    }
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
            base_sequence = value
                .trim()
                .parse::<u64>()
                .map_err(|_| "Invalid HLS media sequence number".to_string())?;
            continue;
        }
        if line.starts_with("#EXT-X-KEY:") {
            pending_key = parse_hls_key(source, line)?;
            continue;
        }
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        // A fragment that cannot be addressed is a hole in the media, not a
        // line to skip: skipping it yields a file with a silent gap.
        let Some(url) = resolve(source, line) else {
            return Err("The VOD playlist lists a fragment whose address cannot be resolved".into());
        };
        let range = if let Some((length, offset)) = pending_range.take() {
            let start = match offset {
                Some(start) => start,
                None => previous_range.as_ref().filter(|(previous_url, _)| previous_url == &url).map(|(_, end)| *end).ok_or_else(|| "An HLS byte range omitted its offset without a preceding range on the same resource".to_string())?
            };
            let end = start.checked_add(length).ok_or_else(|| {
                "An HLS byte range exceeds the addressable resource size".to_string()
            })?;
            previous_range = Some((url.clone(), end));
            Some((start, length))
        } else {
            previous_range = None;
            None
        };
        let sequence = base_sequence.saturating_add(segment_index);
        segment_index = segment_index.saturating_add(1);
        let key = pending_key.clone().map(|template| HlsKey {
            uri: template.uri,
            iv: template.iv,
            sequence
        });
        segments.push(Segment { url, range, key });
    }
    if segments.is_empty() {
        return Err("The VOD playlist did not contain any media fragments".into());
    }
    // One initialization map is prepended to the whole output, so a playlist
    // that switches maps part-way would assemble later fragments under the
    // wrong header: refuse it rather than write a file that cannot play.
    let mut maps = Vec::new();
    for line in body.lines().map(str::trim).filter(|line| line.starts_with("#EXT-X-MAP")) {
        let Some(map) = hls_map_segment(line)? else {
            return Err("The HLS initialization map is missing its URI".into());
        };
        let Some(url) = resolve(source, &map.0) else {
            return Err("The HLS initialization map's address cannot be resolved".into());
        };
        if !maps.contains(&(url.clone(), map.1)) {
            maps.push((url, map.1));
        }
    }
    if maps.len() > 1 {
        return Err("The VOD playlist switches its initialization map part-way, which is not supported".into());
    }
    if let Some((url, range)) = maps.pop() {
        segments.insert(0, Segment { url, range, key: None });
    }
    Ok(segments)
}

pub fn parse_dash_tracks_for_segments(
    source: &str,
    body: &str,
    selected_segments: &[String]
) -> Result<Vec<MediaTrack>, String> {
    let mut reader = Reader::from_str(body);
    reader.config_mut().trim_text(true);
    let mut stack: Vec<Vec<u8>> = Vec::new();
    let mut global_base_urls = Vec::new();
    let mut global_base_text_depth = None;
    let mut current_track: Option<DashTrackBuilder> = None;
    let mut presentation_duration = None;
    let mut tracks = Vec::new();
    let mut periods = 0usize;
    loop {
        match reader.read_event() {
            Ok(Event::Start(element)) => {
                let name = element.name().as_ref().to_ascii_lowercase();
                dash_capability_boundary(&element, &name, &mut periods)?;
                if name.as_slice() == b"mpd" {
                    presentation_duration = attribute(&element, b"mediaPresentationDuration")
                        .and_then(|value| parse_duration(&value));
                }
                if name.as_slice() == b"adaptationset" && current_track.is_none() {
                    current_track = Some(DashTrackBuilder::new(
                        attribute(&element, b"contentType")
                            .or_else(|| attribute(&element, b"mimeType"))
                    ));
                } else if name.as_slice() == b"representation" {
                    if let Some(track) = current_track.as_mut() {
                        let representation_id = attribute(&element, b"id").unwrap_or_default();
                        let mime_type = attribute(&element, b"mimeType");
                        track
                            .segment_base_candidates
                            .push(DashSegmentBaseCandidate {
                                base_url: None,
                                initialization_range: None,
                                index_range: None,
                                container: dash_container_from(mime_type.as_deref(), "")
                            });
                        track.active_representation = Some(track.segment_base_candidates.len() - 1);
                        track.representation_ids.push(representation_id.clone());
                        if !track.selected_representation {
                            track.selected_representation = true;
                            track.representation_open = true;
                            track.representation_id = representation_id;
                            track.bandwidth = attribute(&element, b"bandwidth").unwrap_or_default();
                            if let Some(kind) = attribute(&element, b"contentType")
                                .or_else(|| attribute(&element, b"mimeType"))
                                .and_then(|value| track_kind(&value))
                            {
                                track.kind = kind;
                            }
                        }
                    }
                }
                if let Some(track) = current_track.as_mut() {
                    if name.as_slice() == b"baseurl" {
                        track.base_text_depth = Some(stack.len() + 1);
                    }
                    if name.as_slice() == b"segmentbase" {
                        track.segment_base = true;
                        if let Some(index) = track.active_representation {
                            track.active_segment_base = Some(index);
                            track.segment_base_candidates[index].index_range =
                                dash_range(&element, b"indexRange")?;
                        }
                    }
                    if name.as_slice() == b"initialization" {
                        if let Some(index) = track.active_segment_base {
                            track.segment_base_candidates[index].initialization_range =
                                dash_range(&element, b"range")?;
                        }
                    }
                    if name.as_slice() == b"segmenttemplate" && track.template.is_none() {
                        track.template = Some(DashTemplate::from_element(&element));
                    }
                    if name.as_slice() == b"s"
                        && (track.representation_open || !track.selected_representation)
                    {
                        if let Some(template) = track.template.as_mut() {
                            template.timeline.push(DashTimeline::from_element(&element));
                        }
                    }
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
                            track
                                .segment_refs
                                .push((value, dash_range(&element, range_name)?));
                        }
                    }
                } else if name.as_slice() == b"baseurl" {
                    global_base_text_depth = Some(stack.len() + 1);
                }
                stack.push(name.to_vec());
            }
            Ok(Event::Empty(element)) => {
                let name = element.name().as_ref().to_ascii_lowercase();
                dash_capability_boundary(&element, &name, &mut periods)?;
                if name.as_slice() == b"mpd" {
                    presentation_duration = attribute(&element, b"mediaPresentationDuration")
                        .and_then(|value| parse_duration(&value));
                }
                if name.as_slice() == b"adaptationset" && current_track.is_none() {
                    current_track = Some(DashTrackBuilder::new(
                        attribute(&element, b"contentType")
                            .or_else(|| attribute(&element, b"mimeType"))
                    ));
                }
                if name.as_slice() == b"representation" {
                    if let Some(track) = current_track.as_mut() {
                        let representation_id = attribute(&element, b"id").unwrap_or_default();
                        let mime_type = attribute(&element, b"mimeType");
                        track
                            .segment_base_candidates
                            .push(DashSegmentBaseCandidate {
                                base_url: None,
                                initialization_range: None,
                                index_range: None,
                                container: dash_container_from(mime_type.as_deref(), "")
                            });
                        track.active_representation = Some(track.segment_base_candidates.len() - 1);
                        track.representation_ids.push(representation_id.clone());
                        if !track.selected_representation {
                            track.selected_representation = true;
                            track.representation_id = representation_id;
                            track.bandwidth = attribute(&element, b"bandwidth").unwrap_or_default();
                            if let Some(kind) = attribute(&element, b"contentType")
                                .or_else(|| attribute(&element, b"mimeType"))
                                .and_then(|value| track_kind(&value))
                            {
                                track.kind = kind;
                            }
                        }
                    }
                }
                if let Some(track) = current_track.as_mut() {
                    if name.as_slice() == b"segmentbase" {
                        track.segment_base = true;
                        if let Some(index) = track.active_representation {
                            track.active_segment_base = Some(index);
                            track.segment_base_candidates[index].index_range =
                                dash_range(&element, b"indexRange")?;
                        }
                    }
                    if name.as_slice() == b"initialization" {
                        if let Some(index) = track.active_segment_base {
                            track.segment_base_candidates[index].initialization_range =
                                dash_range(&element, b"range")?;
                        }
                    }
                    if name.as_slice() == b"segmenttemplate" && track.template.is_none() {
                        track.template = Some(DashTemplate::from_element(&element));
                    }
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
                            track
                                .segment_refs
                                .push((value, dash_range(&element, range_name)?));
                        }
                    }
                    if name.as_slice() == b"s"
                        && (track.representation_open || !track.selected_representation)
                    {
                        if let Some(template) = track.template.as_mut() {
                            template.timeline.push(DashTimeline::from_element(&element));
                        }
                    }
                }
            }
            Ok(Event::Text(text)) => {
                if let Ok(value) = text.decode() {
                    if let Some(track) = current_track.as_mut() {
                        if track.base_text_depth == Some(stack.len()) {
                            if let Some(url) = resolve(source, value.trim()) {
                                track.base_urls.push(url.clone());
                                if let Some(index) = track.active_representation {
                                    track.segment_base_candidates[index].base_url =
                                        Some(url.clone());
                                    track.segment_base_candidates[index].container =
                                        dash_container_from(None, &url);
                                }
                            }
                        }
                    } else if global_base_text_depth == Some(stack.len()) {
                        if let Some(url) = resolve(source, value.trim()) {
                            global_base_urls.push(url);
                        }
                    }
                }
            }
            Ok(Event::End(element)) => {
                let name = element.name().as_ref().to_ascii_lowercase();
                if let Some(track) = current_track.as_mut() {
                    if name.as_slice() == b"baseurl" {
                        track.base_text_depth = None;
                    }
                    if name.as_slice() == b"representation" {
                        if track.representation_open {
                            track.representation_open = false;
                        }
                        track.active_representation = None;
                        track.active_segment_base = None;
                    }
                    if name.as_slice() == b"adaptationset" {
                        let finished = current_track.take().and_then(|track| {
                            track.finish(
                                source,
                                global_base_urls.first().map(String::as_str),
                                presentation_duration,
                                selected_segments
                            )
                        });
                        if let Some(track) = finished {
                            tracks.push(track);
                        }
                    }
                } else if name.as_slice() == b"baseurl" {
                    global_base_text_depth = None;
                }
                stack.pop();
            }
            Ok(Event::Eof) => break,
            Err(error) => return Err(format!("Could not parse MPD: {error}")),
            _ => {}
        }
    }
    if let Some(track) = current_track.take().and_then(|track| {
        track.finish(
            source,
            global_base_urls.first().map(String::as_str),
            presentation_duration,
            selected_segments
        )
    }) {
        tracks.push(track);
    }
    if tracks.is_empty() {
        return Err("The static MPD did not contain downloadable segments".into());
    }
    Ok(tracks)
}

/// Refuse what this assembler cannot turn into a faithful file, read from the
/// parsed elements rather than from raw text (F09): live presentations,
/// more than one Period (each would become its own track stretched over the
/// whole duration), and DRM-protected media, whose bytes would be unplayable.
fn dash_capability_boundary(
    element: &quick_xml::events::BytesStart<'_>,
    name: &[u8],
    periods: &mut usize
) -> Result<(), String> {
    match name {
        b"mpd" => {
            let dynamic = attribute(element, b"type")
                .is_some_and(|value| value.trim().eq_ignore_ascii_case("dynamic"));
            if dynamic
                || attribute(element, b"minimumUpdatePeriod").is_some()
                || attribute(element, b"timeShiftBufferDepth").is_some()
            {
                return Err("Live media is not supported; a static MPD is required".into());
            }
        }
        b"period" => {
            *periods += 1;
            if *periods > 1 {
                return Err("The MPD has more than one Period (for example inserted ads), which is not supported".into());
            }
        }
        b"contentprotection" => {
            return Err("The media is DRM-protected (ContentProtection) and cannot be downloaded".into());
        }
        _ => {}
    }
    Ok(())
}

struct DashSegmentBaseCandidate {
    base_url: Option<String>,
    initialization_range: Option<(u64, u64)>,
    index_range: Option<(u64, u64)>,
    container: String,
}

struct DashTrackBuilder {
    kind: String,
    unsupported_kind: bool,
    base_urls: Vec<String>,
    segment_refs: Vec<(String, Option<(u64, u64)>)>,
    template: Option<DashTemplate>,
    representation_id: String,
    bandwidth: String,
    representation_ids: Vec<String>,
    selected_representation: bool,
    representation_open: bool,
    base_text_depth: Option<usize>,
    segment_base: bool,
    segment_base_candidates: Vec<DashSegmentBaseCandidate>,
    active_representation: Option<usize>,
    active_segment_base: Option<usize>,
}

impl DashTrackBuilder {
    fn new(kind: Option<String>) -> Self {
        Self {
            kind: kind.as_deref().and_then(track_kind).unwrap_or_default(),
            unsupported_kind: kind
                .as_deref()
                .map_or(false, |value| track_kind(value).is_none()),
            base_urls: Vec::new(),
            segment_refs: Vec::new(),
            template: None,
            representation_id: String::new(),
            bandwidth: String::new(),
            representation_ids: Vec::new(),
            selected_representation: false,
            representation_open: false,
            base_text_depth: None,
            segment_base: false,
            segment_base_candidates: Vec::new(),
            active_representation: None,
            active_segment_base: None
        }
    }

    fn finish(
        self,
        source: &str,
        inherited_base: Option<&str>,
        presentation_duration: Option<u64>,
        selected_segments: &[String]
    ) -> Option<MediaTrack> {
        if self.unsupported_kind {
            return None;
        }
        if self.segment_base {
            let candidate = if selected_segments.is_empty() {
                self.segment_base_candidates.first()?
            } else {
                selected_segments.iter().find_map(|selected| {
                    self.segment_base_candidates.iter().find(|candidate| {
                        candidate
                            .base_url
                            .as_ref()
                            .is_some_and(|base| base == selected)
                    })
                })?
            };
            let base = candidate
                .base_url
                .clone()
                .or_else(|| inherited_base.map(str::to_owned))
                .or_else(|| self.base_urls.first().cloned())?;
            let index_range = candidate.index_range?;
            if self.kind.is_empty() {
                return None;
            }
            return Some(MediaTrack {
                kind: self.kind,
                segments: Vec::new(),
                segment_base: Some(DashSegmentBase {
                    url: base.clone(),
                    initialization_range: candidate.initialization_range,
                    index_range,
                    container: if candidate.container.is_empty() {
                        dash_container_from(None, &base)
                    } else {
                        candidate.container.clone()
                    }
                })
            });
        }
        let base = self
            .base_urls
            .first()
            .map(String::as_str)
            .or(inherited_base)
            .unwrap_or(source);
        let representation_ids = if self.representation_ids.is_empty() {
            vec![self.representation_id.clone()]
        } else {
            self.representation_ids.clone()
        };
        let mut segments = self
            .segment_refs
            .iter()
            .filter_map(|(value, range)| {
                resolve(base, value).map(|url| Segment {
                    url,
                    range: *range,
                    key: None
                })
            })
            .collect::<Vec<_>>();
        if segments.is_empty() {
            let Some(template) = self.template.as_ref() else {
                return None;
            };
            let mut chosen_id = self.representation_id.as_str();
            for representation_id in &representation_ids {
                if let Ok(candidate) = expand_dash_template(
                    template,
                    base,
                    representation_id,
                    &self.bandwidth,
                    presentation_duration
                ) {
                    if selected_segments
                        .iter()
                        .any(|selected| candidate.iter().any(|segment| &segment.url == selected))
                    {
                        chosen_id = representation_id;
                        break;
                    }
                }
            }
            segments = expand_dash_template(
                template,
                base,
                chosen_id,
                &self.bandwidth,
                presentation_duration
            )
            .ok()?;
        }
        if segments.is_empty() {
            return None;
        }
        Some(MediaTrack {
            kind: self.kind,
            segments,
            segment_base: None
        })
    }
}

fn dash_container_from(mime: Option<&str>, url: &str) -> String {
    let mime = mime.unwrap_or_default().to_ascii_lowercase();
    let url = url.to_ascii_lowercase();
    if mime.contains("webm") || url.contains(".webm") { "webm".into() } else { "mp4".into() }
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
            timescale: attribute(element, b"timescale")
                .and_then(|value| value.parse().ok())
                .unwrap_or(1),
            duration: attribute(element, b"duration").and_then(|value| value.parse().ok()),
            start_number: attribute(element, b"startNumber")
                .and_then(|value| value.parse().ok())
                .unwrap_or(1),
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
            duration: attribute(element, b"d")
                .and_then(|value| value.parse().ok())
                .unwrap_or(0),
            repeat: attribute(element, b"r")
                .and_then(|value| value.parse().ok())
                .unwrap_or(0)
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
    // Malformed timing is a property of the manifest, not a reason to panic:
    // every divisor below is proven non-zero here.
    if template.timescale == 0
        || template.duration == Some(0)
        || template.timeline.iter().any(|item| item.duration == 0)
    {
        return Err("The MPD declares a zero segment duration or timescale".into());
    }
    let mut number = template.start_number;
    let mut current_time = 0u64;
    if !template.timeline.is_empty() {
        for (index, item) in template.timeline.iter().enumerate() {
            let start = item.time.unwrap_or(current_time);
            let next_time = template.timeline.get(index + 1).and_then(|next| next.time);
            let repeat = if item.repeat >= 0 { (item.repeat as u64).saturating_add(1) } else if let Some(next) = next_time { next.saturating_sub(start).div_ceil(item.duration).max(1) } else if let Some(duration) = presentation_duration { duration.saturating_mul(template.timescale).saturating_sub(start).div_ceil(item.duration).max(1) } else { 1 };
            for offset in 0..repeat.min(100_000) {
                let time = start.saturating_add(offset.saturating_mul(item.duration));
                let value = expand_template(media, number, time, representation_id, bandwidth);
                if let Some(url) = resolve(base, &value) { segments.push(Segment { url, range: None, key: None }); }
                number = number.saturating_add(1);
            }
            current_time = start.saturating_add(repeat.saturating_mul(item.duration));
        }
    } else if let (Some(duration), Some(segment_duration)) = (presentation_duration, template.duration) {
        let count = duration.saturating_mul(template.timescale).div_ceil(segment_duration).min(100_000);
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
        // Real templates pad to a handful of digits; a manifest must not be
        // able to ask for a multi-gigabyte URL.
        let width = value[start + 8..width_end].parse::<usize>().unwrap_or(0).min(32);
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
    if !line.starts_with("#EXT-X-MAP") {
        return Ok(None);
    }
    let Some(uri) = hls_attribute(line, "URI") else {
        return Ok(None);
    };
    let range = hls_attribute(line, "BYTERANGE")
        .map(|value| {
            parse_hls_byterange(&value).map(|(length, offset)| (offset.unwrap_or(0), length))
        })
        .transpose()?;
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
    let (length, offset) = value
        .split_once('@')
        .map_or((value, None), |(length, offset)| (length, Some(offset)));
    let length = length
        .parse::<u64>()
        .map_err(|_| "Invalid HLS byte-range length".to_string())?;
    if length == 0 {
        return Err("An HLS byte range must have a positive length".into());
    }
    let offset = offset
        .map(|value| {
            value
                .parse::<u64>()
                .map_err(|_| "Invalid HLS byte-range offset".to_string())
        })
        .transpose()?;
    Ok((length, offset))
}

fn dash_range(
    element: &quick_xml::events::BytesStart<'_>,
    key: &[u8],
) -> Result<Option<(u64, u64)>, String> {
    let Some(value) = attribute(element, key) else {
        return Ok(None);
    };
    let Some((start, end)) = value.split_once('-') else {
        return Err(format!("Invalid DASH byte range: {value}"));
    };
    let start = start
        .parse::<u64>()
        .map_err(|_| format!("Invalid DASH byte range start: {value}"))?;
    let end = end
        .parse::<u64>()
        .map_err(|_| format!("Invalid DASH byte range end: {value}"))?;
    let length = end
        .checked_sub(start)
        .and_then(|length| length.checked_add(1))
        .ok_or_else(|| format!("Invalid DASH byte range: {value}"))?;
    Ok(Some((start, length)))
}

pub fn expand_dash_segment_base(
    base: &DashSegmentBase,
    index_data: &[u8],
    total_length: u64,
    initialization_data: &[u8],
) -> Result<Vec<Segment>, String> {
    let mut segments = Vec::new();
    if let Some(range) = base.initialization_range {
        segments.push(Segment {
            url: base.url.clone(),
            range: Some(range),
            key: None,
        });
    }
    let index_range = base.index_range;
    segments.push(Segment {
        url: base.url.clone(),
        range: Some(index_range),
        key: None,
    });
    if base.container == "webm" {
        let segment_offset = ebml_segment_data_offset(initialization_data)?;
        let cue_offsets = ebml_cue_cluster_offsets(index_data)?;
        for (index, relative) in cue_offsets.iter().enumerate() {
            let start = segment_offset
                .checked_add(*relative)
                .ok_or_else(|| "The WebM Cue offset overflowed".to_string())?;
            let end = cue_offsets
                .get(index + 1)
                .and_then(|next| segment_offset.checked_add(*next))
                .unwrap_or(total_length);
            if start >= end || end > total_length {
                return Err("The WebM Cue range exceeds the representation".into());
            }
            segments.push(Segment {
                url: base.url.clone(),
                range: Some((start, end - start)),
                key: None,
            });
        }
    } else {
        segments.extend(parse_mp4_sidx(base, index_data, total_length)?);
    }
    if segments.len() < 2 {
        return Err("The SegmentBase index did not contain media segments".into());
    }
    Ok(segments)
}

fn parse_mp4_sidx(
    base: &DashSegmentBase,
    index_data: &[u8],
    total_length: u64,
) -> Result<Vec<Segment>, String> {
    if index_data.len() < 32 || &index_data[4..8] != b"sidx" {
        return Err("The MP4 SegmentBase index is not an sidx box".into());
    }
    let size = u32::from_be_bytes(index_data[0..4].try_into().unwrap()) as usize;
    if size < 32 || size > index_data.len() {
        return Err("The MP4 sidx box is truncated".into());
    }
    let version = index_data[8];
    let (first_offset, count_offset, mut cursor) = if version == 0 {
        (
            u32::from_be_bytes(index_data[24..28].try_into().unwrap()) as u64,
            30usize,
            32usize,
        )
    } else if version == 1 {
        if index_data.len() < 40 {
            return Err("The MP4 sidx version-1 box is truncated".into());
        }
        (
            u64::from_be_bytes(index_data[28..36].try_into().unwrap()),
            38usize,
            40usize,
        )
    } else {
        return Err("The MP4 sidx version is unsupported".into());
    };
    let count = u16::from_be_bytes(
        index_data[count_offset..count_offset + 2]
            .try_into()
            .unwrap(),
    ) as usize;
    let mut segments = Vec::with_capacity(count);
    let mut start = base
        .index_range
        .0
        .checked_add(size as u64)
        .and_then(|value| value.checked_add(first_offset))
        .ok_or_else(|| "The MP4 sidx offset overflowed".to_string())?;
    for _ in 0..count {
        if cursor + 12 > size {
            return Err("The MP4 sidx references are truncated".into());
        }
        let reference = u32::from_be_bytes(index_data[cursor..cursor + 4].try_into().unwrap());
        cursor += 4;
        if reference & 0x8000_0000 != 0 {
            return Err("Hierarchical MP4 SegmentBase indexes are unsupported".into());
        }
        let length = u64::from(reference & 0x7fff_ffff);
        cursor += 8;
        if length == 0
            || start
                .checked_add(length)
                .is_none_or(|end| end > total_length)
        {
            return Err("The MP4 sidx reference exceeds the representation".into());
        }
        segments.push(Segment {
            url: base.url.clone(),
            range: Some((start, length)),
            key: None,
        });
        start += length;
    }
    Ok(segments)
}

fn ebml_segment_data_offset(initialization_data: &[u8]) -> Result<u64, String> {
    let mut cursor = 0usize;
    while cursor < initialization_data.len() {
        let (id, id_width) = ebml_id(initialization_data, cursor)?;
        let (size, size_width) = ebml_vint(initialization_data, cursor + id_width)?;
        let data_start = cursor + id_width + size_width;
        if id == 0x1853_8067 {
            return Ok(data_start as u64);
        }
        let Some(size) = size else {
            break;
        };
        let Some(next) = data_start.checked_add(size as usize) else {
            break;
        };
        if next > initialization_data.len() {
            break;
        }
        cursor = next;
    }
    Err("The WebM initialization range does not contain a Segment element".into())
}

fn ebml_cue_cluster_offsets(index_data: &[u8]) -> Result<Vec<u64>, String> {
    let mut offsets = Vec::new();
    for (id, data_start, data_end) in ebml_children(index_data, 0, index_data.len())? {
        if id != 0x1c53_bb6b {
            continue;
        }
        for (cue_id, cue_start, cue_end) in ebml_children(index_data, data_start, data_end)? {
            if cue_id != 0xbb {
                continue;
            }
            for (positions_id, positions_start, positions_end) in
                ebml_children(index_data, cue_start, cue_end)?
            {
                if positions_id != 0xb7 {
                    continue;
                }
                for (offset_id, offset_start, offset_end) in
                    ebml_children(index_data, positions_start, positions_end)?
                {
                    if offset_id == 0xf1 {
                        offsets.push(ebml_uint(index_data, offset_start, offset_end)?);
                    }
                }
            }
        }
    }
    offsets.sort_unstable();
    offsets.dedup();
    if offsets.is_empty() {
        return Err("The WebM SegmentBase index did not contain Cue cluster positions".into());
    }
    Ok(offsets)
}

fn ebml_children(
    data: &[u8],
    mut cursor: usize,
    end: usize,
) -> Result<Vec<(u64, usize, usize)>, String> {
    let mut elements = Vec::new();
    while cursor < end {
        let (id, id_width) = ebml_id(data, cursor)?;
        cursor += id_width;
        let (size, size_width) = ebml_vint(data, cursor)?;
        cursor += size_width;
        let data_start = cursor;
        let data_end = match size {
            Some(size) => data_start
                .checked_add(size as usize)
                .ok_or_else(|| "The EBML element size overflowed".to_string())?,
            None => end,
        };
        if data_end > end {
            return Err("The EBML element is truncated".into());
        }
        elements.push((id, data_start, data_end));
        cursor = data_end;
    }
    Ok(elements)
}

fn ebml_id(data: &[u8], cursor: usize) -> Result<(u64, usize), String> {
    let first = *data
        .get(cursor)
        .ok_or_else(|| "The EBML element ID is truncated".to_string())?;
    let width = if first & 0x80 != 0 {
        1
    } else if first & 0x40 != 0 {
        2
    } else if first & 0x20 != 0 {
        3
    } else if first & 0x10 != 0 {
        4
    } else {
        return Err("The EBML element ID is invalid".into());
    };
    if cursor + width > data.len() {
        return Err("The EBML element ID is truncated".into());
    }
    let mut value = 0u64;
    for byte in &data[cursor..cursor + width] {
        value = (value << 8) | u64::from(*byte);
    }
    Ok((value, width))
}

fn ebml_vint(data: &[u8], cursor: usize) -> Result<(Option<u64>, usize), String> {
    let first = *data
        .get(cursor)
        .ok_or_else(|| "The EBML variable integer is truncated".to_string())?;
    let width = if first & 0x80 != 0 {
        1
    } else if first & 0x40 != 0 {
        2
    } else if first & 0x20 != 0 {
        3
    } else if first & 0x10 != 0 {
        4
    } else if first & 0x08 != 0 {
        5
    } else if first & 0x04 != 0 {
        6
    } else if first & 0x02 != 0 {
        7
    } else if first & 0x01 != 0 {
        8
    } else {
        return Err("The EBML variable integer is invalid".into());
    };
    if cursor + width > data.len() {
        return Err("The EBML variable integer is truncated".into());
    }
    let marker = 1u64 << (8 - width);
    let mut value = u64::from(first & (marker as u8 - 1));
    for byte in &data[cursor + 1..cursor + width] {
        value = (value << 8) | u64::from(*byte);
    }
    // All value bits set means "unknown size" at every width, not only one
    // byte: recorders and chunked DASH write the Segment's unknown size as
    // 01 FF FF FF FF FF FF FF.
    Ok((
        if value == (1u64 << (7 * width)) - 1 {
            None
        } else {
            Some(value)
        },
        width,
    ))
}

fn ebml_uint(data: &[u8], start: usize, end: usize) -> Result<u64, String> {
    if start >= end || end - start > 8 {
        return Err("The EBML unsigned integer is invalid".into());
    }
    let mut value = 0u64;
    for byte in &data[start..end] {
        value = (value << 8) | u64::from(*byte);
    }
    Ok(value)
}
fn attribute(element: &quick_xml::events::BytesStart<'_>, key: &[u8]) -> Option<String> {
    element
        .attributes()
        .flatten()
        .find(|attribute| attribute.key.as_ref().eq_ignore_ascii_case(key))
        .and_then(|attribute| {
            attribute
                .normalized_value(quick_xml::XmlVersion::Implicit1_0)
                .ok()
                .map(|value| value.into_owned())
        })
}

fn resolve(source: &str, value: &str) -> Option<String> {
    Url::parse(value).ok().or_else(|| Url::parse(source).ok()?.join(value).ok()).map(|url| url.to_string())
}

#[derive(Clone, Copy)]
struct Mp4Box {
    kind: [u8; 4],
    start: usize,
    end: usize,
    header_size: usize
}

impl Mp4Box {
    fn payload_start(self) -> usize {
        self.start + self.header_size
    }
}

/// One moof/mdat pair of a track file, remembered by position. The fragment
/// header is re-read when it is written, so a track's fragments never sit in
/// memory together (F13).
struct ParsedFragment {
    moof_offset: u64,
    moof_size: u64,
    mdat_offset: u64,
    mdat_size: u64,
    mdat_header: u64
}

/// One fragmented-MP4 track file, indexed where it lies: the file type and
/// movie headers are held, everything after them stays on disk.
struct ParsedTrack {
    file: std::fs::File,
    /// The moov box alone.
    moov: Vec<u8>,
    track_id: u32,
    fragments: Vec<ParsedFragment>,
    /// First emsg/prft box between fragments (the Matroska audio path refuses these).
    auxiliary_box: Option<[u8; 4]>
}

fn parse_mp4_boxes(
    data: &[u8],
    start: usize,
    end: usize,
    context: &str
) -> Result<Vec<Mp4Box>, String> {
    if start > end || end > data.len() {
        return Err(format!("{context} has an invalid box range"));
    }
    let mut boxes = Vec::new();
    let mut cursor = start;
    while cursor < end {
        if end - cursor < 8 {
            return Err(format!("{context} has a truncated MP4 box header"));
        }
        let size32 = u32::from_be_bytes(data[cursor..cursor + 4].try_into().unwrap());
        let mut header_size = 8usize;
        let size = if size32 == 1 {
            if end - cursor < 16 {
                return Err(format!("{context} has a truncated large MP4 box size"));
            }
            header_size = 16;
            usize::try_from(u64::from_be_bytes(
                data[cursor + 8..cursor + 16].try_into().unwrap()
            ))
            .map_err(|_| format!("{context} contains an MP4 box that is too large"))?
        } else if size32 == 0 {
            end - cursor
        } else {
            usize::try_from(size32).unwrap()
        };
        if size < header_size {
            return Err(format!("{context} contains an invalid MP4 box size"));
        }
        let box_end = cursor
            .checked_add(size)
            .ok_or_else(|| format!("{context} contains an overflowing MP4 box size"))?;
        if box_end > end {
            return Err(format!("{context} contains a truncated MP4 box"));
        }
        boxes.push(Mp4Box {
            kind: data[cursor + 4..cursor + 8].try_into().unwrap(),
            start: cursor,
            end: box_end,
            header_size
        });
        cursor = box_end;
    }
    Ok(boxes)
}

fn child_boxes(data: &[u8], parent: Mp4Box, context: &str) -> Result<Vec<Mp4Box>, String> {
    let mut start = parent.payload_start();
    if parent.kind == *b"meta" {
        start = start
            .checked_add(4)
            .ok_or_else(|| format!("{context} has an invalid meta box"))?;
    }
    if start > parent.end {
        return Err(format!("{context} has a truncated container header"));
    }
    parse_mp4_boxes(data, start, parent.end, context)
}

fn box_bytes<'a>(data: &'a [u8], item: Mp4Box) -> &'a [u8] {
    &data[item.start..item.end]
}

fn matching_boxes(boxes: &[Mp4Box], kind: [u8; 4]) -> Vec<Mp4Box> {
    boxes
        .iter()
        .copied()
        .filter(|item| item.kind == kind)
        .collect()
}

fn exactly_one_box(boxes: &[Mp4Box], kind: [u8; 4], context: &str) -> Result<Mp4Box, String> {
    let matches = matching_boxes(boxes, kind);
    if matches.len() != 1 {
        return Err(format!(
            "{context} must contain exactly one {} box",
            String::from_utf8_lossy(&kind)
        ));
    }
    Ok(matches[0])
}

fn full_box_header(data: &[u8], item: Mp4Box, context: &str) -> Result<(u8, u32, usize), String> {
    let payload = item.payload_start();
    if payload.checked_add(4).is_none_or(|end| end > item.end) {
        return Err(format!("{context} has a truncated full-box header"));
    }
    let flags = u32::from_be_bytes([0, data[payload + 1], data[payload + 2], data[payload + 3]]);
    Ok((data[payload], flags, payload))
}

fn read_u32_at(data: &[u8], offset: usize, context: &str) -> Result<u32, String> {
    let end = offset
        .checked_add(4)
        .ok_or_else(|| format!("{context} has an overflowing field"))?;
    if end > data.len() {
        return Err(format!("{context} has a truncated field"));
    }
    Ok(u32::from_be_bytes(data[offset..end].try_into().unwrap()))
}

fn read_u64_at(data: &[u8], offset: usize, context: &str) -> Result<u64, String> {
    let end = offset
        .checked_add(8)
        .ok_or_else(|| format!("{context} has an overflowing field"))?;
    if end > data.len() {
        return Err(format!("{context} has a truncated field"));
    }
    Ok(u64::from_be_bytes(data[offset..end].try_into().unwrap()))
}

fn patch_u32(data: &mut [u8], offset: usize, value: u32, context: &str) -> Result<(), String> {
    let end = offset
        .checked_add(4)
        .ok_or_else(|| format!("{context} has an overflowing field"))?;
    if end > data.len() {
        return Err(format!("{context} has a truncated field"));
    }
    data[offset..end].copy_from_slice(&value.to_be_bytes());
    Ok(())
}

fn patch_u64(data: &mut [u8], offset: usize, value: u64, context: &str) -> Result<(), String> {
    let end = offset
        .checked_add(8)
        .ok_or_else(|| format!("{context} has an overflowing field"))?;
    if end > data.len() {
        return Err(format!("{context} has a truncated field"));
    }
    data[offset..end].copy_from_slice(&value.to_be_bytes());
    Ok(())
}

fn track_id_from_tkhd(data: &[u8], item: Mp4Box, context: &str) -> Result<u32, String> {
    let (version, _, payload) = full_box_header(data, item, context)?;
    let offset = match version {
        0 => payload + 12,
        1 => payload + 20,
        _ => return Err(format!("{context} uses an unsupported tkhd version")),
    };
    read_u32_at(data, offset, context)
}

fn mdhd_timescale(data: &[u8], item: Mp4Box, context: &str) -> Result<u32, String> {
    let (version, _, payload) = full_box_header(data, item, context)?;
    let offset = match version {
        0 => payload + 12,
        1 => payload + 20,
        _ => return Err(format!("{context} uses an unsupported mdhd version")),
    };
    let timescale = read_u32_at(data, offset, context)?;
    if timescale == 0 {
        return Err(format!("{context} has a zero media timescale"));
    }
    Ok(timescale)
}

fn tfhd_fields(data: &[u8], item: Mp4Box, context: &str) -> Result<(u32, Option<usize>), String> {
    let (_, flags, payload) = full_box_header(data, item, context)?;
    let supported_flags = 0x0003_003b;
    if flags & !supported_flags != 0 {
        return Err(format!(
            "{context} uses unsupported tfhd flags 0x{flags:06x}"
        ));
    }
    let track_id_offset = payload
        .checked_add(4)
        .ok_or_else(|| format!("{context} has an overflowing track ID field"))?;
    let track_id = read_u32_at(data, track_id_offset, context)?;
    let mut cursor = track_id_offset + 4;
    let base_data_offset = if flags & 0x1 != 0 {
        if cursor.checked_add(8).is_none_or(|end| end > item.end) {
            return Err(format!("{context} has a truncated base-data-offset field"));
        }
        let offset = cursor;
        cursor += 8;
        Some(offset)
    } else {
        None
    };
    for (flag, width) in [(0x2, 4usize), (0x8, 4), (0x10, 4), (0x20, 4)] {
        if flags & flag != 0 {
            cursor = cursor
                .checked_add(width)
                .ok_or_else(|| format!("{context} has an overflowing optional field"))?;
            if cursor > item.end {
                return Err(format!("{context} has a truncated optional field"));
            }
        }
    }
    Ok((track_id, base_data_offset))
}

fn tfdt_decode_time(data: &[u8], item: Mp4Box, context: &str) -> Result<u64, String> {
    let (version, _, payload) = full_box_header(data, item, context)?;
    match version {
        0 => Ok(u64::from(read_u32_at(data, payload + 4, context)?)),
        1 => read_u64_at(data, payload + 4, context),
        _ => Err(format!("{context} uses an unsupported tfdt version")),
    }
}

fn validate_trun(data: &[u8], item: Mp4Box, context: &str) -> Result<(), String> {
    let (_, flags, payload) = full_box_header(data, item, context)?;
    let supported_flags = 0x00000f05;
    if flags & !supported_flags != 0 {
        return Err(format!(
            "{context} uses unsupported trun flags 0x{flags:06x}"
        ));
    }
    let sample_count = usize::try_from(read_u32_at(data, payload + 4, context)?)
        .map_err(|_| format!("{context} has too many samples"))?;
    if sample_count == 0 {
        return Err(format!("{context} has no samples"));
    }
    let mut per_sample = 0usize;
    for (flag, width) in [(0x100, 4usize), (0x200, 4), (0x400, 4), (0x800, 4)] {
        if flags & flag != 0 {
            per_sample = per_sample
                .checked_add(width)
                .ok_or_else(|| format!("{context} has overflowing sample fields"))?;
        }
    }
    let mut cursor = payload
        .checked_add(8)
        .ok_or_else(|| format!("{context} has an overflowing sample count field"))?;
    if flags & 0x1 != 0 {
        cursor = cursor
            .checked_add(4)
            .ok_or_else(|| format!("{context} has an overflowing data-offset field"))?;
    }
    if flags & 0x4 != 0 {
        cursor = cursor
            .checked_add(4)
            .ok_or_else(|| format!("{context} has an overflowing first-sample-flags field"))?;
    }
    let sample_bytes = per_sample
        .checked_mul(sample_count)
        .ok_or_else(|| format!("{context} has too many sample fields"))?;
    cursor = cursor
        .checked_add(sample_bytes)
        .ok_or_else(|| format!("{context} has overflowing sample fields"))?;
    if cursor > item.end {
        return Err(format!("{context} has truncated sample fields"));
    }
    Ok(())
}

/// The single box a buffer read from a file holds.
fn only_box(data: &[u8], context: &str) -> Result<Mp4Box, String> {
    parse_mp4_boxes(data, 0, data.len(), context)?
        .first()
        .copied()
        .ok_or_else(|| format!("{context} has an invalid box"))
}

/// Parse one fragment header. `data` is the moof box alone, read from
/// `moof_offset`; `mdat` is the following box as (offset, size, header size).
fn parse_fragment(
    data: &[u8],
    moof_offset: u64,
    mdat: (u64, u64, u64),
    track_id: u32,
    context: &str
) -> Result<ParsedFragment, String> {
    let moof = only_box(data, context)?;
    let children = child_boxes(data, moof, context)?;
    let mfhd = exactly_one_box(&children, *b"mfhd", context)?;
    let (_, _, mfhd_payload) = full_box_header(data, mfhd, context)?;
    read_u32_at(data, mfhd_payload + 4, context)?;
    let trafs = matching_boxes(&children, *b"traf");
    if trafs.is_empty() {
        return Err(format!("{context} has no track fragment"));
    }
    for (traf_index, traf) in trafs.iter().enumerate() {
        let traf_context = format!("{context} traf {traf_index}");
        let traf_children = child_boxes(data, *traf, &traf_context)?;
        let tfhd = exactly_one_box(&traf_children, *b"tfhd", &traf_context)?;
        let (fragment_track_id, _) = tfhd_fields(data, tfhd, &traf_context)?;
        if fragment_track_id != track_id {
            return Err(format!(
                "{traf_context} references track ID {fragment_track_id}, expected {track_id}"
            ));
        }
        let truns = matching_boxes(&traf_children, *b"trun");
        if truns.is_empty() {
            return Err(format!("{traf_context} has no sample run"));
        }
        for (trun_index, trun) in truns.iter().enumerate() {
            validate_trun(data, *trun, &format!("{traf_context} trun {trun_index}"))?;
        }
        let tfdt_boxes = matching_boxes(&traf_children, *b"tfdt");
        if tfdt_boxes.len() > 1 {
            return Err(format!("{traf_context} contains multiple tfdt boxes"));
        }
        if let Some(tfdt) = tfdt_boxes.first() {
            tfdt_decode_time(data, *tfdt, &traf_context)?;
        }
    }
    Ok(ParsedFragment {
        moof_offset,
        moof_size: data.len() as u64,
        mdat_offset: mdat.0,
        mdat_size: mdat.1,
        mdat_header: mdat.2,
    })
}

/// Top-level boxes of a file as (kind, offset, size, header size), read one
/// header at a time.
fn file_top_boxes(
    file: &mut std::fs::File,
    total: u64,
    context: &str
) -> Result<Vec<([u8; 4], u64, u64, u64)>, String> {
    let mut boxes = Vec::new();
    let mut cursor = 0u64;
    while cursor < total {
        let (kind, size, header) = file_box_header(file, cursor, total, context)?;
        boxes.push((kind, cursor, size, header));
        cursor += size;
    }
    Ok(boxes)
}

fn open_media_file(path: &Path, context: &str) -> Result<(std::fs::File, u64), String> {
    let file = std::fs::File::open(path)
        .map_err(|error| format!("{context} could not be opened: {error}"))?;
    let total = file
        .metadata()
        .map_err(|error| format!("{context} could not be read: {error}"))?
        .len();
    Ok((file, total))
}

/// Index one single-track fragmented-MP4 file. The ftyp and moov boxes are
/// read whole (they are small); fragments are walked header by header, so
/// the media data itself is never loaded (F13).
fn parse_fragmented_track(path: &Path, track_index: usize) -> Result<ParsedTrack, String> {
    let context = format!("Media track {track_index}");
    let (mut file, total) = open_media_file(path, &context)?;
    if total == 0 {
        return Err(format!("{context} is empty"));
    }
    let top = file_top_boxes(&mut file, total, &context)?;
    let ftyp_index = top
        .iter()
        .position(|item| item.0 == *b"ftyp")
        .ok_or_else(|| format!("{context} is missing an ftyp box"))?;
    let moov_index = top
        .iter()
        .position(|item| item.0 == *b"moov")
        .ok_or_else(|| format!("{context} is missing a moov box"))?;
    if ftyp_index > moov_index {
        return Err(format!("{context} has ftyp after moov"));
    }
    let moov = file_bytes(&mut file, top[moov_index].1, top[moov_index].2, &context)?;
    let data = moov.as_slice();
    let moov_children = child_boxes(data, only_box(data, &context)?, &format!("{context} moov"))?;
    let trak_boxes = matching_boxes(&moov_children, *b"trak");
    if trak_boxes.is_empty() {
        return Err(format!("{context} has no media tracks"));
    }
    if trak_boxes.len() != 1 {
        return Err(format!("{context} must contain exactly one track"));
    }
    let trak = trak_boxes[0];
    let trak_children = child_boxes(data, trak, &format!("{context} trak"))?;
    let tkhd = exactly_one_box(&trak_children, *b"tkhd", &format!("{context} trak"))?;
    let track_id = track_id_from_tkhd(data, tkhd, &format!("{context} tkhd"))?;
    if track_id == 0 {
        return Err(format!("{context} has an invalid zero track ID"));
    }
    let mdia = exactly_one_box(&trak_children, *b"mdia", &format!("{context} trak"))?;
    let mdia_children = child_boxes(data, mdia, &format!("{context} mdia"))?;
    let mdhd = exactly_one_box(&mdia_children, *b"mdhd", &format!("{context} mdia"))?;
    mdhd_timescale(data, mdhd, &format!("{context} mdhd"))?;
    let mvex = matching_boxes(&moov_children, *b"mvex");
    if mvex.len() != 1 {
        return Err(format!("{context} is not a fragmented MP4 initialization"));
    }
    let mvex_children = child_boxes(data, mvex[0], &format!("{context} mvex"))?;
    let trex_boxes = matching_boxes(&mvex_children, *b"trex");
    if trex_boxes.len() != 1 {
        return Err(format!("{context} must contain exactly one trex box"));
    }
    let trex = trex_boxes[0];
    let (_, _, trex_payload) = full_box_header(data, trex, &format!("{context} trex"))?;
    let trex_track_id = read_u32_at(data, trex_payload + 4, &format!("{context} trex"))?;
    if trex_track_id != track_id {
        return Err(format!(
            "{context} trex references track ID {trex_track_id}, expected {track_id}"
        ));
    }
    let mut fragments = Vec::new();
    let mut pending_moof: Option<(u64, u64)> = None;
    let mut saw_fragment_area = false;
    let mut auxiliary_box = None;
    for &(kind, offset, size, header) in top.iter().skip(moov_index + 1) {
        match &kind {
            b"moof" => {
                if pending_moof.is_some() {
                    return Err(format!("{context} has consecutive moof boxes without mdat"));
                }
                pending_moof = Some((offset, size));
                saw_fragment_area = true;
            }
            b"mdat" => {
                let (moof_offset, moof_size) = pending_moof
                    .take()
                    .ok_or_else(|| format!("{context} has mdat without a preceding moof"))?;
                let fragment_context = format!("{context} fragment {}", fragments.len());
                let moof = file_bytes(&mut file, moof_offset, moof_size, &fragment_context)?;
                let fragment = parse_fragment(
                    &moof,
                    moof_offset,
                    (offset, size, header),
                    track_id,
                    &fragment_context
                )?;
                fragments.push(fragment);
            }
            b"mfra" => {
                if pending_moof.is_some() {
                    return Err(format!(
                        "{context} has a fragment without its mdat before mfra"
                    ));
                }
            }
            b"styp" | b"sidx" | b"emsg" | b"prft" | b"free" | b"skip" | b"wide" => {
                if pending_moof.is_some() {
                    return Err(format!("{context} has data between moof and mdat"));
                }
                if matches!(&kind, b"emsg" | b"prft") && auxiliary_box.is_none() {
                    auxiliary_box = Some(kind);
                }
            }
            b"ftyp" | b"moov" => {
                return Err(format!(
                    "{context} has a duplicate initialization box at top level"
                ))
            }
            _ => {
                return Err(format!(
                    "{context} contains unsupported top-level box {}",
                    String::from_utf8_lossy(&kind)
                ))
            }
        }
    }
    if pending_moof.is_some() {
        return Err(format!("{context} has a moof without mdat"));
    }
    if !saw_fragment_area || fragments.is_empty() {
        return Err(format!("{context} is not a fragmented MP4 media stream"));
    }
    Ok(ParsedTrack {
        file,
        moov,
        track_id,
        fragments,
        auxiliary_box
    })
}

/// Largest box the validator and the muxers hold in memory. Only fragment and
/// movie headers (and Matroska headers) are read whole; they are small beside
/// the media, and the bound keeps a hostile size from becoming an allocation.
const FMP4_VALIDATION_BOX_LIMIT: u64 = 64 * 1024 * 1024;

fn file_bytes(file: &mut std::fs::File, offset: u64, length: u64, context: &str) -> Result<Vec<u8>, String> {
    let length = usize::try_from(length)
        .ok()
        .filter(|length| *length <= FMP4_VALIDATION_BOX_LIMIT as usize)
        .ok_or_else(|| format!("{context} contains a header too large to read"))?;
    let mut buffer = vec![0u8; length];
    file.seek(SeekFrom::Start(offset))
        .and_then(|_| file.read_exact(&mut buffer))
        .map_err(|error| format!("{context} could not be read: {error}"))?;
    Ok(buffer)
}

/// One top-level box header: (kind, total size, header size).
fn file_box_header(file: &mut std::fs::File, offset: u64, total: u64, context: &str) -> Result<([u8; 4], u64, u64), String> {
    if total - offset < 8 {
        return Err(format!("{context} has a truncated MP4 box header"));
    }
    let available = (total - offset).min(16) as usize;
    let head = file_bytes(file, offset, available as u64, context)?;
    let size32 = u32::from_be_bytes(head[0..4].try_into().unwrap());
    let kind: [u8; 4] = head[4..8].try_into().unwrap();
    let (size, header) = if size32 == 1 {
        if head.len() < 16 {
            return Err(format!("{context} has a truncated large MP4 box size"));
        }
        (u64::from_be_bytes(head[8..16].try_into().unwrap()), 16)
    } else if size32 == 0 {
        (total - offset, 8)
    } else {
        (u64::from(size32), 8)
    };
    if size < header {
        return Err(format!("{context} contains an invalid MP4 box size"));
    }
    if offset.checked_add(size).is_none_or(|end| end > total) {
        return Err(format!("{context} contains a truncated MP4 box"));
    }
    Ok((kind, size, header))
}

const MUX_COPY_CHUNK: usize = 1024 * 1024;

/// The muxed file being written. Output goes to disk through a buffer and
/// media payloads are copied from the track files in bounded chunks, so the
/// muxer's memory does not grow with the size of the media (F13).
struct MuxOutput {
    writer: std::io::BufWriter<std::fs::File>,
    position: u64,
    chunk: Vec<u8>
}

impl MuxOutput {
    fn create(path: &Path) -> Result<Self, String> {
        let file = std::fs::File::create(path)
            .map_err(|error| format!("the output could not be created: {error}"))?;
        Ok(Self {
            writer: std::io::BufWriter::with_capacity(MUX_COPY_CHUNK, file),
            position: 0,
            chunk: vec![0; MUX_COPY_CHUNK]
        })
    }

    fn write(&mut self, bytes: &[u8]) -> Result<(), String> {
        self.writer
            .write_all(bytes)
            .map_err(|error| format!("the output could not be written: {error}"))?;
        self.position += bytes.len() as u64;
        Ok(())
    }

    /// Append `length` bytes of `file` starting at `offset`.
    fn copy_from(&mut self, file: &mut std::fs::File, offset: u64, length: u64) -> Result<(), String> {
        file.seek(SeekFrom::Start(offset))
            .map_err(|error| format!("a media track could not be read: {error}"))?;
        let mut remaining = length;
        while remaining > 0 {
            let count = remaining.min(MUX_COPY_CHUNK as u64) as usize;
            file.read_exact(&mut self.chunk[..count])
                .map_err(|error| format!("a media track could not be read: {error}"))?;
            self.writer
                .write_all(&self.chunk[..count])
                .map_err(|error| format!("the output could not be written: {error}"))?;
            self.position += count as u64;
            remaining -= count as u64;
        }
        Ok(())
    }

    fn finish(self) -> Result<(), String> {
        let file = self
            .writer
            .into_inner()
            .map_err(|error| format!("the output could not be written: {}", error.error()))?;
        file.sync_all()
            .map_err(|error| format!("the output could not be written: {error}"))
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum TrackFileKind {
    MpegTs,
    Webm,
    Other
}

/// Recognise a track file from its leading bytes. An MPEG-TS track is whole
/// 188-byte packets; the sync byte of every packet is checked again when the
/// track is read.
fn track_file_kind(path: &Path) -> Result<TrackFileKind, String> {
    let (mut file, length) = open_media_file(path, "A media track")?;
    let mut head = [0u8; MPEG_TS_PACKET_SIZE + 1];
    let mut filled = 0;
    while filled < head.len() {
        match file.read(&mut head[filled..]) {
            Ok(0) => break,
            Ok(count) => filled += count,
            Err(error) => return Err(format!("A media track could not be read: {error}"))
        }
    }
    let head = &head[..filled];
    if head.len() >= 4 && head[..4] == [0x1a, 0x45, 0xdf, 0xa3] {
        return Ok(TrackFileKind::Webm);
    }
    let packet_size = MPEG_TS_PACKET_SIZE as u64;
    if length >= packet_size
        && length % packet_size == 0
        && head[0] == 0x47
        && (length == packet_size || head.get(MPEG_TS_PACKET_SIZE) == Some(&0x47))
    {
        return Ok(TrackFileKind::MpegTs);
    }
    Ok(TrackFileKind::Other)
}

/// Mux separately downloaded track files into one file, file to file.
/// Returns the path written: WebM tracks are muxed into Matroska, so their
/// output takes the .mkv extension instead of `output_path`'s.
pub fn mux_track_files(inputs: &[PathBuf], output_path: &Path) -> Result<PathBuf, String> {
    if inputs.len() < 2 {
        return Err("Separate media tracks require at least two inputs".into());
    }
    let mut kinds = Vec::with_capacity(inputs.len());
    for path in inputs {
        kinds.push(track_file_kind(path)?);
    }
    let webm_count = kinds.iter().filter(|kind| **kind == TrackFileKind::Webm).count();
    let all_ts = kinds.iter().all(|kind| *kind == TrackFileKind::MpegTs);
    let mixed_webm = inputs.len() == 2 && webm_count == 1 && !kinds.contains(&TrackFileKind::MpegTs);
    let both_webm = inputs.len() == 2 && webm_count == 2;
    let mut actual = output_path.to_path_buf();
    if !all_ts && (both_webm || mixed_webm) {
        actual.set_extension("mkv");
    }
    let result = if !all_ts && !both_webm && !mixed_webm {
        write_regular(inputs, &actual, true)
    } else {
        MuxOutput::create(&actual).and_then(|mut output| {
            if all_ts {
                mux_mpeg_ts_tracks(inputs, &mut output)?;
            } else if both_webm {
                mux_webm_webm_tracks(inputs, &mut output)?;
            } else {
                mux_webm_fmp4_tracks(inputs, &mut output)?;
            }
            output.finish()
        })
    };
    if let Err(error) = result {
        let _ = std::fs::remove_file(&actual);
        return Err(format!(
            "Media track finalization failed: {error}; downloaded parts were preserved"
        ));
    }
    Ok(actual)
}

const MPEG_TS_PACKET_SIZE: usize = 188;
const MPEG_TS_PMT_PID: u16 = 0x1000;

struct MpegTsPacket<'a> {
    pid: u16,
    payload_unit_start: bool,
    payload: &'a [u8],
}

struct MpegTsPes {
    data: Vec<u8>,
    pts: Option<u64>,
}

/// One MPEG-TS track file after its checking pass: its elementary stream is
/// known to reassemble, and is read again unit by unit when it is muxed.
struct MpegTsTrack {
    path: PathBuf,
    context: String,
    elementary_pid: u16,
    stream_type: u8,
    descriptors: Vec<u8>,
    has_timestamps: bool,
}

/// Largest PES unit held in memory while a track is read. Units are single
/// frames in practice; the bound keeps a stream that never starts a new unit
/// from growing without limit.
const MPEG_TS_PES_LIMIT: usize = 64 * 1024 * 1024;

/// The 188-byte packets of one track file, read in order through a buffer.
struct MpegTsPackets {
    reader: std::io::BufReader<std::fs::File>,
    index: usize,
    packet: [u8; MPEG_TS_PACKET_SIZE],
}

impl MpegTsPackets {
    fn open(path: &Path, context: &str) -> Result<Self, String> {
        let (file, length) = open_media_file(path, context)?;
        if length % MPEG_TS_PACKET_SIZE as u64 != 0 {
            return Err(format!(
                "{context} is not a whole number of 188-byte MPEG-TS packets"
            ));
        }
        Ok(Self {
            reader: std::io::BufReader::with_capacity(MUX_COPY_CHUNK, file),
            index: 0,
            packet: [0; MPEG_TS_PACKET_SIZE],
        })
    }

    /// The next packet and its index, or None at the end of the file.
    fn next(&mut self) -> Result<Option<(usize, &[u8])>, String> {
        match self.reader.read_exact(&mut self.packet) {
            Ok(()) => {
                self.index += 1;
                Ok(Some((self.index - 1, &self.packet)))
            }
            Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => Ok(None),
            Err(error) => Err(format!("A media track could not be read: {error}")),
        }
    }
}

struct MpegTsPsiAssembler {
    buffer: Vec<u8>,
    expected_length: Option<usize>,
    sections: Vec<Vec<u8>>,
}

impl MpegTsPsiAssembler {
    fn new() -> Self {
        Self {
            buffer: Vec::new(),
            expected_length: None,
            sections: Vec::new(),
        }
    }

    fn append(&mut self, data: &[u8], context: &str) -> Result<(), String> {
        let mut cursor = 0;
        while cursor < data.len() {
            if self.buffer.is_empty() && self.expected_length.is_none() && data[cursor] == 0xff {
                break;
            }
            let remaining = self
                .expected_length
                .map_or(3usize.saturating_sub(self.buffer.len()), |length| {
                    length.saturating_sub(self.buffer.len())
                });
            if remaining == 0 {
                return Err(format!("{context} has an invalid section boundary"));
            }
            let count = remaining.min(data.len() - cursor);
            self.buffer.extend_from_slice(&data[cursor..cursor + count]);
            cursor += count;
            if self.expected_length.is_none() && self.buffer.len() >= 3 {
                let section_length =
                    ((usize::from(self.buffer[1] & 0x0f)) << 8) | usize::from(self.buffer[2]);
                if !(4..=1021).contains(&section_length) {
                    return Err(format!("{context} has an invalid section length"));
                }
                if 3 + section_length < self.buffer.len() {
                    return Err(format!("{context} has an invalid section boundary"));
                }
                self.expected_length = Some(3 + section_length);
            }
            if self.expected_length == Some(self.buffer.len()) {
                // Tables repeat every few packets; a run of identical copies
                // is kept once, so a long track's table history stays small.
                if self.sections.last() == Some(&self.buffer) {
                    self.buffer.clear();
                } else {
                    self.sections.push(std::mem::take(&mut self.buffer));
                }
                self.expected_length = None;
            }
        }
        Ok(())
    }

    fn push(
        &mut self,
        payload_unit_start: bool,
        payload: &[u8],
        context: &str,
    ) -> Result<(), String> {
        let mut cursor = 0;
        if payload_unit_start {
            if payload.is_empty() {
                return Err(format!(
                    "{context} has a payload-unit-start packet without payload"
                ));
            }
            let pointer = usize::from(payload[0]);
            if pointer > payload.len() - 1 {
                return Err(format!("{context} has an invalid PSI pointer field"));
            }
            cursor = 1;
            if pointer != 0 {
                let end = cursor + pointer;
                if self.buffer.is_empty() {
                    if payload[cursor..end].iter().any(|byte| *byte != 0xff) {
                        return Err(format!("{context} has an invalid PSI pointer area"));
                    }
                } else {
                    self.append(&payload[cursor..end], context)?;
                    if !self.buffer.is_empty() {
                        return Err(format!(
                            "{context} has a PSI pointer before a complete section"
                        ));
                    }
                }
                cursor = end;
            }
        }
        self.append(&payload[cursor..], context)
    }

    fn finish(self, context: &str) -> Result<Vec<Vec<u8>>, String> {
        if !self.buffer.is_empty() || self.expected_length.is_some() {
            return Err(format!("{context} ends with a truncated PSI section"));
        }
        if self.sections.is_empty() {
            return Err(format!("{context} contains no PSI section"));
        }
        Ok(self.sections)
    }
}

fn mpeg_ts_crc32(data: &[u8]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for byte in data {
        crc ^= u32::from(*byte) << 24;
        for _ in 0..8 {
            crc = if crc & 0x8000_0000 != 0 {
                (crc << 1) ^ 0x04c1_1db7
            } else {
                crc << 1
            };
        }
    }
    crc
}

fn parse_mpeg_ts_packet<'a>(packet: &'a [u8], track: &str, index: usize) -> Result<MpegTsPacket<'a>, String> {
    let context = || format!("{track} packet {index}");
    if packet.len() != MPEG_TS_PACKET_SIZE {
        return Err(format!("{} is not a 188-byte MPEG-TS packet", context()));
    }
    if packet[0] != 0x47 {
        return Err(format!("{} has an invalid MPEG-TS sync byte", context()));
    }
    if packet[1] & 0x80 != 0 {
        return Err(format!("{} has a transport error indicator", context()));
    }
    if packet[3] >> 6 != 0 {
        return Err(format!("{} uses scrambled MPEG-TS payload", context()));
    }
    let adaptation_control = (packet[3] >> 4) & 0x03;
    if adaptation_control == 0 {
        return Err(format!("{} has a reserved adaptation-field control", context()));
    }
    let mut payload_start = 4;
    if adaptation_control & 0x02 != 0 {
        let adaptation_length = usize::from(packet[4]);
        payload_start = 5 + adaptation_length;
        if payload_start > MPEG_TS_PACKET_SIZE {
            return Err(format!("{} has a truncated adaptation field", context()));
        }
    }
    let payload = if adaptation_control & 0x01 != 0 {
        &packet[payload_start..]
    } else {
        &[]
    };
    Ok(MpegTsPacket {
        pid: ((u16::from(packet[1] & 0x1f)) << 8) | u16::from(packet[2]),
        payload_unit_start: packet[1] & 0x40 != 0,
        payload
    })
}

fn collect_mpeg_ts_sections(path: &Path, pid: u16, context: &str) -> Result<Vec<Vec<u8>>, String> {
    let mut packets = MpegTsPackets::open(path, context)?;
    let mut assembler = MpegTsPsiAssembler::new();
    while let Some((index, packet)) = packets.next()? {
        let packet = parse_mpeg_ts_packet(packet, context, index)?;
        if packet.pid == pid && !packet.payload.is_empty() {
            assembler.push(packet.payload_unit_start, packet.payload, context)?;
        }
    }
    assembler.finish(context)
}

fn validate_mpeg_ts_section(section: &[u8], table_id: u8, context: &str) -> Result<(), String> {
    if section.len() < 7 || section[0] != table_id {
        return Err(format!(
            "{context} has an invalid table identifier or length"
        ));
    }
    if section[1] & 0xf0 != 0xb0 {
        return Err(format!("{context} has unsupported section syntax"));
    }
    let section_length = ((usize::from(section[1] & 0x0f)) << 8) | usize::from(section[2]);
    if !(4..=1021).contains(&section_length) || section.len() != section_length + 3 {
        return Err(format!("{context} has an invalid section length"));
    }
    if mpeg_ts_crc32(section) != 0 {
        return Err(format!("{context} has an invalid MPEG-2 CRC32"));
    }
    Ok(())
}

fn parse_mpeg_ts_pat(sections: &[Vec<u8>], context: &str) -> Result<(u16, u16), String> {
    let mut selected = None;
    for (index, section) in sections.iter().enumerate() {
        let section_context = format!("{context} section {index}");
        validate_mpeg_ts_section(section, 0x00, &section_context)?;
        if section.len() < 12 {
            return Err(format!("{section_context} is too short for a PAT"));
        }
        if section[6] > section[7] {
            return Err(format!("{section_context} has an invalid section number"));
        }
        if section[5] & 0x01 == 0 {
            continue;
        }
        let end = section.len() - 4;
        if end < 8 || (end - 8) % 4 != 0 {
            return Err(format!("{section_context} has malformed PAT entries"));
        }
        let mut cursor = 8;
        while cursor < end {
            let program_number = u16::from_be_bytes([section[cursor], section[cursor + 1]]);
            let pid =
                ((u16::from(section[cursor + 2] & 0x1f)) << 8) | u16::from(section[cursor + 3]);
            if section[cursor + 2] & 0xe0 != 0xe0 {
                return Err(format!("{section_context} has invalid PAT PID flags"));
            }
            if program_number != 0 && selected.is_none() {
                if pid == 0 || pid == 0x1fff {
                    return Err(format!("{section_context} has an invalid PMT PID"));
                }
                selected = Some((program_number, pid));
            }
            cursor += 4;
        }
    }
    selected.ok_or_else(|| format!("{context} contains no current program"))
}

fn validate_mpeg_ts_descriptors(descriptors: &[u8], context: &str) -> Result<(), String> {
    let mut cursor = 0;
    while cursor < descriptors.len() {
        if descriptors.len() - cursor < 2 {
            return Err(format!("{context} has a truncated descriptor"));
        }
        let end = cursor + 2 + usize::from(descriptors[cursor + 1]);
        if end > descriptors.len() {
            return Err(format!(
                "{context} has a descriptor beyond its declared length"
            ));
        }
        cursor = end;
    }
    Ok(())
}

fn parse_mpeg_ts_pmt_stream(
    sections: &[Vec<u8>],
    program_number: u16,
    context: &str
) -> Result<(u16, u8, Vec<u8>), String> {
    let mut selected = None;
    for (index, section) in sections.iter().enumerate() {
        let section_context = format!("{context} section {index}");
        validate_mpeg_ts_section(section, 0x02, &section_context)?;
        if section.len() < 16 {
            return Err(format!("{section_context} is too short for a PMT"));
        }
        if section[6] > section[7] {
            return Err(format!("{section_context} has an invalid section number"));
        }
        if section[5] & 0x01 == 0 {
            continue;
        }
        if u16::from_be_bytes([section[3], section[4]]) != program_number {
            return Err(format!("{section_context} references a different program"));
        }
        if section[8] & 0xe0 != 0xe0 || section[10] & 0xf0 != 0xf0 {
            return Err(format!("{section_context} has invalid PMT flags"));
        }
        let program_info_length =
            ((usize::from(section[10] & 0x0f)) << 8) | usize::from(section[11]);
        let end = section.len() - 4;
        let cursor = 12usize
            .checked_add(program_info_length)
            .ok_or_else(|| format!("{section_context} has an overflowing program-info length"))?;
        if cursor > end {
            return Err(format!(
                "{section_context} has a truncated program-info loop"
            ));
        }
        validate_mpeg_ts_descriptors(
            &section[12..cursor],
            &format!("{section_context} program-info")
        )?;
        let mut cursor = cursor;
        while cursor < end {
            if end - cursor < 5 {
                return Err(format!(
                    "{section_context} has a truncated elementary stream entry"
                ));
            }
            let stream_type = section[cursor];
            if stream_type == 0x00 || stream_type == 0xff {
                return Err(format!("{section_context} uses an unsupported stream type"));
            }
            if section[cursor + 1] & 0xe0 != 0xe0 {
                return Err(format!(
                    "{section_context} has invalid elementary PID flags"
                ));
            }
            let elementary_pid =
                ((u16::from(section[cursor + 1] & 0x1f)) << 8) | u16::from(section[cursor + 2]);
            if elementary_pid == 0 || elementary_pid == 0x1fff {
                return Err(format!("{section_context} has an invalid elementary PID"));
            }
            if section[cursor + 3] & 0xf0 != 0xf0 {
                return Err(format!("{section_context} has invalid ES-info flags"));
            }
            let descriptors_length =
                ((usize::from(section[cursor + 3] & 0x0f)) << 8) | usize::from(section[cursor + 4]);
            let descriptor_start = cursor + 5;
            let descriptor_end = descriptor_start
                .checked_add(descriptors_length)
                .ok_or_else(|| format!("{section_context} has an overflowing ES-info length"))?;
            if descriptor_end > end {
                return Err(format!("{section_context} has a truncated ES-info loop"));
            }
            validate_mpeg_ts_descriptors(
                &section[descriptor_start..descriptor_end],
                &format!("{section_context} ES-info")
            )?;
            if selected.is_none() {
                selected = Some((
                    elementary_pid,
                    stream_type,
                    section[descriptor_start..descriptor_end].to_vec()
                ));
            }
            cursor = descriptor_end;
        }
    }
    selected.ok_or_else(|| format!("{context} contains no current elementary stream"))
}

fn parse_mpeg_ts_pts(bytes: &[u8], prefix: u8, context: &str) -> Result<u64, String> {
    if bytes.len() < 5 {
        return Err(format!("{context} has a truncated PTS field"));
    }
    if bytes[0] >> 4 != prefix || bytes[0] & 1 == 0 || bytes[2] & 1 == 0 || bytes[4] & 1 == 0 {
        return Err(format!("{context} has invalid PTS marker bits"));
    }
    Ok((u64::from((bytes[0] >> 1) & 0x07) << 30)
        | (u64::from(bytes[1]) << 22)
        | (u64::from((bytes[2] >> 1) & 0x7f) << 15)
        | (u64::from(bytes[3]) << 7)
        | u64::from((bytes[4] >> 1) & 0x7f))
}

fn parse_mpeg_ts_pes(mut data: Vec<u8>, context: &str) -> Result<MpegTsPes, String> {
    if data.len() < 6 || data[0..3] != [0x00, 0x00, 0x01] {
        return Err(format!("{context} has an invalid PES start code"));
    }
    let packet_length = usize::from(u16::from_be_bytes([data[4], data[5]]));
    if packet_length != 0 {
        let total_length = 6usize
            .checked_add(packet_length)
            .ok_or_else(|| format!("{context} has an overflowing PES length"))?;
        if data.len() < total_length {
            return Err(format!("{context} is truncated"));
        }
        if data[total_length..].iter().any(|byte| *byte != 0xff) {
            return Err(format!(
                "{context} has non-stuffing bytes after its declared length"
            ));
        }
        data.truncate(total_length);
    }
    let stream_id = data[3];
    let pts = if matches!(
        stream_id,
        0xbc | 0xbe | 0xbf | 0xf0 | 0xf1 | 0xff | 0xf2 | 0xf8
    ) {
        None
    } else {
        if data.len() < 9 {
            return Err(format!("{context} has a truncated PES header"));
        }
        if data[6] & 0xc0 != 0x80 {
            return Err(format!("{context} uses an unsupported PES header format"));
        }
        let header_length = usize::from(data[8]);
        let payload_start = 9usize
            .checked_add(header_length)
            .ok_or_else(|| format!("{context} has an overflowing PES header length"))?;
        if payload_start > data.len() {
            return Err(format!("{context} has a truncated PES optional header"));
        }
        match (data[7] >> 6) & 0x03 {
            0 => None,
            2 => {
                if header_length < 5 {
                    return Err(format!("{context} has a truncated PTS field"));
                }
                Some(parse_mpeg_ts_pts(&data[9..14], 0x2, context)?)
            }
            3 => {
                if header_length < 10 {
                    return Err(format!("{context} has truncated PTS/DTS fields"));
                }
                let pts = parse_mpeg_ts_pts(&data[9..14], 0x3, context)?;
                parse_mpeg_ts_pts(&data[14..19], 0x1, context)?;
                Some(pts)
            }
            _ => return Err(format!("{context} uses a forbidden PTS/DTS flag"))
        }
    };
    Ok(MpegTsPes { data, pts })
}

/// The PES units of one elementary stream, reassembled from a track file one
/// unit at a time.
struct MpegTsPesReader {
    packets: MpegTsPackets,
    pid: u16,
    context: String,
    current: Option<Vec<u8>>,
    count: usize,
    finished: bool,
}

impl MpegTsPesReader {
    fn open(path: &Path, pid: u16, context: String) -> Result<Self, String> {
        Ok(Self {
            packets: MpegTsPackets::open(path, &context)?,
            pid,
            context,
            current: None,
            count: 0,
            finished: false,
        })
    }

    fn next(&mut self) -> Result<Option<MpegTsPes>, String> {
        if self.finished {
            return Ok(None);
        }
        loop {
            let Some((index, raw)) = self.packets.next()? else {
                self.finished = true;
                let Some(data) = self.current.take() else {
                    if self.count == 0 {
                        return Err(format!("{} contains no PES units", self.context));
                    }
                    return Ok(None);
                };
                let pes = parse_mpeg_ts_pes(data, &format!("{} PES {}", self.context, self.count))?;
                self.count += 1;
                return Ok(Some(pes));
            };
            let packet = parse_mpeg_ts_packet(raw, &self.context, index)?;
            if packet.pid != self.pid {
                continue;
            }
            if packet.payload_unit_start {
                if packet.payload.len() < 3 {
                    return Err(format!(
                        "{} packet {index} has a truncated PES start",
                        self.context
                    ));
                }
                let finished = match self.current.take() {
                    Some(data) => Some(parse_mpeg_ts_pes(
                        data,
                        &format!("{} PES {}", self.context, self.count)
                    )?),
                    None => None,
                };
                if packet.payload[0..3] != [0x00, 0x00, 0x01] {
                    return Err(format!("{} packet {index} does not start a PES", self.context));
                }
                self.current = Some(packet.payload.to_vec());
                if let Some(pes) = finished {
                    self.count += 1;
                    return Ok(Some(pes));
                }
            } else if !packet.payload.is_empty() {
                let Some(current) = self.current.as_mut() else {
                    return Err(format!(
                        "{} has PES payload before its first start packet",
                        self.context
                    ));
                };
                current.extend_from_slice(packet.payload);
                if current.len() > MPEG_TS_PES_LIMIT {
                    return Err(format!("{} has a PES unit larger than 64 MiB", self.context));
                }
            }
        }
    }
}

/// Check one MPEG-TS track file: its program tables, and every PES unit of
/// its elementary stream (read and dropped one at a time).
fn parse_mpeg_ts_input(path: &Path, index: usize) -> Result<MpegTsTrack, String> {
    let context = format!("MPEG-TS input {index}");
    let (_, length) = open_media_file(path, &context)?;
    if length == 0 {
        return Err(format!("{context} is empty"));
    }
    if length % MPEG_TS_PACKET_SIZE as u64 != 0 {
        return Err(format!(
            "{context} is not a whole number of 188-byte packets"
        ));
    }
    let pat_sections = collect_mpeg_ts_sections(path, 0, &format!("{context} PAT"))?;
    let (program_number, pmt_pid) = parse_mpeg_ts_pat(&pat_sections, &format!("{context} PAT"))?;
    let pmt_sections = collect_mpeg_ts_sections(path, pmt_pid, &format!("{context} PMT"))?;
    let (elementary_pid, stream_type, descriptors) =
        parse_mpeg_ts_pmt_stream(&pmt_sections, program_number, &format!("{context} PMT"))?;
    if elementary_pid == pmt_pid {
        return Err(format!(
            "{context} maps its PMT PID as an elementary stream"
        ));
    }
    let stream_context = format!("{context} elementary stream");
    let mut reader = MpegTsPesReader::open(path, elementary_pid, stream_context.clone())?;
    let mut has_timestamps = false;
    while let Some(pes) = reader.next()? {
        has_timestamps |= pes.pts.is_some();
    }
    Ok(MpegTsTrack {
        path: path.to_path_buf(),
        context: stream_context,
        elementary_pid,
        stream_type,
        descriptors,
        has_timestamps
    })
}

fn append_mpeg_ts_payload_with_pcr(
    output: &mut Vec<u8>,
    pid: u16,
    payload_unit_start: bool,
    payload: &[u8],
    continuity_counter: &mut u8,
    pcr: Option<u64>
) -> Result<(), String> {
    if payload.is_empty() || payload.len() > 184 {
        return Err("An MPEG-TS payload must contain 1 to 184 bytes".into());
    }
    if pcr.is_some() && payload.len() > 176 {
        return Err("An MPEG-TS PCR packet has too much payload".into());
    }
    let mut packet = [0u8; MPEG_TS_PACKET_SIZE];
    packet[0] = 0x47;
    packet[1] = (if payload_unit_start { 0x40 } else { 0 }) | ((pid >> 8) & 0x1f) as u8;
    packet[2] = pid as u8;
    let payload_start = if payload.len() == 184 && pcr.is_none() {
        packet[3] = 0x10 | (*continuity_counter & 0x0f);
        4
    } else {
        let adaptation_length = 183 - payload.len();
        packet[3] = 0x30 | (*continuity_counter & 0x0f);
        packet[4] = adaptation_length as u8;
        if adaptation_length > 0 {
            packet[5] = if pcr.is_some() { 0x10 } else { 0 };
        }
        if let Some(pcr) = pcr {
            if adaptation_length < 7 {
                return Err("An MPEG-TS PCR adaptation field is truncated".into());
            }
            let base = pcr & ((1u64 << 33) - 1);
            packet[6] = (base >> 25) as u8;
            packet[7] = (base >> 17) as u8;
            packet[8] = (base >> 9) as u8;
            packet[9] = (base >> 1) as u8;
            packet[10] = ((base as u8 & 1) << 7) | 0x7e;
            packet[11] = 0;
            if adaptation_length > 7 {
                packet[12..5 + adaptation_length].fill(0xff);
            }
        } else if adaptation_length > 1 {
            packet[6..5 + adaptation_length].fill(0xff);
        }
        5 + adaptation_length
    };
    if payload_start + payload.len() != MPEG_TS_PACKET_SIZE {
        return Err("An MPEG-TS payload did not fill its packet".into());
    }
    packet[payload_start..].copy_from_slice(payload);
    output.extend_from_slice(&packet);
    *continuity_counter = (*continuity_counter + 1) & 0x0f;
    Ok(())
}

fn append_mpeg_ts_payload(
    output: &mut Vec<u8>,
    pid: u16,
    payload_unit_start: bool,
    payload: &[u8],
    continuity_counter: &mut u8
) -> Result<(), String> {
    append_mpeg_ts_payload_with_pcr(
        output,
        pid,
        payload_unit_start,
        payload,
        continuity_counter,
        None
    )
}

fn append_mpeg_ts_section(
    output: &mut Vec<u8>,
    pid: u16,
    section: &[u8],
    continuity_counter: &mut u8
) -> Result<(), String> {
    if section.is_empty() {
        return Err("An MPEG-TS PSI section is empty".into());
    }
    let first_count = section.len().min(183);
    let mut first_payload = Vec::with_capacity(first_count + 1);
    first_payload.push(0);
    first_payload.extend_from_slice(&section[..first_count]);
    append_mpeg_ts_payload(output, pid, true, &first_payload, continuity_counter)?;
    let mut cursor = first_count;
    while cursor < section.len() {
        let count = (section.len() - cursor).min(184);
        append_mpeg_ts_payload(
            output,
            pid,
            false,
            &section[cursor..cursor + count],
            continuity_counter
        )?;
        cursor += count;
    }
    Ok(())
}

fn build_mpeg_ts_pat(program_number: u16, pmt_pid: u16) -> Vec<u8> {
    let mut section = Vec::with_capacity(16);
    section.extend_from_slice(&[0x00, 0xb0, 0x0d]);
    section.extend_from_slice(&program_number.to_be_bytes());
    section.extend_from_slice(&[0xc1, 0x00, 0x00, 0x00, 0x01]);
    section.push(0xe0 | ((pmt_pid >> 8) & 0x1f) as u8);
    section.push(pmt_pid as u8);
    section.extend_from_slice(&mpeg_ts_crc32(&section).to_be_bytes());
    section
}

fn build_mpeg_ts_pmt(
    program_number: u16,
    pcr_pid: u16,
    pids: &[u16],
    tracks: &[MpegTsTrack]
) -> Result<Vec<u8>, String> {
    let entries_length = tracks
        .iter()
        .try_fold(0usize, |length, track| {
            length
                .checked_add(5)
                .and_then(|value| value.checked_add(track.descriptors.len()))
        })
        .ok_or_else(|| "The MPEG-TS PMT is too large".to_string())?;
    let section_length = 13usize
        .checked_add(entries_length)
        .ok_or_else(|| "The MPEG-TS PMT is too large".to_string())?;
    if section_length > 1021 || pids.len() != tracks.len() {
        return Err("The MPEG-TS PMT is too large or inconsistent".into());
    }
    let mut section = Vec::with_capacity(section_length + 3);
    section.push(0x02);
    section.push(0xb0 | ((section_length >> 8) & 0x0f) as u8);
    section.push(section_length as u8);
    section.extend_from_slice(&program_number.to_be_bytes());
    section.extend_from_slice(&[0xc1, 0x00, 0x00]);
    section.push(0xe0 | ((pcr_pid >> 8) & 0x1f) as u8);
    section.push(pcr_pid as u8);
    section.extend_from_slice(&[0xf0, 0x00]);
    for (track, pid) in tracks.iter().zip(pids) {
        if track.descriptors.len() > 0x0fff || *pid == 0 || *pid >= 0x1fff {
            return Err("The MPEG-TS PMT contains an invalid PID or descriptor loop".into());
        }
        section.push(track.stream_type);
        section.push(0xe0 | ((*pid >> 8) & 0x1f) as u8);
        section.push(*pid as u8);
        section.push(0xf0 | ((track.descriptors.len() >> 8) & 0x0f) as u8);
        section.push(track.descriptors.len() as u8);
        section.extend_from_slice(&track.descriptors);
    }
    section.extend_from_slice(&mpeg_ts_crc32(&section).to_be_bytes());
    Ok(section)
}

fn append_mpeg_ts_pes_with_pcr(
    output: &mut Vec<u8>,
    pid: u16,
    pes: &MpegTsPes,
    continuity_counter: &mut u8,
    pcr: Option<u64>
) -> Result<(), String> {
    if pes.data.is_empty() {
        return Err("An MPEG-TS PES unit is empty".into());
    }
    let mut cursor = 0;
    let mut payload_unit_start = true;
    while cursor < pes.data.len() {
        let capacity = if payload_unit_start && pcr.is_some() {
            176
        } else {
            184
        };
        let count = (pes.data.len() - cursor).min(capacity);
        append_mpeg_ts_payload_with_pcr(
            output,
            pid,
            payload_unit_start,
            &pes.data[cursor..cursor + count],
            continuity_counter,
            if payload_unit_start { pcr } else { None }
        )?;
        cursor += count;
        payload_unit_start = false;
    }
    Ok(())
}

fn next_mpeg_ts_track(heads: &[Option<MpegTsPes>]) -> Option<usize> {
    let mut selected: Option<usize> = None;
    for index in 0..heads.len() {
        let Some(head) = &heads[index] else {
            continue;
        };
        let Some(current) = selected else {
            selected = Some(index);
            continue;
        };
        let left = head.pts;
        let right = heads[current].as_ref().and_then(|pes| pes.pts);
        let ordering = match (left, right) {
            (Some(left), Some(right)) => left.cmp(&right),
            (Some(_), None) => std::cmp::Ordering::Less,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            (None, None) => index.cmp(&current)
        };
        if ordering == std::cmp::Ordering::Less {
            selected = Some(index);
        }
    }
    selected
}

/// Remux separate 188-byte MPEG-TS elementary-stream inputs into one program.
/// Each track is checked in a first pass; the merge then holds one PES unit
/// per track (F13).
fn mux_mpeg_ts_tracks(inputs: &[PathBuf], output: &mut MuxOutput) -> Result<(), String> {
    if inputs.len() < 2 {
        return Err("MPEG-TS muxing requires at least two tracks".into());
    }
    let mut tracks = Vec::with_capacity(inputs.len());
    for (index, input) in inputs.iter().enumerate() {
        tracks.push(parse_mpeg_ts_input(input, index)?);
    }
    let mut pids = Vec::with_capacity(tracks.len());
    for index in 0..tracks.len() {
        let pid = 0x0100usize
            .checked_add(index)
            .and_then(|value| u16::try_from(value).ok())
            .ok_or_else(|| "Too many MPEG-TS tracks".to_string())?;
        if pid >= MPEG_TS_PMT_PID || pid == 0x1fff {
            return Err("Too many MPEG-TS tracks for stable output PIDs".into());
        }
        pids.push(pid);
    }
    let pat = build_mpeg_ts_pat(1, MPEG_TS_PMT_PID);
    let pcr_track = tracks.iter().position(|track| track.has_timestamps);
    let pcr_pid = pcr_track.map(|index| pids[index]).unwrap_or(0x1fff);
    let pmt = build_mpeg_ts_pmt(1, pcr_pid, &pids, &tracks)?;
    let mut buffer = Vec::new();
    let mut pat_continuity = 0;
    append_mpeg_ts_section(&mut buffer, 0, &pat, &mut pat_continuity)?;
    let mut pmt_continuity = 0;
    append_mpeg_ts_section(&mut buffer, MPEG_TS_PMT_PID, &pmt, &mut pmt_continuity)?;
    output.write(&buffer)?;
    let mut readers = Vec::with_capacity(tracks.len());
    let mut heads = Vec::with_capacity(tracks.len());
    for track in &tracks {
        let mut reader = MpegTsPesReader::open(&track.path, track.elementary_pid, track.context.clone())?;
        heads.push(reader.next()?);
        readers.push(reader);
    }
    let mut continuities = vec![0u8; tracks.len()];
    while let Some(track_index) = next_mpeg_ts_track(&heads) {
        let pes = heads[track_index]
            .take()
            .ok_or_else(|| "The MPEG-TS track merge ended unexpectedly".to_string())?;
        let pcr = (pcr_track == Some(track_index)).then_some(pes.pts).flatten();
        buffer.clear();
        append_mpeg_ts_pes_with_pcr(
            &mut buffer,
            pids[track_index],
            &pes,
            &mut continuities[track_index],
            pcr
        )?;
        output.write(&buffer)?;
        heads[track_index] = readers[track_index].next()?;
    }
    Ok(())
}

/// Where one media sample's bytes lie: which input file, and the byte range.
#[derive(Clone, Copy)]
struct SampleBytes {
    input: usize,
    offset: u64,
    length: u64
}

#[derive(Clone)]
struct TimedMediaSample {
    timestamp_ns: i128,
    duration_ns: u64,
    bytes: SampleBytes,
    keyframe: bool
}

struct WebmVideoTrack {
    codec_private: Vec<u8>,
    width: u64,
    height: u64,
    samples: Vec<TimedMediaSample>
}

struct Fmp4AudioTrack {
    codec_private: Vec<u8>,
    sample_rate: u32,
    channels: u16,
    samples: Vec<TimedMediaSample>
}

struct WebmAudioTrack {
    codec_id: String,
    codec_private: Vec<u8>,
    sample_rate: u32,
    channels: u16,
    samples: Vec<TimedMediaSample>,
}

struct AudioTrackInfo {
    codec_id: String,
    codec_private: Vec<u8>,
    sample_rate: u32,
    channels: u16,
    samples: Vec<TimedMediaSample>,
}

fn ebml_child(
    data: &[u8],
    start: usize,
    end: usize,
    id: u64
) -> Result<Option<(usize, usize)>, String> {
    Ok(ebml_children(data, start, end)?
        .into_iter()
        .find(|(child_id, _, _)| *child_id == id)
        .map(|(_, child_start, child_end)| (child_start, child_end)))
}

fn ebml_child_uint(data: &[u8], start: usize, end: usize, id: u64) -> Result<Option<u64>, String> {
    let Some((child_start, child_end)) = ebml_child(data, start, end, id)? else {
        return Ok(None);
    };
    Ok(Some(ebml_uint(data, child_start, child_end)?))
}

fn ebml_child_text(
    data: &[u8],
    start: usize,
    end: usize,
    id: u64,
    context: &str
) -> Result<Option<String>, String> {
    let Some((child_start, child_end)) = ebml_child(data, start, end, id)? else {
        return Ok(None);
    };
    Ok(Some(
        std::str::from_utf8(&data[child_start..child_end])
            .map_err(|_| format!("{context} contains invalid UTF-8"))?
            .to_string()
    ))
}

fn ebml_child_bytes(
    data: &[u8],
    start: usize,
    end: usize,
    id: u64
) -> Result<Option<Vec<u8>>, String> {
    let Some((child_start, child_end)) = ebml_child(data, start, end, id)? else {
        return Ok(None);
    };
    Ok(Some(data[child_start..child_end].to_vec()))
}

/// Parse a Block's header. `head` holds the Block's first bytes (at most
/// `EBML_BLOCK_HEAD`); the Block itself spans `length` bytes from `start` in
/// input file `input`, and the sample is the part after its header.
fn parse_webm_block(
    head: &[u8],
    start: u64,
    length: u64,
    input: usize,
    cluster_timecode: u64,
    timecode_scale: u64,
    track_number: u64,
    keyframe: bool,
    default_duration: Option<u64>,
    context: &str
) -> Result<TimedMediaSample, String> {
    let (block_track, track_width) = ebml_vint(head, 0)?;
    let block_track =
        block_track.ok_or_else(|| format!("{context} has an unknown track number"))?;
    if block_track != track_number {
        return Err(format!(
            "{context} references track {block_track}, expected {track_number}"
        ));
    }
    let header = (track_width + 3) as u64;
    if length < header {
        return Err(format!("{context} is truncated"));
    }
    if head[track_width + 2] & 0x06 != 0 {
        return Err(format!("{context} uses unsupported lacing"));
    }
    let relative = i16::from_be_bytes([head[track_width], head[track_width + 1]]) as i128;
    let ticks = i128::from(cluster_timecode)
        .checked_add(relative)
        .ok_or_else(|| format!("{context} timecode overflowed"))?;
    if ticks < 0 {
        return Err(format!("{context} has a negative timestamp"));
    }
    let timestamp_ns = ticks
        .checked_mul(i128::from(timecode_scale))
        .ok_or_else(|| format!("{context} timestamp overflowed"))?;
    Ok(TimedMediaSample {
        timestamp_ns,
        duration_ns: default_duration.unwrap_or(0),
        bytes: SampleBytes {
            input,
            offset: start + header,
            length: length - header
        },
        keyframe
    })
}

/// A Block's track number (up to 8 bytes), timecode and flags.
const EBML_BLOCK_HEAD: u64 = 11;
const MATROSKA_CLUSTER: u64 = 0x1f43_b675;
/// Segment-level IDs; an unknown-size Cluster ends where one of them begins.
const MATROSKA_SEGMENT_CHILDREN: [u64; 8] = [
    0x1f43_b675,
    0x1c53_bb6b,
    0x1254_c367,
    0x1043_a770,
    0x1941_a469,
    0x114d_9b74,
    0x1549_a966,
    0x1654_ae6b
];

/// One EBML element header read from a file: (id, data start, data end,
/// unknown size). An unknown size runs to `end`, the enclosing element's end.
fn file_ebml_element(
    file: &mut std::fs::File,
    cursor: u64,
    end: u64
) -> Result<(u64, u64, u64, bool), String> {
    let head = file_bytes(file, cursor, (end - cursor).min(12), "A WebM element")?;
    let (id, id_width) = ebml_id(&head, 0)?;
    let (size, size_width) = ebml_vint(&head, id_width)?;
    let data_start = cursor + (id_width + size_width) as u64;
    let (data_end, unknown) = match size {
        Some(size) => (
            data_start
                .checked_add(size)
                .ok_or_else(|| "The EBML element size overflowed".to_string())?,
            false
        ),
        None => (end, true)
    };
    if data_end > end {
        return Err("The EBML element is truncated".into());
    }
    Ok((id, data_start, data_end, unknown))
}

/// The child elements of a file range as (id, data start, data end), read
/// header by header. An unknown-size Cluster (as live recorders write them)
/// ends at the next Segment-level element rather than swallowing the rest of
/// the Segment.
fn file_ebml_children(
    file: &mut std::fs::File,
    mut cursor: u64,
    end: u64
) -> Result<Vec<(u64, u64, u64)>, String> {
    let mut elements = Vec::new();
    while cursor < end {
        let (id, data_start, mut data_end, unknown) = file_ebml_element(file, cursor, end)?;
        if unknown && id == MATROSKA_CLUSTER {
            let mut child = data_start;
            while child < end {
                let (child_id, _, child_end, _) = file_ebml_element(file, child, end)?;
                if MATROSKA_SEGMENT_CHILDREN.contains(&child_id) {
                    break;
                }
                child = child_end;
            }
            data_end = child;
        }
        elements.push((id, data_start, data_end));
        cursor = data_end;
    }
    Ok(elements)
}

/// A WebM file indexed where it lies: the Segment's children by position,
/// with only the Info and Tracks elements read into memory (F13).
struct WebmFile {
    file: std::fs::File,
    segment_children: Vec<(u64, u64, u64)>,
    info: Option<Vec<u8>>,
    tracks: Vec<u8>
}

/// One Block as the cluster walk finds it.
struct WebmBlock<'a> {
    cluster_timecode: u64,
    /// The Block's first bytes, or None for a BlockGroup without a Block.
    head: Option<&'a [u8]>,
    start: u64,
    length: u64,
    /// Some(keyframe) for a BlockGroup, None for a SimpleBlock.
    group_keyframe: Option<bool>
}

fn open_webm(path: &Path, context: &str) -> Result<WebmFile, String> {
    let (mut file, total) = open_media_file(path, context)?;
    let mut magic = [0u8; 4];
    if total < 4 || file.read_exact(&mut magic).is_err() || magic != [0x1a, 0x45, 0xdf, 0xa3] {
        return Err(format!("{context} is not an EBML/WebM file"));
    }
    let top = file_ebml_children(&mut file, 0, total)?;
    let (_, segment_start, segment_end) = top
        .iter()
        .find(|(id, _, _)| *id == 0x1853_8067)
        .copied()
        .ok_or_else(|| format!("{context} is missing a Segment element"))?;
    let segment_children = file_ebml_children(&mut file, segment_start, segment_end)?;
    let info = match segment_children.iter().find(|(id, _, _)| *id == 0x1549_a966) {
        Some(&(_, start, end)) => Some(file_bytes(&mut file, start, end - start, context)?),
        None => None
    };
    let (_, tracks_start, tracks_end) = segment_children
        .iter()
        .find(|(id, _, _)| *id == 0x1654_ae6b)
        .copied()
        .ok_or_else(|| format!("{context} is missing Tracks"))?;
    let tracks = file_bytes(&mut file, tracks_start, tracks_end - tracks_start, context)?;
    Ok(WebmFile {
        file,
        segment_children,
        info,
        tracks
    })
}

impl WebmFile {
    fn timecode_scale(&self, context: &str) -> Result<u64, String> {
        let scale = match &self.info {
            Some(info) => ebml_child_uint(info, 0, info.len(), 0x2ad7_b1)?.unwrap_or(1_000_000),
            None => 1_000_000
        };
        if scale == 0 {
            return Err(format!("{context} has a zero timecode scale"));
        }
        Ok(scale)
    }

    /// The first TrackEntry of `track_type`, as a range of `self.tracks`.
    fn track_entry(&self, track_type: u64) -> Result<Option<(usize, usize)>, String> {
        let data = &self.tracks;
        Ok(ebml_children(data, 0, data.len())?
            .into_iter()
            .filter(|(id, _, _)| *id == 0xae)
            .find_map(|(_, start, end)| {
                let found = ebml_child_uint(data, start, end, 0x83).ok().flatten();
                (found == Some(track_type)).then_some((start, end))
            }))
    }

    /// Visit every Block of every Cluster in file order. Only each Block's
    /// header is read; its media bytes stay in the file.
    fn blocks(
        &mut self,
        mut visit: impl FnMut(WebmBlock<'_>) -> Result<(), String>
    ) -> Result<(), String> {
        let clusters: Vec<(u64, u64)> = self
            .segment_children
            .iter()
            .filter(|(id, _, _)| *id == MATROSKA_CLUSTER)
            .map(|(_, start, end)| (*start, *end))
            .collect();
        for (cluster_start, cluster_end) in clusters {
            let children = file_ebml_children(&mut self.file, cluster_start, cluster_end)?;
            let cluster_timecode = match children.iter().find(|(id, _, _)| *id == 0xe7) {
                Some(&(_, start, end)) => {
                    let bytes = file_bytes(&mut self.file, start, end - start, "A WebM cluster")?;
                    ebml_uint(&bytes, 0, bytes.len())?
                }
                None => 0
            };
            for (id, child_start, child_end) in children {
                let (block, group_keyframe) = if id == 0xa3 {
                    (Some((child_start, child_end)), None)
                } else if id == 0xa0 {
                    let group = file_ebml_children(&mut self.file, child_start, child_end)?;
                    let block = group
                        .iter()
                        .find(|(group_id, _, _)| *group_id == 0xa1)
                        .map(|(_, start, end)| (*start, *end));
                    let keyframe = !group.iter().any(|(group_id, _, _)| *group_id == 0xfb);
                    (block, Some(keyframe))
                } else {
                    continue;
                };
                let head = match block {
                    Some((start, end)) => Some(file_bytes(
                        &mut self.file,
                        start,
                        (end - start).min(EBML_BLOCK_HEAD),
                        "A WebM block"
                    )?),
                    None => None
                };
                let (start, end) = block.unwrap_or((child_start, child_start));
                visit(WebmBlock {
                    cluster_timecode,
                    head: head.as_deref(),
                    start,
                    length: end - start,
                    group_keyframe
                })?;
            }
        }
        Ok(())
    }
}

fn fill_sample_durations(
    samples: &mut [TimedMediaSample],
    fallback: u64,
    context: &str
) -> Result<(), String> {
    if samples.is_empty() {
        return Err(format!("{context} contains no samples"));
    }
    samples.sort_by_key(|sample| sample.timestamp_ns);
    for index in 0..samples.len() {
        if samples[index].duration_ns > 0 {
            continue;
        }
        let next = samples
            .get(index + 1)
            .map(|sample| sample.timestamp_ns - samples[index].timestamp_ns)
            .filter(|duration| *duration > 0)
            .and_then(|duration| u64::try_from(duration).ok());
        samples[index].duration_ns = next.unwrap_or(fallback.max(1));
    }
    Ok(())
}

fn parse_webm_video(path: &Path, input: usize) -> Result<WebmVideoTrack, String> {
    let context = format!("WebM input {input}");
    let mut webm = open_webm(path, &context)?;
    let timecode_scale = webm.timecode_scale(&context)?;
    let (entry_start, entry_end) = webm
        .track_entry(1)?
        .ok_or_else(|| format!("{context} contains no video TrackEntry"))?;
    let data = &webm.tracks;
    let track_number = ebml_child_uint(data, entry_start, entry_end, 0xd7)?
        .ok_or_else(|| format!("{context} video TrackEntry has no track number"))?;
    if track_number == 0 {
        return Err(format!(
            "{context} video TrackEntry has an invalid track number"
        ));
    }
    let codec_id = ebml_child_text(
        data,
        entry_start,
        entry_end,
        0x86,
        &format!("{context} TrackEntry")
    )?
    .ok_or_else(|| format!("{context} video TrackEntry has no codec ID"))?;
    if codec_id != "V_VP9" {
        return Err(format!("{context} uses unsupported video codec {codec_id}"));
    }
    let codec_private = ebml_child_bytes(data, entry_start, entry_end, 0x63a2)?.unwrap_or_default();
    let default_duration = ebml_child_uint(data, entry_start, entry_end, 0x23e383)?;
    let (_, video_start, video_end) = ebml_children(data, entry_start, entry_end)?
        .into_iter()
        .find(|(id, _, _)| *id == 0xe0)
        .ok_or_else(|| format!("{context} video TrackEntry has no Video element"))?;
    let width = ebml_child_uint(data, video_start, video_end, 0xb0)?
        .ok_or_else(|| format!("{context} video width is missing"))?;
    let height = ebml_child_uint(data, video_start, video_end, 0xba)?
        .ok_or_else(|| format!("{context} video height is missing"))?;
    if width == 0 || height == 0 {
        return Err(format!("{context} has invalid video dimensions"));
    }
    let mut samples = Vec::new();
    webm.blocks(|block| {
        let head = block
            .head
            .ok_or_else(|| format!("{context} BlockGroup has no Block"))?;
        let (keyframe, block_context) = match block.group_keyframe {
            Some(keyframe) => (keyframe, format!("{context} BlockGroup {}", samples.len())),
            None => {
                let (_, track_width) = ebml_vint(head, 0)?;
                let flags = *head
                    .get(track_width + 2)
                    .ok_or_else(|| format!("{context} SimpleBlock is truncated"))?;
                (flags & 0x80 != 0, format!("{context} SimpleBlock {}", samples.len()))
            }
        };
        samples.push(parse_webm_block(
            head,
            block.start,
            block.length,
            input,
            block.cluster_timecode,
            timecode_scale,
            track_number,
            keyframe,
            default_duration,
            &block_context
        )?);
        Ok(())
    })?;
    fill_sample_durations(
        &mut samples,
        default_duration.unwrap_or(1_000_000),
        &context
    )?;
    Ok(WebmVideoTrack {
        codec_private,
        width,
        height,
        samples
    })
}

fn parse_webm_audio(path: &Path, input: usize) -> Result<WebmAudioTrack, String> {
    let context = format!("WebM audio input {input}");
    let mut webm = open_webm(path, &context)?;
    let timecode_scale = webm.timecode_scale(&context)?;
    let (entry_start, entry_end) = webm
        .track_entry(2)?
        .ok_or_else(|| format!("{context} contains no audio TrackEntry"))?;
    let data = &webm.tracks;
    let track_number = ebml_child_uint(data, entry_start, entry_end, 0xd7)?
        .ok_or_else(|| format!("{context} audio TrackEntry has no track number"))?;
    if track_number == 0 {
        return Err(format!("{context} audio TrackEntry has an invalid track number"));
    }
    let codec_id = ebml_child_text(
        data,
        entry_start,
        entry_end,
        0x86,
        &format!("{context} TrackEntry"),
    )?
    .ok_or_else(|| format!("{context} audio TrackEntry has no codec ID"))?;
    if !codec_id.starts_with("A_") {
        return Err(format!("{context} uses unsupported audio codec {codec_id}"));
    }
    let codec_private = ebml_child_bytes(data, entry_start, entry_end, 0x63a2)?.unwrap_or_default();
    let default_duration = ebml_child_uint(data, entry_start, entry_end, 0x23e383)?;
    let (sample_rate, channels) = if let Some((_, audio_start, audio_end)) = ebml_children(data, entry_start, entry_end)?
        .into_iter()
        .find(|(id, _, _)| *id == 0xe1)
    {
        let rate = if let Some((child_start, child_end)) = ebml_child(data, audio_start, audio_end, 0xb5)? {
            let len = child_end - child_start;
            if len == 4 {
                let bytes: [u8; 4] = data[child_start..child_end].try_into().unwrap_or([0; 4]);
                f32::from_be_bytes(bytes) as u32
            } else if len == 8 {
                let bytes: [u8; 8] = data[child_start..child_end].try_into().unwrap_or([0; 8]);
                f64::from_be_bytes(bytes) as u32
            } else {
                ebml_uint(data, child_start, child_end).unwrap_or(48000) as u32
            }
        } else {
            48000
        };
        let ch = ebml_child_uint(data, audio_start, audio_end, 0x9f)?.unwrap_or(2) as u16;
        (rate.max(1), ch.max(1))
    } else {
        (48000, 2)
    };

    let mut samples = Vec::new();
    webm.blocks(|block| {
        let Some(head) = block.head else {
            return Ok(());
        };
        let (block_track, _) = ebml_vint(head, 0)?;
        if block_track != Some(track_number) {
            return Ok(());
        }
        let block_context = match block.group_keyframe {
            Some(_) => format!("{context} BlockGroup {}", samples.len()),
            None => format!("{context} SimpleBlock {}", samples.len())
        };
        samples.push(parse_webm_block(
            head,
            block.start,
            block.length,
            input,
            block.cluster_timecode,
            timecode_scale,
            track_number,
            false,
            default_duration,
            &block_context,
        )?);
        Ok(())
    })?;
    fill_sample_durations(
        &mut samples,
        default_duration.unwrap_or(20_000_000),
        &context,
    )?;
    Ok(WebmAudioTrack {
        codec_id,
        codec_private,
        sample_rate,
        channels,
        samples,
    })
}

fn webm_track_type(path: &Path) -> Result<u64, String> {
    let webm = open_webm(path, "WebM input")?;
    let data = &webm.tracks;
    for (id, start, end) in ebml_children(data, 0, data.len())? {
        if id == 0xae {
            if let Some(track_type) = ebml_child_uint(data, start, end, 0x83)? {
                return Ok(track_type);
            }
        }
    }
    Err("No TrackEntry found in WebM".into())
}

fn mp4_descriptor_length(
    data: &[u8],
    cursor: &mut usize,
    end: usize,
    context: &str
) -> Result<usize, String> {
    let mut length = 0usize;
    for _ in 0..4 {
        let byte = *data
            .get(*cursor)
            .ok_or_else(|| format!("{context} has a truncated descriptor length"))?;
        *cursor += 1;
        length = length
            .checked_mul(128)
            .and_then(|value| value.checked_add(usize::from(byte & 0x7f)))
            .ok_or_else(|| format!("{context} descriptor length overflowed"))?;
        if byte & 0x80 == 0 {
            return Ok(length);
        }
    }
    let _ = end;
    Err(format!("{context} uses an unsupported descriptor length"))
}

fn find_mp4_descriptor(
    data: &[u8],
    start: usize,
    end: usize,
    wanted: u8,
    context: &str
) -> Result<Option<Vec<u8>>, String> {
    let mut cursor = start;
    while cursor < end {
        let tag = *data
            .get(cursor)
            .ok_or_else(|| format!("{context} descriptor tag is truncated"))?;
        cursor += 1;
        let length = mp4_descriptor_length(data, &mut cursor, end, context)?;
        let payload_end = cursor
            .checked_add(length)
            .ok_or_else(|| format!("{context} descriptor overflowed"))?;
        if payload_end > end {
            return Err(format!("{context} descriptor is truncated"));
        }
        if tag == wanted {
            return Ok(Some(data[cursor..payload_end].to_vec()));
        }
        let child_start = match tag {
            0x03 => {
                if length < 3 {
                    return Err(format!("{context} ES descriptor is truncated"));
                }
                let flags = data[cursor + 2];
                let mut child_start = cursor + 3;
                if flags & 0x80 != 0 {
                    child_start = child_start
                        .checked_add(2)
                        .ok_or_else(|| format!("{context} ES descriptor overflowed"))?;
                }
                if flags & 0x40 != 0 {
                    let url_length = usize::from(*data.get(child_start).ok_or_else(|| {
                        format!("{context} ES descriptor URL length is truncated")
                    })?);
                    child_start = child_start
                        .checked_add(1 + url_length)
                        .ok_or_else(|| format!("{context} ES descriptor overflowed"))?;
                }
                if flags & 0x20 != 0 {
                    child_start = child_start
                        .checked_add(2)
                        .ok_or_else(|| format!("{context} ES descriptor overflowed"))?;
                }
                if child_start > payload_end {
                    return Err(format!("{context} ES descriptor is truncated"));
                }
                Some(child_start)
            }
            0x04 => {
                if length < 13 {
                    return Err(format!("{context} decoder configuration is truncated"));
                }
                Some(cursor + 13)
            }
            0x06 => Some(cursor),
            _ => None
        };
        if let Some(child_start) = child_start {
            if let Some(found) =
                find_mp4_descriptor(data, child_start, payload_end, wanted, context)?
            {
                return Ok(Some(found));
            }
        }
        cursor = payload_end;
    }
    Ok(None)
}

/// Read one AAC fragment's samples. `data` is the moof box alone, read from
/// file offset `moof_offset`; the mdat payload spans `mdat_payload..mdat_end`
/// of the same file. Samples are recorded by position, not copied.
fn parse_fmp4_audio_fragment(
    data: &[u8],
    moof_offset: u64,
    mdat_payload: u64,
    mdat_end: u64,
    input: usize,
    track_id: u32,
    trex_duration: u32,
    trex_size: u32,
    timescale: u32,
    samples: &mut Vec<TimedMediaSample>,
    context: &str
) -> Result<(), String> {
    let moof = only_box(data, context)?;
    let moof_children = child_boxes(data, moof, context)?;
    let trafs = matching_boxes(&moof_children, *b"traf");
    if trafs.len() != 1 {
        return Err(format!("{context} must contain exactly one traf"));
    }
    let traf_children = child_boxes(data, trafs[0], context)?;
    let tfhd = exactly_one_box(&traf_children, *b"tfhd", context)?;
    let (version, flags, payload) = full_box_header(data, tfhd, context)?;
    let _ = version;
    if flags & !0x0002_003b != 0 {
        return Err(format!(
            "{context} uses unsupported tfhd flags 0x{flags:06x}"
        ));
    }
    let actual_track_id = read_u32_at(data, payload + 4, context)?;
    if actual_track_id != track_id {
        return Err(format!(
            "{context} references track ID {actual_track_id}, expected {track_id}"
        ));
    }
    let mut cursor = payload + 8;
    let base_data_offset = if flags & 0x1 != 0 {
        let value = read_u64_at(data, cursor, context)?;
        cursor += 8;
        value
    } else {
        moof_offset
    };
    if flags & 0x2 != 0 {
        cursor += 4;
    }
    let default_duration = if flags & 0x8 != 0 {
        let value = read_u32_at(data, cursor, context)?;
        cursor += 4;
        value
    } else {
        trex_duration
    };
    let default_size = if flags & 0x10 != 0 {
        let value = read_u32_at(data, cursor, context)?;
        cursor += 4;
        value
    } else {
        trex_size
    };
    if flags & 0x20 != 0 {
        cursor += 4;
    }
    if cursor > tfhd.end {
        return Err(format!("{context} has truncated tfhd fields"));
    }
    let tfdt = exactly_one_box(&traf_children, *b"tfdt", context)?;
    let decode_time = tfdt_decode_time(data, tfdt, context)?;
    let truns = matching_boxes(&traf_children, *b"trun");
    if truns.is_empty() {
        return Err(format!("{context} has no trun"));
    }
    let mut data_cursor = mdat_payload;
    let mut decode_cursor = decode_time;
    for (trun_index, trun) in truns.iter().enumerate() {
        let trun_context = format!("{context} trun {trun_index}");
        let (trun_version, trun_flags, trun_payload) = full_box_header(data, *trun, &trun_context)?;
        if trun_flags & !0x0000_0f05 != 0 {
            return Err(format!(
                "{trun_context} uses unsupported flags 0x{trun_flags:06x}"
            ));
        }
        let sample_count = usize::try_from(read_u32_at(data, trun_payload + 4, &trun_context)?)
            .map_err(|_| format!("{trun_context} has too many samples"))?;
        if sample_count == 0 {
            return Err(format!("{trun_context} has no samples"));
        }
        let mut sample_cursor = trun_payload + 8;
        if trun_flags & 0x1 != 0 {
            let offset =
                i32::from_be_bytes(read_u32_at(data, sample_cursor, &trun_context)?.to_be_bytes());
            sample_cursor += 4;
            data_cursor = if offset >= 0 {
                base_data_offset.checked_add(offset as u64)
            } else {
                base_data_offset.checked_sub(u64::from(offset.unsigned_abs()))
            }
            .ok_or_else(|| format!("{trun_context} data offset overflowed"))?;
        }
        if trun_flags & 0x4 != 0 {
            sample_cursor += 4;
        }
        if sample_cursor > trun.end {
            return Err(format!("{trun_context} has truncated flags"));
        }
        for _ in 0..sample_count {
            let duration = if trun_flags & 0x100 != 0 {
                let value = read_u32_at(data, sample_cursor, &trun_context)?;
                sample_cursor += 4;
                value
            } else {
                default_duration
            };
            let size = if trun_flags & 0x200 != 0 {
                let value = read_u32_at(data, sample_cursor, &trun_context)?;
                sample_cursor += 4;
                value
            } else {
                default_size
            };
            if trun_flags & 0x400 != 0 {
                sample_cursor += 4;
            }
            let composition_offset = if trun_flags & 0x800 != 0 {
                let raw = read_u32_at(data, sample_cursor, &trun_context)?;
                sample_cursor += 4;
                if trun_version == 1 {
                    i64::from(i32::from_be_bytes(raw.to_be_bytes()))
                } else {
                    i64::from(raw)
                }
            } else {
                0
            };
            if duration == 0 || size == 0 {
                return Err(format!("{trun_context} has a zero sample duration or size"));
            }
            if sample_cursor > trun.end {
                return Err(format!("{trun_context} has truncated sample fields"));
            }
            let sample_end = data_cursor
                .checked_add(u64::from(size))
                .ok_or_else(|| format!("{trun_context} sample offset overflowed"))?;
            if data_cursor < mdat_payload || sample_end > mdat_end {
                return Err(format!("{trun_context} sample exceeds mdat"));
            }
            let timestamp = i128::from(decode_cursor)
                .checked_add(i128::from(composition_offset))
                .ok_or_else(|| format!("{trun_context} timestamp overflowed"))?;
            if timestamp < 0 {
                return Err(format!("{trun_context} has a negative timestamp"));
            }
            let timestamp_ns = timestamp
                .checked_mul(1_000_000_000)
                .and_then(|value| value.checked_div(i128::from(timescale)))
                .ok_or_else(|| format!("{trun_context} timestamp overflowed"))?;
            let duration_ns = u64::try_from(
                (u128::from(duration) * 1_000_000_000u128 / u128::from(timescale)).max(1)
            )
            .map_err(|_| format!("{trun_context} duration overflowed"))?;
            samples.push(TimedMediaSample {
                timestamp_ns,
                duration_ns,
                bytes: SampleBytes {
                    input,
                    offset: data_cursor,
                    length: u64::from(size)
                },
                keyframe: false
            });
            data_cursor = sample_end;
            decode_cursor = decode_cursor
                .checked_add(u64::from(duration))
                .ok_or_else(|| format!("{trun_context} decode time overflowed"))?;
        }
        if sample_cursor > trun.end {
            return Err(format!("{trun_context} has truncated sample fields"));
        }
    }
    Ok(())
}

fn parse_fmp4_audio(path: &Path, input: usize) -> Result<Fmp4AudioTrack, String> {
    let context = format!("fMP4 input {input}");
    let mut parsed = parse_fragmented_track(path, input)?;
    if let Some(kind) = parsed.auxiliary_box {
        return Err(format!(
            "{context} contains unsupported top-level box {}",
            String::from_utf8_lossy(&kind)
        ));
    }
    let data = parsed.moov.as_slice();
    let moov_children = child_boxes(data, only_box(data, &context)?, &context)?;
    let trak = exactly_one_box(&moov_children, *b"trak", &context)?;
    let trak_children = child_boxes(data, trak, &context)?;
    let mdia = exactly_one_box(&trak_children, *b"mdia", &context)?;
    let mdia_children = child_boxes(data, mdia, &context)?;
    let mdhd = exactly_one_box(&mdia_children, *b"mdhd", &context)?;
    let timescale = mdhd_timescale(data, mdhd, &context)?;
    let minf = exactly_one_box(&mdia_children, *b"minf", &context)?;
    let minf_children = child_boxes(data, minf, &context)?;
    let stbl = exactly_one_box(&minf_children, *b"stbl", &context)?;
    let stbl_children = child_boxes(data, stbl, &context)?;
    let stsd = exactly_one_box(&stbl_children, *b"stsd", &context)?;
    let stsd_payload = stsd.payload_start();
    let entry_count = read_u32_at(data, stsd_payload + 4, &context)?;
    if entry_count != 1 {
        return Err(format!("{context} must contain exactly one sample entry"));
    }
    let entries = parse_mp4_boxes(data, stsd_payload + 8, stsd.end, &context)?;
    if entries.len() != 1 || entries[0].kind != *b"mp4a" {
        return Err(format!(
            "{context} does not contain an AAC mp4a sample entry"
        ));
    }
    let entry = entries[0];
    let fields = entry.payload_start();
    if fields + 28 > entry.end {
        return Err(format!("{context} has a truncated mp4a sample entry"));
    }
    let channels = u16::from_be_bytes([data[fields + 16], data[fields + 17]]);
    let sample_rate = read_u32_at(data, fields + 24, &context)? >> 16;
    if channels == 0 || sample_rate == 0 {
        return Err(format!("{context} has invalid AAC audio metadata"));
    }
    let entry_children = parse_mp4_boxes(data, fields + 28, entry.end, &context)?;
    let esds = exactly_one_box(&entry_children, *b"esds", &context)?;
    let esds_start = esds
        .payload_start()
        .checked_add(4)
        .ok_or_else(|| format!("{context} has an invalid esds"))?;
    let codec_private = find_mp4_descriptor(data, esds_start, esds.end, 0x05, &context)?
        .ok_or_else(|| format!("{context} AAC AudioSpecificConfig is missing"))?;
    if codec_private.is_empty() {
        return Err(format!("{context} AAC AudioSpecificConfig is empty"));
    }
    let mvex = exactly_one_box(&moov_children, *b"mvex", &context)?;
    let trex = exactly_one_box(&child_boxes(data, mvex, &context)?, *b"trex", &context)?;
    let (_, _, trex_payload) = full_box_header(data, trex, &context)?;
    let trex_track_id = read_u32_at(data, trex_payload + 4, &context)?;
    if trex_track_id != parsed.track_id {
        return Err(format!(
            "{context} trex track ID does not match the media track"
        ));
    }
    let trex_duration = read_u32_at(data, trex_payload + 12, &context)?;
    let trex_size = read_u32_at(data, trex_payload + 16, &context)?;
    let mut samples = Vec::new();
    for fragment in &parsed.fragments {
        let fragment_context = format!("{context} fragment {}", samples.len());
        let moof = file_bytes(
            &mut parsed.file,
            fragment.moof_offset,
            fragment.moof_size,
            &fragment_context
        )?;
        parse_fmp4_audio_fragment(
            &moof,
            fragment.moof_offset,
            fragment.mdat_offset + fragment.mdat_header,
            fragment.mdat_offset + fragment.mdat_size,
            input,
            parsed.track_id,
            trex_duration,
            trex_size,
            timescale,
            &mut samples,
            &fragment_context
        )?;
    }
    if samples.is_empty() {
        return Err(format!("{context} has incomplete media fragments"));
    }
    samples.sort_by_key(|sample| sample.timestamp_ns);
    Ok(Fmp4AudioTrack {
        codec_private,
        sample_rate,
        channels,
        samples
    })
}

fn ebml_size(value: usize) -> Result<Vec<u8>, String> {
    let value = u64::try_from(value).map_err(|_| "EBML element is too large".to_string())?;
    for width in 1..=8usize {
        let max = (1u64 << (7 * width)).saturating_sub(2);
        if value <= max {
            let encoded = (1u64 << (7 * width)) | value;
            let mut bytes = vec![0u8; width];
            for index in 0..width {
                bytes[width - 1 - index] = (encoded >> (index * 8)) as u8;
            }
            return Ok(bytes);
        }
    }
    Err("EBML element is too large".into())
}

fn ebml_element(id: &[u8], payload: &[u8]) -> Result<Vec<u8>, String> {
    let size = ebml_size(payload.len())?;
    let mut output = Vec::with_capacity(id.len() + size.len() + payload.len());
    output.extend_from_slice(id);
    output.extend_from_slice(&size);
    output.extend_from_slice(payload);
    Ok(output)
}

fn ebml_uint_bytes(value: u64) -> Vec<u8> {
    let width = if value == 0 {
        1
    } else {
        ((64 - value.leading_zeros()) as usize).div_ceil(8)
    };
    value.to_be_bytes()[8 - width..].to_vec()
}

fn ebml_uint_element(id: &[u8], value: u64) -> Result<Vec<u8>, String> {
    ebml_element(id, &ebml_uint_bytes(value))
}

fn ebml_text_element(id: &[u8], value: &str) -> Result<Vec<u8>, String> {
    ebml_element(id, value.as_bytes())
}

fn ebml_float_element(id: &[u8], value: f64) -> Result<Vec<u8>, String> {
    ebml_element(id, &value.to_be_bytes())
}

fn ebml_track_number(value: u64) -> Result<Vec<u8>, String> {
    if value == 0 {
        return Err("Matroska track number cannot be zero".into());
    }
    for width in 1..=8usize {
        let max = (1u64 << (7 * width)).saturating_sub(2);
        if value <= max {
            let encoded = (1u64 << (7 * width)) | value;
            let mut bytes = vec![0u8; width];
            for index in 0..width {
                bytes[width - 1 - index] = (encoded >> (index * 8)) as u8;
            }
            return Ok(bytes);
        }
    }
    Err("Matroska track number is too large".into())
}

fn matroska_track_entry(
    track_number: u64,
    track_type: u64,
    codec_id: &str,
    codec_private: &[u8],
    default_duration: u64,
    video: Option<(u64, u64)>,
    audio: Option<(u32, u16)>
) -> Result<Vec<u8>, String> {
    let mut children = Vec::new();
    children.push(ebml_uint_element(&[0xd7], track_number)?);
    children.push(ebml_uint_element(&[0x73, 0xc5], track_number)?);
    children.push(ebml_uint_element(&[0x83], track_type)?);
    children.push(ebml_uint_element(&[0x9c], 0)?);
    children.push(ebml_text_element(&[0x86], codec_id)?);
    if !codec_private.is_empty() {
        children.push(ebml_element(&[0x63, 0xa2], codec_private)?);
    }
    if default_duration > 0 {
        children.push(ebml_uint_element(&[0x23, 0xe3, 0x83], default_duration)?);
    }
    if let Some((width, height)) = video {
        let video = [
            ebml_uint_element(&[0xb0], width)?,
            ebml_uint_element(&[0xba], height)?
        ]
        .concat();
        children.push(ebml_element(&[0xe0], &video)?);
    }
    if let Some((sample_rate, channels)) = audio {
        let audio = [
            ebml_float_element(&[0xb5], f64::from(sample_rate))?,
            ebml_uint_element(&[0x9f], u64::from(channels))?
        ]
        .concat();
        children.push(ebml_element(&[0xe1], &audio)?);
    }
    ebml_element(&[0xae], &children.concat())
}

/// Write one VP9 video track and one audio track as Matroska. Every element
/// size is computed from the sample positions first, then the file is written
/// front to back with each sample copied from its input (F13).
fn write_matroska(
    video: WebmVideoTrack,
    audio: AudioTrackInfo,
    files: &mut [std::fs::File],
    output: &mut MuxOutput
) -> Result<(), String> {
    let first_timestamp = video
        .samples
        .iter()
        .chain(audio.samples.iter())
        .map(|sample| sample.timestamp_ns)
        .min()
        .ok_or_else(|| "The mixed media has no timestamps".to_string())?;
    let duration_end = video
        .samples
        .iter()
        .chain(audio.samples.iter())
        .map(|sample| {
            sample
                .timestamp_ns
                .saturating_add(i128::from(sample.duration_ns))
        })
        .max()
        .ok_or_else(|| "The mixed media has no duration".to_string())?;
    let duration_ms = duration_end.saturating_sub(first_timestamp) as f64 / 1_000_000.0;
    let mut events = Vec::with_capacity(video.samples.len() + audio.samples.len());
    let video_default_duration = video
        .samples
        .first()
        .map(|sample| sample.duration_ns)
        .unwrap_or(0);
    let audio_default_duration = audio
        .samples
        .first()
        .map(|sample| sample.duration_ns)
        .unwrap_or(0);
    for sample in video.samples {
        let relative = sample
            .timestamp_ns
            .checked_sub(first_timestamp)
            .ok_or_else(|| "The video timestamp underflowed".to_string())?;
        let timestamp_ms = u64::try_from((relative + 500_000) / 1_000_000)
            .map_err(|_| "The video timestamp is too large".to_string())?;
        events.push((timestamp_ms, 1u64, sample.keyframe, sample.bytes));
    }
    for sample in audio.samples {
        let relative = sample
            .timestamp_ns
            .checked_sub(first_timestamp)
            .ok_or_else(|| "The audio timestamp underflowed".to_string())?;
        let timestamp_ms = u64::try_from((relative + 500_000) / 1_000_000)
            .map_err(|_| "The audio timestamp is too large".to_string())?;
        events.push((timestamp_ms, 2u64, false, sample.bytes));
    }
    events.sort_by(|left, right| left.0.cmp(&right.0).then_with(|| left.1.cmp(&right.1)));
    let mut clusters: Vec<(u64, Vec<(u64, u64, bool, SampleBytes)>)> = Vec::new();
    for event in events {
        let start_new = clusters
            .last()
            .is_none_or(|(base, _)| event.0 < *base || event.0 - *base > 5_000);
        if start_new {
            clusters.push((event.0, Vec::new()));
        }
        clusters.last_mut().unwrap().1.push(event);
    }
    // Size every Cluster before writing: an EBML element's size precedes it.
    let block_length = |track_number: u64, bytes: &SampleBytes| -> Result<usize, String> {
        usize::try_from(bytes.length)
            .ok()
            .and_then(|length| length.checked_add(ebml_track_number(track_number).ok()?.len() + 3))
            .ok_or_else(|| "The Matroska block is too large".to_string())
    };
    let mut cluster_payloads = Vec::with_capacity(clusters.len());
    for (base, blocks) in &clusters {
        let mut payload = ebml_uint_element(&[0xe7], *base)?.len();
        for (timestamp, track_number, _, bytes) in blocks {
            let relative = timestamp
                .checked_sub(*base)
                .ok_or_else(|| "The Matroska block timestamp underflowed".to_string())?;
            if relative > i64::from(i16::MAX as u16) as u64 {
                return Err("The Matroska block timestamp is out of range".into());
            }
            i16::try_from(relative).map_err(|_| "The Matroska block timestamp is out of range")?;
            let length = block_length(*track_number, bytes)?;
            payload = payload
                .checked_add(1 + ebml_size(length)?.len() + length)
                .ok_or_else(|| "The Matroska cluster is too large".to_string())?;
        }
        cluster_payloads.push(payload);
    }
    let info = [
        ebml_uint_element(&[0x2a, 0xd7, 0xb1], 1_000_000)?,
        ebml_float_element(&[0x44, 0x89], duration_ms)?,
        ebml_text_element(&[0x4d, 0x80], "Download Manager")?,
        ebml_text_element(&[0x57, 0x41], "Download Manager")?
    ]
    .concat();
    let tracks = [
        matroska_track_entry(
            1,
            1,
            "V_VP9",
            &video.codec_private,
            video_default_duration,
            Some((video.width, video.height)),
            None
        )?,
        matroska_track_entry(
            2,
            2,
            &audio.codec_id,
            &audio.codec_private,
            audio_default_duration,
            None,
            Some((audio.sample_rate, audio.channels))
        )?
    ]
    .concat();
    let info = ebml_element(&[0x15, 0x49, 0xa9, 0x66], &info)?;
    let tracks = ebml_element(&[0x16, 0x54, 0xae, 0x6b], &tracks)?;
    let mut segment_length = info.len() + tracks.len();
    for payload in &cluster_payloads {
        segment_length = segment_length
            .checked_add(4 + ebml_size(*payload)?.len() + payload)
            .ok_or_else(|| "The Matroska segment is too large".to_string())?;
    }
    let ebml_header = [
        ebml_uint_element(&[0x42, 0x86], 1)?,
        ebml_uint_element(&[0x42, 0xf7], 1)?,
        ebml_uint_element(&[0x42, 0xf2], 4)?,
        ebml_uint_element(&[0x42, 0xf3], 8)?,
        ebml_text_element(&[0x42, 0x82], "matroska")?,
        ebml_uint_element(&[0x42, 0x87], 4)?,
        ebml_uint_element(&[0x42, 0x85], 2)?
    ]
    .concat();
    output.write(&ebml_element(&[0x1a, 0x45, 0xdf, 0xa3], &ebml_header)?)?;
    output.write(&[0x18, 0x53, 0x80, 0x67])?;
    output.write(&ebml_size(segment_length)?)?;
    output.write(&info)?;
    output.write(&tracks)?;
    for ((base, blocks), payload) in clusters.iter().zip(cluster_payloads) {
        output.write(&[0x1f, 0x43, 0xb6, 0x75])?;
        output.write(&ebml_size(payload)?)?;
        output.write(&ebml_uint_element(&[0xe7], *base)?)?;
        for (timestamp, track_number, keyframe, bytes) in blocks {
            let length = block_length(*track_number, bytes)?;
            let mut header = vec![0xa3];
            header.extend_from_slice(&ebml_size(length)?);
            header.extend_from_slice(&ebml_track_number(*track_number)?);
            header.extend_from_slice(&((timestamp - base) as i16).to_be_bytes());
            header.push(if *track_number == 1 && *keyframe {
                0x80
            } else {
                0
            });
            output.write(&header)?;
            let file = files
                .get_mut(bytes.input)
                .ok_or_else(|| "A media sample refers to an unknown track".to_string())?;
            output.copy_from(file, bytes.offset, bytes.length)?;
        }
    }
    Ok(())
}

fn open_track_files(paths: [&PathBuf; 2]) -> Result<[std::fs::File; 2], String> {
    let open = |path: &PathBuf| {
        std::fs::File::open(path).map_err(|error| format!("A media track could not be opened: {error}"))
    };
    Ok([open(paths[0])?, open(paths[1])?])
}

/// Mux one VP9/WebM track and one AAC/fMP4 track without external tools.
fn mux_webm_fmp4_tracks(inputs: &[PathBuf], output: &mut MuxOutput) -> Result<(), String> {
    if inputs.len() != 2 {
        return Err("Mixed WebM/fMP4 muxing requires exactly two tracks".into());
    }
    let (webm, fmp4) = if track_file_kind(&inputs[0])? == TrackFileKind::Webm {
        (&inputs[0], &inputs[1])
    } else if track_file_kind(&inputs[1])? == TrackFileKind::Webm {
        (&inputs[1], &inputs[0])
    } else {
        return Err("Mixed media inputs contain no WebM track".into());
    };
    let video = parse_webm_video(webm, 0)?;
    let audio = parse_fmp4_audio(fmp4, 1)?;
    let mut files = open_track_files([webm, fmp4])?;
    write_matroska(
        video,
        AudioTrackInfo {
            codec_id: "A_AAC".into(),
            codec_private: audio.codec_private,
            sample_rate: audio.sample_rate,
            channels: audio.channels,
            samples: audio.samples,
        },
        &mut files,
        output
    )
}

/// Mux one VP9/WebM video track and one WebM audio track (e.g. Opus) without external tools.
fn mux_webm_webm_tracks(inputs: &[PathBuf], output: &mut MuxOutput) -> Result<(), String> {
    if inputs.len() != 2 {
        return Err("WebM/WebM muxing requires exactly two tracks".into());
    }
    let first_type = webm_track_type(&inputs[0])?;
    let second_type = webm_track_type(&inputs[1])?;
    let (video_path, audio_path) = if first_type == 1 && second_type == 2 {
        (&inputs[0], &inputs[1])
    } else if first_type == 2 && second_type == 1 {
        (&inputs[1], &inputs[0])
    } else {
        return Err("WebM tracks must contain one video track and one audio track".into());
    };
    let video = parse_webm_video(video_path, 0)?;
    let audio = parse_webm_audio(audio_path, 1)?;
    let mut files = open_track_files([video_path, audio_path])?;
    write_matroska(
        video,
        AudioTrackInfo {
            codec_id: audio.codec_id,
            codec_private: audio.codec_private,
            sample_rate: audio.sample_rate,
            channels: audio.channels,
            samples: audio.samples,
        },
        &mut files,
        output
    )
}

// ---- regular MP4 -----------------------------------------------------------
//
// Segmented media arrives as fragmented MP4: a movie header without sample
// tables, then moof/mdat pairs. Browsers play that, but desktop players seek
// and buffer it poorly: nothing indexes where each frame lies. The finished
// file is written as a regular MP4 instead: one moov with full sample tables,
// ahead of one mdat (fast start). Sample bytes are copied as they are;
// nothing is decoded or re-encoded.

/// Timescale of the written movie header, track headers and edit lists.
const MOVIE_TIMESCALE: u64 = 1000;
/// A chunk (samples stored together) spans at most 1/CHUNKS_PER_SECOND s of
/// media, so the tracks interleave finely enough to play from disk.
const CHUNKS_PER_SECOND: u64 = 2;

struct RegularSample {
    size: u32,
    duration: u32,
    composition: i64,
    sync: bool
}

struct RegularChunk {
    input: usize,
    offset: u64,
    length: u64,
    samples: u32,
    description: u32,
    decode_time: u64
}

#[derive(Clone, Copy)]
struct TrackDefaults {
    description: u32,
    duration: u32,
    size: u32,
    flags: u32
}

struct RegularTrack {
    input: usize,
    source_id: u32,
    timescale: u32,
    /// The source's trak box (sample description, handler, edit list).
    trak: Vec<u8>,
    defaults: TrackDefaults,
    samples: Vec<RegularSample>,
    chunks: Vec<RegularChunk>,
    first_decode_time: Option<u64>,
    next_decode_time: u64
}

impl RegularTrack {
    /// A fragment's own decode time. A gap after the previous fragment
    /// lengthens the last sample, which keeps every later sample in place.
    fn place_at(&mut self, decode_time: u64) {
        if self.samples.is_empty() {
            self.next_decode_time = decode_time;
        } else if decode_time > self.next_decode_time {
            let gap = decode_time - self.next_decode_time;
            if let Some(last) = self.samples.last_mut() {
                last.duration = u32::try_from(u64::from(last.duration) + gap).unwrap_or(u32::MAX);
            }
            self.next_decode_time = decode_time;
        }
    }
}

fn movie_time(value: u64, timescale: u32) -> u64 {
    u64::try_from(u128::from(value) * u128::from(MOVIE_TIMESCALE) / u128::from(timescale.max(1))).unwrap_or(u64::MAX)
}

/// Read every track of one fragmented-MP4 file into `tracks`.
fn regular_tracks_of(input: usize, path: &Path, tracks: &mut Vec<RegularTrack>) -> Result<(), String> {
    let context = format!("Media track {input}");
    let (mut file, total) = open_media_file(path, &context)?;
    let first = tracks.len();
    let mut has_movie = false;
    let mut cursor = 0u64;
    while cursor < total {
        let (kind, size, _) = file_box_header(&mut file, cursor, total, &context)?;
        match &kind {
            b"moov" => {
                if has_movie {
                    return Err(format!("{context} has more than one movie header"));
                }
                has_movie = true;
                let bytes = file_bytes(&mut file, cursor, size, &context)?;
                regular_movie(&bytes, input, &context, tracks)?;
            }
            b"moof" => {
                if !has_movie {
                    return Err(format!("{context} has a fragment before its movie header"));
                }
                let bytes = file_bytes(&mut file, cursor, size, &context)?;
                regular_fragment(&bytes, cursor, total, &context, &mut tracks[first..])?;
            }
            _ => {}
        }
        cursor += size;
    }
    if !has_movie {
        return Err(format!("{context} has no movie header"));
    }
    Ok(())
}

fn regular_movie(data: &[u8], input: usize, context: &str, tracks: &mut Vec<RegularTrack>) -> Result<(), String> {
    let moov = only_box(data, context)?;
    let children = child_boxes(data, moov, context)?;
    let mut defaults = Vec::new();
    for mvex in matching_boxes(&children, *b"mvex") {
        for trex in matching_boxes(&child_boxes(data, mvex, context)?, *b"trex") {
            let (_, _, payload) = full_box_header(data, trex, context)?;
            defaults.push((
                read_u32_at(data, payload + 4, context)?,
                TrackDefaults {
                    description: read_u32_at(data, payload + 8, context)?,
                    duration: read_u32_at(data, payload + 12, context)?,
                    size: read_u32_at(data, payload + 16, context)?,
                    flags: read_u32_at(data, payload + 20, context)?
                }
            ));
        }
    }
    let traks = matching_boxes(&children, *b"trak");
    if traks.is_empty() {
        return Err(format!("{context} has a movie header without a track"));
    }
    for trak in traks {
        let trak_children = child_boxes(data, trak, context)?;
        let tkhd = exactly_one_box(&trak_children, *b"tkhd", context)?;
        let source_id = track_id_from_tkhd(data, tkhd, context)?;
        let mdia = exactly_one_box(&trak_children, *b"mdia", context)?;
        let mdhd = exactly_one_box(&child_boxes(data, mdia, context)?, *b"mdhd", context)?;
        tracks.push(RegularTrack {
            input,
            source_id,
            timescale: mdhd_timescale(data, mdhd, context)?,
            trak: box_bytes(data, trak).to_vec(),
            defaults: defaults
                .iter()
                .find(|(id, _)| *id == source_id)
                .map(|(_, value)| *value)
                .unwrap_or(TrackDefaults { description: 1, duration: 0, size: 0, flags: 0 }),
            samples: Vec::new(),
            chunks: Vec::new(),
            first_decode_time: None,
            next_decode_time: 0
        });
    }
    Ok(())
}

/// Add one fragment's samples to their tracks. `moof_offset` is where the
/// fragment lies in its file; sample data positions are resolved against it
/// as ISO/IEC 14496-12 defines (base data offset, default-base-is-moof, or
/// the end of the previous track fragment's data).
fn regular_fragment(data: &[u8], moof_offset: u64, file_length: u64, context: &str, tracks: &mut [RegularTrack]) -> Result<(), String> {
    let moof = only_box(data, context)?;
    let mut previous_end: Option<u64> = None;
    for traf in matching_boxes(&child_boxes(data, moof, context)?, *b"traf") {
        let children = child_boxes(data, traf, context)?;
        let tfhd = exactly_one_box(&children, *b"tfhd", context)?;
        let (_, flags, payload) = full_box_header(data, tfhd, context)?;
        let id = read_u32_at(data, payload + 4, context)?;
        let track = tracks
            .iter_mut()
            .find(|track| track.source_id == id)
            .ok_or_else(|| format!("{context} has a fragment for unknown track {id}"))?;
        let mut cursor = payload + 8;
        let base_data_offset = if flags & 0x1 != 0 {
            let value = read_u64_at(data, cursor, context)?;
            cursor += 8;
            Some(value)
        } else {
            None
        };
        let mut defaults = track.defaults;
        for (flag, slot) in [
            (0x2, &mut defaults.description),
            (0x8, &mut defaults.duration),
            (0x10, &mut defaults.size),
            (0x20, &mut defaults.flags)
        ] {
            if flags & flag != 0 {
                *slot = read_u32_at(data, cursor, context)?;
                cursor += 4;
            }
        }
        let base = match base_data_offset {
            Some(value) => value,
            None if flags & 0x2_0000 != 0 => moof_offset,
            None => previous_end.unwrap_or(moof_offset)
        };
        if let Some(tfdt) = matching_boxes(&children, *b"tfdt").first() {
            track.place_at(tfdt_decode_time(data, *tfdt, context)?);
        }
        let mut position = base;
        for trun in matching_boxes(&children, *b"trun") {
            let (version, run_flags, payload) = full_box_header(data, trun, context)?;
            let count = read_u32_at(data, payload + 4, context)?;
            let mut cursor = payload + 8;
            if run_flags & 0x1 != 0 {
                let offset = read_u32_at(data, cursor, context)? as i32;
                cursor += 4;
                position = base
                    .checked_add_signed(i64::from(offset))
                    .ok_or_else(|| format!("{context} has a sample run outside its file"))?;
            }
            let first_flags = if run_flags & 0x4 != 0 {
                let value = read_u32_at(data, cursor, context)?;
                cursor += 4;
                Some(value)
            } else {
                None
            };
            let fields = [0x100, 0x200, 0x400, 0x800].iter().filter(|flag| run_flags & **flag != 0).count();
            let table = (fields * 4)
                .checked_mul(count as usize)
                .and_then(|length| length.checked_add(cursor))
                .ok_or_else(|| format!("{context} has an oversized sample run"))?;
            if table > trun.end {
                return Err(format!("{context} has a truncated sample run"));
            }
            let mut field = |present: bool| -> Option<u32> {
                present.then(|| {
                    let value = u32::from_be_bytes(data[cursor..cursor + 4].try_into().unwrap());
                    cursor += 4;
                    value
                })
            };
            let (mut chunk_start, mut chunk_length, mut chunk_samples, mut chunk_time) = (position, 0u64, 0u32, 0u64);
            let mut chunk_decode = track.next_decode_time;
            for index in 0..count {
                let duration = field(run_flags & 0x100 != 0).unwrap_or(defaults.duration);
                let size = field(run_flags & 0x200 != 0).unwrap_or(defaults.size);
                let sample_flags = field(run_flags & 0x400 != 0)
                    .or(if index == 0 { first_flags } else { None })
                    .unwrap_or(defaults.flags);
                let composition = field(run_flags & 0x800 != 0)
                    .map(|raw| if version == 0 { i64::from(raw) } else { i64::from(raw as i32) })
                    .unwrap_or(0);
                if track.first_decode_time.is_none() {
                    track.first_decode_time = Some(track.next_decode_time);
                }
                track.samples.push(RegularSample { size, duration, composition, sync: sample_flags & 0x0001_0000 == 0 });
                track.next_decode_time += u64::from(duration);
                chunk_length += u64::from(size);
                chunk_samples += 1;
                chunk_time += u64::from(duration);
                if chunk_time * CHUNKS_PER_SECOND >= u64::from(track.timescale) || index + 1 == count {
                    if chunk_start.checked_add(chunk_length).is_none_or(|end| end > file_length) {
                        return Err(format!("{context} has sample data outside its file"));
                    }
                    track.chunks.push(RegularChunk {
                        input: track.input,
                        offset: chunk_start,
                        length: chunk_length,
                        samples: chunk_samples,
                        description: defaults.description,
                        decode_time: chunk_decode
                    });
                    chunk_start += chunk_length;
                    chunk_decode = track.next_decode_time;
                    (chunk_length, chunk_samples, chunk_time) = (0, 0, 0);
                }
            }
            position = chunk_start;
        }
        previous_end = Some(position);
    }
    Ok(())
}

fn plain_box(kind: &[u8; 4], payload: &[u8]) -> Vec<u8> {
    let mut output = Vec::with_capacity(payload.len() + 8);
    output.extend_from_slice(&((payload.len() + 8) as u32).to_be_bytes());
    output.extend_from_slice(kind);
    output.extend_from_slice(payload);
    output
}

fn full_box(kind: &[u8; 4], version: u8, flags: u32, payload: &[u8]) -> Vec<u8> {
    let mut body = Vec::with_capacity(payload.len() + 4);
    body.push(version);
    body.extend_from_slice(&flags.to_be_bytes()[1..]);
    body.extend_from_slice(payload);
    plain_box(kind, &body)
}

/// Runs of equal values as (count, value).
fn runs<T: PartialEq + Copy>(values: impl Iterator<Item = T>) -> Vec<(u32, T)> {
    let mut output: Vec<(u32, T)> = Vec::new();
    for value in values {
        match output.last_mut() {
            Some((count, last)) if *last == value => *count += 1,
            _ => output.push((1, value))
        }
    }
    output
}

fn regular_sample_tables(track: &RegularTrack, stsd: &[u8], offsets: &[u64], wide: bool) -> Vec<u8> {
    let mut stbl = stsd.to_vec();
    let entries = |items: &[(u32, u32)]| -> Vec<u8> {
        let mut payload = (items.len() as u32).to_be_bytes().to_vec();
        for (count, value) in items {
            payload.extend_from_slice(&count.to_be_bytes());
            payload.extend_from_slice(&value.to_be_bytes());
        }
        payload
    };
    let durations = runs(track.samples.iter().map(|sample| sample.duration));
    stbl.extend(full_box(b"stts", 0, 0, &entries(&durations)));
    if track.samples.iter().any(|sample| sample.composition != 0) {
        let negative = track.samples.iter().any(|sample| sample.composition < 0);
        let offsets = runs(track.samples.iter().map(|sample| sample.composition as i32 as u32));
        stbl.extend(full_box(b"ctts", u8::from(negative), 0, &entries(&offsets)));
    }
    if !track.samples.iter().all(|sample| sample.sync) {
        let sync: Vec<u32> = (1..).zip(&track.samples).filter(|(_, sample)| sample.sync).map(|(number, _)| number).collect();
        let mut payload = (sync.len() as u32).to_be_bytes().to_vec();
        sync.iter().for_each(|number| payload.extend_from_slice(&number.to_be_bytes()));
        stbl.extend(full_box(b"stss", 0, 0, &payload));
    }
    let mut stsc = Vec::new();
    let mut chunk_number = 1u32;
    for (count, (samples, description)) in runs(track.chunks.iter().map(|chunk| (chunk.samples, chunk.description))) {
        stsc.extend_from_slice(&chunk_number.to_be_bytes());
        stsc.extend_from_slice(&samples.to_be_bytes());
        stsc.extend_from_slice(&description.to_be_bytes());
        chunk_number += count;
    }
    let mut payload = (stsc.len() as u32 / 12).to_be_bytes().to_vec();
    payload.extend(stsc);
    stbl.extend(full_box(b"stsc", 0, 0, &payload));
    let mut payload = 0u32.to_be_bytes().to_vec();
    payload.extend_from_slice(&(track.samples.len() as u32).to_be_bytes());
    track.samples.iter().for_each(|sample| payload.extend_from_slice(&sample.size.to_be_bytes()));
    stbl.extend(full_box(b"stsz", 0, 0, &payload));
    let mut payload = (offsets.len() as u32).to_be_bytes().to_vec();
    for offset in offsets {
        if wide {
            payload.extend_from_slice(&offset.to_be_bytes());
        } else {
            payload.extend_from_slice(&(*offset as u32).to_be_bytes());
        }
    }
    stbl.extend(full_box(if wide { b"co64" } else { b"stco" }, 0, 0, &payload));
    plain_box(b"stbl", &stbl)
}

/// Where the source's edit list starts presentation in the media (an encoder
/// delay, the first composition offset), or 0.
fn source_media_start(data: &[u8], trak_children: &[Mp4Box], context: &str) -> Result<i64, String> {
    let Some(edts) = matching_boxes(trak_children, *b"edts").first().copied() else {
        return Ok(0);
    };
    let Some(elst) = matching_boxes(&child_boxes(data, edts, context)?, *b"elst").first().copied() else {
        return Ok(0);
    };
    let (version, _, payload) = full_box_header(data, elst, context)?;
    let count = read_u32_at(data, payload + 4, context)? as usize;
    let width = if version == 1 { 20 } else { 12 };
    for index in 0..count {
        let entry = payload + 8 + index * width;
        let media_time = if version == 1 {
            read_u64_at(data, entry + 8, context)? as i64
        } else {
            i64::from(read_u32_at(data, entry + 4, context)? as i32)
        };
        if media_time >= 0 {
            return Ok(media_time);
        }
    }
    Ok(0)
}

/// One track of the written file, and how long it presents (movie timescale).
fn regular_trak(track: &RegularTrack, id: u32, offsets: &[u64], delay: u64, wide: bool) -> Result<(Vec<u8>, u64), String> {
    let context = "Media track header";
    let data = track.trak.as_slice();
    let trak = only_box(data, context)?;
    let children = child_boxes(data, trak, context)?;
    let media_duration: u64 = track.samples.iter().map(|sample| u64::from(sample.duration)).sum();
    let media_start = source_media_start(data, &children, context)?;
    let shown = movie_time(media_duration.saturating_sub(media_start.max(0) as u64), track.timescale);
    let presented = delay + shown;

    let tkhd = exactly_one_box(&children, *b"tkhd", context)?;
    let mut tkhd_bytes = box_bytes(data, tkhd).to_vec();
    let (version, _, payload) = full_box_header(data, tkhd, context)?;
    let payload = payload - tkhd.start;
    tkhd_bytes[payload + 3] |= 0x3; // enabled, in movie
    if version == 1 {
        patch_u32(&mut tkhd_bytes, payload + 20, id, context)?;
        patch_u64(&mut tkhd_bytes, payload + 28, presented, context)?;
    } else {
        patch_u32(&mut tkhd_bytes, payload + 12, id, context)?;
        patch_u32(&mut tkhd_bytes, payload + 20, u32::try_from(presented).unwrap_or(u32::MAX), context)?;
    }
    let mut parts = tkhd_bytes;

    // A track that starts after the earliest one keeps its offset as an empty
    // edit; the source's own start in its media is kept.
    if delay > 0 || media_start != 0 {
        let mut edits: Vec<(u64, i64)> = Vec::new();
        if delay > 0 {
            edits.push((delay, -1));
        }
        edits.push((shown, media_start));
        let mut payload = (edits.len() as u32).to_be_bytes().to_vec();
        for (duration, media_time) in &edits {
            payload.extend_from_slice(&duration.to_be_bytes());
            payload.extend_from_slice(&media_time.to_be_bytes());
            payload.extend_from_slice(&0x0001_0000u32.to_be_bytes());
        }
        parts.extend(plain_box(b"edts", &full_box(b"elst", 1, 0, &payload)));
    }

    let mdia = exactly_one_box(&children, *b"mdia", context)?;
    let mut mdia_parts = Vec::new();
    for child in child_boxes(data, mdia, context)? {
        match &child.kind {
            b"mdhd" => {
                let (version, _, payload) = full_box_header(data, child, context)?;
                let language = read_u32_at(data, if version == 1 { payload + 32 } else { payload + 20 }, context)?;
                let mut body = Vec::new();
                if let Ok(duration) = u32::try_from(media_duration) {
                    body.extend_from_slice(&[0; 8]);
                    body.extend_from_slice(&track.timescale.to_be_bytes());
                    body.extend_from_slice(&duration.to_be_bytes());
                    body.extend_from_slice(&language.to_be_bytes());
                    mdia_parts.extend(full_box(b"mdhd", 0, 0, &body));
                } else {
                    body.extend_from_slice(&[0; 16]);
                    body.extend_from_slice(&track.timescale.to_be_bytes());
                    body.extend_from_slice(&media_duration.to_be_bytes());
                    body.extend_from_slice(&language.to_be_bytes());
                    mdia_parts.extend(full_box(b"mdhd", 1, 0, &body));
                }
            }
            b"minf" => {
                let mut minf = Vec::new();
                for item in child_boxes(data, child, context)? {
                    if item.kind == *b"stbl" {
                        let stsd = exactly_one_box(&child_boxes(data, item, context)?, *b"stsd", context)?;
                        minf.extend(regular_sample_tables(track, box_bytes(data, stsd), offsets, wide));
                    } else {
                        minf.extend_from_slice(box_bytes(data, item));
                    }
                }
                mdia_parts.extend(plain_box(b"minf", &minf));
            }
            _ => mdia_parts.extend_from_slice(box_bytes(data, child))
        }
    }
    parts.extend(plain_box(b"mdia", &mdia_parts));
    Ok((plain_box(b"trak", &parts), presented))
}

fn regular_moov(tracks: &[RegularTrack], offsets: &[Vec<u64>], delays: &[u64], wide: bool) -> Result<Vec<u8>, String> {
    let mut traks = Vec::new();
    let mut longest = 0u64;
    for (index, track) in tracks.iter().enumerate() {
        let (trak, presented) = regular_trak(track, index as u32 + 1, &offsets[index], delays[index], wide)?;
        longest = longest.max(presented);
        traks.extend(trak);
    }
    let mut mvhd = Vec::new();
    let version = u8::from(longest > u64::from(u32::MAX));
    mvhd.extend_from_slice(&vec![0; if version == 1 { 16 } else { 8 }]);
    mvhd.extend_from_slice(&(MOVIE_TIMESCALE as u32).to_be_bytes());
    if version == 1 {
        mvhd.extend_from_slice(&longest.to_be_bytes());
    } else {
        mvhd.extend_from_slice(&(longest as u32).to_be_bytes());
    }
    mvhd.extend_from_slice(&0x0001_0000u32.to_be_bytes()); // rate 1.0
    mvhd.extend_from_slice(&0x0100u16.to_be_bytes()); // volume 1.0
    mvhd.extend_from_slice(&[0; 10]);
    for value in [0x0001_0000u32, 0, 0, 0, 0x0001_0000, 0, 0, 0, 0x4000_0000] {
        mvhd.extend_from_slice(&value.to_be_bytes());
    }
    mvhd.extend_from_slice(&[0; 24]);
    mvhd.extend_from_slice(&(tracks.len() as u32 + 1).to_be_bytes());
    let mut moov = full_box(b"mvhd", version, 0, &mvhd);
    moov.extend(traks);
    Ok(plain_box(b"moov", &moov))
}

/// Write the samples of fragmented-MP4 `inputs` (one or more tracks each) as
/// one regular MP4 at `output`.
pub fn write_regular_mp4(inputs: &[PathBuf], output: &Path) -> Result<(), String> {
    write_regular(inputs, output, false)
}

/// The handler of a track (`vide`, `soun`, ...).
fn track_handler(track: &RegularTrack) -> Result<[u8; 4], String> {
    let context = "Media track header";
    let data = track.trak.as_slice();
    let mdia = exactly_one_box(&child_boxes(data, only_box(data, context)?, context)?, *b"mdia", context)?;
    let hdlr = exactly_one_box(&child_boxes(data, mdia, context)?, *b"hdlr", context)?;
    let (_, _, payload) = full_box_header(data, hdlr, context)?;
    // version and flags, pre_defined, then handler_type
    Ok(read_u32_at(data, payload + 8, context)?.to_be_bytes())
}

/// `companions_are_audio`: the inputs after the first are a video's separate
/// audio. What a track holds is read from the track, not from the server's
/// Content-Type: audio-only MP4 is commonly served as video/mp4.
fn write_regular(inputs: &[PathBuf], output: &Path, companions_are_audio: bool) -> Result<(), String> {
    let mut tracks = Vec::new();
    for (index, path) in inputs.iter().enumerate() {
        regular_tracks_of(index, path, &mut tracks)?;
    }
    if companions_are_audio {
        for track in tracks.iter().filter(|track| track.input > 0) {
            if &track_handler(track)? == b"vide" {
                return Err("the companion source holds video, not audio".into());
            }
        }
    }
    tracks.retain(|track| !track.samples.is_empty());
    if tracks.is_empty() {
        return Err("the media holds no fragmented MP4 samples".into());
    }
    if tracks.iter().any(|track| u32::try_from(track.samples.len()).is_err()) {
        return Err("the media has more samples than an MP4 track can index".into());
    }
    let starts: Vec<u64> = tracks
        .iter()
        .map(|track| movie_time(track.first_decode_time.unwrap_or(0), track.timescale))
        .collect();
    let earliest = starts.iter().copied().min().unwrap_or(0);
    let delays: Vec<u64> = starts.iter().map(|start| start - earliest).collect();

    // Chunks of all tracks in decode order, so the file interleaves them.
    let mut order: Vec<(usize, usize)> = tracks
        .iter()
        .enumerate()
        .flat_map(|(track, item)| (0..item.chunks.len()).map(move |chunk| (track, chunk)))
        .collect();
    order.sort_by(|left, right| {
        let at = |(track, chunk): (usize, usize)| {
            u128::from(tracks[track].chunks[chunk].decode_time) * u128::from(MOVIE_TIMESCALE) / u128::from(tracks[track].timescale)
        };
        at(*left).cmp(&at(*right)).then(left.0.cmp(&right.0))
    });
    let data_length: u64 = order.iter().map(|(track, chunk)| tracks[*track].chunks[*chunk].length).sum();
    let ftyp = plain_box(b"ftyp", &[b"isom".as_slice(), &0x200u32.to_be_bytes(), b"isom", b"iso2", b"mp41"].concat());
    let mdat_header: u64 = if data_length + 8 > u64::from(u32::MAX) { 16 } else { 8 };
    let offsets_from = |start: u64| {
        let mut offsets: Vec<Vec<u64>> = tracks.iter().map(|track| vec![0; track.chunks.len()]).collect();
        let mut position = start;
        for (track, chunk) in &order {
            offsets[*track][*chunk] = position;
            position += tracks[*track].chunks[*chunk].length;
        }
        offsets
    };
    // The movie header's size depends only on 32- or 64-bit chunk offsets.
    let narrow = regular_moov(&tracks, &offsets_from(0), &delays, false)?;
    let wide = (ftyp.len() + narrow.len()) as u64 + mdat_header + data_length > u64::from(u32::MAX);
    let sized = if wide { regular_moov(&tracks, &offsets_from(0), &delays, true)? } else { narrow };
    let data_start = (ftyp.len() + sized.len()) as u64 + mdat_header;
    let moov = regular_moov(&tracks, &offsets_from(data_start), &delays, wide)?;

    let mut files = Vec::with_capacity(inputs.len());
    for (index, path) in inputs.iter().enumerate() {
        files.push(open_media_file(path, &format!("Media track {index}"))?.0);
    }
    let mut out = MuxOutput::create(output)?;
    out.write(&ftyp)?;
    out.write(&moov)?;
    if mdat_header == 16 {
        out.write(&1u32.to_be_bytes())?;
        out.write(b"mdat")?;
        out.write(&(data_length + 16).to_be_bytes())?;
    } else {
        out.write(&((data_length + 8) as u32).to_be_bytes())?;
        out.write(b"mdat")?;
    }
    for (track, chunk) in &order {
        let chunk = &tracks[*track].chunks[*chunk];
        out.copy_from(&mut files[chunk.input], chunk.offset, chunk.length)?;
    }
    out.finish()
}
