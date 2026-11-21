"""Turn a landscape segment into a clip with burned-in captions, using ffmpeg.

Frame sizes come from ASPECTS; 9:16 (1080x1920) is the default.
Layouts:
- "split": the streamer's facecam across the top, gameplay filling the rest.
  Needs the facecam box and a frame taller than it is wide; otherwise it
  falls back to "crop".
- "crop": the centre of the gameplay filling the whole frame.
"""

import shutil
import subprocess
from pathlib import Path

from .errors import ZClipperError
from .moments import NOISE, Word

WIDTH, HEIGHT = 1080, 1920
TOP_HEIGHT = 700  # facecam band in the split layout at 1080x1920
ASPECTS = {"9:16": (1080, 1920), "4:5": (1080, 1350), "1:1": (1080, 1080), "16:9": (1920, 1080)}
WHITE, YELLOW = "&H00FFFFFF&", "&H0000E6FF&"  # ASS colours are BGR

_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,{font},{s64},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,3,{s14},0,8,{s70},{s70},{s170},1
Style: Words,{font},{s88},&H00FFFFFF,&H00FFFFFF,&H00281A12,&H00000000,-1,0,0,0,100,100,0,0,1,{s9},{s3},5,{s60},{s60},0,1{extra}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def frame_size(aspect: str) -> tuple[int, int]:
    """(width, height) for an aspect name such as "9:16"; ValueError for an unknown one."""
    if aspect not in ASPECTS:
        raise ValueError(f"Aspect {aspect} is not supported; use one of {', '.join(ASPECTS)}")
    return ASPECTS[aspect]


def _scaled(size: tuple[int, int], *values: int) -> list[int]:
    """Sizes designed for 1080x1920, scaled so text takes the same share of the screen at any aspect."""
    scale = min(size[0], size[1] * 9 / 16) / WIDTH
    return [max(round(value * scale), 1) for value in values]


def can_split(size: tuple[int, int]) -> bool:
    """The facecam-over-gameplay stack only fits a frame taller than it is wide."""
    return size[1] > size[0]


def top_height(size: tuple[int, int]) -> int:
    return round(size[1] * TOP_HEIGHT / HEIGHT)


def _header(font: str, size: tuple[int, int], extra: str = "") -> str:
    names = (3, 9, 14, 60, 64, 70, 88, 170)
    values = {f"s{name}": value for name, value in zip(names, _scaled(size, *names))}
    return _HEADER.format(width=size[0], height=size[1], font=font, extra=f"\n{extra}" if extra else "", **values)


