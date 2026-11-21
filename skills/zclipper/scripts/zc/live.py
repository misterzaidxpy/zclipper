"""Clip streams while they are live, on YouTube, Twitch and Kick. Free and logged out.

Per platform this module can:
- tell whether a streamer is live (`check_live`),
- cut a stretch of the current broadcast by wall-clock time (`cut_live`),
- read live chat as (timestamp, text) pairs (`read_chat`),
- list viewer-made clips of the current broadcast (`viewer_clips`, Twitch and Kick).

`SpikeDetector` turns chat timestamps into moments worth clipping.
All endpoints are public and need no login.
"""

import asyncio
import json
import math
import re
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import AsyncIterator

from pydantic import BaseModel

from .errors import ZClipperError

PLATFORMS = ("youtube", "twitch", "kick")
TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # the public web client ID streamlink uses
TWITCH_IRC = "wss://irc-ws.chat.twitch.tv:443"
KICK_PUSHER_KEY = "34bf7a0ff419a2a775b9"  # fallback; the current key is looked up at start
KICK_PUSHER_CLUSTER = "us2"
YOUTUBE_SEGMENT = 5.0  # seconds per segment in YouTube's live playlists


class LiveStream(BaseModel):
    platform: str
    channel: str  # YouTube handle, Twitch login or Kick slug
    stream_id: str
    title: str = ""
    game: str = ""  # Twitch game or Kick category; empty on YouTube
    started_at: datetime | None = None
    url: str
    vod_id: str | None = None  # Twitch: the archive that grows while live
    vod_started_at: datetime | None = None
    chat_id: str | None = None  # YouTube video id, Twitch login, Kick chatroom id
    dvr_url: str | None = None  # Kick: the session playlist that grows from minute 0

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.stream_id}"

    def about(self, topics: list[str]) -> bool:
        """True when the game (or the title, when the platform gives no game) mentions any topic.

        Titles often promise a game for later ("chess now, GTA 6 later"), so a known game wins.
        No topics means every stream counts.
        """
        text = (self.game or self.title).lower()
        return not topics or any(topic.lower() in text for topic in topics)


class Spike(BaseModel):
    at: datetime  # start of the busy chat window
    count: int
    baseline: float

    @property
    def score(self) -> float:
        return round(self.count / max(self.baseline, 1.0), 2)


class ViewerClip(BaseModel):
    id: str
    title: str = ""
    views: int = 0
    offset: float  # seconds into the broadcast
    duration: float


