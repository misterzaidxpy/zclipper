"""The clip pipelines: picks from a finished video, chat spikes from a live stream, viewer clips.

Each finished clip is described by a plain dict (see clip_record), which the
CLI prints as JSON and writes to clips.json.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import ZClipperError
from .live import LiveStream, SpikeDetector, check_live, cut_live, read_chat, viewer_clips
from .moments import Moment, StreamData, Word, download_range, find_moments, load_stream, words_between
from .render import frame_size, grab_frame, refine_facecam, render_thumbnail, render_vertical, valid_box

MIN_SECONDS, MAX_SECONDS = 8, 180
REPLAY_LAG = {"youtube": 20, "twitch": 75, "kick": 45}  # seconds before a live moment can be cut from the replay
DUPLICATE_SECONDS = 60  # a chat spike and a viewer clip this close are the same moment
FRAME_WINDOW = 10  # seconds downloaded to grab one frame from a URL


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_box(text: str | None) -> list[float] | None:
    """"x,y,w,h" as fractions of the frame; None for empty text. Raises ValueError for a malformed box."""
    if not text:
        return None
    box = [float(part) for part in text.replace(" ", "").split(",")]
    if not valid_box(box):
        raise ValueError(f"Facecam box {text} must be x,y,width,height as fractions between 0 and 1")
    return box


def _transcribe(path: Path, model: str | None) -> list[Word]:
    """Whisper words for a file, or [] when Whisper is off or not installed."""
    if not model:
        return []
    from .whisper import whisper_words

    try:
        return whisper_words(path, model)
    except ImportError:
        return []


def candidates(url: str, count: int = 10, seconds: float = 40, language: str | None = None, whisper: str | None = None) -> dict:
    """The moments worth a closer look in a finished video, as a JSON-ready dict.

    With whisper (a model size such as "base"), candidates that have no
    caption text get their audio transcribed on this computer.
    """
    data = load_stream(url, language)
    stream = data.stream
    if not stream.finished:
        raise ZClipperError(f"{stream.id} is still live or has no duration yet. Use `live` for streams in progress.")
    moments = find_moments(data, count, seconds)
    if not moments:
        raise ZClipperError(
            "No replay graph and no captions for this video, so there is nothing to rank. "
            "Wait for YouTube to add captions, or clip it while live with `live`."
        )
    if whisper and not data.words:
        filled = []
        for moment in moments:
            words = _segment_words(stream.url, moment.start, moment.end, whisper)
            filled.append(moment.model_copy(update={"text": " ".join(word.text for word in words)}))
        moments = filled
    return {
        "id": stream.id, "url": stream.url, "title": stream.title, "channel": stream.channel,
        "duration": stream.duration, "views": stream.views, "language": data.language,
        "has_captions": bool(data.words), "has_replay_graph": bool(data.heatmap),
        "candidates": [
            {"start": m.start, "end": m.end, "score": m.score, "source": m.source, "text": m.text} for m in moments
        ],
    }


def _segment_words(url: str, start: float, end: float, model: str) -> list[Word]:
    """Whisper words for [start, end] of a video, timed from the video's start."""
    from .whisper import home

    scratch = home() / "tmp" / f"words_{int(start)}_{int(end)}.mp4"
    segment = download_range(url, start, end, scratch, height=360)
    try:
        return [Word(start=w.start + start, end=w.end + start, text=w.text) for w in _transcribe(segment, model)]
    finally:
        segment.unlink(missing_ok=True)


def check_pick(pick: dict, duration: float | None) -> Moment:
    """A pick from picks.json as a Moment; ValueError explains what is wrong with it."""
    try:
        moment = Moment(**{key: value for key, value in pick.items() if key in Moment.model_fields})
    except Exception as exc:
        raise ValueError(f"Pick {pick} is malformed: {exc}") from exc
    if moment.start < 0 or moment.end <= moment.start:
        raise ValueError(f"Pick {moment.start}-{moment.end}: end must come after start")
    if not MIN_SECONDS <= moment.seconds <= MAX_SECONDS:
        raise ValueError(f"Pick {moment.start}-{moment.end} is {moment.seconds:.1f} s; clips are {MIN_SECONDS} to {MAX_SECONDS} s")
    if duration and moment.end > duration + 1:
        raise ValueError(f"Pick {moment.start}-{moment.end} ends after the video ({duration:.0f} s)")
    return moment


def clip_record(moment: Moment, video: Path, thumbnail: Path, source_url: str, words: list[Word]) -> dict:
    return {
        "file": str(video), "thumbnail": str(thumbnail), "source": source_url,
        "start": moment.start, "end": moment.end, "seconds": round(moment.seconds, 2),
        "title": moment.title, "caption": moment.caption or moment.title,
        "hashtags": " ".join(f"#{tag.strip('# ').lower()}" for tag in moment.hashtags if tag.strip("# ")),
        "transcript": " ".join(word.text for word in words),
    }


