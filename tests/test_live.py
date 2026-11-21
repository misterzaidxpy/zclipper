import json
from datetime import datetime, timedelta, timezone

import pytest

from zc.live import (
    SpikeDetector, parse_irc_line, parse_kick_live, parse_pusher_message, parse_twitch_live, parse_youtube_chat,
    parse_youtube_chat_page, parse_youtube_live, pick_variant, pick_youtube_live, utc,
)
from zc.errors import ZClipperError

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def test_utc_handles_the_three_platform_formats():
    assert utc("2026-11-19T19:13:14Z") == datetime(2026, 11, 19, 19, 13, 14, tzinfo=timezone.utc)
    assert utc("2026-11-20 09:52:08") == datetime(2026, 11, 20, 9, 52, 8, tzinfo=timezone.utc)
    assert utc("2026-11-20T10:00:00+05:00").utcoffset() == timedelta(hours=5)
    assert utc(None) is None and utc("") is None


def test_youtube_live_page():
    player = {
        "videoDetails": {"videoId": "abc", "title": "GTA 6", "isLive": True},
        "microformat": {"playerMicroformatRenderer": {"liveBroadcastDetails": {"isLiveNow": True, "startTimestamp": "2026-11-20T10:00:00+00:00"}}},
    }
    page = f"<script>var ytInitialPlayerResponse = {json.dumps(player)};var x = 1;</script>"
    stream = parse_youtube_live(page, "@speed")
    assert (stream.stream_id, stream.chat_id, stream.title) == ("abc", "abc", "GTA 6")
    assert stream.started_at == datetime(2026, 11, 20, 10, tzinfo=timezone.utc) and stream.key == "youtube:abc"
    player["videoDetails"]["isLive"] = False
    assert parse_youtube_live(f"var ytInitialPlayerResponse = {json.dumps(player)};", "@speed") is None
    assert parse_youtube_live("<html>channel page</html>", "@speed") is None


def test_twitch_live_answer():
    data = {"data": {"user": {"broadcastSettings": {"title": "GTA"}, "stream": {
        "id": "321", "type": "live", "createdAt": "2026-11-20T10:00:00Z",
        "archiveVideo": {"id": "999", "status": "RECORDING", "createdAt": "2026-11-20T10:00:05Z"}}}}}
    stream = parse_twitch_live(data, "kaicenat")
    assert (stream.stream_id, stream.vod_id, stream.chat_id, stream.title) == ("321", "999", "kaicenat", "GTA")
    data["data"]["user"]["stream"]["archiveVideo"] = None  # replays turned off
    assert parse_twitch_live(data, "kaicenat").vod_id is None
    assert parse_twitch_live({"data": {"user": {"stream": None}}}, "x") is None
    assert parse_twitch_live({"data": {"user": None}}, "x") is None


def test_kick_live_answer():
    channel = {"chatroom": {"id": 875062}, "livestream": {"id": 139081066, "session_title": "Live", "created_at": "2026-11-20 09:52:08", "is_live": True}}
    videos = [{"is_live": False, "source": "old"}, {"is_live": True, "source": "https://stream.kick.com/x/master.m3u8"}]
    stream = parse_kick_live(channel, videos, "adinross")
    assert (stream.stream_id, stream.chat_id, stream.dvr_url) == ("139081066", "875062", "https://stream.kick.com/x/master.m3u8")
    assert stream.started_at == datetime(2026, 11, 20, 9, 52, 8, tzinfo=timezone.utc)
    assert parse_kick_live(channel, [], "adinross").dvr_url is None
    assert parse_kick_live({"livestream": None}, [], "adinross") is None


def test_game_is_read_and_topics_filter_streams():
    twitch = {"data": {"user": {"broadcastSettings": {"title": "day 3"}, "stream": {
        "id": "1", "type": "live", "game": {"name": "Grand Theft Auto VI"}}}}}
    assert parse_twitch_live(twitch, "x").game == "Grand Theft Auto VI"
    kick = {"livestream": {"id": 2, "session_title": "spins", "categories": [{"name": "Slots & Casino"}]}}
    casino = parse_kick_live(kick, [], "n3on")
    assert casino.game == "Slots & Casino"
    topics = ["gta", "grand theft auto"]
    assert parse_twitch_live(twitch, "x").about(topics)
    assert not casino.about(topics) and casino.about([])
    kick["livestream"]["session_title"] = "GTA 6 after slots"
    assert not parse_kick_live(kick, [], "n3on").about(topics)  # the real category wins over the title
    kick["livestream"]["categories"] = []
    assert parse_kick_live(kick, [], "n3on").about(topics)  # no category: the title decides