def utc(value: str | None) -> datetime | None:
    """Parse the timestamps the three platforms use; naive ones are UTC."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    stamp = datetime.fromisoformat(text)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _session():
    from curl_cffi.requests import AsyncSession

    return AsyncSession(impersonate="chrome", timeout=30)


def _json_after(page: str, marker: str) -> dict | None:
    start = page.find(marker)
    if start < 0:
        return None
    start = page.find("{", start)
    try:
        return json.JSONDecoder().raw_decode(page, start)[0]
    except ValueError:
        return None


def find_key(data, key: str):
    """Every value stored under `key` anywhere inside nested dicts and lists."""
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for name, value in item.items():
                if name == key:
                    yield value
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)


# ---------- live checks ----------


def parse_youtube_live(page: str, handle: str) -> LiveStream | None:
    player = _json_after(page, "ytInitialPlayerResponse = ")
    if not player:
        return None
    details = player.get("videoDetails") or {}
    broadcast = (player.get("microformat") or {}).get("playerMicroformatRenderer", {}).get("liveBroadcastDetails") or {}
    if not details.get("isLive") or broadcast.get("isLiveNow") is False:
        return None
    video_id = details["videoId"]
    return LiveStream(
        platform="youtube", channel=handle, stream_id=video_id, title=details.get("title", ""),
        started_at=utc(broadcast.get("startTimestamp")), url=f"https://www.youtube.com/watch?v={video_id}", chat_id=video_id,
    )


def parse_twitch_live(data: dict, login: str) -> LiveStream | None:
    user = (data.get("data") or {}).get("user") or {}
    stream = user.get("stream")
    if not stream or stream.get("type") != "live":
        return None
    archive = stream.get("archiveVideo") or {}
    return LiveStream(
        platform="twitch", channel=login, stream_id=str(stream["id"]), title=(user.get("broadcastSettings") or {}).get("title", ""),
        game=(stream.get("game") or {}).get("name") or "", started_at=utc(stream.get("createdAt")), url=f"https://www.twitch.tv/{login}",
        vod_id=archive.get("id") if archive.get("status") == "RECORDING" else None,
        vod_started_at=utc(archive.get("createdAt")), chat_id=login,
    )


def parse_kick_live(channel: dict, videos: list, slug: str) -> LiveStream | None:
    live = channel.get("livestream")
    if not live or live.get("is_live") is False:
        return None
    session = next((video for video in videos if video.get("is_live")), {})
    return LiveStream(
        platform="kick", channel=slug, stream_id=str(live["id"]), title=live.get("session_title", ""),
        game=((live.get("categories") or [{}])[0] or {}).get("name") or "", started_at=utc(live.get("start_time") or live.get("created_at")), url=f"https://kick.com/{slug}",
        chat_id=str((channel.get("chatroom") or {}).get("id") or ""), dvr_url=session.get("source"),
    )


async def check_live(platform: str, channel: str) -> LiveStream | None:
    """The streamer's current broadcast on one platform, or None when offline."""
    async with _session() as session:
        if platform == "youtube":
            handle = channel if channel.startswith("@") else f"@{channel}"
            response = await session.get(f"https://www.youtube.com/{handle}/live")
            if response.status_code == 404:
                raise ZClipperError(f"YouTube has no channel {handle}")
            return parse_youtube_live(response.text, handle)
        if platform == "twitch":
            query = (
                '{ user(login:"%s"){ broadcastSettings{ title } stream{ id type createdAt game{ name } '
                "archiveVideo{ id status createdAt } } } }" % channel.lower()
            )
            response = await session.post(TWITCH_GQL, json={"query": query}, headers={"Client-ID": TWITCH_CLIENT_ID})
            if response.status_code != 200:
                raise ZClipperError(f"Twitch answered {response.status_code} for {channel}")
            return parse_twitch_live(response.json(), channel.lower())
        if platform == "kick":
            response = await session.get(f"https://kick.com/api/v2/channels/{channel}")
            if response.status_code == 404:
                raise ZClipperError(f"Kick has no channel {channel}")
            if response.status_code != 200:
                raise ZClipperError(f"Kick answered {response.status_code} for {channel}")
            data = response.json()
            if not data.get("livestream"):
                return None
            videos = await session.get(f"https://kick.com/api/v2/channels/{channel}/videos")
            return parse_kick_live(data, videos.json() if videos.status_code == 200 else [], channel)
    raise ValueError(f"Unknown platform {platform}")


# ---------- cutting the broadcast ----------


def _run(command: list[str], what: str) -> None:
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise ZClipperError(f"{what} failed: {done.stderr.strip()[-400:]}")


def pick_variant(master: str, base_url: str, height: int = 1080) -> str:
    """The best rendition up to `height` from an HLS master playlist."""
    from urllib.parse import urljoin

    best, best_key = None, (-1, -1)
    lines = master.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF") or index + 1 >= len(lines):
            continue
        size = re.search(r"RESOLUTION=\d+x(\d+)", line)
        rate = re.search(r"BANDWIDTH=(\d+)", line)
        rendition_height = int(size.group(1)) if size else 0
        if rendition_height > height:
            continue
        key = (rendition_height, int(rate.group(1)) if rate else 0)
        if key > best_key:
            best, best_key = urljoin(base_url, lines[index + 1].strip()), key
    if best is None:
        raise ZClipperError("The stream playlist has no usable rendition")
    return best


def pick_youtube_live(formats: list[dict], height: int = 1080) -> tuple[str, str | None]:
    """(video playlist, audio playlist or None) from yt-dlp's formats for a YouTube live stream.

    YouTube offers live video and audio as separate HLS playlists; a playlist
    with both is used when offered.
    """
    hls = [f for f in formats if "m3u8" in (f.get("protocol") or "")]
    video = [f for f in hls if f.get("vcodec") not in (None, "none") and (f.get("height") or 0) <= height]
    if not video:
        raise ZClipperError("YouTube offered no live video playlist")
    best = max(video, key=lambda f: ((f.get("height") or 0), f.get("tbr") or 0))
    if best.get("acodec") not in (None, "none", ""):
        return best["url"], None
    audio = [f for f in hls if f.get("vcodec") == "none"]
    if not audio:
        raise ZClipperError("YouTube offered no live audio playlist")
    return best["url"], max(audio, key=lambda f: f.get("format_id", ""))["url"]


