"""The shipped video must carry an unqualified ten-scene gate report."""

import json
import re
import subprocess
from pathlib import Path

from publishing.public_scan import scan_file

ROOT = Path(__file__).resolve().parents[2]
VIDEO_SECOND_LIMIT = 190
GATES = (
    "preflight",
    "ssml-lint",
    "prerender-data",
    "timing",
    "stt-roundtrip",
    "stream-uniformity",
    "blackdetect",
    "ocr-leak",
    "joined-timing",
    "final-artifact",
)


def test_ten_scene_video_has_all_ten_passes() -> None:
    report = json.loads((ROOT / "dist/video/validation.json").read_text())
    assert tuple(row["gate"] for row in report) == GATES
    assert all(row["status"] == "PASS" for row in report)
    assert "[allowed]" not in json.dumps(report)
    evidence = " ".join(report[-1]["evidence"])
    assert (
        "10 narrated segments; 8 current pitch frames; 2 early ML frames; 10 labelled replay steps"
        in evidence
    )
    assert "02-ml-process" in evidence and "03-ml-validation" in evidence
    assert evidence.index("02-ml-process") < evidence.index("03-ml-validation")


def test_shipped_video_geometry_and_duration() -> None:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(ROOT / "dist/video/unstuck-demo.mp4"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = json.loads(result.stdout)
    video, audio = metadata["streams"]
    assert (video["codec_name"], video["width"], video["height"], video["pix_fmt"]) == (
        "h264",
        1280,
        720,
        "yuv420p",
    )
    assert video["r_frame_rate"] == video["avg_frame_rate"] == "30/1"
    assert audio["codec_name"] == "aac"
    assert audio["sample_rate"] == "48000" and audio["channels"] == 1
    assert 0 < float(metadata["format"]["duration"]) < VIDEO_SECOND_LIMIT
    readme = (ROOT / "dist/video/README.md").read_text()
    ceiling = f"strictly below {VIDEO_SECOND_LIMIT} seconds"
    assert ceiling in readme
    assert ceiling in (ROOT / "docs/video-script.md").read_text()
    assert f"{float(metadata['format']['duration']):.3f} seconds" in readme
    assert f"{int(metadata['format']['size']):,} bytes" in readme
    assert "Ten narrated scenes" in readme
    rows = re.findall(r"\| (\d+) [^|]+ \| ([\d.]+) s \| ([\d.]+) s \|", readme)
    assert len(rows) == 10
    elapsed = 0.0
    for index, (number, start, duration) in enumerate(rows, 1):
        assert int(number) == index
        assert abs(float(start) - elapsed) < 0.002
        elapsed += float(duration)
    assert abs(elapsed - float(metadata["format"]["duration"])) < 0.002
    assert 0 < sum(float(row[2]) for row in rows[1:3]) < 33
    assert "ML addendum" not in readme


def test_video_public_text_is_clean() -> None:
    for document in (
        "docs/video-script.md",
        "dist/video/README.md",
        "dist/video/validation.json",
    ):
        assert scan_file(document) == [], document