def test_pick_variant_takes_best_rendition_within_height():
    master = "\n".join([
        "#EXTM3U",
        '#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080', "1080p60/playlist.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720', "720p60/playlist.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720', "720p30/playlist.m3u8",
    ])
    base = "https://stream.kick.com/a/b/master.m3u8"
    assert pick_variant(master, base, 1080) == "https://stream.kick.com/a/b/1080p60/playlist.m3u8"
    assert pick_variant(master, base, 720) == "https://stream.kick.com/a/b/720p60/playlist.m3u8"
    with pytest.raises(ZClipperError):
        pick_variant(master, base, 480)


def test_pick_youtube_live_pairs_video_and_audio():
    formats = [
        {"format_id": "233", "protocol": "m3u8_native", "vcodec": "none", "url": "a233"},
        {"format_id": "234", "protocol": "m3u8_native", "vcodec": "none", "url": "a234"},
        {"format_id": "232", "protocol": "m3u8_native", "vcodec": "avc1", "acodec": "none", "height": 720, "tbr": 2400, "url": "v720"},
        {"format_id": "270", "protocol": "m3u8_native", "vcodec": "avc1", "acodec": "none", "height": 1080, "tbr": 4500, "url": "v1080"},
        {"format_id": "137", "protocol": "https", "vcodec": "avc1", "height": 1080, "url": "dash"},
    ]
    assert pick_youtube_live(formats, 1080) == ("v1080", "a234")
    assert pick_youtube_live(formats, 720) == ("v720", "a234")
    assert pick_youtube_live([{"protocol": "m3u8", "vcodec": "avc1", "acodec": "mp4a", "height": 720, "url": "both"}]) == ("both", None)
    with pytest.raises(ZClipperError):
        pick_youtube_live(formats[2:4])


def test_youtube_chat_page_prefers_the_unfiltered_live_chat_view():
    data = {"contents": {"liveChatRenderer": {
        "continuations": [{"invalidationContinuationData": {"continuation": "TOP"}}],
        "header": {"viewSelector": {"subMenuItems": [
            {"title": "Top chat", "continuation": {"reloadContinuationData": {"continuation": "TOP"}}},
            {"title": "Live chat", "continuation": {"reloadContinuationData": {"continuation": "ALL"}}},
        ]}}}}}
    page = f'window["ytInitialData"] = {json.dumps(data)};"INNERTUBE_CLIENT_VERSION":"2.2026","VISITOR_DATA":"VIS"'
    assert parse_youtube_chat_page(page) == ("ALL", "2.2026", "VIS")
    del data["contents"]["liveChatRenderer"]["header"]
    assert parse_youtube_chat_page(f"ytInitialData = {json.dumps(data)};")[0] == "TOP"
    assert parse_youtube_chat_page("<html></html>") == (None, None, None)


def test_youtube_chat_answer():
    answer = {"continuationContents": {"liveChatContinuation": {
        "continuations": [{"timedContinuationData": {"continuation": "NEXT", "timeoutMs": 30000}}],
        "actions": [
            {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {"timestampUsec": "1795165200500000", "message": {"runs": [{"text": "no "}, {"text": "way"}]}}}}},
            {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {"timestampUsec": "1795165199000000", "message": {"runs": []}}}}},
            {"addChatItemAction": {"item": {"liveChatPaidMessageRenderer": {"timestampUsec": "1"}}}},
        ]}}}
    messages, token, wait = parse_youtube_chat(answer)
    assert [text for _, text in messages] == ["", "no way"] and messages[0][0] < messages[1][0]
    assert token == "NEXT" and wait == 10.0
    assert parse_youtube_chat({}) == ([], None, 5.0)