def _stamp(seconds: float) -> str:
    seconds = max(seconds, 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"


def _clean(text: str) -> str:
    # Emoji have no glyph in the caption font and would render as blobs, so they are dropped.
    text = "".join(ch for ch in text if ord(ch) < 0x2190 or 0x3000 <= ord(ch) < 0xFE00)
    return " ".join(text.replace("\\", "").replace("{", "(").replace("}", ")").split())


def caption_groups(words: list[Word], start: float, end: float, per_line: int = 3, max_gap: float = 0.7) -> list[list[Word]]:
    """Words grouped into short on-screen lines, each word timed relative to the clip start.

    A word stays lit until the next word begins, and a line stays up until the
    next line begins, so captions never overlap or flicker.
    """
    groups: list[list[Word]] = []
    for word in words:
        if word.end <= start or word.start >= end or NOISE.match(word.text):
            continue
        if groups and len(groups[-1]) < per_line and word.start - groups[-1][-1].end <= max_gap:
            groups[-1].append(word)
        else:
            groups.append([word])
    flat = [word for group in groups for word in group]
    timed: list[list[Word]] = []
    position = 0
    for group in groups:
        line = []
        for word in group:
            following = flat[position + 1].start if position + 1 < len(flat) else end
            shown_until = min(max(word.end, word.start + 0.2), following, end)
            if position + 1 < len(flat) and following - word.end <= max_gap:
                shown_until = min(following, end)  # no gap inside a run of speech
            begin = max(word.start - start, 0)
            if shown_until - start > begin:
                line.append(Word(start=begin, end=shown_until - start, text=word.text))
            position += 1
        if line:
            timed.append(line)
    return timed


def caption_chunks(words: list[Word], start: float, end: float, per_line: int = 3, max_gap: float = 0.7) -> list[Word]:
    """Each on-screen line as one timed piece of text."""
    return [
        Word(start=line[0].start, end=line[-1].end, text=" ".join(word.text for word in line))
        for line in caption_groups(words, start, end, per_line, max_gap)
    ]


def build_ass(
    words: list[Word], start: float, end: float, title: str = "", font: str = "Arial Black",
    uppercase: bool = True, highlight: bool = True, words_y: int | None = None, size: tuple[int, int] = (WIDTH, HEIGHT),
) -> str:
    """An ASS subtitle file: spoken words centred at words_y, the current word in yellow.

    title adds a bar at the top; leave it empty when something else (a facecam)
    occupies that space.
    """
    if words_y is None:
        words_y = round(size[1] * 1490 / HEIGHT)
    lines = [_header(font, size)]
    if title:
        lines.append(f"Dialogue: 1,0:00:00.00,{_stamp(end - start)},Title,,0,0,0,,{_clean(title)}")
    place = f"{{\\pos({size[0] // 2},{words_y})}}"
    for line in caption_groups(words, start, end):
        texts = [_clean(word.text.upper() if uppercase else word.text) for word in line]
        if not highlight:
            lines.append(f"Dialogue: 0,{_stamp(line[0].start)},{_stamp(line[-1].end)},Words,,0,0,0,,{place}{' '.join(texts)}")
            continue
        for current, word in enumerate(line):
            parts = [f"{{\\c{YELLOW}}}{text}{{\\c{WHITE}}}" if index == current else text for index, text in enumerate(texts)]
            lines.append(f"Dialogue: 0,{_stamp(word.start)},{_stamp(word.end)},Words,,0,0,0,,{place}{' '.join(parts)}")
    return "\n".join(lines) + "\n"


def thumbnail_ass(text: str, y: int, font: str = "Arial Black", size: tuple[int, int] = (WIDTH, HEIGHT)) -> str:
    """One big punchline, uppercase, wrapped by libass; words written in CAPS in `text` turn yellow."""
    big, outline, shadow, margin = _scaled(size, 128, 14, 6, 50)
    header = _header(font, size, f"Style: Thumb,{font},{big},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},5,{margin},{margin},0,1")
    words = []
    for word in _clean(text).split():
        letters = [ch for ch in word if ch.isalpha()]
        loud = len(letters) > 1 and all(ch.isupper() for ch in letters)
        words.append(f"{{\\c{YELLOW}}}{word.upper()}{{\\c{WHITE}}}" if loud else word.upper())
    return header + f"Dialogue: 0,0:00:00.00,0:00:10.00,Thumb,,0,0,0,,{{\\pos({size[0] // 2},{y})}}{' '.join(words)}\n"


def valid_box(box) -> bool:
    """A facecam box is [x, y, width, height] as fractions of the frame."""
    if not box or len(box) != 4:
        return False
    x, y, w, h = box
    return 0 <= x < 1 and 0 <= y < 1 and w >= 0.05 and h >= 0.05 and x + w <= 1.001 and y + h <= 1.001


def splits(layout: str, facecam, size: tuple[int, int] = (WIDTH, HEIGHT)) -> bool:
    """True when the clip gets the facecam-over-gameplay stack."""
    return layout == "split" and valid_box(facecam) and can_split(size)


def _away_from(x: float, w: float) -> str:
    """Horizontal position for the gameplay crop that keeps a side webcam out of it.

    The gameplay band is a narrow window of a wide frame; centred, it often
    still holds the webcam, so the streamer would appear twice. A webcam on
    the left pushes the window to start at its right edge, one on the right
    makes the window end at its left edge, never past the frame. A webcam
    across the middle leaves the window centred.
    """
    if x + w <= 0.5:
        return f":'min(max((iw-ow)/2,iw*{x + w:.4f}),iw-ow)':0"
    if x >= 0.5:
        return f":'max(min((iw-ow)/2,iw*{x:.4f}-ow),0)':0"
    return ""


def _layout_filter(layout: str, facecam, size: tuple[int, int] = (WIDTH, HEIGHT)) -> str:
    fill = "force_original_aspect_ratio=increase"
    width, height = size
    if splits(layout, facecam, size):
        x, y, w, h = facecam
        top = top_height(size)
        bottom = height - top
        return (
            f"[0:v]split=2[a][b];"
            # The camera is a small part of the frame blown up 2 to 3 times, so it gets the sharper scaler and a light sharpen.
            f"[a]crop=iw*{w:.4f}:ih*{h:.4f}:iw*{x:.4f}:ih*{y:.4f},scale={width}:{top}:{fill}:flags=lanczos,"
            f"crop={width}:{top},unsharp=5:5:0.8:5:5:0.0[cam];"
            f"[b]scale={width}:{bottom}:{fill},crop={width}:{bottom}{_away_from(x, w)}[game];"
            f"[cam][game]vstack=inputs=2"
        )
    return f"[0:v]scale={width}:{height}:{fill},crop={width}:{height}"


def ffmpeg_command(source: str, subtitles: str, output: str, layout: str = "split", facecam=None,
                   size: tuple[int, int] = (WIDTH, HEIGHT)) -> list[str]:
    video = _layout_filter(layout, facecam, size)
    return [
        "ffmpeg", "-y", "-loglevel", "error", "-i", source,
        "-filter_complex", f"{video},ass={subtitles},format=yuv420p[v]",
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-r", "30",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", output,
    ]  # fmt: skip


def read_gray(source: str | Path, at: float | None = None, width: int = 960) -> tuple[int, int, bytes]:
    """One frame of a video or image as 8-bit grayscale: (width, height, pixels)."""
    command = ["ffmpeg", "-v", "error"] + (["-ss", f"{at:.2f}"] if at else []) + [
        "-i", str(source), "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]  # fmt: skip
    done = subprocess.run(command, capture_output=True)
    if done.returncode != 0 or not done.stdout:
        raise ZClipperError(f"ffmpeg could not read a frame: {done.stderr.decode(errors='replace').strip()[-300:]}")
    return width, len(done.stdout) // width, done.stdout


def _find_edge(pixels: bytes, width: int, positions: range, span: range, vertical: bool, jump: int = 16) -> int | None:
    """The position in `positions` with the strongest long brightness change across `span`.

    An overlay border runs the whole length of the box and separates two
    unrelated pictures, so it is both long and sharp. A line must cover at
    least 60% of the span to count; among those, the sharpest wins, which
    keeps lines inside the camera image (shelves, door frames) from winning.
    """
    best, best_score = None, 0.0
    for position in positions:
        if vertical:
            diffs = [abs(pixels[row * width + position] - pixels[row * width + position - 1]) for row in span]
        else:
            diffs = [abs(pixels[position * width + col] - pixels[(position - 1) * width + col]) for col in span]
        strong = [min(diff, 120) for diff in diffs if diff >= jump]
        if len(strong) < 0.6 * len(diffs):
            continue
        score = sum(strong) / len(diffs)
        if score > best_score:
            best, best_score = position, score
    return best


def snap_box(frame: tuple[int, int, bytes], box: list[float], reach: float = 0.06, inset: float = 0.004) -> list[float]:
    """Move each side of a roughly drawn facecam box to the overlay's real edge.

    Each side looks up to `reach` of the frame either way for the overlay
    border; a side with no clear border stays where it was. `inset` then pulls
    every side in a little so a thin border line or shadow is not included.
    """
    width, height, pixels = frame
    x, y, w, h = box
    left, top, right, bottom = x * width, y * height, (x + w) * width, (y + h) * height
    reach_x, reach_y = max(int(reach * width), 2), max(int(reach * height), 2)

    def around(value: float, limit: int, spread: int) -> range:
        return range(max(int(value) - spread, 1), min(int(value) + spread + 1, limit))

    rows = range(max(int(top + (bottom - top) * 0.15), 0), min(int(bottom - (bottom - top) * 0.15), height))
    cols = range(max(int(left + (right - left) * 0.15), 0), min(int(right - (right - left) * 0.15), width))
    if len(rows) < 4 or len(cols) < 4:
        return box
    found = [
        _find_edge(pixels, width, around(left, width, reach_x), rows, vertical=True),
        _find_edge(pixels, width, around(top, height, reach_y), cols, vertical=False),
        _find_edge(pixels, width, around(right, width, reach_x), rows, vertical=True),
        _find_edge(pixels, width, around(bottom, height, reach_y), cols, vertical=False),
    ]
    if not any(side is not None for side in found):
        return box
    left, top, right, bottom = (side if side is not None else old for side, old in zip(found, (left, top, right, bottom)))
    snapped = [left / width + inset, top / height + inset, (right - left) / width - 2 * inset, (bottom - top) / height - 2 * inset]
    snapped = [round(min(max(value, 0.0), 1.0), 4) for value in snapped]
    return snapped if valid_box(snapped) else box


def refine_facecam(source: str | Path, box: list[float] | None, at: float | None = None) -> list[float] | None:
    """snap_box against a frame of `source`; the box unchanged when the frame cannot be read."""
    if not valid_box(box):
        return box
    try:
        return snap_box(read_gray(source, at), box)
    except ZClipperError:
        return box


def grab_frame(source: str | Path, output: str | Path, at: float = 1.0, width: int = 1280) -> Path:
    """Save one frame of a video as a JPEG."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(source), "-frames:v", "1", "-vf", f"scale={width}:-2", str(output)]
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0 or not output.exists():
        raise ZClipperError(f"ffmpeg could not read a frame: {done.stderr.strip()[-300:]}")
    return output


def video_seconds(path: str | Path) -> float:
    done = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(done.stdout.strip())
    except ValueError as exc:
        raise ZClipperError(f"ffprobe could not read {Path(path).name}") from exc


def render_thumbnail(
    source: str | Path, output: str | Path, at: float, text: str = "", layout: str = "split", facecam=None,
    size: tuple[int, int] = (WIDTH, HEIGHT),
) -> Path:
    """A JPEG the size of the clip from `source` (an uncaptioned segment) at `at` seconds, framed like the clip.

    The punchline sits on the seam between facecam and gameplay in the split
    layout, a little above the middle otherwise. No text gives a plain frame.
    """
    source, output = Path(source).resolve(), Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    video = _layout_filter(layout, facecam, size)
    if text.strip():
        subtitles = output.with_suffix(".thumb.ass")
        y = top_height(size) if splits(layout, facecam, size) else round(size[1] * 820 / HEIGHT)
        subtitles.write_text(thumbnail_ass(text, y, size=size), encoding="utf-8")
        video += f",ass={subtitles.name}"
    try:
        local_source = source.relative_to(output.parent).as_posix()
    except ValueError:
        local_source = str(source)
    output.unlink(missing_ok=True)
    for seek in (max(at, 0), 0):  # a time past the segment's real end gives no frame, so fall back to the start
        command = [
            "ffmpeg", "-y", "-loglevel", "error", "-ss", f"{seek:.2f}", "-i", local_source,
            "-filter_complex", f"{video}[v]", "-map", "[v]", "-frames:v", "1", "-q:v", "2", output.name,
        ]  # fmt: skip
        done = subprocess.run(command, cwd=output.parent, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if done.returncode == 0 and output.exists():
            break
    if not output.exists():
        raise ZClipperError(f"ffmpeg could not make the thumbnail: {done.stderr.strip()[-400:]}")
    return output


def render_vertical(
    source: str | Path, output: str | Path, words: list[Word], start: float, end: float,
    title: str = "", layout: str = "split", facecam=None, size: tuple[int, int] = (WIDTH, HEIGHT),
) -> Path:
    """Render source (a segment that begins at `start` of the stream) as a captioned clip of `size` (default 1080x1920).

    ffmpeg runs inside the output folder with relative file names, which avoids
    the drive-letter escaping that the ass filter needs on Windows.
    """
    if shutil.which("ffmpeg") is None:
        raise ZClipperError("ffmpeg is not installed or not on PATH")
    source, output = Path(source).resolve(), Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    split = splits(layout, facecam, size)
    subtitles = output.with_suffix(".ass")
    words_y = top_height(size) + _scaled(size, 330)[0] if split else round(size[1] * 1490 / HEIGHT)
    ass = build_ass(words, start, end, "" if split else title, words_y=words_y, size=size)
    subtitles.write_text(ass, encoding="utf-8")
    try:
        local_source = source.relative_to(output.parent).as_posix()
    except ValueError:
        local_source = str(source)
    command = ffmpeg_command(local_source, subtitles.name, output.name, layout, facecam, size)
    done = subprocess.run(command, cwd=output.parent, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0 or not output.exists():
        raise ZClipperError(f"ffmpeg failed: {done.stderr.strip()[-600:]}")
    return output
