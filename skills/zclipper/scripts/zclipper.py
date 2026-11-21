"""zclipper command line. Every command prints JSON on stdout; progress and errors go to stderr.

    python zclipper.py doctor
    python zclipper.py find "GTA 6" [--count 5] [--period week]
    python zclipper.py find --streamer ishowspeed
    python zclipper.py moments <url> [--count 10] [--seconds 40] [--whisper base]
    python zclipper.py frame <url or file> --at 120 --out frame.jpg
    python zclipper.py make picks.json [--layout split|crop] [--facecam x,y,w,h] [--out folder]
    python zclipper.py live <twitch|kick|youtube> <channel> [--minutes 60]
    python zclipper.py viewer-clips <twitch|kick> <channel> [--make 3]
    python zclipper.py transcript <file>
"""

import argparse
import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for stream in (sys.stdout, sys.stderr):  # titles and chat carry emoji; Windows consoles default to cp1252
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def out(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str), flush=True)


def log(message: str) -> None:
    print(f"zclipper: {message}", file=sys.stderr, flush=True)


def doctor(_args) -> int:
    report = {"python": sys.version.split()[0], "ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe")}
    for module in ("yt_dlp", "pydantic", "curl_cffi", "faster_whisper", "numpy"):
        report[module] = importlib.util.find_spec(module) is not None
    if report["yt_dlp"]:
        import yt_dlp

        report["yt_dlp_version"] = yt_dlp.version.__version__
    from zc.whisper import home

    report["zclipper_home"] = str(home())
    missing = [name for name in ("yt_dlp", "pydantic", "curl_cffi") if not report[name]]
    fixes = []
    if missing:
        fixes.append("pip install -U " + " ".join(name.replace("_", "-") for name in missing))
    if not report["ffmpeg"]:
        fixes.append("install ffmpeg: winget install Gyan.FFmpeg (Windows), brew install ffmpeg (macOS), apt install ffmpeg (Linux)")
    if not report["faster_whisper"]:
        report["note"] = "Optional: pip install faster-whisper numpy for captions on videos and live clips without captions"
    report["ready"] = not fixes
    report["fix"] = fixes
    out(report)
    return 0 if not fixes else 1


def find(args) -> int:
    from zc import find as finder

    if args.streamer:
        out(asyncio.run(finder.streamer(args.streamer)))
    elif args.topic:
        out({"topic": args.topic, "videos": finder.search(args.topic, args.count, args.period, args.min_minutes)})
    else:
        raise ValueError("Give a topic, or --streamer <name>")
    return 0


def moments(args) -> int:
    from zc.clips import candidates

    out(candidates(args.url, args.count, args.seconds, args.language, args.whisper))
    return 0


def frame(args) -> int:
    from zc.clips import frame as grab

    out({"frame": str(grab(args.source, args.at, args.out).resolve())})
    return 0


def make(args) -> int:
    from zc.clips import make as build, parse_box

    picks_path = Path(args.picks)
    picks = json.loads(picks_path.read_text(encoding="utf-8"))
    folder = Path(args.out) if args.out else picks_path.parent
    records = build(picks, folder, args.layout, parse_box(args.facecam), args.height, args.whisper or None, args.keep, log, args.aspect)
    out({"clips": records, "clips_json": str((folder / "clips.json").resolve())})
    return 0


def live(args) -> int:
    from zc.clips import parse_box, watch

    def emit(record):
        print(json.dumps(record, ensure_ascii=False, default=str), flush=True)

    made = asyncio.run(watch(
        args.platform, args.channel, args.minutes, args.out, args.before, args.after, args.layout, parse_box(args.facecam),
        args.height, args.whisper or None, args.factor, args.min_messages, emit, log, args.aspect,
    ))
    log(f"done, {made} clip(s)")
    return 0


def viewer(args) -> int:
    from zc.clips import parse_box, popular

    out(asyncio.run(popular(args.platform, args.channel, args.make, args.out, args.layout, parse_box(args.facecam),
                            args.height, args.whisper or None, log, args.aspect)))
    return 0


