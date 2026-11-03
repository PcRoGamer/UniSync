"""Canonical Echo360 identity, transcript validation, syllabus and queue behaviour.

These tests are deliberately browser-free: they pin the rules that decide what
counts as a discovered link, a real transcript, and honest coverage, because the
legacy bridge produced false positives from the shared Canvas LTI tool UUID.
"""

from __future__ import annotations

import json

from unisync.services.sync.echo360_ingest import (
    build_coverage,
    canonical_embed_key,
    classify_embed,
    cues_to_vtt,
    embed_lesson_id,
    embed_transcript_lesson_ids,
    embed_transcript_present,
    is_cue_valid_vtt,
    link_id_from_url,
    merge_canvas_embeds,
    parse_hls_subtitle_uris,
    persist_syllabus,
    read_transcript,
    transcript_from_json,
    write_transcript,
)
from unisync.services.sync.native_sync import NativeSyncState

TOOL_UUID = "9381284d-5253-4e5b-b173-fb47a63576cb"
LINK_A = "de5554c9-cdf5-4ff4-98b2-f9b156ebe79f"
LINK_B = "85d37d20-63b1-4879-ac96-8a0ff278a792"
LINK_C = "11111111-2222-3333-4444-555555555555"

VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello world\n"
HEADER_ONLY = "WEBVTT\n\nNOTE no cues here\n"


def _record(**overrides):
    record = {
        "canvas_course_id": "240718",
        "uuid": TOOL_UUID,
        "media_id": TOOL_UUID,
        "link_id": LINK_A,
        "title": "Seminar 1",
        "player_url": f"https://echo360.net.au/lti/{TOOL_UUID}?linkId={LINK_A}",
    }
    record.update(overrides)
    return record


def test_canonical_key_keeps_course_uuid_and_link() -> None:
    assert canonical_embed_key("240718", TOOL_UUID.upper(), LINK_A) == (
        f"240718::{TOOL_UUID}::{LINK_A}"
    )
    assert canonical_embed_key("240718", TOOL_UUID, None) == f"240718::{TOOL_UUID}::"


def test_link_specific_embed_never_uses_the_shared_uuid_file(tmp_path) -> None:
    generic = embed_lesson_id(TOOL_UUID, None)
    specific = embed_lesson_id(TOOL_UUID, LINK_A)
    assert specific != generic
    (tmp_path / f"{generic}.vtt").write_text(VTT, encoding="utf-8")

    record = _record()
    assert embed_transcript_lesson_ids(record) == [specific]
    # A file for the shared tool UUID must not satisfy a link-specific embed.
    assert embed_transcript_present(tmp_path, record) is False
    assert read_transcript(tmp_path, generic) is not None

    assert write_transcript(tmp_path, specific, VTT) is True
    assert embed_transcript_present(tmp_path, record) is True


def test_embed_without_link_may_use_uuid_and_media_id(tmp_path) -> None:
    media_id = "138231fa-fe0b-411d-a87a-f09be345870e"
    record = _record(link_id=None, media_id=media_id)
    ids = embed_transcript_lesson_ids(record)
    assert embed_lesson_id(TOOL_UUID, None) in ids
    assert embed_lesson_id(media_id, None) in ids
    write_transcript(tmp_path, embed_lesson_id(media_id, None), VTT)
    assert embed_transcript_present(tmp_path, record) is True


def test_transcript_requires_webvtt_and_a_timestamped_cue(tmp_path) -> None:
    assert is_cue_valid_vtt(VTT) is True
    assert is_cue_valid_vtt(HEADER_ONLY) is False
    assert is_cue_valid_vtt("{}") is False
    assert is_cue_valid_vtt("") is False

    assert write_transcript(tmp_path, "BLAW_EMBED_empty", HEADER_ONLY) is False
    assert not (tmp_path / "BLAW_EMBED_empty.vtt").exists()
    assert write_transcript(tmp_path, "BLAW_EMBED_ok", "\ufeff" + VTT) is True
    assert read_transcript(tmp_path, "BLAW_EMBED_ok").startswith("WEBVTT")