def test_twitch_irc_lines():
    line = "@badge-info=;color=#FF0000;tmi-sent-ts=1795165200000;user-id=1 :someone!someone@someone.tmi.twitch.tv PRIVMSG #kaicenat :LUL no way"
    when, text = parse_irc_line(line)
    assert text == "LUL no way" and when == datetime.fromtimestamp(1795165200, timezone.utc)
    assert parse_irc_line(":tmi.twitch.tv 001 justinfan1 :Welcome") is None
    assert parse_irc_line("PING :tmi.twitch.tv") is None


def test_kick_pusher_messages():
    raw = json.dumps({"event": "App\\Events\\ChatMessageEvent", "channel": "chatrooms.1.v2",
                      "data": json.dumps({"content": "W", "created_at": "2026-11-20T11:06:09+00:00"})})
    assert parse_pusher_message(raw) == (datetime(2026, 11, 20, 11, 6, 9, tzinfo=timezone.utc), "W")
    assert parse_pusher_message(json.dumps({"event": "App\\Events\\MessageDeletedEvent", "data": "{}"})) is None
    assert parse_pusher_message(json.dumps({"event": "pusher_internal:subscription_succeeded", "data": "{}"})) is None


def feed(detector, per_bucket):
    """per_bucket[i] messages spread inside bucket i (10 s each) from T0."""
    for index, count in enumerate(per_bucket):
        for n in range(count):
            detector.add(T0 + timedelta(seconds=index * 10 + n * 10 / max(count, 1)))


def test_spike_detector_finds_a_burst_after_warmup():
    detector = SpikeDetector(warmup=12, settle=15, min_messages=15, factor=2.5)
    feed(detector, [8] * 20 + [40] + [8] * 5)
    spikes = detector.check(T0 + timedelta(seconds=26 * 10 + 15))
    assert len(spikes) == 1
    spike = spikes[0]
    assert spike.at == T0 + timedelta(seconds=200) and spike.count == 40 and spike.baseline == 8 and spike.score == 5.0


def test_spike_detector_ignores_quiet_chat_warmup_and_cooldown():
    quiet = SpikeDetector(min_messages=15)
    feed(quiet, [2] * 20 + [9] + [2] * 5)  # 4.5x the baseline, but only 9 messages
    assert quiet.check(T0 + timedelta(seconds=400)) == []

    early = SpikeDetector(warmup=12)
    feed(early, [8] * 5 + [60] + [8] * 10)
    assert early.check(T0 + timedelta(seconds=300)) == []  # the burst came before there was a baseline

    close = SpikeDetector(cooldown=180)
    feed(close, [8] * 20 + [40] + [8] * 5 + [40] + [8] * 3)
    assert len(close.check(T0 + timedelta(seconds=400))) == 1  # the second burst was 60 s later


def test_spike_detector_judges_each_window_once_and_waits_for_late_chat():
    detector = SpikeDetector()
    feed(detector, [8] * 20 + [40])
    assert detector.check(T0 + timedelta(seconds=210)) == []  # the busy window is still settling
    assert len(detector.check(T0 + timedelta(seconds=230))) == 1
    assert detector.check(T0 + timedelta(seconds=240)) == []
    detector.add(T0 + timedelta(seconds=205))  # a late message for a judged window is dropped
    assert detector.counts.get(int((T0 + timedelta(seconds=205)).timestamp() // 10)) == 40
    assert SpikeDetector().check(T0) == []


def test_whisper_words_from_any_model_with_word_timestamps():
    from types import SimpleNamespace

    from zc.whisper import whisper_words

    class FakeModel:
        def transcribe(self, path, **options):
            assert options["word_timestamps"] is True
            words = [SimpleNamespace(word=" No", start=1.0, end=1.2), SimpleNamespace(word=" way!", start=1.3, end=1.3), SimpleNamespace(word=" ", start=2, end=2)]
            return [SimpleNamespace(words=words), SimpleNamespace(words=None)], None

    words = whisper_words("clip.mp4", model=FakeModel())
    assert [(w.text, w.start, w.end) for w in words] == [("No", 1.0, 1.2), ("way!", 1.3, 1.35)]
