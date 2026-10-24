"""Validate the encoded submission cut, captions and current validation binding."""

import json
import re
import subprocess
from pathlib import Path

from publishing.public_scan import scan_file

ROOT = Path(__file__).resolve().parents[2]
VIDEO_SECOND_LIMIT = 180


def probe():
    return json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(ROOT / "dist/video/unstuck-demo.mp4"),
            ]
        )
    )


def test_current_checks_pass_and_describe_this_file():
    report = json.loads((ROOT / "dist/video/validation.json").read_text())
    metadata = probe()
    assert report["schema_version"] == 2
    assert report["status"] == "PASS"
    assert {c["name"] for c in report["checks"]} == {
        "geometry_and_duration",
        "full_decode",
        "black_frames",
        "audio_gaps",
        "narration_roundtrip",
        "public_text_scan",
        "encoded_text_scan",
        "visual_review",
    }
    assert all(c["status"] == "PASS" for c in report["checks"])
    assert report["video"]["bytes"] == int(metadata["format"]["size"])
    assert (
        abs(report["video"]["duration_seconds"] - float(metadata["format"]["duration"]))
        < 0.001
    )
    assert report["narration"]["token_similarity"] >= 0.90
    assert "private ten-gate report" in report["scope"]


def test_shipped_video_geometry_duration_and_author():
    metadata = probe()
    video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
    audio = next(s for s in metadata["streams"] if s["codec_type"] == "audio")
    assert (video["codec_name"], video["width"], video["height"], video["pix_fmt"]) == (
        "h264",
        1280,
        720,
        "yuv420p",
    )
    assert video["r_frame_rate"] == video["avg_frame_rate"] == "30/1"
    assert int(video["nb_frames"]) == 3864
    assert audio["codec_name"] == "aac"
    assert audio["sample_rate"] == "48000" and audio["channels"] == 1
    assert 0 < float(metadata["format"]["duration"]) < VIDEO_SECOND_LIMIT
    assert metadata["format"]["tags"]["artist"] == "Barath Ganesh"
    readme = (ROOT / "dist/video/README.md").read_text()
    assert f"{float(metadata['format']['duration']):.3f} seconds" in readme
    assert f"{int(metadata['format']['size']):,} bytes" in readme
    assert "strictly below 180 seconds" in readme
    rows = re.findall(r"\| (\d+) [^|]+ \| ([\d.]+) s \| ([\d.]+) s \|", readme)
    assert len(rows) == 9
    elapsed = 0.0
    for index, (number, start, duration) in enumerate(rows, 1):
        assert int(number) == index and abs(float(start) - elapsed) < 0.003
        elapsed += float(duration)
    assert abs(elapsed - float(metadata["format"]["duration"])) < 0.003


def test_video_fully_decodes_without_an_error():
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(ROOT / "dist/video/unstuck-demo.mp4"),
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert not result.stderr.strip()


def test_captions_are_ordered_and_fit_the_video():
    srt = (ROOT / "dist/video/unstuck-demo.srt").read_text()
    previous_end = 0

    def seconds(value):
        h, m, s, ms = map(int, re.split("[:,]", value))
        return 3600 * h + 60 * m + s + ms / 1000

    cues = srt.strip().split("\n\n")
    assert len(cues) >= 30
    for i, cue in enumerate(cues, 1):
        lines = cue.splitlines()
        assert int(lines[0]) == i
        start, end = map(seconds, lines[1].split(" --> "))
        assert previous_end <= start < end <= 128.801
        assert 1 <= len(lines[2:]) <= 2
        previous_end = end
    assert "three remaining diagnostics" not in srt.lower()


def test_video_public_text_is_clean():
    for document in (
        "docs/video-script.md",
        "dist/video/README.md",
        "dist/video/validation.json",
        "dist/video/unstuck-demo.srt",
    ):
        assert scan_file(document, root=ROOT) == [], document