def youtube_live_formats(video_id: str, height: int = 1080) -> tuple[str, str | None]:
    from .moments import _extract

    return pick_youtube_live(_extract(f"https://www.youtube.com/watch?v={video_id}").get("formats") or [], height)


def cut_live(stream: LiveStream, start_at: datetime, seconds: float, out_path: str | Path, height: int = 1080) -> Path:
    """Save `seconds` of the broadcast starting at wall-clock `start_at` as mp4.

    YouTube keeps about the last hour in its live playlist; Twitch and Kick
    keep a recording that grows from the start of the stream. Both need the
    streamer to allow replays.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    if stream.platform == "youtube":
        video, audio = youtube_live_formats(stream.stream_id, height)
        back = math.ceil((now - start_at).total_seconds() / YOUTUBE_SEGMENT) + 1
        if back > 700:
            raise ZClipperError("That moment is more than an hour back, outside YouTube's live window")
        inputs = ["-live_start_index", str(-back), "-i", video]
        if audio:
            inputs += ["-live_start_index", str(-back), "-i", audio, "-map", "0:v", "-map", "1:a"]
        command = ["ffmpeg", "-y", "-v", "error", *inputs, "-t", f"{seconds:.1f}", "-c", "copy", str(out_path)]
        _run(command, "Cutting the YouTube stream")
    elif stream.platform == "twitch":
        if not stream.vod_id or not stream.vod_started_at:
            raise ZClipperError(f"{stream.channel} has replays turned off on Twitch, so past minutes cannot be cut")
        from .moments import download_range

        offset = (start_at - stream.vod_started_at).total_seconds()
        download_range(f"https://www.twitch.tv/videos/{stream.vod_id}", offset, offset + seconds, out_path, height)
    elif stream.platform == "kick":
        if not stream.dvr_url or not stream.started_at:
            raise ZClipperError(f"{stream.channel} has replays turned off on Kick, so past minutes cannot be cut")
        from curl_cffi import requests as curl

        master = curl.get(stream.dvr_url, impersonate="chrome", timeout=30)
        variant = pick_variant(master.text, stream.dvr_url, height) if "#EXT-X-STREAM-INF" in master.text else stream.dvr_url
        offset = max((start_at - stream.started_at).total_seconds(), 0)
        command = ["ffmpeg", "-y", "-v", "error", "-ss", f"{offset:.1f}", "-i", variant, "-t", f"{seconds:.1f}", "-c", "copy", str(out_path)]
        _run(command, "Cutting the Kick stream")
    else:
        raise ValueError(f"Unknown platform {stream.platform}")
    if not out_path.exists() or out_path.stat().st_size < 10_000:
        raise ZClipperError(f"Cutting {stream.key} wrote no usable file")
    return out_path


# ---------- chat ----------


def parse_youtube_chat_page(page: str) -> tuple[str | None, str | None, str | None]:
    """(continuation for the unfiltered "Live chat" view, client version, visitor data)."""
    data = _json_after(page, 'window["ytInitialData"] = ') or _json_after(page, "ytInitialData = ") or {}

    def token(node) -> str | None:  # the token is a string nested under some "continuation" key
        return next((value for value in find_key(node, "continuation") if isinstance(value, str)), None)

    continuation = None
    for items in find_key(data, "subMenuItems"):
        for item in items:
            if item.get("title") == "Live chat":
                continuation = token(item)
    if continuation is None:
        continuation = token(data)
    version = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', page)
    visitor = re.search(r'"VISITOR_DATA":"([^"]+)"', page)
    return continuation, version.group(1) if version else None, visitor.group(1) if visitor else None


def parse_youtube_chat(data: dict) -> tuple[list[tuple[datetime, str]], str | None, float]:
    """(messages, next continuation, seconds to wait) from a get_live_chat answer."""
    messages = []
    for item in find_key(data, "liveChatTextMessageRenderer"):
        stamp = item.get("timestampUsec")
        if stamp:
            text = "".join(run.get("text", "") for run in (item.get("message") or {}).get("runs") or [])
            messages.append((datetime.fromtimestamp(int(stamp) / 1e6, timezone.utc), text))
    messages.sort(key=lambda pair: pair[0])
    next_token, wait = None, 5.0
    for name in ("invalidationContinuationData", "timedContinuationData", "reloadContinuationData"):
        found = next(find_key(data, name), None)
        if found:
            next_token = found.get("continuation")
            wait = min(max(found.get("timeoutMs", 5000) / 1000, 1.0), 10.0)
            break
    return messages, next_token, wait


async def _youtube_chat(video_id: str) -> AsyncIterator[tuple[datetime, str]]:
    async with _session() as session:
        page = (await session.get(f"https://www.youtube.com/live_chat?is_popout=1&v={video_id}")).text
        continuation, version, visitor = parse_youtube_chat_page(page)
        if not continuation:
            raise ZClipperError(f"YouTube chat is off or unavailable for {video_id}")
        client = {"clientName": "WEB", "clientVersion": version or "2.20261119.01.00", "hl": "en", "gl": "US"}
        if visitor:
            client["visitorData"] = visitor
        while continuation:
            response = await session.post(
                "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?prettyPrint=false",
                json={"context": {"client": client}, "continuation": continuation},
            )
            if response.status_code != 200:
                raise ZClipperError(f"YouTube chat answered {response.status_code}")
            messages, continuation, wait = parse_youtube_chat(response.json())
            for message in messages:
                yield message
            await asyncio.sleep(wait)


def parse_irc_line(line: str) -> tuple[datetime, str] | None:
    """A Twitch IRC PRIVMSG as (sent time, text); None for anything else."""
    if " PRIVMSG " not in line:
        return None
    tags = {}
    if line.startswith("@"):
        raw, _, line = line[1:].partition(" ")
        tags = dict(pair.split("=", 1) for pair in raw.split(";") if "=" in pair)
    text = line.split(" :", 1)[1] if " :" in line else ""
    sent = tags.get("tmi-sent-ts")
    when = datetime.fromtimestamp(int(sent) / 1000, timezone.utc) if sent and sent.isdigit() else datetime.now(timezone.utc)
    return when, text


async def _twitch_chat(login: str) -> AsyncIterator[tuple[datetime, str]]:
    async with _session() as session:
        socket = await session.ws_connect(TWITCH_IRC)
        try:
            for line in ("CAP REQ :twitch.tv/tags twitch.tv/commands", "PASS SCHMOOPIIE", f"NICK justinfan{secrets.randbelow(80000) + 10000}", f"JOIN #{login.lower()}"):
                await socket.send_str(line)
            while True:
                payload, _ = await socket.recv()
                for line in payload.decode(errors="replace").split("\r\n"):
                    if line.startswith("PING"):
                        await socket.send_str("PONG :tmi.twitch.tv")
                    elif message := parse_irc_line(line):
                        yield message
        finally:
            await socket.close()


async def kick_pusher_key() -> tuple[str, str]:
    """Kick's current chat key and cluster; it changed in October 2026."""
    body = {"client": {"id": "zclipper", "type": "web"}, "capabilities": {"accepted_providers": [{"provider": "pusher"}]}}
    try:
        async with _session() as session:
            response = await session.post("https://web.kick.com/api/v1/realtime/connection", json=body)
        credentials = next(find_key(response.json(), "credentials"))
        return credentials["app_key"], credentials.get("cluster", KICK_PUSHER_CLUSTER)
    except Exception:  # the fallback key worked when this was written
        return KICK_PUSHER_KEY, KICK_PUSHER_CLUSTER


