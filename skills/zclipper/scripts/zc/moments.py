"""Find the moments worth clipping in a finished stream or video, and download only those seconds.

Uses yt-dlp and ffmpeg on PATH.

Where moments come from:
- "most_replayed": YouTube's own replay graph. Present on normal uploads,
  usually missing on long streams.
- "transcript": bursts of fast speech and reaction words in the captions.
"""

import json
import re
from pathlib import Path
from statistics import mean, pstdev

from pydantic import BaseModel

from .errors import NotFound, ZClipperError

BASE_URL = "https://www.youtube.com"
REACTIONS = re.compile(
    r"\b(no way|oh my (god|gosh)|what the|let'?s go+|holy|insane|crazy|wait wait|are you (kidding|serious)|"
    r"clip (it|that)|wow|omg|oh god|i can'?t believe)\b",
    re.I,
)
NOISE = re.compile(r"^(\[[^\]]*\]|>>|\[|\]|_+|-+|♪+)$")  # sound tags, speaker dashes, music notes
ZERO_WIDTH = re.compile("[​-‍⁠﻿]")


class Stream(BaseModel):
    id: str
    title: str = ""
    channel: str | None = None
    channel_id: str | None = None
    url: str
    duration: float | None = None
    views: int | None = None
    live_status: str | None = None

    @property
    def finished(self) -> bool:
        return self.live_status in ("was_live", "not_live", None) and bool(self.duration)


class Word(BaseModel):
    start: float
    end: float
    text: str


class Moment(BaseModel):
    start: float
    end: float
    score: float = 0.0
    source: str = "transcript"
    title: str = ""
    reason: str = ""
    text: str = ""
    caption: str = ""  # the post's punchline, emoji allowed
    hashtags: list[str] = []
    thumbnail_text: str = ""  # a few words for the thumbnail; ALL CAPS words are drawn in yellow
    thumbnail_at: float | None = None  # seconds into the clip for the thumbnail frame

    @property
    def seconds(self) -> float:
        return self.end - self.start


class StreamData(BaseModel):
    stream: Stream
    heatmap: list[dict] = []
    words: list[Word] = []
    language: str | None = None


def _ydl(**options):
    try:
        import yt_dlp
    except ImportError as exc:
        raise ImportError("zclipper needs yt-dlp: pip install -U yt-dlp") from exc
    base = {"quiet": True, "no_warnings": True, "noprogress": True, "js_runtimes": {"node": {}, "deno": {}}}
    return yt_dlp.YoutubeDL({**base, **options})


def _extract(url: str, **options) -> dict:
    import yt_dlp

    try:
        with _ydl(skip_download=True, **options) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except yt_dlp.utils.DownloadError as exc:
        message = str(exc)
        if "does not have a" in message or "not exist" in message or "unavailable" in message.lower():
            raise NotFound(message) from exc
        raise ZClipperError(message) from exc


def _stream(entry: dict) -> Stream:
    return Stream(
        id=entry["id"],
        title=entry.get("title") or "",
        channel=entry.get("channel") or entry.get("uploader"),
        channel_id=entry.get("channel_id"),
        url=entry.get("webpage_url") or f"{BASE_URL}/watch?v={entry['id']}",
        duration=entry.get("duration"),
        views=entry.get("view_count"),
        live_status=entry.get("live_status"),
    )


def list_streams(channel: str, limit: int = 6, keyword: str | None = None) -> list[Stream]:
    """A channel's newest streams, newest first. channel is "@handle" or a channel URL.

    keyword keeps only streams whose title contains it (for example "gta").
    Streams still live have no duration yet; check Stream.finished.
    """
    base = channel if channel.startswith("http") else f"{BASE_URL}/{channel if channel.startswith('@') else '@' + channel}"
    info = _extract(f"{base.rstrip('/')}/streams", extract_flat="in_playlist", playlistend=max(limit * 3, limit))
    streams = []
    for entry in info.get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        entry.setdefault("channel", info.get("channel"))
        entry.setdefault("channel_id", info.get("channel_id"))
        stream = _stream(entry)
        if keyword and keyword.lower() not in stream.title.lower():
            continue
        streams.append(stream)
    return streams[:limit]