def _render(segment: Path, out: Path, moment: Moment, words: list[Word], offset: float, layout: str, box,
            size: tuple[int, int]) -> tuple[Path, Path]:
    """Caption and frame one downloaded segment; words are timed from `offset` (the segment's start)."""
    seconds = moment.seconds
    if layout == "split":
        box = refine_facecam(segment, box, seconds / 2)
    video = render_vertical(segment, out, words, offset, offset + seconds, moment.title, layout, box, size)
    at = moment.thumbnail_at if moment.thumbnail_at is not None else seconds * 0.4
    thumbnail = render_thumbnail(segment, out.with_suffix(".jpg"), min(max(at, 0), seconds), moment.thumbnail_text, layout, box, size)
    return video, thumbnail


def make(picks_doc: dict, out_dir: str | Path, layout: str = "split", facecam: list[float] | None = None,
         height: int = 1080, whisper: str | None = "base", keep_segments: bool = False, log=print,
         aspect: str = "9:16") -> list[dict]:
    """Download, caption and render every pick in a picks.json document. Returns the clip records.

    Picks may come from several videos: a pick's own "url" (and "facecam")
    overrides the document's. Every pick is checked before anything is
    downloaded; a pick that then fails to download or render is reported
    as {"error": ...} and the others still finish.
    """
    size = frame_size(aspect)
    picks = picks_doc.get("picks") or []
    if not picks:
        raise ValueError('picks.json has no "picks"')
    default_box = facecam or _box(picks_doc.get("facecam"))
    sources: dict[str, StreamData] = {}
    planned = []
    for pick in picks:
        url = pick.get("url") or picks_doc.get("url")
        if not url:
            raise ValueError(f'Pick {pick} has no "url", and picks.json has no top-level "url"')
        if url not in sources:
            sources[url] = load_stream(url)
        planned.append((sources[url], check_pick(pick, sources[url].stream.duration), facecam or _box(pick.get("facecam")) or default_box))
    folder = Path(out_dir)
    records = []
    for data, moment, box in planned:
        stream = data.stream
        name = f"{stream.id}_{int(moment.start)}"
        log(f"cutting {stream.id} {moment.start:.1f}-{moment.end:.1f} s")
        try:
            segment = download_range(stream.url, moment.start, moment.end, folder / f"{name}_src.mp4", height)
            words = words_between(data.words, moment.start, moment.end)
            offset = moment.start
            if not words:
                words, offset = _transcribe(segment, whisper), 0.0
            try:
                video, thumbnail = _render(segment, folder / f"{name}.mp4", moment, words, offset, layout, box, size)
            finally:
                if not keep_segments:
                    segment.unlink(missing_ok=True)
        except ZClipperError as exc:
            log(f"{name} failed: {str(exc)[:200]}")
            records.append({"source": stream.url, "start": moment.start, "end": moment.end, "title": moment.title, "error": str(exc)[:300]})
            continue
        records.append(clip_record(moment, video, thumbnail, stream.url, words))
    (folder / "clips.json").write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    return records


def _box(value) -> list[float] | None:
    """A facecam box from picks.json: a list of four numbers or an "x,y,w,h" string."""
    if not value:
        return None
    return parse_box(value if isinstance(value, str) else ",".join(map(str, value)))


def frame(source: str, at: float, out: str | Path) -> Path:
    """A JPEG of one moment, from a local file or a video URL.

    For a URL only a short window is downloaded. It is 10 seconds, not 1 or 2:
    yt-dlp fails to convert very short ranges of Twitch replays.
    """
    out = Path(out)
    if not source.startswith("http"):
        return grab_frame(source, out, at)
    start = max(at - 1, 0)
    segment = download_range(source, start, start + FRAME_WINDOW, out.with_name(out.stem + "_src.mp4"), height=1080)
    try:
        return grab_frame(segment, out, at - start)
    finally:
        segment.unlink(missing_ok=True)


# ---------- live ----------


async def _live_clip(stream: LiveStream, start_at: datetime, seconds: float, out_dir: Path, name: str, layout: str,
                     box, height: int, whisper: str | None, size: tuple[int, int]) -> dict:
    """Cut, caption and render `seconds` of a live broadcast from wall-clock `start_at`."""
    segment = await asyncio.to_thread(cut_live, stream, start_at, seconds, out_dir / f"{name}_src.mp4", height)
    try:
        words = await asyncio.to_thread(_transcribe, segment, whisper)
        moment = Moment(start=0, end=seconds, title=stream.title, source="live")
        video, thumbnail = await asyncio.to_thread(_render, segment, out_dir / f"{name}.mp4", moment, words, 0.0, layout, box, size)
    finally:
        segment.unlink(missing_ok=True)
    record = clip_record(moment, video, thumbnail, stream.url, words)
    offset = (start_at - stream.started_at).total_seconds() if stream.started_at else None
    record.update({"start": offset, "end": offset + seconds if offset is not None else None, "at": start_at.isoformat(timespec="seconds")})
    return record