def test_json_cues_and_hls_renditions_convert_to_vtt() -> None:
    vtt = transcript_from_json(
        {
            "transcript": [
                {"startMs": 0, "endMs": 1200, "content": "First line"},
                {"start": 2.5, "end": 4.0, "text": "Second line", "speaker": "Lecturer"},
            ]
        }
    )
    assert vtt.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.200" in vtt
    assert "First line" in vtt
    assert "<v Lecturer>Second line" in vtt
    assert is_cue_valid_vtt(vtt)

    # Empty/foreign payloads must not fabricate a transcript.
    assert transcript_from_json({"transcript": []}) == ""
    assert transcript_from_json({"unrelated": True}) == ""
    assert cues_to_vtt([{"start": 0, "end": 1000, "content": ""}]) == ""

    manifest = (
        "#EXTM3U\n"
        '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",'
        'URI="captions/en.m3u8"\n'
        '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Français",'
        'URI="https://content.example/fr.m3u8"\n'
    )
    uris = parse_hls_subtitle_uris(
        manifest, "https://content.example/0000.m3u8"
    )
    assert "https://content.example/captions/en.m3u8" in uris
    assert "https://content.example/fr.m3u8" in uris


def test_link_id_is_extracted_from_encoded_lti_urls() -> None:
    raw = (
        "https://canvas.lms.unimelb.edu.au/courses/240718/external_tools/retrieve"
        "?url=https%3A%2F%2Fecho360.net.au%2Flti%2F"
        f"{TOOL_UUID}%3FlinkId%3D{LINK_A}%26autoplay%3Dfalse"
    )
    assert link_id_from_url(raw) == LINK_A
    assert link_id_from_url(f"https://echo360.net.au/lti/{TOOL_UUID}?linkId={LINK_B}") == LINK_B
    assert link_id_from_url(f"https://echo360.net.au/lti/{TOOL_UUID}") is None


def test_embed_merge_dedupes_by_course_uuid_and_link_without_losing_metadata() -> None:
    legacy = [
        _record(title="embedded content", media_url="https://cdn/a.m3u8", subtitle_path="/old.vtt"),
        _record(link_id=LINK_B, title="Career Confusion (03:09)", media_url="https://cdn/b.m3u8"),
    ]
    discovered = [
        _record(title="Seminar 1 Recording", source_title="Seminar 1 Recording"),
        _record(link_id=LINK_B, title="embedded content"),
        _record(link_id="aaaaaaaa-1111-2222-3333-444444444444", title="New link"),
    ]
    merged, added, updated = merge_canvas_embeds(legacy, discovered, course_id="240718")

    assert len(merged) == 3
    assert added == 1
    assert updated == 2
    keys = [canonical_embed_key(r["canvas_course_id"], r["uuid"], r["link_id"]) for r in merged]
    assert len(keys) == len(set(keys))
    # Richer legacy metadata survives, while a better title replaces a placeholder.
    first = merged[0]
    assert first["media_url"] == "https://cdn/a.m3u8"
    assert first["subtitle_path"] == "/old.vtt"
    assert first["title"] == "Seminar 1 Recording"
    # A real title is never replaced by a discovered placeholder.
    assert merged[1]["title"] == "Career Confusion (03:09)"


def test_syllabus_persistence_merges_and_keeps_previous_sections(tmp_path) -> None:
    path = tmp_path / "echo360_syllabus.json"
    path.write_text(
        json.dumps(
            [
                {
                    "sectionId": "sec-1",
                    "name": "Business Law",
                    "syllabus": [{"lesson": {"lesson": {"id": "old-lesson"}}}],
                }
            ]
        ),
        encoding="utf-8",
    )

    fetched = {
        "sec-1": [{"lesson": {"lesson": {"id": "new-lesson"}}}],
        "sec-2": [{"lesson": {"lesson": {"id": "other-course"}}}],
    }
    merged = persist_syllabus(
        path, fetched, section_names={"sec-1": "Business Law", "sec-2": "Marketing"}, fetched_at="2026-11-01T00:00:00+00:00"
    )

    assert len(merged) == 2
    assert merged[0]["sectionId"] == "sec-1"
    assert merged[0]["name"] == "Business Law"
    assert merged[0]["syllabus"][0]["lesson"]["lesson"]["id"] == "new-lesson"
    assert merged[0]["last_syllabus_fetch"] == "2026-11-01T00:00:00+00:00"
    assert merged[1]["sectionId"] == "sec-2"
    assert merged[1]["name"] == "Marketing"
    # The file on disk is the merged snapshot, not just the fetched subset.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert {section["sectionId"] for section in on_disk} == {"sec-1", "sec-2"}


def test_syllabus_failed_fetch_does_not_erase_cached_lessons(tmp_path) -> None:
    path = tmp_path / "echo360_syllabus.json"
    path.write_text(
        json.dumps([{"sectionId": "sec-1", "syllabus": [{"lesson": {"lesson": {"id": "kept"}}}]}]),
        encoding="utf-8",
    )
    merged = persist_syllabus(path, {"sec-1": []}, fetched_at="2026-11-01T00:00:00+00:00")
    assert merged[0]["syllabus"][0]["lesson"]["lesson"]["id"] == "kept"
    assert merged[0]["last_syllabus_fetch_attempt"] == "2026-11-01T00:00:00+00:00"


