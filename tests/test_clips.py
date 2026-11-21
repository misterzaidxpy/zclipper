import json
import shutil
import subprocess
from pathlib import Path

import pytest

import zclipper
from zc import clips
from zc.errors import ZClipperError
from zc.moments import Stream, StreamData, Word

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def fake_video(path, seconds=12):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=1280x720:rate=10:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-pix_fmt", "yuv420p", "-shortest", str(path)], check=True)
    return path


def stream_data(words=(), heatmap=(), duration=600.0, live_status="was_live"):
    stream = Stream(id="abc", title="Big stream", channel="Streamer", url="https://www.youtube.com/watch?v=abc",
                    duration=duration, live_status=live_status)
    return StreamData(stream=stream, words=list(words), heatmap=list(heatmap))


def test_parse_box_accepts_fractions_and_rejects_bad_boxes():
    assert clips.parse_box("0.74, 0.02,0.24,0.27") == [0.74, 0.02, 0.24, 0.27]
    assert clips.parse_box("") is None and clips.parse_box(None) is None
    for bad in ("0.9,0,0.5,0.5", "1,2,3", "a,b,c,d", "0,0,0.01,0.5"):
        with pytest.raises(ValueError):
            clips.parse_box(bad)


def test_check_pick_enforces_length_order_and_video_end():
    moment = clips.check_pick({"start": 10, "end": 40, "title": "T", "hashtags": ["a"], "unknown": 1}, 600)
    assert (moment.start, moment.end, moment.title) == (10, 40, "T")
    for pick in ({"start": 40, "end": 10}, {"start": 10, "end": 15}, {"start": 0, "end": 400}, {"start": 590, "end": 620}, {"start": -5, "end": 20}):
        with pytest.raises(ValueError):
            clips.check_pick(pick, 600)
    with pytest.raises(ValueError):
        clips.check_pick({"start": "soon", "end": 20}, 600)


def test_candidates_refuse_live_and_unrankable_videos(monkeypatch):
    monkeypatch.setattr(clips, "load_stream", lambda url, language=None: stream_data(live_status="is_live", duration=None))
    with pytest.raises(ZClipperError, match="still live"):
        clips.candidates("u")
    monkeypatch.setattr(clips, "load_stream", lambda url, language=None: stream_data())
    with pytest.raises(ZClipperError, match="No replay graph and no captions"):
        clips.candidates("u")


def test_candidates_from_the_replay_graph_are_json_ready(monkeypatch):
    heat = [{"start_time": i * 10, "end_time": i * 10 + 10, "value": 0.9 if i == 30 else 0.1} for i in range(60)]
    monkeypatch.setattr(clips, "load_stream", lambda url, language=None: stream_data(heatmap=heat))
    found = clips.candidates("u", count=2, seconds=30)
    assert found["has_replay_graph"] and not found["has_captions"]
    assert found["candidates"][0]["source"] == "most_replayed"
    json.dumps(found)


def test_clip_record_formats_hashtags_and_falls_back_to_title():
    from zc.moments import Moment

    record = clips.clip_record(Moment(start=1, end=11, title="Title", hashtags=["#GTA6", " shorts", "#"]), "v.mp4", "v.jpg", "u", [Word(start=1, end=2, text="hi")])
    assert record["hashtags"] == "#gta6 #shorts" and record["caption"] == "Title" and record["transcript"] == "hi" and record["seconds"] == 10