def parse_json3(data: dict) -> list[Word]:
    """Word timings from YouTube's json3 caption format.

    Styled manual captions pad lines with zero-width spaces and send each
    line once per pen layer (outline and fill) at the same time; the padding
    is removed and a repeated (time, text) event is read once.
    """
    words: list[Word] = []
    seen: set[tuple[int, str]] = set()
    for event in data.get("events") or []:
        base = event.get("tStartMs")
        if base is None:
            continue
        event_end = (base + event.get("dDurationMs", 0)) / 1000
        segs = [{**seg, "utf8": ZERO_WIDTH.sub("", seg.get("utf8") or "")} for seg in event.get("segs") or []]
        segs = [seg for seg in segs if seg["utf8"].strip()]
        line = (base, " ".join(" ".join(seg["utf8"].split()) for seg in segs))
        if not segs or line in seen:
            continue
        seen.add(line)
        for position, seg in enumerate(segs):
            start = (base + seg.get("tOffsetMs", 0)) / 1000
            following = segs[position + 1] if position + 1 < len(segs) else None
            end = (base + following.get("tOffsetMs", 0)) / 1000 if following else event_end
            # Auto captions time every word; manual captions time a whole line. A line's
            # time is shared out by word length so each word gets its own moment on screen.
            pieces = seg["utf8"].split()
            span = max(end - start, 0.05 * len(pieces))
            total = sum(len(piece) + 1 for piece in pieces)
            at = start
            for piece in pieces:
                share = span * (len(piece) + 1) / total
                words.append(Word(start=round(at, 3), end=round(at + share, 3), text=piece))
                at += share
    for current, following in zip(words, words[1:]):
        if current.end > following.start >= current.start:
            current.end = max(following.start, current.start + 0.05)
    return words


def _caption_track(info: dict, language: str | None) -> tuple[str | None, str | None]:
    """(language, json3 url) for the best caption track.

    Order: the requested language, then a manual track in the language the
    video is spoken in, then YouTube's original-language auto track, then any
    manual track, then English auto captions. Big channels upload many
    translated manual tracks, so "any manual track first" would often pick a
    translation instead of the words actually spoken.
    """
    manual, auto = info.get("subtitles") or {}, info.get("automatic_captions") or {}
    spoken = (info.get("language") or "").split("-")[0].lower()

    def same(code: str, base: str) -> bool:
        return bool(base) and (code.lower() == base or code.lower().startswith(base + "-"))

    order = []
    if language:
        order += [(manual, code) for code in manual if same(code, language.lower())]
        order += [(auto, code) for code in (f"{language}-orig", language) if code in auto]
    order += [(manual, code) for code in manual if same(code, spoken)]
    order += [(auto, code) for code in auto if code.endswith("-orig")]
    order += [(auto, code) for code in auto if spoken and code.lower() == spoken]
    order += [(manual, code) for code in manual]
    order += [(auto, "en")]
    for pool, code in order:
        for track in pool.get(code) or []:
            if track.get("ext") == "json3":
                return code, track.get("url")
    return None, None


def load_stream(video: str, language: str | None = None, fetch=None) -> StreamData:
    """Details, replay graph and word-timed captions for one video.

    video is a URL (YouTube, or a Twitch or Kick VOD) or a YouTube video id.
    Only YouTube offers the replay graph and captions; other sites give the
    details only. fetch(url) -> bytes is replaceable for tests; by default
    yt-dlp's own network stack downloads the caption track.
    """
    info = _extract(video if video.startswith("http") else f"{BASE_URL}/watch?v={video}")
    code, url = _caption_track(info, language)
    words: list[Word] = []
    if url:
        if fetch is None:
            with _ydl() as ydl:
                raw = ydl.urlopen(url).read()
        else:
            raw = fetch(url)
        words = parse_json3(json.loads(raw))
    return StreamData(stream=_stream(info), heatmap=info.get("heatmap") or [], words=words, language=code)


def _spread(candidates: list[Moment], count: int, gap: float) -> list[Moment]:
    """The highest-scoring candidates that do not sit within gap seconds of each other."""
    chosen: list[Moment] = []
    for moment in sorted(candidates, key=lambda m: m.score, reverse=True):
        if all(moment.start >= other.end + gap or moment.end <= other.start - gap for other in chosen):
            chosen.append(moment)
        if len(chosen) == count:
            break
    return sorted(chosen, key=lambda m: m.start)


