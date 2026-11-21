"""Find what to clip: popular recent videos about a topic, or where a streamer is live right now."""

import asyncio

from .live import PLATFORMS, check_live
from .moments import BASE_URL, _extract, list_streams

# YouTube search filters, sorted by view count, videos only, uploaded within the period.
PERIODS = {"today": "CAMSBAgCEAE%3D", "week": "CAMSBAgDEAE%3D", "month": "CAMSBAgEEAE%3D", "year": "CAMSBAgFEAE%3D"}


def pick_videos(entries: list[dict], count: int, min_minutes: float) -> list[dict]:
    """Finished videos at least min_minutes long, most viewed first; live and upcoming ones are skipped."""
    videos = []
    for entry in entries:
        if not entry or not entry.get("id") or entry.get("live_status") in ("is_live", "is_upcoming"):
            continue
        duration = entry.get("duration") or 0
        if duration < min_minutes * 60:
            continue
        videos.append({
            "id": entry["id"], "url": f"{BASE_URL}/watch?v={entry['id']}", "title": entry.get("title") or "",
            "channel": entry.get("channel") or entry.get("uploader"), "duration": duration,
            "views": entry.get("view_count") or 0, "was_live": entry.get("live_status") == "was_live",
        })
    videos.sort(key=lambda video: video["views"], reverse=True)
    return videos[:count]


def search(topic: str, count: int = 5, period: str = "week", min_minutes: float = 10) -> list[dict]:
    """The most viewed YouTube videos about `topic` from the period, long enough to hold several moments."""
    if period not in PERIODS:
        raise ValueError(f"Period {period} is not supported; use one of {', '.join(PERIODS)}")
    query = "+".join(topic.split())
    info = _extract(f"{BASE_URL}/results?search_query={query}&sp={PERIODS[period]}", extract_flat="in_playlist",
                    playlistend=max(count * 4, 20))
    return pick_videos(info.get("entries") or [], count, min_minutes)


async def streamer(name: str, recent: int = 3) -> dict:
    """Where `name` is live now (YouTube, Twitch, Kick) and their latest finished YouTube streams.

    The same name is tried on every platform; a platform that fails is
    reported under "errors" and does not stop the others.
    """
    name = name.strip().lstrip("@")
    checks = await asyncio.gather(*(check_live(platform, name) for platform in PLATFORMS), return_exceptions=True)
    live, errors = [], {}
    for platform, result in zip(PLATFORMS, checks):
        if isinstance(result, Exception):
            errors[platform] = str(result)[:200]
        elif result is not None:
            live.append(result.model_dump(mode="json"))
    finished = []
    try:
        streams = await asyncio.to_thread(list_streams, f"@{name}", recent * 2)
        finished = [stream.model_dump() for stream in streams if stream.finished][:recent]
    except Exception as exc:  # no YouTube channel by that handle, or no streams tab
        errors["youtube_streams"] = str(exc)[:200]
    return {"name": name, "live": live, "recent_streams": finished, "errors": errors}
