from zc.render import snap_box

W, H = 960, 540
CAM = (50, 200, 300, 400)  # left, top, right (exclusive), bottom (exclusive) in pixels


def frame():
    """A smooth 'game' background with a webcam overlay whose content has its own sharp lines."""
    left, top, right, bottom = CAM
    data = bytearray(W * H)
    for y in range(H):
        for x in range(W):
            if left <= x < right and top <= y < bottom:
                value = 40 + (x * 3) % 30
                if x == 180 and y < 260:  # a shelf edge inside the camera, short
                    value = 230
            else:
                value = 120 + (x + y) // 20  # gentle gradient, no sharp edges
            data[y * W + x] = value
    return bytes(data)


def pixels(box):
    x, y, w, h = box
    return round(x * W), round(y * H), round((x + w) * W), round((y + h) * H)


def test_loose_box_snaps_to_the_overlay_edges():
    loose = [60 / W, 190 / H, (320 - 60) / W, (390 - 190) / H]  # too far right, top and bottom off
    snapped = pixels(snap_box((W, H, frame()), loose, inset=0))
    assert all(abs(got - want) <= 1 for got, want in zip(snapped, CAM)), snapped


def test_inset_keeps_the_box_inside_the_overlay():
    loose = [60 / W, 190 / H, (320 - 60) / W, (390 - 190) / H]
    left, top, right, bottom = pixels(snap_box((W, H, frame()), loose))
    assert left > CAM[0] and top > CAM[1] and right < CAM[2] and bottom < CAM[3]


def test_box_is_left_alone_when_no_edge_is_clear():
    flat = bytes([128]) * (W * H)
    box = [0.1, 0.2, 0.3, 0.3]
    assert snap_box((W, H, flat), box, inset=0) == box


def test_thumbnail_text_is_uppercase_with_caps_words_in_yellow():
    from zc.render import YELLOW, thumbnail_ass

    line = thumbnail_ass("he tried to FLY 😭", 700).splitlines()[-1]
    assert line.endswith(f"HE TRIED TO {{\\c{YELLOW}}}FLY{{\\c&H00FFFFFF&}}")
    assert "\\pos(540,700)" in line and "Style: Thumb" in thumbnail_ass("x", 1)


def test_render_thumbnail_makes_a_vertical_jpeg(tmp_path):
    import shutil
    import subprocess

    import pytest

    from zc.render import read_gray, render_thumbnail

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    source = tmp_path / "segment.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=10:duration=3",
                    "-pix_fmt", "yuv420p", str(source)], check=True)
    thumb = render_thumbnail(source, tmp_path / "out" / "thumb.jpg", 1.5, "he tried to FLY", "split", [0, 0, 0.3, 0.3])
    width, height, _ = read_gray(thumb, width=540)
    assert (width, height) == (540, 960)
    plain = render_thumbnail(source, tmp_path / "plain.jpg", 9, "", "crop")  # past the end: falls back to the first frame
    assert plain.exists()


def test_aspects_size_the_frame_and_wide_frames_never_split():
    import pytest

    from zc.render import build_ass, ffmpeg_command, frame_size, splits

    box = [0.0, 0.0, 0.3, 0.3]
    assert frame_size("9:16") == (1080, 1920) and frame_size("16:9") == (1920, 1080)
    with pytest.raises(ValueError):
        frame_size("21:9")
    tall = ffmpeg_command("in.mp4", "s.ass", "o.mp4", "split", box, (1080, 1350))
    assert "scale=1080:492" in tall[7] and "crop=1080:858:" in tall[7]  # facecam band scales with the height
    assert not splits("split", box, (1080, 1080)) and not splits("split", box, (1920, 1080))
    wide = ffmpeg_command("in.mp4", "s.ass", "o.mp4", "split", box, (1920, 1080))[7]
    assert "vstack" not in wide and "crop=1920:1080,ass=" in wide
    ass = build_ass([], 0, 5, title="T", size=(1920, 1080))
    assert "PlayResX: 1920" in ass and "PlayResY: 1080" in ass and "Style: Words,Arial Black,50," in ass


def test_render_vertical_honours_the_aspect(tmp_path):
    import shutil
    import subprocess

    import pytest

    from zc.moments import Word
    from zc.render import render_vertical

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    source = tmp_path / "segment.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=10:duration=2",
                    "-pix_fmt", "yuv420p", str(source)], check=True)
    for size in ((1080, 1080), (1920, 1080)):
        out = render_vertical(source, tmp_path / f"o{size[0]}.mp4", [Word(start=0.2, end=0.8, text="hi")], 0, 2, "T", "split", [0, 0, 0.3, 0.3], size)
        probed = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
                                capture_output=True, text=True).stdout.strip()
        assert probed == f"{size[0]},{size[1]}"


def test_split_gameplay_crop_slides_away_from_a_side_webcam():
    from zc.render import ffmpeg_command

    def game(box):
        graph = ffmpeg_command("in.mp4", "s.ass", "o.mp4", "split", box)[7]
        return graph.split("[b]", 1)[1].split("[game]")[0]

    left = game([0.0, 0.6, 0.41, 0.4])  # webcam bottom left: the window starts after its right edge
    assert "crop=1080:1220:'min(max((iw-ow)/2,iw*0.4100),iw-ow)':0" in left
    right = game([0.74, 0.02, 0.24, 0.27])  # top right: the window ends before its left edge
    assert "crop=1080:1220:'max(min((iw-ow)/2,iw*0.7400-ow),0)':0" in right
    centre = game([0.35, 0.0, 0.3, 0.3])  # across the middle: nothing to slide away from
    assert centre.endswith("crop=1080:1220")