def transcript(args) -> int:
    from zc.whisper import whisper_words

    words = whisper_words(args.file, args.model, args.language)
    out({"text": " ".join(word.text for word in words), "words": [word.model_dump() for word in words]})
    return 0


def render_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--aspect", choices=("9:16", "4:5", "1:1", "16:9"), default="9:16", help="frame shape; facecam split needs a tall one")
    parser.add_argument("--layout", choices=("split", "crop"), default="split", help="split: facecam on top; crop: centre crop")
    parser.add_argument("--facecam", help="webcam box as x,y,width,height fractions, e.g. 0.74,0.02,0.24,0.27")
    parser.add_argument("--height", type=int, default=1080, help="source quality to download")
    parser.add_argument("--whisper", default="base", help="Whisper model for clips without captions; empty to skip")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="zclipper", description="Find, cut and render vertical clips from streams.")
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor", help="check the tools zclipper needs").set_defaults(run=doctor)

    p = commands.add_parser("find", help="popular recent videos on a topic, or where a streamer is live")
    p.add_argument("topic", nargs="?", help='what to search YouTube for, e.g. "GTA 6"')
    p.add_argument("--streamer", help="check YouTube, Twitch and Kick for this name, and list their recent streams")
    p.add_argument("--count", type=int, default=5)
    p.add_argument("--period", choices=("today", "week", "month", "year"), default="week")
    p.add_argument("--min-minutes", type=float, default=10, help="skip videos shorter than this")
    p.set_defaults(run=find)

    p = commands.add_parser("moments", help="candidate moments of a finished video")
    p.add_argument("url")
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--seconds", type=float, default=40)
    p.add_argument("--language", help="caption language code to prefer")
    p.add_argument("--whisper", help="transcribe candidates locally when the video has no captions (model size, e.g. base)")
    p.set_defaults(run=moments)

    p = commands.add_parser("frame", help="save one frame as JPEG, to find the facecam box")
    p.add_argument("source", help="video URL or local file")
    p.add_argument("--at", type=float, required=True, help="seconds into the video")
    p.add_argument("--out", default="zclipper-out/frame.jpg")
    p.set_defaults(run=frame)

    p = commands.add_parser("make", help="render the picks in a picks.json")
    p.add_argument("picks")
    p.add_argument("--out", help="output folder (default: next to picks.json)")
    p.add_argument("--keep", action="store_true", help="keep the downloaded source segments")
    render_options(p)
    p.set_defaults(run=make)

    p = commands.add_parser("live", help="watch a live stream's chat and clip every burst")
    p.add_argument("platform", choices=("twitch", "kick", "youtube"))
    p.add_argument("channel", help="Twitch login, Kick slug or YouTube @handle")
    p.add_argument("--minutes", type=float, default=60)
    p.add_argument("--before", type=float, default=25, help="seconds kept before the chat burst")
    p.add_argument("--after", type=float, default=10, help="seconds kept after it")
    p.add_argument("--factor", type=float, default=2.5, help="burst = this many times the usual chat rate")
    p.add_argument("--min-messages", type=int, default=15, help="and at least this many messages in 10 s")
    p.add_argument("--out", default="zclipper-out")
    render_options(p)
    p.set_defaults(run=live)

    p = commands.add_parser("viewer-clips", help="clips viewers made of the current broadcast (Twitch, Kick)")
    p.add_argument("platform", choices=("twitch", "kick"))
    p.add_argument("channel")
    p.add_argument("--make", type=int, default=0, help="render the N most viewed")
    p.add_argument("--out", default="zclipper-out")
    render_options(p)
    p.set_defaults(run=viewer)

    p = commands.add_parser("transcript", help="word-timed transcript of a local file (Whisper)")
    p.add_argument("file")
    p.add_argument("--model", default="base")
    p.add_argument("--language")
    p.set_defaults(run=transcript)
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.run(args)
    except KeyboardInterrupt:
        log("stopped")
        return 130
    except (ValueError, ImportError, OSError, subprocess.SubprocessError) as exc:
        log(str(exc))
        return 2
    except Exception as exc:  # ZClipperError and platform failures: one clear line, no traceback
        log(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