async def watch(platform: str, channel: str, minutes: float, out_dir: str | Path, seconds_before: float = 25,
                seconds_after: float = 10, layout: str = "split", facecam=None, height: int = 1080,
                whisper: str | None = "base", factor: float = 2.5, min_messages: int = 15, emit=print, log=print,
                aspect: str = "9:16") -> int:
    """Read a live stream's chat for `minutes` and clip every burst. emit(record) is called per clip; returns the count."""
    size = frame_size(aspect)
    stream = await check_live(platform, channel)
    if stream is None:
        raise ZClipperError(f"{channel} is not live on {platform} right now")
    log(f"{platform} {channel} is live: {stream.title[:80]}")
    folder = Path(out_dir) / f"{platform}_{stream.stream_id}"
    detector = SpikeDetector(factor=factor, min_messages=min_messages)
    deadline = _now() + timedelta(minutes=minutes)
    pending: set[asyncio.Task] = set()
    lock = asyncio.Lock()  # one cut and render at a time
    made = 0
    messages = 0
    taken: list[datetime] = []

    async def clip(start_at: datetime, reason: str):
        nonlocal made
        if any(abs((start_at - other).total_seconds()) < DUPLICATE_SECONDS for other in taken):
            return
        taken.append(start_at)
        ready_at = start_at + timedelta(seconds=seconds_before + seconds_after + REPLAY_LAG.get(platform, 60))
        await asyncio.sleep(max((ready_at - _now()).total_seconds(), 0))
        async with lock:
            try:
                record = await _live_clip(stream, start_at, seconds_before + seconds_after, folder, f"{start_at:%H%M%S}_chat",
                                          layout, facecam, height, whisper, size)
            except ZClipperError as exc:
                log(f"clip at {start_at:%H:%M:%S} failed: {str(exc)[:200]}")
                return
        record["reason"] = reason
        made += 1
        emit(record)

    async def chat():
        nonlocal messages
        while True:
            try:
                async for when, _text in read_chat(stream):
                    detector.add(when)
                    messages += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"chat dropped, reconnecting: {str(exc)[:120]}")
            await asyncio.sleep(20)

    reader = asyncio.create_task(chat())
    try:
        while _now() < deadline:
            await asyncio.sleep(5)
            for spike in detector.check(_now()):
                reason = f"Chat burst: {spike.count} messages in 10 s, {spike.score}x the usual rate"
                log(f"{reason} at {spike.at:%H:%M:%S} UTC")
                task = asyncio.create_task(clip(spike.at - timedelta(seconds=seconds_before), reason))
                pending.add(task)
                task.add_done_callback(pending.discard)
    finally:
        reader.cancel()
    log(f"read {messages} chat messages")
    if pending:
        log(f"watch time is up, finishing {len(pending)} clip(s)")
        await asyncio.gather(*pending, return_exceptions=True)
    return made


async def popular(platform: str, channel: str, make_top: int, out_dir: str | Path, layout: str = "split",
                  facecam=None, height: int = 1080, whisper: str | None = "base", log=print, aspect: str = "9:16") -> dict:
    """Viewer-made clips of the current broadcast (Twitch, Kick); with make_top > 0 the most viewed are rendered."""
    size = frame_size(aspect)
    if platform not in ("twitch", "kick"):
        raise ZClipperError("Viewer clips are only available on Twitch and Kick")
    stream = await check_live(platform, channel)
    if stream is None:
        raise ZClipperError(f"{channel} is not live on {platform} right now")
    found = sorted(await viewer_clips(stream), key=lambda clip: clip.views, reverse=True)
    result = {"stream": stream.model_dump(mode="json"), "viewer_clips": [clip.model_dump() for clip in found], "made": []}
    origin = stream.vod_started_at if platform == "twitch" else stream.started_at
    for clip in found[:make_top]:
        if origin is None:
            break
        log(f"rendering viewer clip '{clip.title[:50]}' ({clip.views} views)")
        seconds = min(max(clip.duration, 15), 60)
        try:
            record = await _live_clip(stream, origin + timedelta(seconds=clip.offset), seconds, Path(out_dir) / f"{platform}_{stream.stream_id}",
                                      f"viewer_{clip.id}"[:60], layout, facecam, height, whisper, size)
        except ZClipperError as exc:
            log(f"viewer clip {clip.id} failed: {str(exc)[:200]}")
            continue
        record["reason"] = f"Viewers clipped this ({clip.views} views): {clip.title}"
        result["made"].append(record)
    return result