@needs_ffmpeg
def test_make_renders_each_pick_with_captions_and_thumbnail(monkeypatch, tmp_path):
    words = [Word(start=100 + i * 0.5, end=100 + i * 0.5 + 0.4, text=f"word{i}") for i in range(20)]
    monkeypatch.setattr(clips, "load_stream", lambda url: stream_data(words=words))

    def download(url, start, end, out_path, height=1080):
        return fake_video(out_path.with_suffix(".mp4"), seconds=end - start)

    monkeypatch.setattr(clips, "download_range", download)
    picks = {"url": "https://www.youtube.com/watch?v=abc", "picks": [
        {"start": 100, "end": 110, "title": "He did it", "caption": "He did IT 😭", "hashtags": ["gta6"], "thumbnail_text": "he did IT", "thumbnail_at": 4},
    ]}
    records = clips.make(picks, tmp_path, layout="split", facecam=[0.0, 0.0, 0.3, 0.3], whisper=None, log=lambda m: None)
    assert len(records) == 1
    record = records[0]
    assert record["caption"] == "He did IT 😭" and record["hashtags"] == "#gta6" and record["transcript"].startswith("word0")
    video = tmp_path / "abc_100.mp4"
    assert video.exists() and (tmp_path / "abc_100.jpg").exists() and not (tmp_path / "abc_100_src.mp4").exists()
    size = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video)],
                          capture_output=True, text=True).stdout.strip()
    assert size == "1080,1920"
    saved = json.loads((tmp_path / "clips.json").read_text(encoding="utf-8"))
    assert [Path(entry["file"]).resolve() for entry in saved] == [video.resolve()]


def test_make_rejects_documents_without_url_or_picks(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="url"):
        clips.make({"picks": [{"start": 1, "end": 20}]}, tmp_path)
    with pytest.raises(ValueError, match="no \"picks\""):
        clips.make({"url": "u", "picks": []}, tmp_path)


def test_cli_doctor_reports_json(capsys):
    code = zclipper.main(["doctor"])
    report = json.loads(capsys.readouterr().out)
    assert {"python", "ffmpeg", "yt_dlp", "ready", "fix"} <= set(report)
    assert code == (0 if report["ready"] else 1)


def test_cli_errors_are_one_line_with_nonzero_exit(tmp_path, capsys):
    picks = tmp_path / "picks.json"
    picks.write_text(json.dumps({"picks": [{"start": 1, "end": 20}]}), encoding="utf-8")
    assert zclipper.main(["make", str(picks)]) == 2
    assert "url" in capsys.readouterr().err
    assert zclipper.main(["make", str(picks), "--facecam", "2,2,2,2"]) == 2


def test_frame_from_a_url_downloads_a_window_long_enough_for_hls_replays(monkeypatch, tmp_path):
    asked = {}

    def download(url, start, end, out_path, height=1080):
        asked.update(start=start, end=end)
        out_path.write_bytes(b"x")
        return out_path

    monkeypatch.setattr(clips, "download_range", download)
    monkeypatch.setattr(clips, "grab_frame", lambda source, out, at: asked.update(at=at) or out)
    clips.frame("https://www.twitch.tv/videos/1", 3600, tmp_path / "f.jpg")
    assert asked["end"] - asked["start"] >= 8  # Twitch replays fail to convert ranges of a couple of seconds
    assert asked["start"] + asked["at"] == 3600


def test_make_takes_picks_from_several_videos_and_survives_one_failure(monkeypatch, tmp_path):
    loaded = []

    def load(url):
        loaded.append(url)
        data = stream_data()
        data.stream.id, data.stream.url = url[-1], url
        return data

    def download(url, start, end, out_path, height=1080):
        if url.endswith("b"):
            raise ZClipperError("video unavailable")
        return out_path

    monkeypatch.setattr(clips, "load_stream", load)
    monkeypatch.setattr(clips, "download_range", download)
    monkeypatch.setattr(clips, "_render", lambda segment, out, *rest: (out, out.with_suffix(".jpg")))
    picks = {"url": "https://x/a", "picks": [{"start": 10, "end": 30}, {"start": 50, "end": 70}, {"url": "https://x/b", "start": 5, "end": 25}]}
    records = clips.make(picks, tmp_path, whisper=None, log=lambda m: None)
    assert loaded == ["https://x/a", "https://x/b"]  # each video loaded once
    assert [Path(r["file"]).name for r in records if "file" in r] == ["a_10.mp4", "a_50.mp4"]
    assert records[2]["error"] == "video unavailable" and records[2]["source"] == "https://x/b"