def moments_from_heatmap(heatmap: list[dict], count: int = 5, seconds: float = 40, skip_intro: float = 60) -> list[Moment]:
    """Peaks of the replay graph. The first minute is skipped: it always scores highest."""
    candidates = []
    for point in heatmap:
        if point["start_time"] < skip_intro:
            continue
        middle = (point["start_time"] + point["end_time"]) / 2
        start = max(middle - seconds / 2, 0)
        candidates.append(Moment(start=start, end=start + seconds, score=point["value"], source="most_replayed"))
    return _spread(candidates, count, gap=seconds)


def moments_from_transcript(
    words: list[Word], count: int = 5, seconds: float = 40, step: float = 10, skip_intro: float = 120
) -> list[Moment]:
    """Windows where people talk fastest and react most.

    Each window scores its words per second against the stream's average (in
    standard deviations) plus a bonus for reaction phrases. This is a rough
    signal: it finds loud, busy moments, not necessarily good ones.
    """
    spoken = [word for word in words if not NOISE.match(word.text)]
    if not spoken:
        return []
    last = spoken[-1].end
    windows: list[tuple[float, list[Word]]] = []
    start, low = skip_intro if last > skip_intro * 3 else 0.0, 0
    while start + seconds <= last + step:
        while low < len(spoken) and spoken[low].start < start:
            low += 1
        high = low
        while high < len(spoken) and spoken[high].start < start + seconds:
            high += 1
        windows.append((start, spoken[low:high]))
        start += step
    rates = [len(inside) / seconds for _, inside in windows]
    average, deviation = mean(rates), pstdev(rates) or 1.0
    candidates = []
    for (begin, inside), rate in zip(windows, rates):
        if len(inside) < 8:
            continue
        text = " ".join(word.text for word in inside)
        reactions = len(REACTIONS.findall(text))
        score = (rate - average) / deviation + 0.5 * min(reactions, 4)
        candidates.append(Moment(start=begin, end=begin + seconds, score=round(score, 3), text=text))
    return _spread(candidates, count, gap=seconds / 2)


def snap_to_speech(moment: Moment, words: list[Word], pause: float = 0.45, reach: float = 4.0) -> Moment:
    """Move the cut points to nearby pauses so a clip does not start or end mid-sentence."""

    def nearest(target: float) -> float:
        best, distance = target, reach
        for current, following in zip(words, words[1:]):
            if following.start - current.end >= pause:
                cut = (current.end + following.start) / 2
                if abs(cut - target) < distance:
                    best, distance = cut, abs(cut - target)
        return best

    start, end = nearest(moment.start), nearest(moment.end)
    if end - start < 8:
        return moment
    return moment.model_copy(update={"start": round(max(start, 0), 2), "end": round(end, 2)})


def words_between(words: list[Word], start: float, end: float) -> list[Word]:
    return [word for word in words if word.end > start and word.start < end]


def find_moments(data: StreamData, count: int = 5, seconds: float = 40) -> list[Moment]:
    """Candidate moments for a stream: the replay graph when it exists, else the transcript."""
    moments = moments_from_heatmap(data.heatmap, count, seconds) if data.heatmap else []
    if not moments:
        moments = moments_from_transcript(data.words, count, seconds)
    snapped = []
    for moment in moments:
        moment = snap_to_speech(moment, data.words)
        text = " ".join(word.text for word in words_between(data.words, moment.start, moment.end))
        snapped.append(moment.model_copy(update={"text": text}))
    return snapped


def download_segment(video_id: str, start: float, end: float, out_path: str | Path, height: int = 1080) -> Path:
    """Download only [start, end] of a YouTube video as mp4. Needs ffmpeg."""
    return download_range(f"{BASE_URL}/watch?v={video_id}", start, end, out_path, height)


def download_range(url: str, start: float, end: float, out_path: str | Path, height: int = 1080) -> Path:
    """Download only [start, end] seconds of any video yt-dlp supports (YouTube, Twitch or Kick VODs) as mp4."""
    import yt_dlp

    video_id = url
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    options = {
        "format": f"bv*[height<={height}]+ba/b[height<={height}]/b",
        "download_ranges": yt_dlp.utils.download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
        "merge_output_format": "mp4",
        "outtmpl": str(out_path.with_suffix("")) + ".%(ext)s",
        "overwrites": True,
    }
    try:
        with _ydl(**options) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        raise ZClipperError(f"download {video_id} {start:.0f}-{end:.0f}: {exc}") from exc
    result = out_path.with_suffix(".mp4")
    if not result.exists():
        raise ZClipperError(f"download {video_id}: no file written")
    return result