def parse_pusher_message(raw: str) -> tuple[datetime, str] | None:
    event = json.loads(raw)
    if not event.get("event", "").endswith("ChatMessageEvent"):
        return None
    data = event.get("data")
    data = json.loads(data) if isinstance(data, str) else data or {}
    return utc(data.get("created_at")) or datetime.now(timezone.utc), data.get("content", "")


async def _kick_chat(chatroom_id: str) -> AsyncIterator[tuple[datetime, str]]:
    key, cluster = await kick_pusher_key()
    url = f"wss://ws-{cluster}.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0&flash=false"
    async with _session() as session:
        socket = await session.ws_connect(url)
        try:
            await socket.send_str(json.dumps({"event": "pusher:subscribe", "data": {"auth": "", "channel": f"chatrooms.{chatroom_id}.v2"}}))
            while True:
                payload, _ = await socket.recv()
                raw = payload.decode(errors="replace")
                if '"pusher:ping"' in raw:
                    await socket.send_str(json.dumps({"event": "pusher:pong", "data": {}}))
                elif '"pusher:error"' in raw:
                    raise ZClipperError(f"Kick chat refused the connection: {raw[:200]}")
                elif message := parse_pusher_message(raw):
                    yield message
        finally:
            await socket.close()


def read_chat(stream: LiveStream) -> AsyncIterator[tuple[datetime, str]]:
    """Live chat messages as (UTC time, text), until the connection ends."""
    if not stream.chat_id:
        raise ZClipperError(f"No chat id for {stream.key}")
    return {"youtube": _youtube_chat, "twitch": _twitch_chat, "kick": _kick_chat}[stream.platform](stream.chat_id)


