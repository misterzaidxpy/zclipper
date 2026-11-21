import pytest

from zc.moments import (
    Moment, StreamData, Stream, Word, find_moments, moments_from_heatmap, moments_from_transcript, parse_json3,
    snap_to_speech,
)
from zc.render import build_ass, caption_chunks, ffmpeg_command, valid_box


def talk(start, count, gap=0.4, text="word"):
    return [Word(start=start + i * gap, end=start + i * gap + 0.3, text=text) for i in range(count)]


def test_parse_json3_gives_word_timings_and_skips_blank_segments():
    data = {"events": [
        {"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "hello"}, {"utf8": " big world", "tOffsetMs": 500}]},
        {"tStartMs": 3000, "dDurationMs": 500, "segs": [{"utf8": "\n"}]},
        {"segs": [{"utf8": "no start time"}]},
    ]}
    words = parse_json3(data)
    assert [w.text for w in words] == ["hello", "big", "world"]
    assert (words[0].start, words[0].end) == (1.0, 1.5)
    assert words[2].end == 3.0


def test_parse_json3_empty():
    assert parse_json3({}) == []


def test_heatmap_skips_intro_and_spreads_picks():
    heat = [{"start_time": i * 10, "end_time": i * 10 + 10, "value": v} for i, v in enumerate([1.0, 0.2, 0.2, 0.2, 0.2, 0.2, 0.9, 0.85, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.6])]
    picks = moments_from_heatmap(heat, count=2, seconds=30, skip_intro=60)
    assert [round(m.start) for m in picks] == [50, 140]
    assert all(m.source == "most_replayed" for m in picks)


def test_transcript_moments_find_the_busy_reaction_window():
    words = talk(0, 100, gap=3.0)  # slow background speech for 300 seconds
    words += [Word(start=150 + i * 0.2, end=150 + i * 0.2 + 0.15, text=t) for i, t in enumerate(["no", "way", "oh", "my", "god"] * 12)]
    words.sort(key=lambda w: w.start)
    picks = moments_from_transcript(words, count=1, seconds=30, step=10, skip_intro=0)
    assert len(picks) == 1
    assert picks[0].start <= 150 < picks[0].end


def test_transcript_moments_ignore_music_markers_and_empty_input():
    assert moments_from_transcript([]) == []
    assert moments_from_transcript([Word(start=i, end=i + 0.5, text="[Music]") for i in range(200)]) == []


def test_snap_moves_cuts_to_pauses_and_refuses_tiny_results():
    words = talk(0, 20, gap=0.4) + talk(12, 40, gap=0.4) + talk(32, 20, gap=0.4)
    snapped = snap_to_speech(Moment(start=11, end=30), words)
    assert 7.9 < snapped.start < 12 and 27.9 < snapped.end < 32
    untouched = Moment(start=11, end=13)
    assert snap_to_speech(untouched, words) == untouched


def test_find_moments_falls_back_to_transcript_and_fills_text():
    words = talk(0, 400, gap=0.5, text="yes")
    data = StreamData(stream=Stream(id="x", url="u", duration=200), words=words)
    moments = find_moments(data, count=2, seconds=30)
    assert moments and all(m.source == "transcript" and m.text for m in moments)


def test_caption_chunks_are_relative_short_and_never_overlap():
    words = talk(100, 7, gap=0.4, text="hey") + [Word(start=110, end=110.3, text="[Music]"), Word(start=130, end=131, text="late")]
    chunks = caption_chunks(words, start=100, end=120, per_line=3)
    assert [c.text for c in chunks] == ["hey hey hey", "hey hey hey", "hey"]
    assert chunks[0].start == 0
    assert all(a.end <= b.start for a, b in zip(chunks, chunks[1:]))


def test_build_ass_title_is_optional_and_there_is_no_credit_line():
    words = [Word(start=5, end=5.5, text="hi{there}")]
    with_title = build_ass(words, 5, 15, title="A\nTitle", highlight=False)
    assert "Title,,0,0,0,,A Title" in with_title and "0:00:10.00" in with_title
    assert "Words,,0,0,0,,{\\pos(540,1490)}HI(THERE)" in with_title
    bare = build_ass(words, 5, 15)
    assert "Dialogue: 1" not in bare and "Credit" not in bare


def test_build_ass_highlights_one_word_at_a_time():
    words = [Word(start=10, end=10.3, text="i"), Word(start=10.4, end=10.7, text="break"), Word(start=10.8, end=11.1, text="this")]
    events = [line for line in build_ass(words, 10, 20, words_y=1030).splitlines() if line.startswith("Dialogue: 0")]
    assert len(events) == 3
    assert events[1].endswith("{\\pos(540,1030)}I {\\c&H0000E6FF&}BREAK{\\c&H00FFFFFF&} THIS")
    starts_ends = [line.split(",")[1:3] for line in events]
    assert starts_ends[0][1] == starts_ends[1][0] and starts_ends[1][1] == starts_ends[2][0]  # no flicker between words


@pytest.mark.parametrize("layout", ["crop", "blur", "split"])
def test_without_a_facecam_every_layout_is_a_full_screen_crop(layout):
    command = ffmpeg_command("in.mp4", "subs.ass", "out.mp4", layout)
    graph = command[command.index("-filter_complex") + 1]
    assert "crop=1080:1920,ass=" in graph and "boxblur" not in graph
    assert command[-1] == "out.mp4"


def test_split_layout_stacks_facecam_over_gameplay_and_needs_a_valid_box():
    graph = ffmpeg_command("in.mp4", "s.ass", "o.mp4", "split", [0.0, 0.4, 0.25, 0.25])[7]
    assert "crop=iw*0.2500:ih*0.2500:iw*0.0000:ih*0.4000" in graph
    assert "scale=1080:700" in graph and "crop=1080:1220:" in graph and "vstack=inputs=2" in graph
    for bad in (None, [0.9, 0, 0.5, 0.5], [0, 0, 0.01, 0.5], [0, 0, 1]):
        assert not valid_box(bad)
        assert "crop=1080:1920,ass=" in ffmpeg_command("in.mp4", "s.ass", "o.mp4", "split", bad)[7]
    assert valid_box([0, 0, 1, 1])


def test_caption_track_prefers_the_videos_own_language_over_translations():
    from zc.moments import _caption_track

    track = lambda code: [{"ext": "vtt", "url": f"{code}.vtt"}, {"ext": "json3", "url": f"{code}.json3"}]
    info = {"language": "en", "subtitles": {"ar": track("ar"), "de": track("de"), "en": track("en")},
            "automatic_captions": {"en-orig": track("en-orig")}}
    assert _caption_track(info, None) == ("en", "en.json3")
    assert _caption_track(info, "de") == ("de", "de.json3")
    info["subtitles"] = {"ar": track("ar"), "en-US": track("en-US")}
    assert _caption_track(info, None) == ("en-US", "en-US.json3")
    info["subtitles"] = {"ar": track("ar")}
    assert _caption_track(info, None) == ("en-orig", "en-orig.json3")  # the spoken language beats a translation
    assert _caption_track({"subtitles": {"ar": track("ar")}}, None) == ("ar", "ar.json3")  # nothing else: any track


def test_parse_json3_drops_zero_width_padding_and_styled_duplicate_layers():
    def styled(start, text, pen):
        return {"tStartMs": start, "dDurationMs": 800, "segs": [
            {"utf8": "\u200b", "pPenId": 1}, {"utf8": "\u200b"}, {"utf8": f"\u200b \u200b{text}\u200b \u200b", "pPenId": pen}, {"utf8": "\u200b"}]}

    data = {"events": [styled(1000, "Red light.", 25), styled(1000, "Red light.", 26), styled(3000, "Red light.", 5)]}
    words = parse_json3(data)
    assert [w.text for w in words] == ["Red", "light.", "Red", "light."]  # the same line later is real speech, kept
    assert words[0].start == 1.0 and words[2].start == 3.0


def test_parse_json3_spreads_a_line_timed_segment_across_its_words():
    data = {"events": [{"tStartMs": 10000, "dDurationMs": 2000, "segs": [{"utf8": "Only two minutes left"}]}]}
    words = parse_json3(data)
    assert [w.text for w in words] == ["Only", "two", "minutes", "left"]
    assert words[0].start == 10.0 and words[-1].end == 12.0
    assert all(a.start < b.start and a.end <= b.start + 1e-9 for a, b in zip(words, words[1:]))
    longer = words[2].end - words[2].start  # "minutes" gets more time than "two"
    assert longer > words[1].end - words[1].start
    chunks = caption_chunks(words, 10, 12, per_line=3)
    assert [c.text for c in chunks] == ["Only two minutes", "left"]


def test_speaker_dashes_and_music_notes_are_not_caption_words():
    words = [Word(start=0, end=0.3, text="-"), Word(start=0.3, end=0.6, text="Lets"), Word(start=0.6, end=0.9, text="go!"), Word(start=0.9, end=1.2, text="♪")]
    assert [c.text for c in caption_chunks(words, 0, 2)] == ["Lets go!"]