def test_coverage_counts_only_cue_valid_link_specific_transcripts(tmp_path) -> None:
    cache = tmp_path / "subs"
    thumbs = tmp_path / "thumbs"
    cache.mkdir()
    thumbs.mkdir()

    covered = _record(link_id=LINK_A, media_url="https://cdn/a.m3u8")
    # The real BLAW gap: a discovered link with no resolved media URL at all.
    missing = _record(link_id=LINK_B, title="Seminar 4 Recording and Slides")
    # Media resolved but no caption source: transcript-missing, not unresolved.
    resolved_no_transcript = _record(
        link_id=LINK_C,
        title="Resolved media without captions",
        media_url="https://cdn/c.m3u8",
    )
    write_transcript(cache, embed_lesson_id(TOOL_UUID, LINK_A), VTT)
    # A generic file must not inflate coverage.
    write_transcript(cache, embed_lesson_id(TOOL_UUID, None), VTT)

    coverage = build_coverage(
        "240718",
        [covered, missing, resolved_no_transcript],
        cache_dir=cache,
        thumbnail_dir=thumbs,
        catalog_updated_at="2026-10-30T00:00:00+00:00",
        syllabus_fetched_at="2026-09-27T00:00:00+00:00",
    )

    assert coverage["discovered_links"] == 3
    assert coverage["cue_valid_transcripts"] == 1
    assert coverage["missing_transcripts"] == 2
    by_link = {row["link_id"]: row for row in coverage["missing"]}
    assert by_link[LINK_B]["status"] == "unresolved"
    assert by_link[LINK_B]["reason"] == "no_media_url"
    assert by_link[LINK_C]["status"] == "missing_transcript"
    assert by_link[LINK_C]["reason"] == "no_cue_valid_transcript"
    assert coverage["transcripts_current"] is False
    # Catalog and transcript freshness stay separate signals.
    assert coverage["last_catalog_refresh"] == "2026-10-30T00:00:00+00:00"
    assert coverage["last_syllabus_fetch"] == "2026-09-27T00:00:00+00:00"


def test_classify_embed_reports_media_and_identity_gaps(tmp_path) -> None:
    no_media = classify_embed(
        _record(media_url="", link_id=LINK_B), cache_dir=tmp_path
    )
    assert no_media["status"] == "unresolved"
    assert no_media["reason"] == "no_media_url"

    no_identity = classify_embed(
        {"canvas_course_id": "240718", "uuid": TOOL_UUID, "media_url": "https://cdn/x.m3u8"},
        cache_dir=tmp_path,
    )
    assert no_identity["status"] == "unresolved"
    assert no_identity["reason"] == "missing_link_identity"


def test_native_queue_persists_and_resumes_after_failure(tmp_path) -> None:
    cache = tmp_path / "subs"
    cache.mkdir()
    state_path = tmp_path / "native_sync.json"
    records = [_record(), _record(link_id=LINK_B, title="Second")]

    state = NativeSyncState(state_path)
    queued = state.enqueue(records, cache_dir=cache)
    assert len(queued) == 2

    key_a = NativeSyncState.key_for(records[0])
    key_b = NativeSyncState.key_for(records[1])
    # Disk is the source of truth for completion: a run writes the validated VTT
    # before recording the outcome, so the resume pass must not re-queue it.
    write_transcript(cache, embed_lesson_id(TOOL_UUID, LINK_A), VTT)
    state.note_outcome(key_a, {"status": "transcript", "reason": ""}, transcript_present=True)
    state.note_outcome(key_b, {"status": "error", "reason": "TimeoutError"}, transcript_present=False)
    assert state.pending_keys() == [key_b]

    # A restart must recover the same pending work, and honour the retry delay.
    revived = NativeSyncState(state_path)
    assert revived.pending_keys() == [key_b]
    revived.data["pending"][key_b]["retry_after"] = 0
    retry = revived.enqueue(records, cache_dir=cache)
    assert [r["link_id"] for r in retry] == [LINK_B]

    # A success clears the queue and writes coverage.
    write_transcript(cache, embed_lesson_id(TOOL_UUID, LINK_B), VTT)
    revived.note_outcome(key_b, {"status": "transcript", "reason": ""}, transcript_present=True)
    assert revived.pending_keys() == []
    revived.record_coverage(
        "240718",
        records,
        cache_dir=cache,
        catalog_updated_at="2026-11-01T00:00:00+00:00",
        syllabus_fetched_at="2026-11-01T00:00:00+00:00",
    )
    summary = revived.summary()
    assert summary["totals"]["discovered_links"] == 2
    assert summary["totals"]["cue_valid_transcripts"] == 2
    assert summary["totals"]["missing_transcripts"] == 0
    assert summary["courses"]["240718"]["transcripts_current"] is True