# ---------- viewer clips ----------


async def viewer_clips(stream: LiveStream) -> list[ViewerClip]:
    """Clips viewers made of this broadcast, most viewed first (Twitch and Kick)."""
    async with _session() as session:
        if stream.platform == "twitch":
            query = (
                '{ user(login:"%s"){ clips(first:20, criteria:{period:LAST_DAY, sort:VIEWS_DESC}){ edges{ node{ '
                "slug title viewCount durationSeconds videoOffsetSeconds video{ id } } } } } }" % stream.channel
            )
            response = await session.post(TWITCH_GQL, json={"query": query}, headers={"Client-ID": TWITCH_CLIENT_ID})
            edges = ((((response.json().get("data") or {}).get("user") or {}).get("clips") or {}).get("edges")) or []
            return [
                ViewerClip(id=node["slug"], title=node.get("title") or "", views=node.get("viewCount") or 0,
                           offset=node["videoOffsetSeconds"], duration=node.get("durationSeconds") or 30)
                for node in (edge["node"] for edge in edges)
                if (node.get("video") or {}).get("id") == stream.vod_id and node.get("videoOffsetSeconds") is not None
            ]
        if stream.platform == "kick":
            response = await session.get(f"https://kick.com/api/v2/channels/{stream.channel}/clips?sort=view&time=day")
            clips = []
            for clip in (response.json().get("clips") if response.status_code == 200 else None) or []:
                if str(clip.get("livestream_id")) != stream.stream_id or not stream.started_at:
                    continue
                started = utc(clip.get("started_at"))
                offset = clip.get("vod_starts_at")
                if offset is None and started is not None:
                    offset = (started - stream.started_at).total_seconds()
                if offset is None:
                    continue
                clips.append(ViewerClip(id=clip["id"], title=clip.get("title") or "", views=clip.get("views") or clip.get("view_count") or 0,
                                        offset=float(offset), duration=clip.get("duration") or 30))
            return clips
    return []


# ---------- moments ----------


class SpikeDetector:
    """Spots sudden bursts of chat, which is when something worth clipping happened.

    Messages are counted in `bucket`-second windows. A window is a spike when
    it holds at least `factor` times the median of the previous `history`
    windows and at least `min_messages` messages. Windows are judged only once
    `settle` seconds have passed, since chat arrives late, and only after
    `warmup` windows of history. Spikes closer than `cooldown` seconds to the
    last one are ignored.
    """

    def __init__(self, bucket: float = 10, history: int = 30, factor: float = 2.5, min_messages: int = 15,
                 warmup: int = 12, settle: float = 15, cooldown: float = 180):
        self.bucket, self.history, self.factor, self.min_messages = bucket, history, factor, min_messages
        self.warmup, self.settle, self.cooldown = warmup, settle, cooldown
        self.counts: dict[int, int] = {}
        self.first: int | None = None
        self.judged: int | None = None
        self.last_spike: datetime | None = None

    def _index(self, when: datetime) -> int:
        return int(when.timestamp() // self.bucket)

    def add(self, when: datetime) -> None:
        index = self._index(when)
        if self.judged is not None and index <= self.judged:
            return  # too late to count; that window was already judged
        self.counts[index] = self.counts.get(index, 0) + 1
        self.first = index if self.first is None else min(self.first, index)

    def check(self, now: datetime) -> list[Spike]:
        if self.first is None:
            return []
        ready = self._index(now - timedelta(seconds=self.settle)) - 1
        start = self.first + self.warmup if self.judged is None else self.judged + 1
        spikes = []
        for index in range(start, ready + 1):
            earlier = [self.counts.get(i, 0) for i in range(max(index - self.history, self.first), index)]
            baseline = median(earlier) if earlier else 0.0
            count = self.counts.get(index, 0)
            at = datetime.fromtimestamp(index * self.bucket, timezone.utc)
            if count >= max(self.factor * baseline, self.min_messages):
                if self.last_spike is None or (at - self.last_spike).total_seconds() >= self.cooldown:
                    spikes.append(Spike(at=at, count=count, baseline=baseline))
                    self.last_spike = at
        if ready >= start:
            self.judged = ready
        for old in [i for i in self.counts if i < ready - self.history - 1]:
            del self.counts[old]
        return spikes
