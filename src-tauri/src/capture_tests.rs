    use super::{
        bridge_host_allowed, bridge_json_content, bridge_origin_allowed,
        browser_policy_from_value, browser_policy_value, capture_input_from_args,
        cleanup_media_track_files, complete_job, default_settings, manifest_output_name,
        mark_ready_for_confirmation, snapshot_from_database, sweep_temp_root,
        provisional_input_from_message, protect_job_for_storage, redact_url_credentials,
        referer_value, restrict_data_dir, restrict_file, retryable_status, segment_identity,
        source_compatible, terminal_source_error, terminal_source_status, is_terminal_source_error,
        page_instead_of_file, persist_dirty_jobs, persist_job,
        tray_status_text, tray_toggle_next,
        unprotect_job_from_storage, update_settings_snapshot, write_browser_policy,
        DownloadJob, NotificationItem, ProgressThrottle, ProvisionalInput,
    };
    use rusqlite::Connection;
    use super::ipc;
    use serde_json::json;

    #[test]
    fn special_launches_start_hidden_but_normal_launches_do_not() {
        assert!(super::background_launch(&[
            "download-manager".into(),
            "--startup".into()
        ]));
        assert!(super::background_launch(&[
            "download-manager".into(),
            "--capture".into()
        ]));
        assert!(super::background_launch(&[
            "download-manager".into(),
            "--policy".into()
        ]));
        assert!(super::background_launch(&[
            "download-manager".into(),
            "--commit".into()
        ]));
        assert!(!super::background_launch(&["download-manager".into()]));
    }

    #[test]
    fn commit_args_parse_id_and_input() {
        let args = vec![
            "download-manager".to_string(),
            "--commit".to_string(),
            serde_json::json!({"id": "provisional-test", "input": {"name": "file.bin", "destination": "/tmp/file.bin"}}).to_string(),
        ];
        let (id, input) = super::commit_from_args(&args).expect("valid commit args");
        assert_eq!(id, "provisional-test");
        assert_eq!(input.name, "file.bin");
        assert_eq!(input.destination, "/tmp/file.bin");
    }

    #[test]
    fn cleanup_removes_media_track_artifacts_but_keeps_primary_part() {
        let root = std::env::temp_dir().join(format!("dm-track-cleanup-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let temp = root.join("job.part");
        std::fs::write(&temp, b"primary").unwrap();
        std::fs::write(format!("{}.track-00", temp.display()), b"video").unwrap();
        std::fs::write(format!("{}.track-01", temp.display()), b"audio").unwrap();
        cleanup_media_track_files(temp.to_str().unwrap());
        assert!(temp.exists());
        assert!(!root.join("job.part.track-00").exists());
        assert!(!root.join("job.part.track-01").exists());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn tray_tooltip_reports_idle_and_live_state() {
        assert_eq!(tray_status_text(0, 0), "Download Manager — idle");
        assert_eq!(tray_status_text(0, 99999), "Download Manager — idle");
        assert_eq!(
            tray_status_text(1, 0),
            "Download Manager — 1 active download · 0 B/s"
        );
        assert_eq!(
            tray_status_text(2, 1536),
            "Download Manager — 2 active downloads · 1 KB/s"
        );
        assert_eq!(
            tray_status_text(3, 5 * 1024 * 1024),
            "Download Manager — 3 active downloads · 5.0 MB/s"
        );
    }

    #[test]
    fn per_job_cap_parses_from_capture_message() {
        // Bytes/sec on the wire; absent or garbage means no per-job cap.
        let message = serde_json::json!({ "type": "capture-acquisition", "payload": { "source": "http://127.0.0.1:8901/range.bin", "bandwidthLimit": 524288 } });
        let input = provisional_input_from_message(&message).expect("cap accepted");
        assert_eq!(input.bandwidth_limit, Some(524288));
        let plain = serde_json::json!({ "type": "capture-acquisition", "payload": { "source": "http://127.0.0.1:8901/range.bin" } });
        assert_eq!(
            provisional_input_from_message(&plain)
                .expect("plain")
                .bandwidth_limit,
            None
        );
        let bad = serde_json::json!({ "type": "capture-acquisition", "payload": { "source": "http://127.0.0.1:8901/range.bin", "bandwidthLimit": "fast" } });
        assert_eq!(
            provisional_input_from_message(&bad)
                .expect("bad cap")
                .bandwidth_limit,
            None
        );
    }

    #[test]
    fn effective_rate_constrains_job_inside_global() {
        use super::effective_rate;
        assert_eq!(effective_rate(None, None), None);
        assert_eq!(effective_rate(Some(1_000_000.0), None), Some(1_000_000.0));
        assert_eq!(effective_rate(None, Some(500_000.0)), Some(500_000.0));
        assert_eq!(
            effective_rate(Some(1_000_000.0), Some(500_000.0)),
            Some(500_000.0)
        );
        assert_eq!(
            effective_rate(Some(500_000.0), Some(1_000_000.0)),
            Some(500_000.0)
        );
        assert_eq!(effective_rate(Some(0.0), Some(500_000.0)), Some(500_000.0));
        assert_eq!(
            effective_rate(Some(1_000_000.0), Some(0.0)),
            Some(1_000_000.0)
        );
    }

    #[test]
    fn capture_acquisition_accepts_source_and_name() {
        let message = json!({ "type": "capture-acquisition", "payload": { "source": "https://cdn.example.test/file.zip", "name": "file.zip" } });
        let input = provisional_input_from_message(&message).expect("valid capture");
        assert_eq!(input.source, "https://cdn.example.test/file.zip");
        assert_eq!(input.name.as_deref(), Some("file.zip"));
        assert_eq!(input.media, Some(false));
    }

    #[test]
    fn media_capture_marks_media_without_explicit_flag() {
        let message = json!({ "type": "media-capture", "payload": { "source": "https://cdn.example.test/vod/index.m3u8" } });
        let input = provisional_input_from_message(&message).expect("valid media capture");
        assert_eq!(input.media, Some(true));
        assert_eq!(input.player_kind.as_deref(), None);
    }

    #[test]
    fn media_capture_preserves_explicit_player_kind_and_companion_audio() {
        let message = json!({ "type": "media-capture", "payload": { "source": "https://cdn.example.test/vod/video.mp4", "playerKind": "video", "companionAudio": "https://cdn.example.test/vod/audio.m4a", "selectedSegments": ["https://cdn.example.test/vod/audio.m4a"] } });
        let input = provisional_input_from_message(&message).expect("valid dual-track capture");
        assert_eq!(input.player_kind.as_deref(), Some("video"));
        assert_eq!(input.companion_audio.as_deref(), Some("https://cdn.example.test/vod/audio.m4a"));
        assert_eq!(input.selected_segments, ["https://cdn.example.test/vod/audio.m4a"]);
    }

    #[test]
    fn media_capture_rejects_invalid_player_metadata() {
        assert!(provisional_input_from_message(&json!({ "type": "media-capture", "payload": { "source": "https://cdn.example.test/vod/video.mp4", "playerKind": "document" } })).is_none());
        assert!(provisional_input_from_message(&json!({ "type": "media-capture", "payload": { "source": "https://cdn.example.test/vod/video.mp4", "playerKind": "audio", "companionAudio": "https://cdn.example.test/vod/audio.m4a" } })).is_none());
        assert!(provisional_input_from_message(&json!({ "type": "media-capture", "payload": { "source": "https://cdn.example.test/vod/video.mp4", "companionAudio": "file:///tmp/audio.m4a" } })).is_none());
    }

    #[test]
    fn unknown_media_bytes_reject_unambiguous_audio_signatures() {
        let mut mpeg = vec![0u8; 834];
        mpeg[..4].copy_from_slice(&[0xff, 0xfb, 0x90, 0x64]);
        mpeg[417..421].copy_from_slice(&[0xff, 0xfb, 0x90, 0x64]);
        assert!(super::body_looks_like_audio(b"ID3\x04\0\0"));
        assert!(super::body_looks_like_audio(b"fLaC\0\0\0\x22"));
        assert!(super::body_looks_like_audio(b"RIFF\0\0\0\0WAVE"));
        assert!(super::body_looks_like_audio(b"OggS\0\0\0\0OpusHead"));
        assert!(super::body_looks_like_audio(&mpeg));
        assert!(!super::body_looks_like_audio(b"\0\0\0\x18ftypmp42"));
    }

    #[test]
    fn capture_accepts_url_alias_and_trims_whitespace() {
        let message = json!({ "type": "capture-acquisition", "payload": { "url": "  http://127.0.0.1:8901/range.bin  " } });
        let input = provisional_input_from_message(&message).expect("url alias");
        assert_eq!(input.source, "http://127.0.0.1:8901/range.bin");
    }

    #[test]
    fn selected_dash_segments_are_limited_to_http_urls() {
        let message = json!({ "type": "media-capture", "payload": { "source": "https://cdn.example.test/vod/manifest.mpd", "selectedSegments": ["https://cdn.example.test/vod/one.m4v", "blob:https://cdn.example.test/no", "file:///tmp/no", "http://cdn.example.test/two.m4a"] } });
        let input = provisional_input_from_message(&message).expect("valid media capture");
        assert_eq!(
            input.selected_segments,
            [
                "https://cdn.example.test/vod/one.m4v",
                "http://cdn.example.test/two.m4a"
            ]
        );
    }

    #[test]
    fn capture_rejects_non_http_wrong_type_and_missing_source() {
        assert!(provisional_input_from_message(&json!({ "type": "capture-acquisition", "payload": { "source": "ftp://cdn.example.test/file.zip" } })).is_none());
        assert!(provisional_input_from_message(&json!({ "type": "open-manager" })).is_none());
        assert!(provisional_input_from_message(
            &json!({ "type": "capture-acquisition", "payload": {} })
        )
        .is_none());
        assert!(provisional_input_from_message(
            &json!({ "type": "media-capture", "payload": { "source": "blob:https://x.test/abc" } })
        )
        .is_none());
    }

    #[test]
    fn capture_args_parse_forwards_bridge_payload() {
        let raw = serde_json::to_string(&json!({ "type": "capture-acquisition", "payload": { "source": "http://127.0.0.1:8901/range.bin", "name": "range.bin" } })).unwrap();
        let args = vec!["download-manager".to_string(), "--capture".to_string(), raw];
        let input = capture_input_from_args(&args).expect("args capture");
        assert_eq!(input.source, "http://127.0.0.1:8901/range.bin");
        assert_eq!(input.name.as_deref(), Some("range.bin"));
    }

    #[test]
    fn capture_referrer_prefers_explicit_key_then_page_url() {
        let input = provisional_input_from_message(&json!({ "type": "capture-acquisition", "payload": { "source": "http://127.0.0.1:9/f.bin", "pageUrl": "http://127.0.0.1:9/page.html" } })).expect("pageUrl fallback");
        assert_eq!(
            input.referrer.as_deref(),
            Some("http://127.0.0.1:9/page.html")
        );
        let input = provisional_input_from_message(&json!({ "type": "capture-acquisition", "payload": { "source": "http://127.0.0.1:9/f.bin", "pageUrl": "http://127.0.0.1:9/page.html", "referrer": "http://127.0.0.1:9/other.html" } })).expect("explicit key");
        assert_eq!(
            input.referrer.as_deref(),
            Some("http://127.0.0.1:9/other.html")
        );
        let input = provisional_input_from_message(&json!({ "type": "capture-acquisition", "payload": { "source": "http://127.0.0.1:9/f.bin", "pageUrl": "not a url" } })).expect("bad pageUrl tolerated");
        assert_eq!(input.referrer, None);
        let input = provisional_input_from_message(&json!({ "type": "capture-acquisition", "payload": { "source": "http://127.0.0.1:9/f.bin" } })).expect("missing referrer tolerated");
        assert_eq!(input.referrer, None);
    }

    #[test]
    fn capture_post_body_keeps_small_bodies_only() {
        let input = provisional_input_from_message(&json!({ "type": "capture-acquisition", "payload": { "source": "http://127.0.0.1:9/purchase", "postBody": "fixture=post-only" } })).expect("post body kept");
        assert_eq!(input.post_body.as_deref(), Some("fixture=post-only"));
        let input = provisional_input_from_message(&json!({ "type": "capture-acquisition", "payload": { "source": "http://127.0.0.1:9/purchase" } })).expect("missing body tolerated");
        assert_eq!(input.post_body, None);
        let input = provisional_input_from_message(&json!({ "type": "capture-acquisition", "payload": { "source": "http://127.0.0.1:9/purchase", "postBody": "" } })).expect("empty body tolerated");
        assert_eq!(input.post_body, None);
        let big = "x".repeat(64 * 1024 + 1);
        let input = provisional_input_from_message(&json!({ "type": "capture-acquisition", "payload": { "source": "http://127.0.0.1:9/purchase", "postBody": big } })).expect("oversized body tolerated");
        assert_eq!(input.post_body, None);
    }

    #[test]
    fn capture_sources_are_ranked_by_url_evidence_before_fetching() {
        use super::url_implies_media;
        // A URL that names its own container never needs a probe.
        for media in [
            "https://cdn.test/vod/master.m3u8",
            "https://cdn.test/vod/manifest.mpd",
            "https://cdn.test/v/clip.mp4",
            "https://cdn.test/s/0001.m4s",
            "https://cdn.test/a/track.m4a?tag=1",
        ] {
            assert!(url_implies_media(media), "{media} should not need a probe");
        }
        // A page URL proves nothing, so it must be probed (or rejected).
        for page in [
            "https://cdn.test/watch/abc",
            "https://cdn.test/graphql/xyz",
            "https://cdn.test/page.html",
            "not a url",
        ] {
            assert!(!url_implies_media(page), "{page} must not be trusted as media");
        }
    }

    #[test]
    fn byte_range_fragments_are_recognized() {
        use super::url_is_byte_range_fragment;
        // Signed range query (YouTube shape).
        assert!(url_is_byte_range_fragment(
            "https://rr1---sn-x.googlevideo.com/videoplayback?expire=1&range=0-62000&sparams=expire%2Cid%2Crange&mime=video%2Fmp4"
        ));
        // Range as a path segment (Vimeo shape).
        assert!(url_is_byte_range_fragment(
            "https://vod-adaptive-ak.vimeocdn.com/exp=1~acl=%2Fx~hmac=ab/uuid/psid=1/v2/range/prot/cHI9NTQw/avf/x.mp4?pathsig=1~a"
        ));
        // Unsigned range: strippable, so it is not a fragment.
        assert!(!url_is_byte_range_fragment("https://cdn.test/video.mp4?range=0-1000"));
        assert!(!url_is_byte_range_fragment("https://cdn.test/video.mp4"));
        assert!(!url_is_byte_range_fragment("https://cdn.test/hls/master.m3u8"));
        assert!(!url_is_byte_range_fragment("not a url"));
        // Manifests are never fragments even when they carry a range parameter.
        assert!(crate::media::is_manifest_source(
            "https://cdn.test/hls/master.m3u8?range=0-1000&sparams=range",
            None
        ));
    }

    #[test]
    fn capture_candidates_are_validated_deduplicated_and_bounded() {
        use super::provisional_input_from_message;
        let message = json!({
            "type": "media-capture",
            "payload": {
                "source": "https://cdn.test/a.mp4",
                "candidates": [
                    "https://cdn.test/a.mp4",
                    "blob:https://cdn.test/9f0a",
                    "https://cdn.test/b.m3u8",
                    "https://cdn.test/b.m3u8",
                    "https://cdn.test/c.mp4",
                    "https://cdn.test/d.mp4",
                    "https://cdn.test/e.mp4",
                    "https://cdn.test/f.mp4",
                    "https://cdn.test/g.mp4",
                    "https://cdn.test/h.mp4",
                ],
            },
        });
        let input = provisional_input_from_message(&message).expect("valid capture");
        assert_eq!(
            input.candidates,
            vec![
                "https://cdn.test/b.m3u8",
                "https://cdn.test/c.mp4",
                "https://cdn.test/d.mp4",
                "https://cdn.test/e.mp4",
                "https://cdn.test/f.mp4",
                "https://cdn.test/g.mp4",
            ],
            "alternates drop the primary, non-http entries, duplicates, and everything past the cap"
        );
        // A capture without alternates keeps working exactly as before.
        let plain = provisional_input_from_message(&json!({
            "type": "media-capture",
            "payload": { "source": "https://cdn.test/a.mp4" },
        }))
        .expect("valid capture");
        assert!(plain.candidates.is_empty());
    }

    #[test]
    fn bridge_rejects_web_origins_and_foreign_hosts() {
        // Astra F01 reproduction: text/plain + https Origin must not reach a
        // mutation route. Absent Origin (non-browser local clients) stays
        // allowed behind the loopback bind.
        assert!(bridge_origin_allowed(&None));
        assert!(bridge_origin_allowed(&Some("chrome-extension://abc".into())));
        assert!(!bridge_origin_allowed(&Some("https://untrusted.example".into())));
        assert!(!bridge_origin_allowed(&Some("http://127.0.0.1:38217".into())));
        assert!(bridge_host_allowed(&None));
        assert!(bridge_host_allowed(&Some("127.0.0.1:38217".into())));
        assert!(bridge_host_allowed(&Some("localhost".into())));
        assert!(bridge_host_allowed(&Some("[::1]:38217".into())));
        assert!(!bridge_host_allowed(&Some("evil.example:38217".into())));
        assert!(!bridge_host_allowed(&Some("127.0.0.1.evil.example".into())));
        let json = Some("application/json".to_string());
        let json_charset = Some("application/json; charset=utf-8".to_string());
        let plain = Some("text/plain".to_string());
        assert!(bridge_json_content(&ipc::Request { method: "POST".into(), path: "/v1/capture".into(), body: vec![], host: None, origin: None, content_type: json }));
        assert!(bridge_json_content(&ipc::Request { method: "POST".into(), path: "/v1/capture".into(), body: vec![], host: None, origin: None, content_type: json_charset }));
        assert!(!bridge_json_content(&ipc::Request { method: "POST".into(), path: "/v1/capture".into(), body: vec![], host: None, origin: None, content_type: plain }));
        assert!(!bridge_json_content(&ipc::Request { method: "POST".into(), path: "/v1/capture".into(), body: vec![], host: None, origin: None, content_type: None }));
    }

    #[test]
    fn dead_source_urls_fail_fast_without_retry() {
        // Astra F05: 404/410 end the acquisition at the failing stage; the
        // engine must not grind through degrade/fallback sequences.
        assert!(terminal_source_status(reqwest::StatusCode::GONE));
        assert!(terminal_source_status(reqwest::StatusCode::NOT_FOUND));
        assert!(!terminal_source_status(reqwest::StatusCode::TOO_MANY_REQUESTS));
        assert!(!terminal_source_status(reqwest::StatusCode::INTERNAL_SERVER_ERROR));
        assert!(!retryable_status(reqwest::StatusCode::GONE));
        let error = terminal_source_error(reqwest::StatusCode::GONE);
        assert!(is_terminal_source_error(&error));
        assert!(!is_terminal_source_error("range request returned 503 Service Unavailable"));
    }

    #[test]
    fn html_pages_are_not_completed_as_named_files() {
        // Astra F06: a form/login/soft-error page must fail, never complete
        // as export.zip. Disposition-named files and real HTML names pass.
        assert!(page_instead_of_file(Some("text/html"), None, "export.zip"));
        assert!(page_instead_of_file(Some("text/html; charset=utf-8"), None, "export.zip"));
        assert!(!page_instead_of_file(Some("text/html"), Some("attachment; filename=\"export.zip\""), "export.zip"));
        assert!(!page_instead_of_file(Some("text/html"), Some("inline; filename=\"doc.html\""), "doc.html"));
        assert!(!page_instead_of_file(Some("text/html"), None, "page.html"));
        assert!(!page_instead_of_file(Some("text/html"), None, "page.HTM"));
        assert!(!page_instead_of_file(Some("application/zip"), None, "export.zip"));
        assert!(!page_instead_of_file(None, None, "export.zip"));
    }

    #[test]
    fn timestamps_are_real_chronological_instants() {
        // F15: stamps sort as text and parse as UTC instants.
        assert_eq!(super::utc_iso_label(0), "1970-01-01T00:00:00Z");
        assert_eq!(super::utc_iso_label(1_700_000_000), "2023-11-14T22:13:20Z");
        assert_eq!(super::utc_iso_label(-1), "1969-12-31T23:59:59Z");
        let now = super::now_label();
        assert_eq!(now.len(), 20, "unexpected stamp: {now}");
        assert!(now.ends_with('Z'));
        assert!(now > super::utc_iso_label(1_700_000_000), "stamp is not current: {now}");
    }

    #[test]
    fn segment_identity_covers_encryption_identity() {
        // Astra F10: rotated keys at unchanged URLs must not reuse old parts.
        let track = |segments: Vec<super::media::Segment>| super::media::MediaTrack {
            kind: "video".into(),
            segments,
            segment_base: None,
        };
        let plain = || super::media::Segment { url: "https://cdn.example.test/seg-0.m4s".into(), range: None, key: None };
        let key = |uri: &str| super::media::HlsKey { uri: uri.into(), iv: None, sequence: 3 };
        let keyed = |uri: &str| super::media::Segment { url: "https://cdn.example.test/seg-0.m4s".into(), range: None, key: Some(key(uri)) };
        assert_eq!(segment_identity(&[track(vec![plain()])]), segment_identity(&[track(vec![plain()])]));
        assert_ne!(segment_identity(&[track(vec![plain()])]), segment_identity(&[track(vec![keyed("https://cdn.example.test/k1")])]));
        assert_ne!(
            segment_identity(&[track(vec![keyed("https://cdn.example.test/k1")])]),
            segment_identity(&[track(vec![keyed("https://cdn.example.test/k2")])])
        );
    }

    #[test]
    fn manifest_output_name_follows_acquired_container() {
        // Astra F02: a page-title `.mp4` name must not mislabel TS bytes.
        // The stem is preserved; the acquired container decides the suffix.
        assert_eq!(manifest_output_name("Player title.mp4", "ts"), "Player title.ts");
        assert_eq!(manifest_output_name("real-ts.ts", "ts"), "real-ts.ts");
        assert_eq!(manifest_output_name("Clip.MP4", "ts"), "Clip.ts");
        assert_eq!(manifest_output_name("vod", "mp4"), "vod.mp4");
        assert_eq!(manifest_output_name("show.mp4", "mp4"), "show.mp4");
        assert_eq!(manifest_output_name("track.m4s", "mp4"), "track.mp4");
    }

    #[test]
    fn provisional_input_accepts_page_url_alias() {
        // The Tauri command path deserializes this struct directly, so it
        // must accept the extension's pageUrl key (the gated-media E2E
        // caught it being silently dropped there).
        let input: ProvisionalInput = serde_json::from_value(json!({ "source": "http://127.0.0.1:9/hls/gated.m3u8", "pageUrl": "http://127.0.0.1:9/page/video.html" })).expect("pageUrl alias");
        assert_eq!(input.referrer.as_deref(), Some("http://127.0.0.1:9/page/video.html"));
        let input: ProvisionalInput = serde_json::from_value(json!({ "source": "http://127.0.0.1:9/hls/gated.m3u8" })).expect("missing referrer tolerated");
        assert_eq!(input.referrer, None);
    }

    #[test]
    fn referer_value_scopes_to_origin() {
        assert_eq!(
            referer_value("http://127.0.0.1:9/page/video.html", "http://127.0.0.1:9/file/ref-gated.bin").as_deref(),
            Some("http://127.0.0.1:9/page/video.html"),
        );
        assert_eq!(
            referer_value("http://127.0.0.1:9/page/video.html?token=secret", "http://cdn.example.test/v.mp4").as_deref(),
            Some("http://127.0.0.1:9"),
        );
        assert_eq!(referer_value("not a url", "http://127.0.0.1:9/f.bin"), None);
        assert_eq!(referer_value("ftp://127.0.0.1:9/p", "http://127.0.0.1:9/f.bin"), None);
        assert_eq!(referer_value("", "http://127.0.0.1:9/f.bin"), None);
    }

    #[test]
    fn browser_policy_roundtrip_normalizes_sites() {
        let policy = (true, false, vec!["Example.COM".to_string(), "  ".to_string(), "cdn.example.test".to_string()]);
        let value = browser_policy_value(&policy);
        let parsed = browser_policy_from_value(&value).expect("policy roundtrip");
        assert_eq!(parsed.0, true);
        assert_eq!(parsed.1, false);
        assert_eq!(parsed.2, vec!["example.com".to_string(), "cdn.example.test".to_string()]);
    }

    #[test]
    fn browser_policy_migrates_url_and_www_site_entries() {
        let value = json!({
            "interceptDownloads": true,
            "showMediaButtons": true,
            "excludedSites": [" https://www.Example.com/watch/ ", "example.com:8443/path"]
        });
        let parsed = browser_policy_from_value(&value).expect("legacy policy");
        assert_eq!(parsed.2, vec!["example.com".to_string(), "example.com".to_string()]);
    }

    #[test]
    fn browser_policy_rejects_incomplete_payload() {
        assert!(browser_policy_from_value(&json!({ "interceptDownloads": true })).is_none());
    }

    #[test]
    fn resume_evidence_trusts_validators_and_samples_bytes_otherwise() {
        use super::{resume_evidence, ResumeEvidence, ResourceIdentity};
        let bare = ResourceIdentity { length: 123, etag: None, last_modified: None };
        // No validator on either side: the stored bytes must prove themselves.
        assert_eq!(resume_evidence(Some(&bare), &bare), ResumeEvidence::Sampled);
        assert_eq!(resume_evidence(None, &bare), ResumeEvidence::Rejected);
        let longer = ResourceIdentity { length: 124, etag: None, last_modified: None };
        assert_eq!(resume_evidence(Some(&bare), &longer), ResumeEvidence::Rejected);
        let tagged = ResourceIdentity { length: 123, etag: Some("\"v1\"".into()), last_modified: None };
        assert_eq!(resume_evidence(Some(&tagged), &tagged), ResumeEvidence::Trusted);
        let retagged = ResourceIdentity { length: 123, etag: Some("\"v2\"".into()), last_modified: None };
        assert_eq!(resume_evidence(Some(&tagged), &retagged), ResumeEvidence::Rejected);
        // A validator that disappeared cannot be read as agreement.
        assert_eq!(resume_evidence(Some(&tagged), &bare), ResumeEvidence::Sampled);
        let dated = ResourceIdentity { length: 123, etag: None, last_modified: Some("Mon".into()) };
        let redated = ResourceIdentity { length: 123, etag: None, last_modified: Some("Tue".into()) };
        assert_eq!(resume_evidence(Some(&dated), &redated), ResumeEvidence::Rejected);
    }

    #[test]
    fn reattach_compatibility_ignores_query_but_not_path() {
        assert!(source_compatible("https://cdn.example.test/vod/a.mp4?token=1", "https://cdn.example.test/vod/a.mp4?token=2"));
        assert!(!source_compatible("https://cdn.example.test/vod/a.mp4", "https://cdn.example.test/vod/b.mp4"));
        assert!(!source_compatible("https://cdn.example.test/vod/a.mp4", "https://other.example.test/vod/a.mp4"));
        assert!(!source_compatible("https://cdn.example.test/vod/a.mp4", "http://cdn.example.test/vod/a.mp4"));
        assert!(!source_compatible("https://cdn.example.test:8443/vod/a.mp4", "https://cdn.example.test:9443/vod/a.mp4"));
        assert!(source_compatible("https://cdn.example.test/vod/a.mp4", "https://cdn.example.test:443/vod/a.mp4"));
    }

    #[test]
    fn corrupt_settings_field_does_not_reset_everything() {
        use super::settings_from_stored;
        // A float limit is rejected by the u64 schema; the user's folders and
        // toggles must survive anyway.
        let stored = serde_json::json!({
            "startAtSignIn": false,
            "defaultFolder": "/tmp/custom-downloads",
            "bandwidthLimit": 1.5,
            "bandwidthUnit": "MB/s",
            "maxConnections": 4,
        })
        .to_string();
        let settings = settings_from_stored(&stored);
        assert_eq!(settings.bandwidth_limit, None);
        assert_eq!(settings.start_at_sign_in, false);
        assert_eq!(settings.default_folder, "/tmp/custom-downloads");
        assert_eq!(settings.max_connections, 4);
    }

    #[test]
    fn commit_cap_semantics_keep_clear_and_set() {
        use super::CommitInput;
        let keep: CommitInput = serde_json::from_str(r#"{"name":"a","destination":"b"}"#).unwrap();
        assert_eq!(keep.bandwidth_limit, None);
        let clear: CommitInput = serde_json::from_str(r#"{"name":"a","destination":"b","bandwidthLimit":null}"#).unwrap();
        assert_eq!(clear.bandwidth_limit, Some(None));
        let set: CommitInput = serde_json::from_str(r#"{"name":"a","destination":"b","bandwidthLimit":524288}"#).unwrap();
        assert_eq!(set.bandwidth_limit, Some(Some(524288)));
    }

    #[test]
    fn settings_fallbacks_stay_sane() {
        use super::settings_from_stored;
        let defaults = super::default_settings();
        assert_eq!(settings_from_stored("not json at all").default_folder, defaults.default_folder);
        // Valid payloads round-trip untouched.
        let round = serde_json::to_string(&defaults).unwrap();
        let parsed = settings_from_stored(&round);
        assert_eq!(parsed.max_connections, defaults.max_connections);
        assert_eq!(parsed.bandwidth_limit, defaults.bandwidth_limit);
    }

    #[test]
    fn settings_patch_keeps_good_keys_when_one_key_is_bad() {
        use super::{apply_settings_patch, default_settings};
        let current = default_settings();
        // A float limit is rejected by the u64 schema, but maxConnections
        // in the same patch must still apply.
        let patch = serde_json::json!({"maxConnections": 4, "bandwidthLimit": 1.5});
        let next = apply_settings_patch(&current, &patch);
        assert_eq!(next.max_connections, 4);
        assert_eq!(next.bandwidth_limit, None);
        // Clean patches apply fully; unknown future keys are ignored.
        let patch = serde_json::json!({"maxRetries": 3, "unknownFutureKey": true});
        let next = apply_settings_patch(&current, &patch);
        assert_eq!(next.max_connections, current.max_connections);
        assert_eq!(next.max_retries, 3);
        // Non-object patches leave settings untouched.
        let next = apply_settings_patch(&current, &serde_json::json!("nope"));
        assert_eq!(next.max_connections, current.max_connections);
    }

    #[test]
    fn settings_patch_survives_database_round_trip() {
        use super::{apply_settings_patch, default_settings, save_snapshot, settings_from_stored, AppSnapshot, BandwidthBucket, CoreState, TransferRegistry};
        use rusqlite::Connection;
        use std::sync::Mutex;
        let database = Connection::open_in_memory().unwrap();
        database.execute_batch("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL); CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);").unwrap();
        let state = CoreState { snapshot: Mutex::new(AppSnapshot { jobs: vec![], settings: default_settings(), connected: true, aggregate_speed: 0, notifications: vec![], bridge_available: true }), database: Mutex::new(database), reattach_target: Mutex::new(None), bandwidth: Mutex::new(BandwidthBucket { tokens: 0.0, updated: std::time::Instant::now() }), transfer_controls: TransferRegistry::default(), job_bandwidth: Mutex::new(std::collections::HashMap::new()), lifecycle: Mutex::new(()), tray_checks: Mutex::new(None), progress: Mutex::new(ProgressThrottle::default()) };
        // Simulate a live patch, then a restart: the patched values must
        // come back through the same SQL row and boot parser the app uses.
        let patched = apply_settings_patch(&default_settings(), &serde_json::json!({"maxConnections": 4, "bandwidthLimit": 1048576}));
        state.snapshot.lock().unwrap().settings = patched;
        save_snapshot(&state).expect("settings snapshot persistence");
        let payload: String = state
            .database
            .lock()
            .unwrap()
            .query_row("SELECT payload FROM settings WHERE id = 1", [], |row| {
                row.get(0)
            })
            .unwrap();
        let rebooted = settings_from_stored(&payload);
        assert_eq!(rebooted.max_connections, 4);
        assert_eq!(rebooted.bandwidth_limit, Some(1048576));
    }

    #[test]
    fn settings_patch_rejects_invalid_enum_and_zero_limit_values() {
        use super::{apply_settings_patch, default_settings};
        let current = default_settings();
        let patch = serde_json::json!({
            "closeBehavior": "unexpected",
            "collisionBehavior": "overwrite-everything",
            "bandwidthLimit": 0,
            "bandwidthUnit": "bits/s",
            "maxConnections": 0,
            "maxRetries": 21,
            "theme": "neon",
            "density": "tiny"
        });
        let next = apply_settings_patch(&current, &patch);
        assert_eq!(next.close_behavior, current.close_behavior);
        assert_eq!(next.collision_behavior, current.collision_behavior);
        assert_eq!(next.bandwidth_limit, current.bandwidth_limit);
        assert_eq!(next.bandwidth_unit, current.bandwidth_unit);
        assert_eq!(next.max_connections, current.max_connections);
        assert_eq!(next.max_retries, current.max_retries);
        assert_eq!(next.theme, current.theme);
        assert_eq!(next.density, current.density);
        let next = apply_settings_patch(&current, &serde_json::json!({"maxRetries": 3}));
        assert_eq!(next.max_retries, 3);
    }

    #[test]
    fn commit_during_active_acquisition_requires_idle_then_recheck() {
        use super::{commit_decision, CommitDecision};
        assert_eq!(commit_decision(Some(true), Some("ready"), 100.0, true), CommitDecision::WaitForIdle);
        assert_eq!(commit_decision(Some(true), Some("ready"), 100.0, false), CommitDecision::Accept);
        assert_eq!(commit_decision(Some(true), Some("downloading"), 48.0, true), CommitDecision::Accept);
        assert_eq!(commit_decision(Some(true), Some("paused"), 48.0, false), CommitDecision::Accept);
        assert_eq!(commit_decision(Some(true), Some("paused"), 100.0, false), CommitDecision::Accept);
        assert_eq!(commit_decision(Some(true), Some("completed"), 100.0, false), CommitDecision::Reject);
        assert_eq!(commit_decision(Some(false), Some("ready"), 100.0, false), CommitDecision::Reject);
        assert_eq!(commit_decision(None, None, 0.0, false), CommitDecision::Reject);
    }

    #[test]
    fn commit_post_wait_recheck_rejects_cancellation_and_non_finalizing_states() {
        use super::commit_is_ready;
        assert!(commit_is_ready(Some(true), Some("ready"), 100.0, false));
        assert!(!commit_is_ready(Some(true), Some("ready"), 100.0, true));
        assert!(!commit_is_ready(Some(true), Some("paused"), 100.0, false));
        assert!(!commit_is_ready(Some(true), Some("failed"), 100.0, false));
        assert!(!commit_is_ready(None, None, 0.0, false));
    }
    #[test]
    fn settings_patch_rejects_blank_folder_paths() {
        use super::{apply_settings_patch, default_settings, settings_from_stored};
        let mut current = default_settings();
        current.default_folder = String::from("/dl");
        let patch = serde_json::json!({"defaultFolder": "   ", "maxConnections": 6});
        let next = apply_settings_patch(&current, &patch);
        assert_eq!(next.default_folder, "/dl");
        assert_eq!(next.max_connections, 6);
        let patch = serde_json::json!({"defaultFolder": "/new-dl"});
        let next = apply_settings_patch(&current, &patch);
        assert_eq!(next.default_folder, "/new-dl");
        let stored = serde_json::json!({"defaultFolder": ""}).to_string();
        let rebooted = settings_from_stored(&stored);
        assert!(!rebooted.default_folder.trim().is_empty());
    }

    #[test]
    fn resume_all_flips_unfinished_jobs_and_leaves_confirmation_gates_alone() {
        use super::{plan_resume_all, AppSnapshot};
        let job = |id: &str, state: &str| provisional_job(id, state);
        let mut snapshot = AppSnapshot {
            jobs: vec![
                job("paused-1", "paused"),
                job("pending-1", "pending"),
                job("ready-1", "ready"),
                job("downloading-1", "downloading"),
            ],
            settings: super::default_settings(),
            connected: true,
            aggregate_speed: 0,
            notifications: vec![],
            bridge_available: true,
        };
        let sources = plan_resume_all(&mut snapshot, "Resumed", |_| false);
        assert_eq!(
            sources.iter().map(|(id, _)| id.as_str()).collect::<Vec<_>>(),
            vec!["paused-1", "pending-1"]
        );
        assert_eq!(snapshot.jobs[0].state, "downloading");
        assert_eq!(snapshot.jobs[1].state, "downloading");
        assert_eq!(snapshot.jobs[2].state, "ready");
        assert_eq!(snapshot.jobs[3].state, "downloading");
    }

    #[test]
    fn collision_reservation_allocates_distinct_paths_before_moves() {
        use super::reserve_collision_destination_with_marker;
        let root = std::env::temp_dir().join(format!("download-manager-collision-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let requested = root.join("same.bin").to_string_lossy().into_owned();
        let first = reserve_collision_destination_with_marker(&requested).unwrap().0;
        let second = reserve_collision_destination_with_marker(&requested).unwrap().0;
        assert_eq!(first, requested);
        assert_eq!(second, root.join("same (1).bin").to_string_lossy());
        std::fs::remove_file(first).unwrap();
        std::fs::remove_file(second).unwrap();
        std::fs::remove_dir(root).unwrap();
    }

    #[test]
    fn move_fallback_accepts_existing_and_cross_device_errors() {
        use super::move_needs_fallback;
        assert!(move_needs_fallback(&std::io::Error::from_raw_os_error(17)));
        assert!(move_needs_fallback(&std::io::Error::from_raw_os_error(18)));
        assert!(move_needs_fallback(&std::io::Error::from_raw_os_error(183)));
        assert!(!move_needs_fallback(&std::io::Error::from_raw_os_error(13)));
    }

    #[cfg(windows)]
    #[test]
    fn relocates_segment_directory_with_nested_parts() {
        use super::relocate_temp_artifact;
        let root = std::env::temp_dir().join(format!("download-manager-segments-{}", uuid::Uuid::new_v4()));
        let source = root.join("old.part.segments");
        let destination = root.join("new.part.segments");
        std::fs::create_dir_all(source.join("nested")).unwrap();
        std::fs::write(source.join("nested").join("segment-1"), b"segment").unwrap();
        assert!(relocate_temp_artifact(&source, &destination));
        assert!(!source.exists());
        assert_eq!(std::fs::read(destination.join("nested").join("segment-1")).unwrap(), b"segment");
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn abort_cleanup_preserves_changed_reserved_destination() {
        use super::cleanup_reserved_destination;
        let root = std::env::temp_dir().join(format!("download-manager-abort-owner-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let destination = root.join("managed.bin");
        let marker = "download-manager-reservation-v1:test-abort";
        std::fs::write(&destination, b"foreign-output").unwrap();

        let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        runtime.block_on(cleanup_reserved_destination(destination.to_str().unwrap(), Some(marker)));
        assert_eq!(std::fs::read(&destination).unwrap(), b"foreign-output", "abort cleanup must not delete changed destination");
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn reserved_nonfallback_error_preserves_changed_destination() {
        use super::{inject_reserved_nonfallback_for_test, move_completed_file};
        let root = std::env::temp_dir().join(format!("download-manager-reservation-owner-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let source = root.join("source.part");
        let destination = root.join("managed.bin");
        let marker = "download-manager-reservation-v1:test-owner";
        std::fs::write(&source, b"source").unwrap();
        std::fs::write(&destination, b"foreign-output").unwrap();
        inject_reserved_nonfallback_for_test();

        let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        let result = runtime.block_on(move_completed_file(source.to_str().unwrap(), destination.to_str().unwrap(), false, Some(marker)));
        assert!(result.is_err(), "injected non-fallback move failure must be reported");
        assert_eq!(std::fs::read(&destination).unwrap(), b"foreign-output", "changed destination must not be deleted");
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn durable_reserved_destination_survives_source_cleanup_failure() {
        use super::{inject_reserved_fallback_for_test, inject_source_cleanup_failure_for_test, move_completed_file};
        let root = std::env::temp_dir().join(format!("download-manager-source-cleanup-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let source = root.join("source.part");
        let destination = root.join("managed.bin");
        let marker = "download-manager-reservation-v1:test-source-cleanup";
        std::fs::write(&source, b"complete-output").unwrap();
        std::fs::write(&destination, marker.as_bytes()).unwrap();
        inject_reserved_fallback_for_test();
        inject_source_cleanup_failure_for_test();

        let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        let result = runtime.block_on(move_completed_file(source.to_str().unwrap(), destination.to_str().unwrap(), false, Some(marker)));
        assert!(result.is_err(), "injected source cleanup failure must be reported");
        assert_eq!(std::fs::read(&destination).unwrap(), b"complete-output", "durable output must survive source cleanup failure");
        assert!(source.exists(), "failed source cleanup must leave the source for diagnosis/retry");
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn replacement_rollback_reports_preserved_backup_on_restore_failure() {
        use super::{inject_replacement_restore_failure_for_test, restore_replacement_backup};
        let root = std::env::temp_dir().join(format!("download-manager-replace-rollback-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let backup = root.join("destination.backup");
        let destination = root.join("destination.bin");
        std::fs::write(&backup, b"old-output").unwrap();
        inject_replacement_restore_failure_for_test();

        let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        let result = runtime.block_on(restore_replacement_backup(backup.to_str().unwrap(), destination.to_str().unwrap()));
        assert!(result.is_err(), "fault injection must make backup restoration fail");
        assert!(backup.exists(), "backup must remain available after restoration failure");
        assert!(!destination.exists(), "failed restoration must not fabricate a destination");
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn reserved_staging_install_restores_marker_when_final_rename_fails() {
        use super::{inject_reserved_install_failure_for_test, install_reserved_staging};
        let root = std::env::temp_dir().join(format!("download-manager-reservation-rollback-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let destination = root.join("managed.bin");
        let staging = root.join("managed.bin.download-manager-staging-test");
        let marker = "download-manager-reservation-v1:test-marker";
        std::fs::write(&destination, marker.as_bytes()).unwrap();
        std::fs::write(&staging, b"complete-output").unwrap();
        inject_reserved_install_failure_for_test();

        let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        let result = runtime.block_on(install_reserved_staging(staging.to_str().unwrap(), destination.to_str().unwrap(), marker));
        assert!(result.is_err(), "fault injection must make the final install fail");
        assert_eq!(std::fs::read(&destination).unwrap(), marker.as_bytes(), "reservation marker must be restored");
        assert!(!staging.exists(), "failed install must clean staging");
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn destination_reservation_recovery_removes_only_its_marker() {
        use super::{destination_reservation_marker, reconcile_destination_reservation, DestinationReservationRecovery};
        let root = std::env::temp_dir().join(format!("download-manager-reservation-recovery-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let target = root.join("managed.bin");
        let marker = destination_reservation_marker();
        let other_marker = destination_reservation_marker();
        assert_ne!(other_marker, marker);
        std::fs::write(&target, other_marker.as_bytes()).unwrap();
        assert_eq!(reconcile_destination_reservation(&target, &marker, false), DestinationReservationRecovery::Unknown);
        assert_eq!(std::fs::read(&target).unwrap(), other_marker.as_bytes());
        std::fs::write(&target, marker.as_bytes()).unwrap();
        assert_eq!(reconcile_destination_reservation(&target, &marker, false), DestinationReservationRecovery::Retry);
        assert!(!target.exists());
        std::fs::write(&target, marker.as_bytes()[..marker.len() / 2].to_vec()).unwrap();
        assert_eq!(reconcile_destination_reservation(&target, &marker, false), DestinationReservationRecovery::Retry);
        assert!(!target.exists());
        std::fs::write(&target, b"completed-output").unwrap();
        assert_eq!(reconcile_destination_reservation(&target, &marker, false), DestinationReservationRecovery::Completed);
        assert_eq!(std::fs::read(&target).unwrap(), b"completed-output");
        std::fs::write(&target, []).unwrap();
        assert_eq!(reconcile_destination_reservation(&target, &marker, false), DestinationReservationRecovery::Retry);
        assert!(!target.exists());
        std::fs::write(&target, []).unwrap();
        assert_eq!(reconcile_destination_reservation(&target, &marker, true), DestinationReservationRecovery::Completed);
        assert!(target.exists());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn user_filename_is_reduced_to_a_safe_leaf() {
        use super::{destination_for_filename, safe_filename};
        assert_eq!(safe_filename("../escape.bin"), "escape.bin");
        assert_eq!(safe_filename(r"C:\\Users\\kaz\\escape.bin"), "escape.bin");
        assert_eq!(safe_filename("  report.pdf  "), "report.pdf");
        assert_eq!(safe_filename("bad:name?.bin"), "bad_name_.bin");
        #[cfg(windows)]
        assert_eq!(safe_filename("CON.txt"), "_CON.txt");
        assert_eq!(safe_filename("../"), "download.bin");
        assert_eq!(
            destination_for_filename("/downloads", "../../outside.bin"),
            std::path::Path::new("/downloads")
                .join("outside.bin")
                .to_string_lossy()
        );
    }

    fn ranges(pairs: &[(u64, u64)]) -> Vec<super::ByteRange> {
        pairs.iter().map(|(start, end)| super::ByteRange { start: *start, end: *end }).collect()
    }

    fn pairs(ranges: &[super::ByteRange]) -> Vec<(u64, u64)> {
        ranges.iter().map(|range| (range.start, range.end)).collect()
    }

    #[test]
    fn merge_range_coalesces_overlap_and_adjacency() {
        use super::merge_range;
        // Overlapping.
        assert_eq!(pairs(&merge_range(&ranges(&[(0, 100)]), super::ByteRange { start: 50, end: 200 })), [(0, 200)]);
        // Adjacent (end+1) merges — byte ranges are inclusive.
        assert_eq!(pairs(&merge_range(&ranges(&[(0, 99)]), super::ByteRange { start: 100, end: 199 })), [(0, 199)]);
        // Disjoint stays split and sorted even when added out of order.
        assert_eq!(pairs(&merge_range(&ranges(&[(500, 599)]), super::ByteRange { start: 0, end: 99 })), [(0, 99), (500, 599)]);
        // A bridge spanning two ranges collapses all three.
        assert_eq!(
            pairs(&merge_range(&ranges(&[(0, 99), (200, 299)]), super::ByteRange { start: 50, end: 250 })),
            [(0, 299)]
        );
    }

    #[test]
    fn covered_bytes_counts_inclusive_ends() {
        use super::covered_bytes;
        assert_eq!(covered_bytes(&[]), 0);
        assert_eq!(covered_bytes(&ranges(&[(0, 0)])), 1);
        assert_eq!(covered_bytes(&ranges(&[(0, 99), (200, 299)])), 200);
    }

    #[test]
    fn missing_ranges_returns_complement_in_bounded_chunks() {
        use super::missing_ranges;
        // 5 MiB untouched with 8 workers: chunk = max(5MiB/32, 1MiB) = 1MiB.
        let missing = missing_ranges(5 * 1024 * 1024, &[], 8);
        assert_eq!(missing.len(), 5);
        assert_eq!(missing[0], (0, 1024 * 1024 - 1));
        assert_eq!(missing[4], (4 * 1024 * 1024, 5 * 1024 * 1024 - 1));
        // Fully covered means no work left.
        assert!(missing_ranges(1024, &ranges(&[(0, 1023)]), 8).is_empty());
        // A middle gap is the only work; completed edges are excluded.
        assert_eq!(
            missing_ranges(3 * 1024 * 1024, &ranges(&[(0, 1024 * 1024 - 1), (2 * 1024 * 1024, 3 * 1024 * 1024 - 1)]), 8),
            [(1024 * 1024, 2 * 1024 * 1024 - 1)]
        );
        // Zero-length resources need no ranges.
        assert!(missing_ranges(0, &[], 8).is_empty());
    }

    #[test]
    fn commit_idle_wait_observes_active_owner_release() {
        let state = std::sync::Arc::new(super::CoreState {
            snapshot: std::sync::Mutex::new(super::AppSnapshot { jobs: Vec::new(), settings: super::default_settings(), connected: false, aggregate_speed: 0, notifications: Vec::new(), bridge_available: false }),
            database: std::sync::Mutex::new(rusqlite::Connection::open_in_memory().unwrap()),
            reattach_target: std::sync::Mutex::new(None),
            bandwidth: std::sync::Mutex::new(super::BandwidthBucket { tokens: 0.0, updated: std::time::Instant::now() }),
            job_bandwidth: std::sync::Mutex::new(std::collections::HashMap::new()),
            transfer_controls: super::TransferRegistry::default(),
            lifecycle: std::sync::Mutex::new(()),
            tray_checks: std::sync::Mutex::new(None),
            progress: std::sync::Mutex::new(super::ProgressThrottle::default()),
        });
        let (generation, _) = state.transfer_controls.claim_with_generation("job-1").expect("active owner");
        let release_state = std::sync::Arc::clone(&state);
        std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(40));
            release_state.transfer_controls.release_if_current("job-1", generation);
        });
        let runtime = tokio::runtime::Builder::new_current_thread().enable_time().build().unwrap();
        assert!(runtime.block_on(super::commit_wait_for_transfer_idle(&state, "job-1")));
        assert!(!state.transfer_controls.is_active("job-1"));
    }

    #[test]
    fn settings_update_rolls_back_when_snapshot_persistence_fails() {
        use super::{default_settings, update_settings_snapshot, AppSnapshot, BandwidthBucket, CoreState, TransferRegistry};
        use rusqlite::Connection;
        use serde_json::json;
        use std::sync::Mutex;

        let database = Connection::open_in_memory().unwrap();
        database.execute_batch("CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);").unwrap();
        database.execute("INSERT INTO settings (id, payload) VALUES (1, ?1)", rusqlite::params!["old-settings"]).unwrap();
        database.execute_batch("CREATE TRIGGER reject_settings BEFORE INSERT ON settings BEGIN SELECT RAISE(ABORT, 'settings locked'); END;").unwrap();
        let state = CoreState {
            snapshot: Mutex::new(AppSnapshot { jobs: Vec::new(), settings: default_settings(), connected: true, aggregate_speed: 0, notifications: Vec::new(), bridge_available: true }),
            database: Mutex::new(database),
            reattach_target: Mutex::new(None),
            bandwidth: Mutex::new(BandwidthBucket { tokens: 0.0, updated: std::time::Instant::now() }),
            job_bandwidth: Mutex::new(std::collections::HashMap::new()),
            transfer_controls: TransferRegistry::default(),
            lifecycle: Mutex::new(()),
            tray_checks: Mutex::new(None),
            progress: Mutex::new(ProgressThrottle::default()),
        };
        let before = state.snapshot.lock().unwrap().clone();
        let error = update_settings_snapshot(&state, &json!({ "maxConnections": 7 })).expect_err("settings persistence failure must reach the command");
        assert!(error.contains("settings locked"), "unexpected settings error: {error}");
        assert_eq!(state.snapshot.lock().unwrap().settings.max_connections, before.settings.max_connections);
        let database = state.database.lock().unwrap();
        let stored: String = database
            .query_row("SELECT payload FROM settings WHERE id = 1", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(stored, "old-settings");
    }

    #[test]
    fn settings_update_reports_startup_flag_changes() {
        // F14: the command must only touch the OS startup entry when its
        // setting changed. The snapshot layer reports both sides.
        use super::{default_settings, AppSnapshot, BandwidthBucket, CoreState, TransferRegistry};
        use rusqlite::Connection;
        use serde_json::json;
        use std::sync::Mutex;
        let database = Connection::open_in_memory().unwrap();
        database.execute_batch("CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL); CREATE TABLE settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);").unwrap();
        let state = CoreState {
            snapshot: Mutex::new(AppSnapshot { jobs: vec![], settings: default_settings(), connected: true, aggregate_speed: 0, notifications: vec![], bridge_available: true }),
            database: Mutex::new(database),
            reattach_target: Mutex::new(None),
            bandwidth: Mutex::new(BandwidthBucket { tokens: 0.0, updated: std::time::Instant::now() }),
            transfer_controls: TransferRegistry::default(),
            job_bandwidth: Mutex::new(std::collections::HashMap::new()),
            lifecycle: Mutex::new(()),
            tray_checks: Mutex::new(None),
            progress: Mutex::new(ProgressThrottle::default()),
        };
        assert!(state.snapshot.lock().unwrap().settings.start_at_sign_in);
        let (previous, checks) = update_settings_snapshot(&state, &json!({ "theme": "dark" })).expect("theme patch").expect("object patch");
        assert_eq!(previous, checks.2, "theme-only patch must not flag a startup change");
        let (previous, checks) = update_settings_snapshot(&state, &json!({ "startAtSignIn": false })).expect("startup patch").expect("object patch");
        assert_ne!(previous, checks.2, "startup toggle must flag a startup change");
    }

    #[test]
    fn browser_policy_cache_failure_propagates() {
        // F14: cache failures reach the caller for rollback, never silence.
        let probe = std::env::temp_dir().join(format!("dm-policy-probe-{}", std::process::id()));
        std::fs::write(&probe, b"not a dir").unwrap();
        let result = write_browser_policy(&probe.join("child"), &(true, true, Vec::new()));
        let _ = std::fs::remove_file(&probe);
        assert!(result.is_err(), "file-as-directory must fail loudly");
    }

    #[test]
    fn snapshot_persistence_is_atomic_when_job_write_fails() {
        use super::{save_snapshot, AppSnapshot, BandwidthBucket, CoreState, TransferRegistry};
        use rusqlite::Connection;
        use std::sync::Mutex;

        let job = |id: &str| {
            serde_json::from_value(serde_json::json!({
                "id": id,
                "name": "file.bin",
                "source": "http://127.0.0.1:9/file.bin",
                "domain": "127.0.0.1",
                "kind": "document",
                "state": "downloading",
                "progress": 0.0,
                "downloaded": 0,
                "total": 1,
                "speed": 0,
                "eta": null,
                "connections": 1,
                "maxConnections": 1,
                "bandwidthLimit": null,
                "mode": "single-stream",
                "media": false,
                "mediaDetails": null,
                "mediaTracks": null,
                "destination": "/tmp/file.bin",
                "tempPath": "/tmp/file.part",
                "resumable": false,
                "mime": null,
                "error": null,
                "created": "now",
                "started": null,
                "completed": null,
                "provisional": false,
                "segments": null,
                "completedRanges": [],
                "resourceIdentity": null,
                "destinationReservation": null,
                "selectedSegments": [],
                "referrer": null,
                "postBody": null,
                "userAgent": null,
                "events": [{"at": "now", "message": "test", "tone": null}]
            }))
            .unwrap()
        };

        let database = Connection::open_in_memory().unwrap();
        database.execute_batch("CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL); CREATE TABLE settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);").unwrap();
        database.execute("INSERT INTO jobs (id, created_at, payload) VALUES (?1, ?2, ?3)", rusqlite::params!["old-job", "old", "old-payload"]).unwrap();
        database.execute("INSERT INTO settings (id, payload) VALUES (1, ?1)", rusqlite::params!["old-settings"]).unwrap();
        let state = CoreState {
            snapshot: Mutex::new(AppSnapshot { jobs: vec![job("same-id"), job("same-id")], settings: super::default_settings(), connected: true, aggregate_speed: 0, notifications: vec![], bridge_available: true }),
            database: Mutex::new(database),
            reattach_target: Mutex::new(None),
            bandwidth: Mutex::new(BandwidthBucket { tokens: 0.0, updated: std::time::Instant::now() }),
            job_bandwidth: Mutex::new(std::collections::HashMap::new()),
            transfer_controls: TransferRegistry::default(),
            lifecycle: Mutex::new(()),
            tray_checks: Mutex::new(None),
            progress: Mutex::new(ProgressThrottle::default()),
        };

        let error = save_snapshot(&state).expect_err("duplicate job ids must fail snapshot persistence");
        assert!(error.contains("same-id"), "unexpected persistence error: {error}");
        let database = state.database.lock().unwrap();
        let payload: String = database
            .query_row("SELECT payload FROM jobs WHERE id = 'old-job'", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(payload, "old-payload");
        let settings: String = database
            .query_row("SELECT payload FROM settings WHERE id = 1", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(settings, "old-settings");
        let count: i64 = database.query_row("SELECT COUNT(*) FROM jobs", [], |row| row.get(0)).unwrap();
        assert_eq!(count, 1);
    }

    fn finished_job_with_context() -> DownloadJob {
        DownloadJob { id: "done-1".into(), name: "archive.zip".into(), source: "https://cdn.example.test/archive.zip?token=abc".into(), domain: "cdn.example.test".into(), kind: "archive".into(), state: "downloading".into(), progress: 100.0, downloaded: 1024, total: Some(1024), speed: 0, eta: None, connections: 1, max_connections: 8, bandwidth_limit: None, mode: "whole-object".into(), media: false, media_details: None, media_tracks: None, destination: "/tmp/archive.zip".into(), temp_path: "/tmp/done-1.part".into(), resumable: true, mime: None, error: None, created: "now".into(), started: Some("now".into()), completed: None, provisional: Some(false), segments: None, completed_ranges: vec![], resource_identity: None, destination_reservation: None, selected_segments: vec![], player_kind: None, companion_audio: None, referrer: Some("https://example.test/page?session=s3cr3t".into()), post_body: Some("fixture=renewed".into()), user_agent: Some("Mozilla/5.0".into()), candidates: vec![], events: vec![] }
    }

    fn provisional_job(id: &str, state: &str) -> DownloadJob {
        let mut job = finished_job_with_context();
        job.id = id.into();
        job.name = format!("{id}.bin");
        job.state = state.into();
        job.progress = 0.0;
        job.downloaded = 0;
        job.temp_path = format!("/tmp/{id}.part");
        job.provisional = Some(true);
        job.events.clear();
        job
    }

    #[test]
    fn ready_for_confirmation_is_a_gate_not_a_transfer() {
        let mut job = provisional_job("provisional-1", "downloading");
        job.progress = 40.0;
        job.speed = 1024;
        job.connections = 4;
        mark_ready_for_confirmation(&mut job, "Download ready; waiting for destination");
        assert_eq!(job.state, "ready");
        assert_eq!(job.progress, 100.0);
        assert_eq!(job.speed, 0);
        assert_eq!(job.connections, 0);
        assert_eq!(job.eta, None);
        assert_eq!(job.events[0].message, "Download ready; waiting for destination");
        assert_eq!(job.provisional, Some(true), "the user still owns the decision");
    }

    #[test]
    fn boot_keeps_the_list_and_the_store_in_agreement() {
        let database = Connection::open_in_memory().unwrap();
        database
            .execute_batch(
                "CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
                 CREATE TABLE notifications (id TEXT PRIMARY KEY, payload TEXT);",
            )
            .unwrap();
        let durable = finished_job_with_context();
        // An unaccepted provisional is explicitly not durable, and a payload that
        // cannot be read is not a download either: neither may linger in the
        // store where the list cannot show it.
        let provisional = provisional_job("provisional-ghost", "completed");
        for job in [&durable, &provisional] {
            database
                .execute(
                    "INSERT INTO jobs (id, created_at, payload) VALUES (?1, ?2, ?3)",
                    rusqlite::params![job.id, job.created, serde_json::to_string(job).unwrap()],
                )
                .unwrap();
        }
        database
            .execute(
                "INSERT INTO jobs (id, created_at, payload) VALUES ('broken-1', 'now', 'not json')",
                [],
            )
            .unwrap();
        let notification = NotificationItem {
            id: format!("completed-{}", provisional.id),
            notification_type: "completed".into(),
            title: "Download completed".into(),
            detail: "provisional-ghost.bin".into(),
            time: "now".into(),
            job_id: provisional.id.clone(),
        };
        database
            .execute(
                "INSERT INTO notifications (id, payload) VALUES (?1, ?2)",
                rusqlite::params![notification.id, serde_json::to_string(&notification).unwrap()],
            )
            .unwrap();

        let snapshot = snapshot_from_database(&database, default_settings());

        let listed = snapshot.jobs.iter().map(|job| job.id.as_str()).collect::<Vec<_>>();
        assert_eq!(listed, vec![durable.id.as_str()], "only durable downloads are listed");
        let stored: i64 = database
            .query_row("SELECT COUNT(*) FROM jobs", [], |row| row.get(0))
            .unwrap();
        assert_eq!(stored, 1, "the store keeps exactly what the list shows");
        assert!(
            snapshot.notifications.is_empty(),
            "a job that leaves the list takes its notifications with it"
        );
    }

    #[test]
    fn sweep_temp_root_keeps_job_artifacts_and_removes_the_rest() {
        let root = std::env::temp_dir().join(format!("download-manager-sweep-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(root.join("job-1.part.segments")).unwrap();
        std::fs::write(root.join("job-1.part"), b"partial").unwrap();
        std::fs::write(root.join("job-1.part.track-01"), b"track").unwrap();
        std::fs::write(root.join("job-2.part"), b"crashed transfer").unwrap();
        std::fs::write(root.join("job-2.part.track-00"), b"crashed track").unwrap();
        std::fs::write(root.join("job.part.track-00"), b"unrelated").unwrap();
        std::fs::write(root.join("scratch.tmp"), b"debris").unwrap();
        std::fs::create_dir_all(root.join("orphan-dir")).unwrap();
        sweep_temp_root(&root, &[provisional_job("job-1", "paused")]);
        assert!(root.join("job-1.part").exists());
        assert!(root.join("job-1.part.segments").exists());
        assert!(root.join("job-1.part.track-01").exists());
        assert!(!root.join("job-2.part").exists());
        assert!(!root.join("job-2.part.track-00").exists());
        assert!(!root.join("job.part.track-00").exists());
        assert!(!root.join("scratch.tmp").exists());
        assert!(!root.join("orphan-dir").exists());
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn completed_job_drops_replay_context() {
        let mut job = finished_job_with_context();
        complete_job(&mut job);
        assert_eq!(job.state, "completed");
        assert_eq!(job.referrer, None, "capture-page URL must not outlive the job");
        assert_eq!(job.post_body, None, "form body must not outlive the job");
        assert_eq!(job.user_agent, None, "browser UA must not outlive the job");
    }

    #[test]
    fn stored_job_hides_signed_source_and_restores_it() {
        // F11: the database payload must not contain the signed query, but
        // the live job keeps working after a save/load cycle on this machine.
        let mut job = finished_job_with_context();
        let source = job.source.clone();
        protect_job_for_storage(&mut job);
        let payload = serde_json::to_string(&job).expect("stored job serializes");
        assert!(!payload.contains("token=abc"), "signed query at rest: {payload}");
        let mut loaded: DownloadJob = serde_json::from_str(&payload).expect("stored job loads");
        unprotect_job_from_storage(&mut loaded).expect("own envelopes open here");
        assert_eq!(loaded.source, source);
    }

    #[test]
    fn persist_job_upserts_only_that_job() {
        // F09: hot-loop checkpoints rewrite one row, never the whole table.
        use super::{default_settings, save_snapshot, AppSnapshot, BandwidthBucket, CoreState, TransferRegistry};
        use rusqlite::Connection;
        use std::sync::Mutex;
        let database = Connection::open_in_memory().unwrap();
        database.execute_batch("CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL); CREATE TABLE settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);").unwrap();
        let second = DownloadJob { id: "done-2".into(), source: "https://cdn.example.test/other.bin".into(), ..finished_job_with_context() };
        let state = CoreState {
            snapshot: Mutex::new(AppSnapshot { jobs: vec![finished_job_with_context(), second], settings: default_settings(), connected: true, aggregate_speed: 0, notifications: vec![], bridge_available: true }),
            database: Mutex::new(database),
            reattach_target: Mutex::new(None),
            bandwidth: Mutex::new(BandwidthBucket { tokens: 0.0, updated: std::time::Instant::now() }),
            transfer_controls: TransferRegistry::default(),
            job_bandwidth: Mutex::new(std::collections::HashMap::new()),
            lifecycle: Mutex::new(()),
            tray_checks: Mutex::new(None),
            progress: Mutex::new(ProgressThrottle::default()),
        };
        save_snapshot(&state).expect("seed both jobs");
        state.snapshot.lock().unwrap().jobs[0].downloaded = 2048;
        persist_job(&state, "done-1").expect("upsert one job");
        let database = state.database.lock().unwrap();
        let first_payload: String = database.query_row("SELECT payload FROM jobs WHERE id = 'done-1'", [], |row| row.get(0)).unwrap();
        let second_payload: String = database.query_row("SELECT payload FROM jobs WHERE id = 'done-2'", [], |row| row.get(0)).unwrap();
        assert!(first_payload.contains("2048"), "updated job missing: {first_payload}");
        assert!(!first_payload.contains("token=abc"), "signed query at rest: {first_payload}");
        assert!(second_payload.contains("1024"), "sibling job rewritten: {second_payload}");
        drop(database);
        state.snapshot.lock().unwrap().jobs[1].downloaded = 4096;
        state.progress.lock().unwrap().dirty_jobs.insert("done-2".into());
        persist_dirty_jobs(&state).expect("drain dirty set");
        assert!(state.progress.lock().unwrap().dirty_jobs.is_empty());
        let database = state.database.lock().unwrap();
        let second_payload: String = database.query_row("SELECT payload FROM jobs WHERE id = 'done-2'", [], |row| row.get(0)).unwrap();
        assert!(second_payload.contains("4096"), "dirty job missing: {second_payload}");
    }

    #[test]
    fn notification_center_survives_a_restart() {
        // F16: the durable in-app center reloads; the OS toast is only a shortcut.
        use super::{default_settings, load_notifications, persist_notifications, AppSnapshot, BandwidthBucket, CoreState, TransferRegistry};
        use rusqlite::Connection;
        use std::sync::Mutex;
        let database = Connection::open_in_memory().unwrap();
        database.execute_batch("CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL); CREATE TABLE settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL); CREATE TABLE notifications (id TEXT PRIMARY KEY, payload TEXT);").unwrap();
        let state = CoreState {
            snapshot: Mutex::new(AppSnapshot { jobs: vec![], settings: default_settings(), connected: true, aggregate_speed: 0, notifications: vec![super::NotificationItem { id: "completed-done-1".into(), notification_type: "completed".into(), title: "Download completed".into(), detail: "archive.zip".into(), time: "2026-09-08T05:00:00Z".into(), job_id: "done-1".into() }], bridge_available: true }),
            database: Mutex::new(database),
            reattach_target: Mutex::new(None),
            bandwidth: Mutex::new(BandwidthBucket { tokens: 0.0, updated: std::time::Instant::now() }),
            transfer_controls: TransferRegistry::default(),
            job_bandwidth: Mutex::new(std::collections::HashMap::new()),
            lifecycle: Mutex::new(()),
            tray_checks: Mutex::new(None),
            progress: Mutex::new(ProgressThrottle::default()),
        };
        persist_notifications(&state);
        let database = state.database.lock().unwrap();
        let loaded = load_notifications(&database);
        assert_eq!(loaded.len(), 1);
        assert_eq!(loaded[0].id, "completed-done-1");
        assert_eq!(loaded[0].detail, "archive.zip");
    }

    #[test]
    fn transport_error_redacts_url_credentials() {
        let raw = "error sending request for url (https://cdn.example.test/v.mp4?token=secret&sig=abc#frag): connection closed";
        let clean = redact_url_credentials(raw);
        assert!(!clean.contains("token=secret"), "query leaked: {clean}");
        assert!(!clean.contains("#frag"), "fragment leaked: {clean}");
        assert!(clean.contains("https://cdn.example.test/v.mp4"), "host/path lost: {clean}");
        assert_eq!(redact_url_credentials("Could not write temporary data"), "Could not write temporary data");
    }

    #[test]
    fn tray_toggle_flips_one_flag_only() {
        assert_eq!(tray_toggle_next((true, true), "browser-integration"), (false, true));
        assert_eq!(tray_toggle_next((true, true), "media-buttons"), (true, false));
        assert_eq!(tray_toggle_next((false, true), "browser-integration"), (true, true));
        assert_eq!(tray_toggle_next((true, false), "bogus"), (true, false));
    }

    #[test]
    fn data_dir_restricts_permissions() {
        let dir = std::env::temp_dir().join(format!("dm-perm-{}", uuid::Uuid::new_v4()));
        restrict_data_dir(&dir);
        let file = dir.join("download-manager.db");
        std::fs::write(&file, b"{}").unwrap();
        restrict_file(&file);
        #[cfg(unix)] {
            use std::os::unix::fs::PermissionsExt;
            let dir_mode = std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777;
            let file_mode = std::fs::metadata(&file).unwrap().permissions().mode() & 0o777;
            assert_eq!(dir_mode, 0o700, "data dir too open: {dir_mode:o}");
            assert_eq!(file_mode, 0o600, "database too open: {file_mode:o}");
        }
        std::fs::remove_dir_all(&dir).ok();
    }
