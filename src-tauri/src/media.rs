use quick_xml::events::Event;
use quick_xml::Reader;
use reqwest::Url;

#[derive(Clone, Debug)]
pub struct Segment {
    pub url: String,
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
    for line in body.lines().map(str::trim) {
        if line.is_empty() || line.starts_with('#') { continue; }
        if let Some(url) = resolve(source, line) { segments.push(Segment { url }); }
    }
    if segments.is_empty() { return Err("The VOD playlist did not contain any media fragments".into()); }
    if let Some(map) = body.lines().map(str::trim).find_map(hls_map_uri) {
        if let Some(url) = resolve(source, &map) { segments.insert(0, Segment { url }); }
    }
    Ok(segments)
}

pub fn parse_dash(source: &str, body: &str) -> Result<Vec<Segment>, String> {
    Ok(parse_dash_tracks(source, body)?.into_iter().flat_map(|track| track.segments).collect())
}

pub fn parse_dash_tracks(source: &str, body: &str) -> Result<Vec<MediaTrack>, String> {
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
                        if !track.selected_representation {
                            track.selected_representation = true;
                            track.representation_open = true;
                            track.representation_id = attribute(&element, b"id").unwrap_or_default();
                            track.bandwidth = attribute(&element, b"bandwidth").unwrap_or_default();
                            if let Some(kind) = attribute(&element, b"contentType").or_else(|| attribute(&element, b"mimeType")).and_then(|value| track_kind(&value)) { track.kind = kind; }
                        }
                    }
                }
                if let Some(track) = current_track.as_mut() {
                    if name.as_slice() == b"baseurl" { track.base_text_depth = Some(stack.len() + 1); }
                    if name.as_slice() == b"segmenttemplate" && track.template.is_none() { track.template = Some(DashTemplate::from_element(&element)); }
                    if name.as_slice() == b"s" && (track.representation_open || !track.selected_representation) { if let Some(template) = track.template.as_mut() { template.timeline.push(DashTimeline::from_element(&element)); } }
                    if track.representation_open {
                        if name.as_slice() == b"initialization" { if let Some(value) = attribute(&element, b"sourceURL") { track.segment_refs.push(value); } }
                        if name.as_slice() == b"segmenturl" { if let Some(value) = attribute(&element, b"media") { track.segment_refs.push(value); } }
                    }
                } else if name.as_slice() == b"baseurl" { global_base_text_depth = Some(stack.len() + 1); }
                stack.push(name.to_vec());
            }
            Ok(Event::Empty(element)) => {
                let name = element.name().as_ref().to_ascii_lowercase();
                if name.as_slice() == b"mpd" { presentation_duration = attribute(&element, b"mediaPresentationDuration").and_then(|value| parse_duration(&value)); }
                if name.as_slice() == b"adaptationset" && current_track.is_none() { current_track = Some(DashTrackBuilder::new(attribute(&element, b"contentType").or_else(|| attribute(&element, b"mimeType")))); }
                if let Some(track) = current_track.as_mut() {
                    if name.as_slice() == b"segmenttemplate" && track.template.is_none() { track.template = Some(DashTemplate::from_element(&element)); }
                    if name.as_slice() == b"initialization" && track.representation_open { if let Some(value) = attribute(&element, b"sourceURL") { track.segment_refs.push(value); } }
                    if name.as_slice() == b"segmenturl" && track.representation_open { if let Some(value) = attribute(&element, b"media") { track.segment_refs.push(value); } }
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
                        let finished = current_track.take().and_then(|track| track.finish(source, global_base_urls.first().map(String::as_str), presentation_duration));
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
    if let Some(track) = current_track.take().and_then(|track| track.finish(source, global_base_urls.first().map(String::as_str), presentation_duration)) { tracks.push(track); }
    if tracks.is_empty() { return Err("The static MPD did not contain downloadable segments".into()); }
    Ok(tracks)
}

struct DashTrackBuilder {
    kind: String,
    base_urls: Vec<String>,
    segment_refs: Vec<String>,
    template: Option<DashTemplate>,
    representation_id: String,
    bandwidth: String,
    selected_representation: bool,
    representation_open: bool,
    base_text_depth: Option<usize>,
}

impl DashTrackBuilder {
    fn new(kind: Option<String>) -> Self { Self { kind: kind.as_deref().and_then(track_kind).unwrap_or_default(), base_urls: Vec::new(), segment_refs: Vec::new(), template: None, representation_id: String::new(), bandwidth: String::new(), selected_representation: false, representation_open: false, base_text_depth: None } }

    fn finish(self, source: &str, inherited_base: Option<&str>, presentation_duration: Option<u64>) -> Option<MediaTrack> {
        let base = self.base_urls.first().map(String::as_str).or(inherited_base).unwrap_or(source);
        let mut segments = self.segment_refs.into_iter().filter_map(|value| resolve(base, &value)).map(|url| Segment { url }).collect::<Vec<_>>();
        if segments.is_empty() { if let Some(template) = self.template { segments = expand_dash_template(&template, base, &self.representation_id, &self.bandwidth, presentation_duration).ok()?; } }
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
        if let Some(url) = resolve(base, &value) { segments.push(Segment { url }); }
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
                if let Some(url) = resolve(base, &value) { segments.push(Segment { url }); }
                number = number.saturating_add(1);
            }
            current_time = start.saturating_add(repeat.saturating_mul(item.duration));
        }
    } else if let (Some(duration), Some(segment_duration)) = (presentation_duration, template.duration) {
        let count = ((duration.saturating_mul(template.timescale) + segment_duration.saturating_sub(1)) / segment_duration).min(100_000);
        for index in 0..count {
            let time = index.saturating_mul(segment_duration);
            let value = expand_template(media, number, time, representation_id, bandwidth);
            if let Some(url) = resolve(base, &value) { segments.push(Segment { url }); }
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

fn hls_map_uri(line: &str) -> Option<String> {
    if !line.starts_with("#EXT-X-MAP") { return None; }
    let start = line.find("URI=\"")? + 5;
    let rest = &line[start..];
    Some(rest.split('"').next()?.to_string())
}

fn hls_attribute(line: &str, key: &str) -> Option<String> {
    let marker = format!("{key}=\"");
    let start = line.find(&marker)? + marker.len();
    Some(line[start..].split('\"').next()?.to_string())
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
    fn finds_hls_video_and_alternate_audio_playlists() {
        let body = "#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"audio\",URI=\"audio/index.m3u8\"\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,AUDIO=\"audio\"\nvideo/index.m3u8";
        let tracks = hls_variant_tracks("https://cdn.example.test/vod/master.m3u8", body).expect("HLS master");
        assert_eq!(tracks, [("video".into(), "https://cdn.example.test/vod/video/index.m3u8".into()), ("audio".into(), "https://cdn.example.test/vod/audio/index.m3u8".into())]);
    }
}
