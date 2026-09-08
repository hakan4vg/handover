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
        let Some(url) = resolve(source, line) else {
            continue;
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
    if let Some(line) = body
        .lines()
        .map(str::trim)
        .find(|line| line.starts_with("#EXT-X-MAP"))
    {
        let Some(map) = hls_map_segment(line)? else {
            return Err("The HLS initialization map is missing its URI".into());
        };
        if let Some(url) = resolve(source, &map.0) {
            segments.insert(
                0,
                Segment {
                    url,
                    range: map.1,
                    key: None
                }
            );
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

pub fn parse_dash_tracks_for_segments(
    source: &str,
    body: &str,
    selected_segments: &[String]
) -> Result<Vec<MediaTrack>, String> {
    let lower = body.to_ascii_lowercase();
    if lower.contains("type=\"dynamic\"")
        || lower.contains("type='dynamic'")
        || lower.contains("minimumupdateperiod=")
        || lower.contains("timeshiftbufferdepth=")
    {
        return Err("Live media is not supported; a static MPD is required".into());
    }
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
    Ok((
        if value == marker - 1 {
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

struct ParsedFragment<'a> {
    mdat: &'a [u8],
    moof: Vec<u8>,
    decode_time: Option<u64>,
    mfhd_sequence_offset: usize,
    tfhd_track_offsets: Vec<usize>,
    base_data_offset_offsets: Vec<usize>,
    ordinal: usize
}

struct ParsedTrack<'a> {
    data: &'a [u8],
    ftyp: &'a [u8],
    moov_children: Vec<Mp4Box>,
    mvex_children: Vec<Mp4Box>,
    trak: Vec<u8>,
    trex: Vec<u8>,
    track_id: u32,
    timescale: u32,
    fragments: Vec<ParsedFragment<'a>>
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

fn tkhd_track_id_offset(data: &[u8], item: Mp4Box, context: &str) -> Result<usize, String> {
    let (version, _, payload) = full_box_header(data, item, context)?;
    match version {
        0 => Ok(payload + 12),
        1 => Ok(payload + 20),
        _ => return Err(format!("{context} uses an unsupported tkhd version")),
    }
}

fn patch_tkhd_track_id(
    data: &mut [u8],
    item: Mp4Box,
    base_start: usize,
    track_id: u32,
    context: &str,
) -> Result<(), String> {
    let offset = tkhd_track_id_offset(data, item, context)?
        .checked_sub(base_start)
        .ok_or_else(|| format!("{context} has an invalid track ID offset"))?;
    patch_u32(data, offset, track_id, context)
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

fn patch_mvhd_next_track_id(
    data: &mut [u8],
    item: Mp4Box,
    next_track_id: u32,
    context: &str,
) -> Result<(), String> {
    let (version, _, payload) = full_box_header(data, item, context)?;
    let offset = match version {
        0 => payload + 96,
        1 => payload + 108,
        _ => return Err(format!("{context} uses an unsupported mvhd version")),
    };
    patch_u32(data, offset, next_track_id, context)
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

fn parse_fragment<'a>(
    data: &'a [u8],
    moof: Mp4Box,
    mdat: &'a [u8],
    track_id: u32,
    ordinal: usize,
    context: &str
) -> Result<ParsedFragment<'a>, String> {
    let children = child_boxes(data, moof, context)?;
    let mfhd = exactly_one_box(&children, *b"mfhd", context)?;
    let (_, _, mfhd_payload) = full_box_header(data, mfhd, context)?;
    let mfhd_sequence_offset = mfhd_payload + 4 - moof.start;
    read_u32_at(data, mfhd_payload + 4, context)?;
    let trafs = matching_boxes(&children, *b"traf");
    if trafs.is_empty() {
        return Err(format!("{context} has no track fragment"));
    }
    let mut tfhd_track_offsets = Vec::new();
    let mut base_data_offset_offsets = Vec::new();
    let mut decode_time = None;
    for (traf_index, traf) in trafs.iter().enumerate() {
        let traf_context = format!("{context} traf {traf_index}");
        let traf_children = child_boxes(data, *traf, &traf_context)?;
        let tfhd = exactly_one_box(&traf_children, *b"tfhd", &traf_context)?;
        let (fragment_track_id, base_data_offset) = tfhd_fields(data, tfhd, &traf_context)?;
        if fragment_track_id != track_id {
            return Err(format!(
                "{traf_context} references track ID {fragment_track_id}, expected {track_id}"
            ));
        }
        let (_, _, tfhd_payload) = full_box_header(data, tfhd, &traf_context)?;
        tfhd_track_offsets.push(tfhd_payload + 4 - moof.start);
        if let Some(offset) = base_data_offset {
            base_data_offset_offsets.push(offset - moof.start);
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
            decode_time = Some(tfdt_decode_time(data, *tfdt, &traf_context)?);
        }
    }
    let moof_bytes = box_bytes(data, moof).to_vec();
    Ok(ParsedFragment {
        mdat,
        moof: moof_bytes,
        decode_time,
        mfhd_sequence_offset,
        tfhd_track_offsets,
        base_data_offset_offsets,
        ordinal,
    })
}

/// Shared ftyp/moov preamble for fragmented presentations: locates the
/// initialization and every track box without assuming a track count (F03).
/// Returns the top-level boxes, the ftyp/moov positions, the moov children,
/// and every trak box.
fn fragmented_presentation_parts(
    data: &[u8],
    context: &str,
) -> Result<(Vec<Mp4Box>, usize, usize, Vec<Mp4Box>, Vec<Mp4Box>), String> {
    if data.is_empty() {
        return Err(format!("{context} is empty"));
    }
    let top = parse_mp4_boxes(data, 0, data.len(), context)?;
    let ftyp_index = top
        .iter()
        .position(|item| item.kind == *b"ftyp")
        .ok_or_else(|| format!("{context} is missing an ftyp box"))?;
    let moov_index = top
        .iter()
        .position(|item| item.kind == *b"moov")
        .ok_or_else(|| format!("{context} is missing a moov box"))?;
    if ftyp_index > moov_index {
        return Err(format!("{context} has ftyp after moov"));
    }
    let moov_children = child_boxes(data, top[moov_index], &format!("{context} moov"))?;
    let trak_boxes = matching_boxes(&moov_children, *b"trak");
    if trak_boxes.is_empty() {
        return Err(format!("{context} has no media tracks"));
    }
    Ok((top, ftyp_index, moov_index, moov_children, trak_boxes))
}

fn parse_fragmented_track<'a>(
    data: &'a [u8],
    track_index: usize,
) -> Result<ParsedTrack<'a>, String> {
    let context = format!("Media track {track_index}");
    let (top, ftyp_index, moov_index, moov_children, trak_boxes) =
        fragmented_presentation_parts(data, &context)?;
    if trak_boxes.len() != 1 {
        return Err(format!("{context} must contain exactly one track"));
    }
    let trak = trak_boxes[0];
    let ftyp = box_bytes(data, top[ftyp_index]);
    let trak_children = child_boxes(data, trak, &format!("{context} trak"))?;
    let tkhd = exactly_one_box(&trak_children, *b"tkhd", &format!("{context} trak"))?;
    let track_id = track_id_from_tkhd(data, tkhd, &format!("{context} tkhd"))?;
    if track_id == 0 {
        return Err(format!("{context} has an invalid zero track ID"));
    }
    let mdia = exactly_one_box(&trak_children, *b"mdia", &format!("{context} trak"))?;
    let mdia_children = child_boxes(data, mdia, &format!("{context} mdia"))?;
    let mdhd = exactly_one_box(&mdia_children, *b"mdhd", &format!("{context} mdia"))?;
    let timescale = mdhd_timescale(data, mdhd, &format!("{context} mdhd"))?;
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
    let mut pending_moof = None;
    let mut saw_fragment_area = false;
    for item in top.iter().skip(moov_index + 1) {
        match item.kind {
            [b'm', b'o', b'o', b'f'] => {
                if pending_moof.is_some() {
                    return Err(format!("{context} has consecutive moof boxes without mdat"));
                }
                pending_moof = Some(*item);
                saw_fragment_area = true;
            }
            [b'm', b'd', b'a', b't'] => {
                let moof = pending_moof
                    .take()
                    .ok_or_else(|| format!("{context} has mdat without a preceding moof"))?;
                let fragment = parse_fragment(
                    data,
                    moof,
                    box_bytes(data, *item),
                    track_id,
                    fragments.len(),
                    &format!("{context} fragment {}", fragments.len())
                )?;
                fragments.push(fragment);
            }
            [b'm', b'f', b'r', b'a'] => {
                if pending_moof.is_some() {
                    return Err(format!(
                        "{context} has a fragment without its mdat before mfra"
                    ));
                }
            }
            [b's', b't', b'y', b'p']
            | [b's', b'i', b'd', b'x']
            | [b'e', b'm', b's', b'g']
            | [b'p', b'r', b'f', b't']
            | [b'f', b'r', b'e', b'e']
            | [b's', b'k', b'i', b'p']
            | [b'w', b'i', b'd', b'e'] => {
                if pending_moof.is_some() {
                    return Err(format!("{context} has data between moof and mdat"));
                }
            }
            [b'f', b't', b'y', b'p'] | [b'm', b'o', b'o', b'v'] => {
                return Err(format!(
                    "{context} has a duplicate initialization box at top level"
                ))
            }
            _ => {
                return Err(format!(
                    "{context} contains unsupported top-level box {}",
                    String::from_utf8_lossy(&item.kind)
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
    let trak_bytes = box_bytes(data, trak).to_vec();
    let trex_bytes = box_bytes(data, trex).to_vec();
    Ok(ParsedTrack {
        data,
        ftyp,
        moov_children,
        mvex_children,
        trak: trak_bytes,
        trex: trex_bytes,
        track_id,
        timescale,
        fragments
    })
}

fn patch_trex_track_id(
    data: &mut [u8],
    item: Mp4Box,
    track_id: u32,
    context: &str
) -> Result<(), String> {
    let (_, _, payload) = full_box_header(data, item, context)?;
    patch_u32(data, payload + 4 - item.start, track_id, context)
}

fn wrap_mp4_box(kind: [u8; 4], children: &[Vec<u8>], context: &str) -> Result<Vec<u8>, String> {
    let payload_len = children
        .iter()
        .try_fold(0usize, |length, child| length.checked_add(child.len()))
        .ok_or_else(|| format!("{context} is too large"))?;
    let total_len = payload_len
        .checked_add(8)
        .ok_or_else(|| format!("{context} is too large"))?;
    let size = u32::try_from(total_len)
        .map_err(|_| format!("{context} is too large for a standard MP4 box"))?;
    let mut output = Vec::with_capacity(total_len);
    output.extend_from_slice(&size.to_be_bytes());
    output.extend_from_slice(&kind);
    for child in children {
        output.extend_from_slice(child);
    }
    Ok(output)
}

fn build_muxed_initialization(tracks: &mut [ParsedTrack<'_>]) -> Result<Vec<u8>, String> {
    let first = &tracks[0];
    let mut mvhd = None;
    let mut other_moov_children = Vec::new();
    let mut first_mvex_children = Vec::new();
    for child in &first.moov_children {
        if child.kind == *b"mvhd" {
            if mvhd.is_some() {
                return Err("The first media initialization contains multiple mvhd boxes".into());
            }
            mvhd = Some(box_bytes(first.data, *child).to_vec());
        } else if child.kind != *b"trak" && child.kind != *b"mvex" {
            other_moov_children.push(box_bytes(first.data, *child).to_vec());
        }
    }
    let mut mvhd = mvhd.ok_or_else(|| "The media initialization is missing mvhd".to_string())?;
    let next_track_id = u32::try_from(
        tracks
            .len()
            .checked_add(1)
            .ok_or_else(|| "Too many media tracks".to_string())?,
    )
    .map_err(|_| "Too many media tracks".to_string())?;
    let mvhd_box = parse_mp4_boxes(&mvhd, 0, mvhd.len(), "mvhd")?
        .first()
        .copied()
        .ok_or_else(|| "The media initialization has an invalid mvhd".to_string())?;
    patch_mvhd_next_track_id(&mut mvhd, mvhd_box, next_track_id, "mvhd")?;
    for child in &first.mvex_children {
        if child.kind != *b"trex" {
            first_mvex_children.push(box_bytes(first.data, *child).to_vec());
        }
    }
    let mut moov_children = vec![mvhd];
    for (index, track) in tracks.iter_mut().enumerate() {
        let new_track_id =
            u32::try_from(index + 1).map_err(|_| "Too many media tracks".to_string())?;
        let trak_box = parse_mp4_boxes(&track.trak, 0, track.trak.len(), "trak")?
            .first()
            .copied()
            .ok_or_else(|| "The media track has an invalid trak".to_string())?;
        let trak_children = child_boxes(&track.trak, trak_box, "trak")?;
        let tkhd = exactly_one_box(&trak_children, *b"tkhd", "trak")?;
        patch_tkhd_track_id(&mut track.trak, tkhd, 0, new_track_id, "tkhd")?;
        let trex_box = parse_mp4_boxes(&track.trex, 0, track.trex.len(), "trex")?
            .first()
            .copied()
            .ok_or_else(|| "The media track has an invalid trex".to_string())?;
        patch_trex_track_id(&mut track.trex, trex_box, new_track_id, "trex")?;
        track.track_id = new_track_id;
        moov_children.push(std::mem::take(&mut track.trak));
    }
    let mut mvex_children = first_mvex_children;
    for track in tracks.iter_mut() {
        mvex_children.push(std::mem::take(&mut track.trex));
    }
    moov_children.push(wrap_mp4_box(
        *b"mvex",
        &mvex_children,
        "The merged mvex box",
    )?);
    moov_children.extend(other_moov_children);
    wrap_mp4_box(*b"moov", &moov_children, "The merged moov box")
}

/// A multiplexed fragmented MP4 (audio + video in one initialization, common
/// in HLS) is already playable: validate every track's structure and every
/// fragment's pairing, then pass the bytes through untouched. The single-track
/// parser stays the contract for per-track mux inputs (F03).
fn validate_multiplexed_fmp4(data: &[u8], context: &str) -> Result<(), String> {
    let (top, _, moov_index, moov_children, trak_boxes) =
        fragmented_presentation_parts(data, context)?;
    let mut track_ids = Vec::with_capacity(trak_boxes.len());
    for (index, trak) in trak_boxes.iter().enumerate() {
        let trak_context = format!("{context} trak {index}");
        let trak_children = child_boxes(data, *trak, &trak_context)?;
        let tkhd = exactly_one_box(&trak_children, *b"tkhd", &trak_context)?;
        let track_id = track_id_from_tkhd(data, tkhd, &trak_context)?;
        if track_id == 0 {
            return Err(format!("{trak_context} has an invalid zero track ID"));
        }
        if track_ids.contains(&track_id) {
            return Err(format!("{trak_context} reuses track ID {track_id}"));
        }
        let mdia = exactly_one_box(&trak_children, *b"mdia", &trak_context)?;
        let mdia_children = child_boxes(data, mdia, &trak_context)?;
        let mdhd = exactly_one_box(&mdia_children, *b"mdhd", &trak_context)?;
        mdhd_timescale(data, mdhd, &trak_context)?;
        track_ids.push(track_id);
    }
    let mvex = matching_boxes(&moov_children, *b"mvex");
    if mvex.len() != 1 {
        return Err(format!("{context} is not a fragmented MP4 initialization"));
    }
    let mvex_children = child_boxes(data, mvex[0], &format!("{context} mvex"))?;
    let trex_boxes = matching_boxes(&mvex_children, *b"trex");
    if trex_boxes.len() != trak_boxes.len() {
        return Err(format!(
            "{context} has {} trex boxes for {} tracks",
            trex_boxes.len(),
            trak_boxes.len()
        ));
    }
    for trex in &trex_boxes {
        let (_, _, trex_payload) = full_box_header(data, *trex, &format!("{context} trex"))?;
        let trex_track_id = read_u32_at(data, trex_payload + 4, &format!("{context} trex"))?;
        if !track_ids.contains(&trex_track_id) {
            return Err(format!(
                "{context} trex references unknown track ID {trex_track_id}"
            ));
        }
    }
    let mut pending_moof = None;
    let mut fragments = 0usize;
    for item in top.iter().skip(moov_index + 1) {
        match item.kind {
            [b'm', b'o', b'o', b'f'] => {
                if pending_moof.is_some() {
                    return Err(format!("{context} has consecutive moof boxes without mdat"));
                }
                pending_moof = Some(*item);
            }
            [b'm', b'd', b'a', b't'] => {
                let moof = pending_moof
                    .take()
                    .ok_or_else(|| format!("{context} has mdat without a preceding moof"))?;
                validate_multiplexed_moof(data, moof, &track_ids, context)?;
                fragments += 1;
            }
            [b'm', b'f', b'r', b'a'] => {
                if pending_moof.is_some() {
                    return Err(format!(
                        "{context} has a fragment without its mdat before mfra"
                    ));
                }
            }
            [b's', b't', b'y', b'p']
            | [b's', b'i', b'd', b'x']
            | [b'e', b'm', b's', b'g']
            | [b'p', b'r', b'f', b't']
            | [b'f', b'r', b'e', b'e']
            | [b's', b'k', b'i', b'p']
            | [b'w', b'i', b'd', b'e'] => {
                if pending_moof.is_some() {
                    return Err(format!("{context} has data between moof and mdat"));
                }
            }
            [b'f', b't', b'y', b'p'] | [b'm', b'o', b'o', b'v'] => {
                return Err(format!(
                    "{context} has a duplicate initialization box at top level"
                ))
            }
            _ => {
                return Err(format!(
                    "{context} contains unsupported top-level box {}",
                    String::from_utf8_lossy(&item.kind)
                ))
            }
        }
    }
    if pending_moof.is_some() {
        return Err(format!("{context} has a moof without mdat"));
    }
    if fragments == 0 {
        return Err(format!("{context} is not a fragmented MP4 media stream"));
    }
    Ok(())
}

/// Validate one multiplexed moof: every traf references a known track and
/// every sample run parses. Mirrors the single-track fragment rules without
/// assuming one track per moof (F03).
fn validate_multiplexed_moof(
    data: &[u8],
    moof: Mp4Box,
    track_ids: &[u32],
    context: &str,
) -> Result<(), String> {
    let children = child_boxes(data, moof, context)?;
    exactly_one_box(&children, *b"mfhd", context)?;
    let trafs = matching_boxes(&children, *b"traf");
    if trafs.is_empty() {
        return Err(format!("{context} has no track fragment"));
    }
    for (traf_index, traf) in trafs.iter().enumerate() {
        let traf_context = format!("{context} traf {traf_index}");
        let traf_children = child_boxes(data, *traf, &traf_context)?;
        let tfhd = exactly_one_box(&traf_children, *b"tfhd", &traf_context)?;
        let (fragment_track_id, _) = tfhd_fields(data, tfhd, &traf_context)?;
        if !track_ids.contains(&fragment_track_id) {
            return Err(format!(
                "{traf_context} references unknown track ID {fragment_track_id}"
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
    Ok(())
}

pub fn finalize_fmp4(input: &[u8]) -> Result<Vec<u8>, String> {
    let (_, _, _, _, trak_boxes) = fragmented_presentation_parts(input, "Media")?;
    if trak_boxes.len() == 1 {
        parse_fragmented_track(input, 0)?;
    } else {
        validate_multiplexed_fmp4(input, "Media")?;
    }
    Ok(input.to_vec())
}

pub fn mux_fmp4_tracks<T: AsRef<[u8]>>(inputs: &[T]) -> Result<Vec<u8>, String> {
    if inputs.len() < 2 {
        return Err("Fragmented MP4 muxing requires at least two tracks".into());
    }
    let mut tracks = Vec::with_capacity(inputs.len());
    for (index, input) in inputs.iter().enumerate() {
        tracks.push(parse_fragmented_track(input.as_ref(), index)?);
    }
    let ftyp = tracks[0].ftyp;
    let initialization = build_muxed_initialization(&mut tracks)?;
    let mut references = Vec::new();
    let all_have_decode_times = tracks.iter().all(|track| {
        track
            .fragments
            .iter()
            .all(|fragment| fragment.decode_time.is_some())
    });
    for (track_index, track) in tracks.iter().enumerate() {
        for (fragment_index, fragment) in track.fragments.iter().enumerate() {
            references.push((
                track_index,
                fragment_index,
                fragment.decode_time,
                fragment.ordinal,
            ));
        }
    }
    references.sort_by(|left, right| {
        if all_have_decode_times {
            let left_time = left.2.unwrap();
            let right_time = right.2.unwrap();
            let left_scale = u128::from(tracks[left.0].timescale);
            let right_scale = u128::from(tracks[right.0].timescale);
            (u128::from(left_time) * right_scale)
                .cmp(&(u128::from(right_time) * left_scale))
                .then_with(|| left.0.cmp(&right.0))
                .then_with(|| left.1.cmp(&right.1))
        } else {
            left.3
                .cmp(&right.3)
                .then_with(|| left.0.cmp(&right.0))
                .then_with(|| left.1.cmp(&right.1))
        }
    });
    let mut output = Vec::new();
    output.extend_from_slice(ftyp);
    output.extend_from_slice(&initialization);
    for (sequence, (track_index, fragment_index, _, _)) in references.iter().enumerate() {
        let sequence =
            u32::try_from(sequence + 1).map_err(|_| "Too many media fragments".to_string())?;
        let track = &mut tracks[*track_index];
        let fragment = &mut track.fragments[*fragment_index];
        patch_u32(
            &mut fragment.moof,
            fragment.mfhd_sequence_offset,
            sequence,
            "mfhd",
        )?;
        for offset in &fragment.tfhd_track_offsets {
            patch_u32(&mut fragment.moof, *offset, track.track_id, "tfhd")?;
        }
        if !fragment.base_data_offset_offsets.is_empty() {
            let moof_offset = u64::try_from(output.len())
                .map_err(|_| "The merged media is too large".to_string())?;
            for offset in &fragment.base_data_offset_offsets {
                patch_u64(
                    &mut fragment.moof,
                    *offset,
                    moof_offset,
                    "tfhd base-data-offset",
                )?;
            }
        }
        output.extend_from_slice(&fragment.moof);
        output.extend_from_slice(fragment.mdat);
    }
    Ok(output)
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

struct MpegTsTrack {
    stream_type: u8,
    descriptors: Vec<u8>,
    pes: Vec<MpegTsPes>,
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
                self.sections.push(std::mem::take(&mut self.buffer));
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

fn parse_mpeg_ts_packet<'a>(packet: &'a [u8], context: &str) -> Result<MpegTsPacket<'a>, String> {
    if packet.len() != MPEG_TS_PACKET_SIZE {
        return Err(format!("{context} is not a 188-byte MPEG-TS packet"));
    }
    if packet[0] != 0x47 {
        return Err(format!("{context} has an invalid MPEG-TS sync byte"));
    }
    if packet[1] & 0x80 != 0 {
        return Err(format!("{context} has a transport error indicator"));
    }
    if packet[3] >> 6 != 0 {
        return Err(format!("{context} uses scrambled MPEG-TS payload"));
    }
    let adaptation_control = (packet[3] >> 4) & 0x03;
    if adaptation_control == 0 {
        return Err(format!("{context} has a reserved adaptation-field control"));
    }
    let mut payload_start = 4;
    if adaptation_control & 0x02 != 0 {
        let adaptation_length = usize::from(packet[4]);
        payload_start = 5 + adaptation_length;
        if payload_start > MPEG_TS_PACKET_SIZE {
            return Err(format!("{context} has a truncated adaptation field"));
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

fn collect_mpeg_ts_sections(data: &[u8], pid: u16, context: &str) -> Result<Vec<Vec<u8>>, String> {
    if data.len() % MPEG_TS_PACKET_SIZE != 0 {
        return Err(format!(
            "{context} is not a whole number of 188-byte MPEG-TS packets"
        ));
    }
    let mut assembler = MpegTsPsiAssembler::new();
    for (index, packet) in data.chunks_exact(MPEG_TS_PACKET_SIZE).enumerate() {
        let packet = parse_mpeg_ts_packet(packet, &format!("{context} packet {index}"))?;
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

fn collect_mpeg_ts_pes(data: &[u8], pid: u16, context: &str) -> Result<Vec<MpegTsPes>, String> {
    if data.len() % MPEG_TS_PACKET_SIZE != 0 {
        return Err(format!(
            "{context} is not a whole number of 188-byte MPEG-TS packets"
        ));
    }
    let mut current = None;
    let mut pes = Vec::new();
    for (index, packet) in data.chunks_exact(MPEG_TS_PACKET_SIZE).enumerate() {
        let packet = parse_mpeg_ts_packet(packet, &format!("{context} packet {index}"))?;
        if packet.pid != pid {
            continue;
        }
        if packet.payload_unit_start {
            if packet.payload.len() < 3 {
                return Err(format!(
                    "{context} packet {index} has a truncated PES start"
                ));
            }
            if let Some(data) = current.take() {
                pes.push(parse_mpeg_ts_pes(
                    data,
                    &format!("{context} PES {}", pes.len())
                )?);
            }
            if packet.payload[0..3] != [0x00, 0x00, 0x01] {
                return Err(format!("{context} packet {index} does not start a PES"));
            }
            current = Some(packet.payload.to_vec());
        } else if !packet.payload.is_empty() {
            let Some(current) = current.as_mut() else {
                return Err(format!(
                    "{context} has PES payload before its first start packet"
                ));
            };
            current.extend_from_slice(packet.payload);
        }
    }
    if let Some(data) = current {
        pes.push(parse_mpeg_ts_pes(
            data,
            &format!("{context} PES {}", pes.len())
        )?);
    }
    if pes.is_empty() {
        return Err(format!("{context} contains no PES units"));
    }
    Ok(pes)
}

fn parse_mpeg_ts_input(data: &[u8], index: usize) -> Result<MpegTsTrack, String> {
    let context = format!("MPEG-TS input {index}");
    if data.is_empty() {
        return Err(format!("{context} is empty"));
    }
    if data.len() % MPEG_TS_PACKET_SIZE != 0 {
        return Err(format!(
            "{context} is not a whole number of 188-byte packets"
        ));
    }
    let pat_sections = collect_mpeg_ts_sections(data, 0, &format!("{context} PAT"))?;
    let (program_number, pmt_pid) = parse_mpeg_ts_pat(&pat_sections, &format!("{context} PAT"))?;
    let pmt_sections = collect_mpeg_ts_sections(data, pmt_pid, &format!("{context} PMT"))?;
    let (elementary_pid, stream_type, descriptors) =
        parse_mpeg_ts_pmt_stream(&pmt_sections, program_number, &format!("{context} PMT"))?;
    if elementary_pid == pmt_pid {
        return Err(format!(
            "{context} maps its PMT PID as an elementary stream"
        ));
    }
    let pes = collect_mpeg_ts_pes(
        data,
        elementary_pid,
        &format!("{context} elementary stream")
    )?;
    Ok(MpegTsTrack {
        stream_type,
        descriptors,
        pes
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

#[cfg(test)]
fn append_mpeg_ts_pes(
    output: &mut Vec<u8>,
    pid: u16,
    pes: &MpegTsPes,
    continuity_counter: &mut u8
) -> Result<(), String> {
    append_mpeg_ts_pes_with_pcr(output, pid, pes, continuity_counter, None)
}

fn next_mpeg_ts_track(tracks: &[MpegTsTrack], cursors: &[usize]) -> Option<usize> {
    let mut selected = None;
    for index in 0..tracks.len() {
        if cursors[index] >= tracks[index].pes.len() {
            continue;
        }
        let Some(current) = selected else {
            selected = Some(index);
            continue;
        };
        let left = tracks[index].pes[cursors[index]].pts;
        let right = tracks[current].pes[cursors[current]].pts;
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
pub fn mux_mpeg_ts_tracks<T: AsRef<[u8]>>(inputs: &[T]) -> Result<Vec<u8>, String> {
    if inputs.len() < 2 {
        return Err("MPEG-TS muxing requires at least two tracks".into());
    }
    let mut tracks = Vec::with_capacity(inputs.len());
    for (index, input) in inputs.iter().enumerate() {
        tracks.push(parse_mpeg_ts_input(input.as_ref(), index)?);
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
    let pcr_track = tracks
        .iter()
        .position(|track| track.pes.iter().any(|pes| pes.pts.is_some()));
    let pcr_pid = pcr_track.map(|index| pids[index]).unwrap_or(0x1fff);
    let pmt = build_mpeg_ts_pmt(1, pcr_pid, &pids, &tracks)?;
    let mut output = Vec::new();
    let mut pat_continuity = 0;
    append_mpeg_ts_section(&mut output, 0, &pat, &mut pat_continuity)?;
    let mut pmt_continuity = 0;
    append_mpeg_ts_section(&mut output, MPEG_TS_PMT_PID, &pmt, &mut pmt_continuity)?;
    let total_pes = tracks
        .iter()
        .try_fold(0usize, |total, track| total.checked_add(track.pes.len()))
        .ok_or_else(|| "Too many MPEG-TS PES units".to_string())?;
    let mut cursors = vec![0usize; tracks.len()];
    let mut continuities = vec![0u8; tracks.len()];
    for _ in 0..total_pes {
        let track_index = next_mpeg_ts_track(&tracks, &cursors)
            .ok_or_else(|| "The MPEG-TS track merge ended unexpectedly".to_string())?;
        let pes_index = cursors[track_index];
        let pcr = (pcr_track == Some(track_index))
            .then(|| tracks[track_index].pes[pes_index].pts)
            .flatten();
        append_mpeg_ts_pes_with_pcr(
            &mut output,
            pids[track_index],
            &tracks[track_index].pes[pes_index],
            &mut continuities[track_index],
            pcr
        )?;
        cursors[track_index] += 1;
    }
    Ok(output)
}

#[derive(Clone)]
struct TimedMediaSample {
    timestamp_ns: i128,
    duration_ns: u64,
    data: Vec<u8>,
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

fn parse_webm_block(
    data: &[u8],
    cluster_timecode: u64,
    timecode_scale: u64,
    track_number: u64,
    keyframe: bool,
    default_duration: Option<u64>,
    context: &str
) -> Result<TimedMediaSample, String> {
    let (block_track, track_width) = ebml_vint(data, 0)?;
    let block_track =
        block_track.ok_or_else(|| format!("{context} has an unknown track number"))?;
    if block_track != track_number {
        return Err(format!(
            "{context} references track {block_track}, expected {track_number}"
        ));
    }
    if data.len() < track_width + 3 {
        return Err(format!("{context} is truncated"));
    }
    if data[track_width + 2] & 0x06 != 0 {
        return Err(format!("{context} uses unsupported lacing"));
    }
    let relative = i16::from_be_bytes([data[track_width], data[track_width + 1]]) as i128;
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
        data: data[track_width + 3..].to_vec(),
        keyframe
    })
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

fn parse_webm_video(data: &[u8], track_index: usize) -> Result<WebmVideoTrack, String> {
    let context = format!("WebM input {track_index}");
    if data.len() < 4 || &data[..4] != [0x1a, 0x45, 0xdf, 0xa3] {
        return Err(format!("{context} is not an EBML/WebM file"));
    }
    let top = ebml_children(data, 0, data.len())?;
    let (_, segment_start, segment_end) = top
        .iter()
        .find(|(id, _, _)| *id == 0x1853_8067)
        .copied()
        .ok_or_else(|| format!("{context} is missing a Segment element"))?;
    let segment_children = ebml_children(data, segment_start, segment_end)?;
    let timecode_scale = if let Some((_, info_start, info_end)) = segment_children
        .iter()
        .find(|(id, _, _)| *id == 0x1549_a966)
        .copied()
    {
        ebml_child_uint(data, info_start, info_end, 0x2ad7_b1)?.unwrap_or(1_000_000)
    } else {
        1_000_000
    };
    if timecode_scale == 0 {
        return Err(format!("{context} has a zero timecode scale"));
    }
    let (_, tracks_start, tracks_end) = segment_children
        .iter()
        .find(|(id, _, _)| *id == 0x1654_ae6b)
        .copied()
        .ok_or_else(|| format!("{context} is missing Tracks"))?;
    let track_entry = ebml_children(data, tracks_start, tracks_end)?
        .into_iter()
        .filter(|(id, _, _)| *id == 0xae)
        .find_map(|(_, start, end)| {
            let track_type = ebml_child_uint(data, start, end, 0x83).ok().flatten();
            (track_type == Some(1)).then_some((start, end))
        })
        .ok_or_else(|| format!("{context} contains no video TrackEntry"))?;
    let (entry_start, entry_end) = track_entry;
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
    for (_, cluster_start, cluster_end) in segment_children
        .iter()
        .filter(|(id, _, _)| *id == 0x1f43_b675)
    {
        let cluster_timecode =
            ebml_child_uint(data, *cluster_start, *cluster_end, 0xe7)?.unwrap_or(0);
        for (id, child_start, child_end) in ebml_children(data, *cluster_start, *cluster_end)? {
            if id == 0xa3 {
                let block = &data[child_start..child_end];
                let (_, track_width) = ebml_vint(block, 0)?;
                let flags = *block
                    .get(track_width + 2)
                    .ok_or_else(|| format!("{context} SimpleBlock is truncated"))?;
                let sample_index = samples.len();
                samples.push(parse_webm_block(
                    block,
                    cluster_timecode,
                    timecode_scale,
                    track_number,
                    flags & 0x80 != 0,
                    default_duration,
                    &format!("{context} SimpleBlock {sample_index}")
                )?);
            } else if id == 0xa0 {
                let group = ebml_children(data, child_start, child_end)?;
                let (_, block_start, block_end) = group
                    .iter()
                    .find(|(child_id, _, _)| *child_id == 0xa1)
                    .copied()
                    .ok_or_else(|| format!("{context} BlockGroup has no Block"))?;
                let keyframe = !group.iter().any(|(child_id, _, _)| *child_id == 0xfb);
                samples.push(parse_webm_block(
                    &data[block_start..block_end],
                    cluster_timecode,
                    timecode_scale,
                    track_number,
                    keyframe,
                    default_duration,
                    &format!("{context} BlockGroup {}", samples.len())
                )?);
            }
        }
    }
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

fn parse_fmp4_audio_fragment(
    data: &[u8],
    moof: Mp4Box,
    mdat: Mp4Box,
    track_id: u32,
    trex_duration: u32,
    trex_size: u32,
    timescale: u32,
    samples: &mut Vec<TimedMediaSample>,
    context: &str
) -> Result<(), String> {
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
        moof.start as u64
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
    let mut data_cursor = mdat.payload_start() as u64;
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
            if data_cursor < mdat.payload_start() as u64 || sample_end > mdat.end as u64 {
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
                data: data[data_cursor as usize..sample_end as usize].to_vec(),
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

fn parse_fmp4_audio(data: &[u8], track_index: usize) -> Result<Fmp4AudioTrack, String> {
    let context = format!("fMP4 input {track_index}");
    let parsed = parse_fragmented_track(data, track_index)?;
    let top = parse_mp4_boxes(data, 0, data.len(), &context)?;
    let moov = exactly_one_box(&top, *b"moov", &context)?;
    let moov_children = child_boxes(data, moov, &context)?;
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
    let moov_index = top
        .iter()
        .position(|item| item.kind == *b"moov")
        .ok_or_else(|| format!("{context} is missing moov"))?;
    let mut pending_moof = None;
    let mut samples = Vec::new();
    for item in top.iter().skip(moov_index + 1) {
        match item.kind {
            [b'm', b'o', b'o', b'f'] => {
                if pending_moof.is_some() {
                    return Err(format!("{context} has consecutive moof boxes"));
                }
                pending_moof = Some(*item);
            }
            [b'm', b'd', b'a', b't'] => {
                let moof = pending_moof
                    .take()
                    .ok_or_else(|| format!("{context} has mdat without moof"))?;
                let sample_index = samples.len();
                parse_fmp4_audio_fragment(
                    data,
                    moof,
                    *item,
                    parsed.track_id,
                    trex_duration,
                    trex_size,
                    timescale,
                    &mut samples,
                    &format!("{context} fragment {sample_index}")
                )?;
            }
            [b's', b'i', b'd', b'x']
            | [b's', b't', b'y', b'p']
            | [b'm', b'f', b'r', b'a']
            | [b'f', b'r', b'e', b'e']
            | [b's', b'k', b'i', b'p']
            | [b'w', b'i', b'd', b'e'] => {
                if pending_moof.is_some() {
                    return Err(format!("{context} has data between moof and mdat"));
                }
            }
            [b'f', b't', b'y', b'p'] | [b'm', b'o', b'o', b'v'] => {
                return Err(format!("{context} has duplicate initialization boxes"))
            }
            _ => {
                return Err(format!(
                    "{context} contains unsupported top-level box {}",
                    String::from_utf8_lossy(&item.kind)
                ))
            }
        }
    }
    if pending_moof.is_some() || samples.is_empty() {
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

fn build_matroska_vp9_aac(video: WebmVideoTrack, audio: Fmp4AudioTrack) -> Result<Vec<u8>, String> {
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
        events.push((timestamp_ms, 1u64, sample.keyframe, sample.data));
    }
    for sample in audio.samples {
        let relative = sample
            .timestamp_ns
            .checked_sub(first_timestamp)
            .ok_or_else(|| "The audio timestamp underflowed".to_string())?;
        let timestamp_ms = u64::try_from((relative + 500_000) / 1_000_000)
            .map_err(|_| "The audio timestamp is too large".to_string())?;
        events.push((timestamp_ms, 2u64, false, sample.data));
    }
    events.sort_by(|left, right| left.0.cmp(&right.0).then_with(|| left.1.cmp(&right.1)));
    let mut clusters: Vec<(u64, Vec<(u64, u64, bool, Vec<u8>)>)> = Vec::new();
    for event in events {
        let start_new = clusters
            .last()
            .is_none_or(|(base, _)| event.0 < *base || event.0 - *base > 5_000);
        if start_new {
            clusters.push((event.0, Vec::new()));
        }
        clusters.last_mut().unwrap().1.push(event);
    }
    let mut cluster_bytes = Vec::new();
    for (base, blocks) in clusters {
        let mut payload = ebml_uint_element(&[0xe7], base)?;
        for (timestamp, track_number, keyframe, data) in blocks {
            let relative = timestamp
                .checked_sub(base)
                .ok_or_else(|| "The Matroska block timestamp underflowed".to_string())?;
            if relative > i64::from(i16::MAX as u16) as u64 {
                return Err("The Matroska block timestamp is out of range".into());
            }
            let mut block = ebml_track_number(track_number)?;
            block.extend_from_slice(
                &(i16::try_from(relative)
                    .map_err(|_| "The Matroska block timestamp is out of range"))?
                .to_be_bytes()
            );
            block.push(if track_number == 1 && keyframe {
                0x80
            } else {
                0
            });
            block.extend_from_slice(&data);
            payload.extend_from_slice(&ebml_element(&[0xa3], &block)?);
        }
        cluster_bytes.extend_from_slice(&ebml_element(&[0x1f, 0x43, 0xb6, 0x75], &payload)?);
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
            "A_AAC",
            &audio.codec_private,
            audio_default_duration,
            None,
            Some((audio.sample_rate, audio.channels))
        )?
    ]
    .concat();
    let segment_payload = [
        ebml_element(&[0x15, 0x49, 0xa9, 0x66], &info)?,
        ebml_element(&[0x16, 0x54, 0xae, 0x6b], &tracks)?,
        cluster_bytes
    ]
    .concat();
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
    Ok([
        ebml_element(&[0x1a, 0x45, 0xdf, 0xa3], &ebml_header)?,
        ebml_element(&[0x18, 0x53, 0x80, 0x67], &segment_payload)?
    ]
    .concat())
}

/// Mux one VP9/WebM track and one AAC/fMP4 track without external tools.
pub fn mux_webm_fmp4_tracks<T: AsRef<[u8]>>(inputs: &[T]) -> Result<Vec<u8>, String> {
    if inputs.len() != 2 {
        return Err("Mixed WebM/fMP4 muxing requires exactly two tracks".into());
    }
    let first = inputs[0].as_ref();
    let second = inputs[1].as_ref();
    let (webm, fmp4) = if first.len() >= 4 && &first[..4] == [0x1a, 0x45, 0xdf, 0xa3] {
        (first, second)
    } else if second.len() >= 4 && &second[..4] == [0x1a, 0x45, 0xdf, 0xa3] {
        (second, first)
    } else {
        return Err("Mixed media inputs contain no WebM track".into());
    };
    Ok(build_matroska_vp9_aac(
        parse_webm_video(webm, 0)?,
        parse_fmp4_audio(fmp4, 1)?
    )?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_finite_hls_in_order() {
        let body = "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:2,\none.m4s\n#EXTINF:2,\ntwo.m4s\n#EXT-X-ENDLIST";
        let segments =
            parse_hls("https://cdn.example.test/vod/index.m3u8", body).expect("finite playlist");
        assert_eq!(
            segments
                .iter()
                .map(|segment| segment.url.as_str())
                .collect::<Vec<_>>(),
            [
                "https://cdn.example.test/vod/init.mp4",
                "https://cdn.example.test/vod/one.m4s",
                "https://cdn.example.test/vod/two.m4s"
            ]
        );
    }

    #[test]
    fn parses_hls_byte_ranges_and_rejects_unsafe_implicit_offsets() {
        let body = "#EXTM3U\n#EXT-X-VERSION:4\n#EXT-X-MAP:URI=\"shared.mp4\",BYTERANGE=\"2@30\"\n#EXTINF:2,\n#EXT-X-BYTERANGE:4@0\nshared.mp4\n#EXTINF:2,\n#EXT-X-BYTERANGE:3\nshared.mp4\n#EXT-X-ENDLIST";
        let segments = parse_hls("https://cdn.example.test/vod/index.m3u8", body)
            .expect("byte-range playlist");
        assert_eq!(segments[0].url, "https://cdn.example.test/vod/shared.mp4");
        assert_eq!(segments[0].range, Some((30, 2)));
        assert_eq!(segments[1].range, Some((0, 4)));
        assert_eq!(segments[2].range, Some((4, 3)));
        assert!(parse_hls(
            "https://cdn.example.test/vod/index.m3u8",
            "#EXTM3U\n#EXT-X-BYTERANGE:3\nshared.mp4\n#EXT-X-ENDLIST"
        )
        .is_err());
    }

    #[test]
    fn rejects_live_hls() {
        assert!(parse_hls(
            "https://cdn.example.test/live.m3u8",
            "#EXTM3U\n#EXTINF:2,\none.ts"
        )
        .is_err());
    }

    #[test]
    fn parses_static_dash_segment_urls() {
        let body = "<MPD type=\"static\"><Period><AdaptationSet><Representation><BaseURL>https://cdn.example.test/vod/</BaseURL><SegmentList><Initialization sourceURL=\"init.mp4\"/><SegmentURL media=\"one.m4s\"/><SegmentURL media=\"two.m4s\"/></SegmentList></Representation></AdaptationSet></Period></MPD>";
        let segments =
            parse_dash("https://cdn.example.test/manifest.mpd", body).expect("static MPD");
        assert_eq!(
            segments
                .iter()
                .map(|segment| segment.url.as_str())
                .collect::<Vec<_>>(),
            [
                "https://cdn.example.test/vod/init.mp4",
                "https://cdn.example.test/vod/one.m4s",
                "https://cdn.example.test/vod/two.m4s"
            ]
        );
    }

    #[test]
    fn expands_static_dash_segment_template() {
        let body = "<MPD type=\"static\" mediaPresentationDuration=\"PT6S\"><Period><AdaptationSet><Representation id=\"video\"><BaseURL>https://cdn.example.test/vod/</BaseURL><SegmentTemplate timescale=\"1\" media=\"seg-$Number$.m4s\" initialization=\"init.mp4\" startNumber=\"1\"><SegmentTimeline><S t=\"0\" d=\"2\" r=\"2\"/></SegmentTimeline></SegmentTemplate></Representation></AdaptationSet></Period></MPD>";
        let segments =
            parse_dash("https://cdn.example.test/manifest.mpd", body).expect("static template MPD");
        assert_eq!(
            segments
                .iter()
                .map(|segment| segment.url.as_str())
                .collect::<Vec<_>>(),
            [
                "https://cdn.example.test/vod/init.mp4",
                "https://cdn.example.test/vod/seg-1.m4s",
                "https://cdn.example.test/vod/seg-2.m4s",
                "https://cdn.example.test/vod/seg-3.m4s"
            ]
        );
    }

    #[test]
    fn expands_dash_number_padding_and_iso_duration() {
        let body = "<MPD type=\"static\" mediaPresentationDuration=\"PT1M2.2S\"><Period><AdaptationSet><Representation id=\"video\"><BaseURL>https://cdn.example.test/vod/</BaseURL><SegmentTemplate timescale=\"1\" duration=\"10\" media=\"seg-$Number%05d$.m4s\" initialization=\"init.mp4\"/></Representation></AdaptationSet></Period></MPD>";
        let segments = parse_dash("https://cdn.example.test/manifest.mpd", body)
            .expect("duration template MPD");
        assert_eq!(
            segments.first().map(|segment| segment.url.as_str()),
            Some("https://cdn.example.test/vod/init.mp4")
        );
        assert_eq!(
            segments.get(1).map(|segment| segment.url.as_str()),
            Some("https://cdn.example.test/vod/seg-00001.m4s")
        );
        assert_eq!(segments.len(), 8);
    }

    #[test]
    fn keeps_static_dash_audio_and_video_tracks_separate() {
        let body = "<MPD type=\"static\"><Period><AdaptationSet contentType=\"video\"><Representation id=\"v\"><BaseURL>https://cdn.example.test/v/</BaseURL><SegmentList><Initialization sourceURL=\"init.mp4\"/><SegmentURL media=\"one.m4s\"/></SegmentList></Representation></AdaptationSet><AdaptationSet contentType=\"audio\"><Representation id=\"a\"><BaseURL>https://cdn.example.test/a/</BaseURL><SegmentList><Initialization sourceURL=\"init.mp4\"/><SegmentURL media=\"one.m4s\"/></SegmentList></Representation></AdaptationSet></Period></MPD>";
        let tracks = parse_dash_tracks("https://cdn.example.test/manifest.mpd", body)
            .expect("separate tracks");
        assert_eq!(
            tracks
                .iter()
                .map(|track| track.kind.as_str())
                .collect::<Vec<_>>(),
            ["video", "audio"]
        );
        assert_eq!(
            tracks[0].segments[1].url,
            "https://cdn.example.test/v/one.m4s"
        );
        assert_eq!(
            tracks[1].segments[1].url,
            "https://cdn.example.test/a/one.m4s"
        );
    }

    #[test]
    fn ignores_non_audio_video_dash_adaptations() {
        let body = "<MPD type=\"static\" mediaPresentationDuration=\"PT4S\"><Period><AdaptationSet contentType=\"video\"><Representation id=\"v\"><BaseURL>https://cdn.example.test/v/</BaseURL><SegmentList><Initialization sourceURL=\"init.m4s\"/><SegmentURL media=\"one.m4s\"/></SegmentList></Representation></AdaptationSet><AdaptationSet contentType=\"audio\"><Representation id=\"a\"><BaseURL>https://cdn.example.test/a/</BaseURL><SegmentList><Initialization sourceURL=\"init.m4s\"/><SegmentURL media=\"one.m4s\"/></SegmentList></Representation></AdaptationSet><AdaptationSet contentType=\"image\" mimeType=\"image/jpeg\"><SegmentTemplate timescale=\"1\" duration=\"2\" media=\"tile-$Number$.jpg\"/><Representation id=\"thumbs\"><BaseURL>https://cdn.example.test/t/</BaseURL></Representation></AdaptationSet></Period></MPD>";
        let tracks = parse_dash_tracks("https://cdn.example.test/manifest.mpd", body)
            .expect("audio/video tracks");
        assert_eq!(
            tracks
                .iter()
                .map(|track| track.kind.as_str())
                .collect::<Vec<_>>(),
            ["video", "audio"]
        );
    }

    #[test]
    fn parses_adaptation_level_dash_segment_list() {
        let body = "<MPD type=\"static\"><Period><AdaptationSet contentType=\"video\"><SegmentList><Initialization sourceURL=\"init.mp4\" range=\"30-31\"/><SegmentURL media=\"one.m4s\" mediaRange=\"100-109\"/><SegmentURL media=\"two.m4s\" mediaRange=\"110-119\"/></SegmentList><Representation id=\"video\"><BaseURL>https://cdn.example.test/vod/</BaseURL></Representation></AdaptationSet></Period></MPD>";
        let segments = parse_dash("https://cdn.example.test/manifest.mpd", body)
            .expect("adaptation-level segment list");
        assert_eq!(
            segments
                .iter()
                .map(|segment| segment.url.as_str())
                .collect::<Vec<_>>(),
            [
                "https://cdn.example.test/vod/init.mp4",
                "https://cdn.example.test/vod/one.m4s",
                "https://cdn.example.test/vod/two.m4s"
            ]
        );
        assert_eq!(segments[0].range, Some((30, 2)));
        assert_eq!(segments[1].range, Some((100, 10)));
        assert_eq!(segments[2].range, Some((110, 10)));
        assert!(parse_dash("https://cdn.example.test/manifest.mpd", "<MPD type=\"static\"><Period><AdaptationSet><Representation><SegmentList><SegmentURL media=\"one.m4s\" mediaRange=\"10-2\"/></SegmentList></Representation></AdaptationSet></Period></MPD>").is_err());
    }

    #[test]
    fn selects_dash_representation_matching_browser_segment() {
        let body = "<MPD type=\"static\" mediaPresentationDuration=\"PT4S\"><Period><AdaptationSet contentType=\"video\"><SegmentTemplate timescale=\"1\" duration=\"2\" media=\"$RepresentationID$-$Number$.m4s\" initialization=\"$RepresentationID$-init.m4s\"/><Representation id=\"low\"/><Representation id=\"high\"/></AdaptationSet><AdaptationSet contentType=\"audio\"><Representation id=\"audio\"><SegmentList><Initialization sourceURL=\"audio-init.m4s\"/><SegmentURL media=\"audio-1.m4s\"/><SegmentURL media=\"audio-2.m4s\"/></SegmentList></Representation></AdaptationSet></Period></MPD>";
        let tracks = parse_dash_tracks_for_segments(
            "https://cdn.example.test/vod/manifest.mpd",
            body,
            &["https://cdn.example.test/vod/high-1.m4s".into()]
        )
        .expect("selected DASH representation");
        assert_eq!(
            tracks[0].segments[0].url,
            "https://cdn.example.test/vod/high-init.m4s"
        );
        assert_eq!(
            tracks[0].segments[1].url,
            "https://cdn.example.test/vod/high-1.m4s"
        );
        assert_eq!(
            tracks[0].segments[2].url,
            "https://cdn.example.test/vod/high-2.m4s"
        );
    }

    #[test]
    fn selects_static_dash_segment_base_representations_from_browser_segments() {
        let body = "<MPD type=\"static\" mediaPresentationDuration=\"PT60S\"><Period><AdaptationSet contentType=\"audio\"><Representation id=\"es\"><BaseURL>https://cdn.example.test/audio_es.mp4</BaseURL><SegmentBase indexRange=\"10-20\"><Initialization range=\"0-9\"/></SegmentBase></Representation><Representation id=\"en\"><BaseURL>https://cdn.example.test/audio_en.mp4</BaseURL><SegmentBase indexRange=\"10-20\"><Initialization range=\"0-9\"/></SegmentBase></Representation></AdaptationSet><AdaptationSet contentType=\"video\"><Representation id=\"h264\"><BaseURL>https://cdn.example.test/video_240p.mp4</BaseURL><SegmentBase indexRange=\"10-20\"><Initialization range=\"0-9\"/></SegmentBase></Representation><Representation id=\"vp9\"><BaseURL>https://cdn.example.test/video_576p.webm</BaseURL><SegmentBase indexRange=\"10-20\"><Initialization range=\"0-9\"/></SegmentBase></Representation></AdaptationSet></Period></MPD>";
        let selected = [
            "https://cdn.example.test/audio_en.mp4".into(),
            "https://cdn.example.test/video_576p.webm".into()
        ];
        let tracks = parse_dash_tracks_for_segments(
            "https://cdn.example.test/manifest.mpd",
            body,
            &selected
        )
        .expect("SegmentBase tracks");
        assert_eq!(
            tracks
                .iter()
                .map(|track| track.kind.as_str())
                .collect::<Vec<_>>(),
            ["audio", "video"]
        );
        assert_eq!(tracks[0].segments.len(), 0);
        assert_eq!(tracks[1].segments.len(), 0);
        let audio_base = tracks[0].segment_base.as_ref().expect("audio SegmentBase");
        assert_eq!(audio_base.url, "https://cdn.example.test/audio_en.mp4");
        assert_eq!(audio_base.initialization_range, Some((0, 10)));
        assert_eq!(audio_base.index_range, (10, 11));
        assert_eq!(audio_base.container, "mp4");
        let video_base = tracks[1].segment_base.as_ref().expect("video SegmentBase");
        assert_eq!(video_base.url, "https://cdn.example.test/video_576p.webm");
        assert_eq!(video_base.initialization_range, Some((0, 10)));
        assert_eq!(video_base.index_range, (10, 11));
        assert_eq!(video_base.container, "webm");
    }

    #[test]
    fn finds_hls_video_and_alternate_audio_playlists() {
        let body = "#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"audio\",URI=\"audio/index.m3u8\"\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,AUDIO=\"audio\"\nvideo/index.m3u8";
        let tracks = hls_variant_tracks("https://cdn.example.test/vod/master.m3u8", body, &[])
            .expect("HLS master");
        assert_eq!(
            tracks,
            [
                (
                    "video".into(),
                    "https://cdn.example.test/vod/video/index.m3u8".into()
                ),
                (
                    "audio".into(),
                    "https://cdn.example.test/vod/audio/index.m3u8".into()
                )
            ]
        );
    }

    #[test]
    fn keeps_pending_hls_variant_across_comments() {
        let body = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\n#EXT-X-INDEPENDENT-SEGMENTS\nvideo/index.m3u8";
        let tracks = hls_variant_tracks("https://cdn.example.test/vod/master.m3u8", body, &[])
            .expect("commented HLS master");
        assert_eq!(tracks[0].1, "https://cdn.example.test/vod/video/index.m3u8");
    }

    #[test]
    fn chooses_hls_video_and_audio_playlists_from_observed_active_segments() {
        let body = "#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"audio\",NAME=\"English\",DEFAULT=YES,URI=\"audio/en.m3u8\"\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"audio\",NAME=\"French\",DEFAULT=NO,URI=\"audio/fr.m3u8\"\n#EXT-X-STREAM-INF:BANDWIDTH=800000,AUDIO=\"audio\"\nvideo/low.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2400000,AUDIO=\"audio\"\nvideo/high.m3u8";
        let selected = [
            "https://cdn.example.test/vod/video/high/seg-01.ts".to_string(),
            "https://cdn.example.test/vod/audio/en/seg-01.aac".to_string()
        ];
        let tracks =
            hls_variant_tracks("https://cdn.example.test/vod/master.m3u8", body, &selected)
                .expect("HLS master");
        assert_eq!(
            tracks,
            [
                (
                    "video".into(),
                    "https://cdn.example.test/vod/video/high.m3u8".into()
                ),
                (
                    "audio".into(),
                    "https://cdn.example.test/vod/audio/en.m3u8".into()
                )
            ]
        );
    }

    #[test]
    fn follows_ordered_manifest_hints_before_master_variant_order() {
        let body = "#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"audio\",NAME=\"English\",DEFAULT=YES,URI=\"audio/en.m3u8\"\n#EXT-X-STREAM-INF:BANDWIDTH=2400000,AUDIO=\"audio\"\nvideo/high.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=900000,AUDIO=\"audio\"\nvideo/active.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=500000,AUDIO=\"audio\"\nvideo/low.m3u8";
        let selected = [
            "https://cdn.example.test/vod/video/active.m3u8".to_string(),
            "https://cdn.example.test/vod/audio/en.m3u8".to_string(),
            "https://cdn.example.test/vod/video/high.m3u8".to_string()
        ];
        let tracks =
            hls_variant_tracks("https://cdn.example.test/vod/master.m3u8", body, &selected)
                .expect("ordered HLS master");
        assert_eq!(
            tracks,
            [
                (
                    "video".into(),
                    "https://cdn.example.test/vod/video/active.m3u8".into()
                ),
                (
                    "audio".into(),
                    "https://cdn.example.test/vod/audio/en.m3u8".into()
                )
            ]
        );
    }

    #[test]
    fn matches_hls_variant_when_segment_filename_embeds_playlist_identity() {
        let body = "#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"audio\",NAME=\"English\",DEFAULT=YES,URI=\"index-s0q3570v1-a1.m3u8\"\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"audio\",NAME=\"Spanish\",DEFAULT=NO,URI=\"index-s1q3570v1-a2.m3u8\"\n#EXT-X-STREAM-INF:BANDWIDTH=578000,AUDIO=\"audio\"\nindex-s0q3576v1-v1-a1.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=1928000,AUDIO=\"audio\"\nindex-s0q3570v1-v1-a1.m3u8";
        let selected = [
            "https://cdn.example.test/vod/segment-17-s0q3570v1-v1-a1.ts".to_string(),
            "https://cdn.example.test/vod/segment-17-s1q3570v1-a2.ts".to_string()
        ];
        let tracks =
            hls_variant_tracks("https://cdn.example.test/vod/master.m3u8", body, &selected)
                .expect("HLS master");
        assert_eq!(
            tracks,
            [
                (
                    "video".into(),
                    "https://cdn.example.test/vod/index-s0q3570v1-v1-a1.m3u8".into()
                ),
                (
                    "audio".into(),
                    "https://cdn.example.test/vod/index-s1q3570v1-a2.m3u8".into()
                )
            ]
        );
    }

    #[test]
    fn parses_aes128_key_with_sequence_iv_and_transition() {
        let body = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:7\n#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n#EXTINF:2,\none.ts\n#EXTINF:2,\ntwo.ts\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:2,\nthree.ts\n#EXT-X-ENDLIST";
        let segments =
            parse_hls("https://cdn.example.test/vod/index.m3u8", body).expect("aes playlist");
        assert_eq!(segments.len(), 3);
        let first = segments[0].key.as_ref().expect("first encrypted");
        assert_eq!(first.uri, "https://cdn.example.test/vod/key.bin");
        assert_eq!(first.sequence, 7);
        assert_eq!(hls_key_iv(first)[8..], 7u64.to_be_bytes());
        assert_eq!(
            segments[1].key.as_ref().expect("second encrypted").sequence,
            8
        );
        assert!(segments[2].key.is_none());
    }

    #[test]
    fn parses_aes128_explicit_iv_and_rejects_other_methods() {
        let body = "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\",IV=0x0000000000000000000000000000002a\n#EXTINF:2,\none.ts\n#EXT-X-ENDLIST";
        let segments =
            parse_hls("https://cdn.example.test/vod/index.m3u8", body).expect("explicit iv");
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
        let ciphertext: [u8; 32] = [
            0xdb, 0x65, 0x64, 0x3d, 0x41, 0x77, 0x4f, 0x88, 0xbf, 0xbd, 0x33, 0x07, 0x80, 0x24,
            0xb6, 0xc0, 0xb5, 0x18, 0x95, 0x3b, 0x30, 0x6d, 0xf8, 0xb9, 0x2b, 0xd1, 0xf5, 0x9b,
            0x94, 0xb7, 0xe7, 0x4d
        ];
        let decrypted = decrypt_aes128_segment(&ciphertext, &key, iv).expect("known vector");
        assert_eq!(decrypted, plaintext);
        assert!(decrypt_aes128_segment(&ciphertext[..15], &key, iv).is_err());
        let mut tampered = ciphertext;
        let last = tampered.len() - 1;
        tampered[last] ^= 0xff;
        assert!(decrypt_aes128_segment(&tampered, &key, iv).is_err());
    }

    fn synthetic_pts(pts: u64, prefix: u8) -> [u8; 5] {
        [
            (prefix << 4) | ((((pts >> 30) & 0x07) as u8) << 1) | 1,
            (pts >> 22) as u8,
            ((((pts >> 15) & 0x7f) as u8) << 1) | 1,
            (pts >> 7) as u8,
            (((pts & 0x7f) as u8) << 1) | 1
        ]
    }

    fn synthetic_pes(stream_id: u8, payload: &[u8], pts: Option<u64>) -> Vec<u8> {
        let optional_length = if pts.is_some() { 8usize } else { 3 };
        let packet_length = optional_length + payload.len();
        let mut data = vec![
            0x00,
            0x00,
            0x01,
            stream_id,
            (packet_length >> 8) as u8,
            packet_length as u8
        ];
        data.extend_from_slice(&[
            0x80,
            if pts.is_some() { 0x80 } else { 0x00 },
            if pts.is_some() { 5 } else { 0 }
        ]);
        if let Some(pts) = pts {
            data.extend_from_slice(&synthetic_pts(pts, 0x02));
        }
        data.extend_from_slice(payload);
        data
    }

    fn synthetic_pmt(stream_type: u8, elementary_pid: u16) -> Vec<u8> {
        let mut section = vec![
            0x02,
            0xb0,
            0x12,
            0x00,
            0x01,
            0xc1,
            0x00,
            0x00,
            0xe0 | ((elementary_pid >> 8) & 0x1f) as u8,
            elementary_pid as u8,
            0xf0,
            0x00,
            stream_type,
            0xe0 | ((elementary_pid >> 8) & 0x1f) as u8,
            elementary_pid as u8,
            0xf0,
            0x00
        ];
        section.extend_from_slice(&mpeg_ts_crc32(&section).to_be_bytes());
        section
    }

    fn synthetic_ts_track(
        pmt_pid: u16,
        stream_type: u8,
        elementary_pid: u16,
        pes_units: &[Vec<u8>],
        continuity_counter: u8
    ) -> Vec<u8> {
        let mut data = Vec::new();
        let mut psi_continuity = continuity_counter;
        append_mpeg_ts_section(
            &mut data,
            0,
            &build_mpeg_ts_pat(1, pmt_pid),
            &mut psi_continuity
        )
        .expect("PAT");
        append_mpeg_ts_section(
            &mut data,
            pmt_pid,
            &synthetic_pmt(stream_type, elementary_pid),
            &mut psi_continuity
        )
        .expect("PMT");
        let mut pes_continuity = continuity_counter;
        for unit in pes_units {
            append_mpeg_ts_pes(
                &mut data,
                elementary_pid,
                &MpegTsPes {
                    data: unit.clone(),
                    pts: None
                },
                &mut pes_continuity
            )
            .expect("PES");
        }
        data
    }

    fn output_pmt_pids(output: &[u8]) -> Vec<u16> {
        let section = &collect_mpeg_ts_sections(output, MPEG_TS_PMT_PID, "output PMT")
            .expect("output PMT")[0];
        let end = section.len() - 4;
        let program_info_length =
            ((usize::from(section[10] & 0x0f)) << 8) | usize::from(section[11]);
        let mut cursor = 12 + program_info_length;
        let mut pids = Vec::new();
        while cursor < end {
            let pid =
                ((u16::from(section[cursor + 1] & 0x1f)) << 8) | u16::from(section[cursor + 2]);
            let descriptors_length =
                ((usize::from(section[cursor + 3] & 0x0f)) << 8) | usize::from(section[cursor + 4]);
            pids.push(pid);
            cursor += 5 + descriptors_length;
        }
        pids
    }

    #[test]
    fn remuxes_mpeg_ts_with_valid_psi_unique_pids_and_pts_order() {
        let video_units = [
            synthetic_pes(0xe0, b"video-one", Some(90_000)),
            synthetic_pes(0xe0, b"video-two", Some(270_000))
        ];
        let audio_units = [
            synthetic_pes(0xc0, b"audio-one", Some(45_000)),
            synthetic_pes(0xc0, b"audio-two", Some(180_000))
        ];
        let video = synthetic_ts_track(0x0100, 0x1b, 0x0101, &video_units, 7);
        let audio = synthetic_ts_track(0x0100, 0x0f, 0x0101, &audio_units, 11);
        let output = mux_mpeg_ts_tracks(&[video, audio]).expect("MPEG-TS mux");
        assert_eq!(output.len() % MPEG_TS_PACKET_SIZE, 0);
        assert_eq!(
            &output[..2 * MPEG_TS_PACKET_SIZE]
                .chunks_exact(MPEG_TS_PACKET_SIZE)
                .map(|packet| ((u16::from(packet[1] & 0x1f)) << 8) | u16::from(packet[2]))
                .collect::<Vec<_>>(),
            &[0, MPEG_TS_PMT_PID]
        );
        let pat = &collect_mpeg_ts_sections(&output, 0, "output PAT").expect("output PAT")[0];
        let pmt = &collect_mpeg_ts_sections(&output, MPEG_TS_PMT_PID, "output PMT")
            .expect("output PMT")[0];
        assert_eq!(mpeg_ts_crc32(pat), 0);
        assert_eq!(mpeg_ts_crc32(pmt), 0);
        let output_pids = output_pmt_pids(&output);
        assert_eq!(output_pids, [0x0100, 0x0101]);
        assert_ne!(output_pids[0], output_pids[1]);
        let mut ordered = Vec::new();
        let mut continuity = [Vec::new(), Vec::new()];
        for packet in output.chunks_exact(MPEG_TS_PACKET_SIZE) {
            let continuity_counter = packet[3] & 0x0f;
            let packet = parse_mpeg_ts_packet(packet, "output packet").expect("output packet");
            if let Some(index) = output_pids.iter().position(|pid| *pid == packet.pid) {
                continuity[index].push(continuity_counter);
                if packet.payload_unit_start {
                    let pes = parse_mpeg_ts_pes(packet.payload.to_vec(), "output PES")
                        .expect("output PES");
                    ordered.push((packet.pid, pes.pts, pes.data));
                }
            }
        }
        assert_eq!(continuity, [vec![0, 1], vec![0, 1]]);
        assert_eq!(
            ordered.iter().map(|(_, pts, _)| *pts).collect::<Vec<_>>(),
            [Some(45_000), Some(90_000), Some(180_000), Some(270_000)]
        );
        assert_eq!(
            ordered.iter().map(|(_, _, data)| data).collect::<Vec<_>>(),
            [
                &audio_units[0],
                &video_units[0],
                &audio_units[1],
                &video_units[1]
            ]
        );
    }

    #[test]
    fn remuxes_mpeg_ts_without_pts_in_input_order() {
        let first_unit = synthetic_pes(0xe0, b"first", None);
        let second_unit = synthetic_pes(0xc0, b"second", None);
        let first = synthetic_ts_track(0x0120, 0x1b, 0x0121, std::slice::from_ref(&first_unit), 5);
        let second =
            synthetic_ts_track(0x0120, 0x0f, 0x0121, std::slice::from_ref(&second_unit), 9);
        let output = mux_mpeg_ts_tracks(&[first, second]).expect("MPEG-TS mux without PTS");
        let pids = output_pmt_pids(&output);
        let first_output = collect_mpeg_ts_pes(&output, pids[0], "first output track")
            .expect("first output track");
        let second_output = collect_mpeg_ts_pes(&output, pids[1], "second output track")
            .expect("second output track");
        assert_eq!(first_output[0].pts, None);
        assert_eq!(second_output[0].pts, None);
        assert_eq!(first_output[0].data, first_unit);
        assert_eq!(second_output[0].data, second_unit);
    }

    #[test]
    fn rejects_malformed_mpeg_ts_inputs() {
        let unit = synthetic_pes(0xe0, b"payload", Some(90_000));
        let valid = synthetic_ts_track(0x0140, 0x1b, 0x0141, std::slice::from_ref(&unit), 3);
        let mut bad_sync = valid.clone();
        bad_sync[0] = 0;
        assert!(mux_mpeg_ts_tracks(&[bad_sync, valid.clone()]).is_err());
        let mut bad_crc = valid.clone();
        let last = MPEG_TS_PACKET_SIZE - 1;
        bad_crc[last] ^= 1;
        assert!(mux_mpeg_ts_tracks(&[bad_crc, valid.clone()]).is_err());
        assert!(mux_mpeg_ts_tracks(&[valid[..valid.len() - 1].to_vec(), valid]).is_err());
    }

    fn fixture_track(initialization: &[u8], fragments: &[&[u8]]) -> Vec<u8> {
        let mut track = initialization.to_vec();
        for fragment in fragments {
            track.extend_from_slice(fragment);
        }
        track
    }

    fn fixture_mdat(fragment: &[u8]) -> &[u8] {
        let boxes = parse_mp4_boxes(fragment, 0, fragment.len(), "fixture fragment")
            .expect("fixture boxes");
        box_bytes(
            fragment,
            exactly_one_box(&boxes, *b"mdat", "fixture fragment").expect("fixture mdat")
        )
    }

    #[test]
    fn finalizes_a_concatenated_fmp4_track_without_changing_bytes() {
        let input = fixture_track(
            include_bytes!("../../fixtures/media/v-init.mp4"),
            &[
                include_bytes!("../../fixtures/media/v-0.m4s"),
                include_bytes!("../../fixtures/media/v-1.m4s"),
                include_bytes!("../../fixtures/media/v-2.m4s")
            ]
        );
        assert_eq!(finalize_fmp4(&input).expect("fragmented track"), input);
    }

    #[test]
    fn rejects_non_fragmented_mp4_inputs() {
        assert!(finalize_fmp4(include_bytes!("../../fixtures/real.mp4")).is_err());
        assert!(mux_fmp4_tracks(&[
            include_bytes!("../../fixtures/real.mp4"),
            include_bytes!("../../fixtures/real.mp4")
        ])
        .is_err());
    }

    #[test]
    fn muxes_fragmented_tracks_with_unique_ids_and_ordered_fragments() {
        let video = fixture_track(
            include_bytes!("../../fixtures/media/v-init.mp4"),
            &[
                include_bytes!("../../fixtures/media/v-0.m4s"),
                include_bytes!("../../fixtures/media/v-1.m4s"),
                include_bytes!("../../fixtures/media/v-2.m4s")
            ]
        );
        let audio = fixture_track(
            include_bytes!("../../fixtures/media/a-init.mp4"),
            &[include_bytes!("../../fixtures/media/a-0.m4s")]
        );
        let output = mux_fmp4_tracks(&[video, audio]).expect("muxed fragmented tracks");
        let top = parse_mp4_boxes(&output, 0, output.len(), "muxed output").expect("output boxes");
        assert_eq!(
            top.iter().map(|item| item.kind).collect::<Vec<_>>(),
            [
                *b"ftyp", *b"moov", *b"moof", *b"mdat", *b"moof", *b"mdat", *b"moof", *b"mdat",
                *b"moof", *b"mdat"
            ]
        );
        let moov = top[1];
        let moov_children = child_boxes(&output, moov, "muxed moov").expect("moov children");
        let traks = matching_boxes(&moov_children, *b"trak");
        assert_eq!(traks.len(), 2);
        assert_eq!(
            traks
                .iter()
                .map(|trak| {
                    let children =
                        child_boxes(&output, *trak, "muxed trak").expect("trak children");
                    track_id_from_tkhd(
                        &output,
                        exactly_one_box(&children, *b"tkhd", "muxed trak").expect("tkhd"),
                        "tkhd"
                    )
                    .expect("track ID")
                })
                .collect::<Vec<_>>(),
            [1, 2]
        );
        let mvex = exactly_one_box(&moov_children, *b"mvex", "muxed moov").expect("mvex");
        let trex = matching_boxes(
            &child_boxes(&output, mvex, "muxed mvex").expect("mvex children"),
            *b"trex"
        );
        assert_eq!(
            trex.iter()
                .map(|item| {
                    let (_, _, payload) =
                        full_box_header(&output, *item, "trex").expect("trex header");
                    read_u32_at(&output, payload + 4, "trex").expect("trex ID")
                })
                .collect::<Vec<_>>(),
            [1, 2]
        );
        let expected_mdats = [
            fixture_mdat(include_bytes!("../../fixtures/media/v-0.m4s")),
            fixture_mdat(include_bytes!("../../fixtures/media/a-0.m4s")),
            fixture_mdat(include_bytes!("../../fixtures/media/v-1.m4s")),
            fixture_mdat(include_bytes!("../../fixtures/media/v-2.m4s"))
        ];
        let expected_track_ids = [1, 2, 1, 1];
        for (index, pair) in top[2..].chunks_exact(2).enumerate() {
            assert_eq!(box_bytes(&output, pair[1]), expected_mdats[index]);
            let moof_children = child_boxes(&output, pair[0], "muxed moof").expect("moof children");
            let (_, _, mfhd_payload) = full_box_header(
                &output,
                exactly_one_box(&moof_children, *b"mfhd", "muxed moof").expect("mfhd"),
                "mfhd"
            )
            .expect("mfhd header");
            assert_eq!(
                read_u32_at(&output, mfhd_payload + 4, "mfhd").expect("sequence"),
                u32::try_from(index + 1).unwrap()
            );
            let traf = exactly_one_box(&moof_children, *b"traf", "muxed moof").expect("traf");
            let tfhd = exactly_one_box(
                &child_boxes(&output, traf, "muxed traf").expect("traf children"),
                *b"tfhd",
                "muxed traf"
            )
            .expect("tfhd");
            let (_, _, tfhd_payload) = full_box_header(&output, tfhd, "tfhd").expect("tfhd header");
            assert_eq!(
                read_u32_at(&output, tfhd_payload + 4, "tfhd").expect("track ID"),
                expected_track_ids[index]
            );
        }
    }

    #[test]
    fn finalizes_multiplexed_fmp4_without_changing_bytes() {
        // Astra F03: the muxer's own two-track output re-enters as a single
        // HLS resource. It is already playable; validation must not demand
        // one track per download.
        let video = fixture_track(
            include_bytes!("../../fixtures/media/v-init.mp4"),
            &[
                include_bytes!("../../fixtures/media/v-0.m4s"),
                include_bytes!("../../fixtures/media/v-1.m4s"),
                include_bytes!("../../fixtures/media/v-2.m4s")
            ]
        );
        let audio = fixture_track(
            include_bytes!("../../fixtures/media/a-init.mp4"),
            &[include_bytes!("../../fixtures/media/a-0.m4s")]
        );
        let multiplexed = mux_fmp4_tracks(&[video, audio]).expect("muxed fragmented tracks");
        assert_eq!(finalize_fmp4(&multiplexed).expect("multiplexed fMP4"), multiplexed);
    }
}
