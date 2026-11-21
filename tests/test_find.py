import asyncio

import pytest

from zc import find
from zc.errors import ZClipperError
from zc.live import LiveStream
from zc.moments import Stream


def test_pick_videos_keeps_long_finished_videos_most_viewed_first():
    entries = [
        {"id": "a", "title": "short", "duration": 120, "view_count": 9_000_000},
        {"id": "b", "title": "stream", "duration": 30_000, "view_count": 2_000_000, "live_status": "was_live", "channel": "S"},
        {"id": "c", "title": "live now", "duration": None, "view_count": None, "live_status": "is_live"},
        {"id": "d", "title": "video", "duration": 900, "view_count": 5_000_000},
        None,
        {"title": "no id", "duration": 900},
    ]
    picked = find.pick_videos(entries, count=5, min_minutes=10)
    assert [video["id"] for video in picked] == ["d", "b"]
    assert picked[1]["was_live"] and picked[1]["url"] == "https://www.youtube.com/watch?v=b"
    assert len(find.pick_videos(entries, count=1, min_minutes=10)) == 1


def test_search_rejects_unknown_periods():
    with pytest.raises(ValueError):
        find.search("gta 6", period="decade")


def test_streamer_reports_live_platforms_errors_and_recent_streams(monkeypatch):
    async def check(platform, name):
        if platform == "twitch":
            raise ZClipperError("Twitch answered 500")
        if platform == "youtube":
            return LiveStream(platform="youtube", channel=f"@{name}", stream_id="x", url="https://www.youtube.com/watch?v=x")
        return None

    def recent(channel, limit):
        assert channel == "@ishowspeed"
        return [Stream(id="old", url="u", duration=9000, live_status="was_live"), Stream(id="now", url="u2", live_status="is_live")]

    monkeypatch.setattr(find, "check_live", check)
    monkeypatch.setattr(find, "list_streams", recent)
    result = asyncio.run(find.streamer("@ishowspeed"))
    assert [stream["platform"] for stream in result["live"]] == ["youtube"]
    assert "twitch" in result["errors"] and [stream["id"] for stream in result["recent_streams"]] == ["old"]