def test_failed_run_records_status_and_login_requirement(tmp_path) -> None:
    state = NativeSyncState(tmp_path / "native.json")
    state.begin_run()
    state.finish_run("login_required", error="Canvas/Echo360 session not authenticated", login_required=True)
    summary = state.summary()
    assert summary["last_run_status"] == "login_required"
    assert summary["login_required"] is True
    assert summary["last_error"]
    assert summary["next_run_at"] > 0


DIRECT_LESSON_ID = (
    "G_b885d35a-cd4d-41ff-8de2-66d883037da6_5645c5ef-f457-430f-939a-6415f263ba6a"
    "_2026-07-28T17:05:00.000_2026-07-28T18:00:00.000"
)


def _lesson_item(**overrides):
    inner = {
        "id": DIRECT_LESSON_ID,
        "name": "Week 1 - Introduction",
        "hasMedia": True,
        "timing": {"start": "2026-07-28T17:05:00.000", "end": "2026-07-28T18:00:00.000"},
    }
    inner.update(overrides.pop("lesson", {}))
    item = {"lesson": {"lesson": inner, "medias": [{"id": "f943cb65-b75e-469c-ae4d-cf4326069801"}]}}
    item.update(overrides)
    return item


def test_direct_lesson_stem_keeps_millisecond_dots(tmp_path) -> None:
    from unisync.services.sync.echo360_ingest import lesson_stem

    stem = lesson_stem(DIRECT_LESSON_ID)
    assert stem == (
        "G_b885d35a-cd4d-41ff-8de2-66d883037da6_5645c5ef-f457-430f-939a-6415f263ba6a"
        "_2026-07-28T17_05_00.000_2026-07-28T18_00_00.000"
    )
    # The embed-key sanitiser would lose the dots; the lesson path must not.
    assert "T17_05_00_000" not in stem


def test_direct_lesson_coverage_uses_the_cached_stem(tmp_path) -> None:
    from unisync.services.sync.echo360_ingest import (
        build_echo360_coverage,
        direct_lesson_present,
        lesson_stem,
    )

    cache = tmp_path / "subs"
    cache.mkdir()
    item = _lesson_item()
    assert direct_lesson_present(cache, item) is False

    (cache / f"{lesson_stem(DIRECT_LESSON_ID)}.vtt").write_text(VTT, encoding="utf-8")
    assert direct_lesson_present(cache, item) is True

    sections = [{"sectionId": "sec-1", "name": "COMP10001", "syllabus": [item]}]
    coverage = build_echo360_coverage(
        sections,
        course_id="241246",
        course_name="Foundations of Computing (COMP10001_2026_SM2)",
        cache_dir=cache,
        syllabus_fetched_at="2026-11-01T00:00:00+00:00",
    )
    assert coverage["source"] == "echo360_direct"
    assert coverage["discovered_lessons"] == 1
    assert coverage["cue_valid_transcripts"] == 1
    assert coverage["missing_transcripts"] == 0
    assert coverage["transcripts_current"] is True
    assert coverage["course_name"].startswith("Foundations of Computing")


def test_direct_lesson_coverage_ignores_header_only_and_embed_rows(tmp_path) -> None:
    from unisync.services.sync.echo360_ingest import build_echo360_coverage, lesson_stem

    cache = tmp_path / "subs"
    cache.mkdir()
    (cache / f"{lesson_stem(DIRECT_LESSON_ID)}.vtt").write_text(HEADER_ONLY, encoding="utf-8")
    embed_item = _lesson_item(lesson={"id": "BLAW_EMBED_9381284d-5253-4e5b-b173-fb47a63576cb_de5554c9"})

    coverage = build_echo360_coverage(
        [{"sectionId": "sec-1", "syllabus": [_lesson_item(), embed_item]}],
        course_id="241246",
        cache_dir=cache,
    )

    # A header-only file is not a transcript, and embed rows belong to the
    # canonical embed path, not the direct lesson path.
    assert coverage["discovered_lessons"] == 1
    assert coverage["cue_valid_transcripts"] == 0
    assert coverage["missing"][0]["reason"] == "no_cached_vtt"
    assert coverage["transcripts_current"] is False
