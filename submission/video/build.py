"""Rebuild the submission cut from the original ten-scene MP4, entirely locally.

Requires Python 3.12, ffmpeg, ffprobe, Node.js and the sharp Node package.
Keep a copy of the original 178.533-second video before replacing it.
"""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Original 5356-frame demo")
    parser.add_argument("output", type=Path, help="New MP4 destination")
    parser.add_argument("--sharp-module", default="sharp")
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if source == output:
        parser.error("Preserve the original; source and output must differ.")
    plan = json.loads((HERE / "edit-plan.json").read_text())
    metadata = json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(source)]
        )
    )
    video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
    if (video["width"], video["height"], int(video["nb_frames"])) != (
        1280,
        720,
        plan["source_frames"],
    ) or video["r_frame_rate"] != "30/1":
        parser.error("Expected the original 1280x720, 30 fps, 5356-frame video.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="unstuck-video-") as directory:
        work = Path(directory)
        render = (
            "const fs=require('fs'); const sharp=require(process.argv[1]); "
            "sharp(fs.readFileSync(process.argv[2])).png().toFile(process.argv[3])"
            ".catch(e=>{console.error(e);process.exit(1)});"
        )
        cards = {s["card"] for s in plan["scenes"] if s["card"]}
        cards.update(v["card"] for s in plan["scenes"] for v in s.get("visuals", []))
        for card in sorted(cards):
            run(
                "node",
                "-e",
                render,
                args.sharp_module,
                str(HERE / card),
                str(work / (card + ".png")),
            )
        parts = []
        for scene in plan["scenes"]:
            print(f"Rendering {scene['key']}", flush=True)
            position = 0
            chunks = []
            visuals = scene.get("visuals", [{"frame": 0, "card": scene["card"]}])
            for start, end in scene["source_ranges"]:
                boundaries = sorted({position, position + end - start} | {
                    v["frame"] for v in visuals if position < v["frame"] < position + end - start
                })
                for left, right in zip(boundaries, boundaries[1:], strict=False):
                    card = next(v["card"] for v in reversed(visuals) if v["frame"] <= left)
                    chunks.append((start + left - position, start + right - position, card))
                position += end - start
            previous_card = None
            for start, end, card in chunks:
                count = end - start
                command = ["ffmpeg", "-v", "error", "-y", "-i", str(source)]
                if card:
                    command += ["-loop", "1", "-framerate", "30", "-i", str(work / (card + ".png"))]
                    if previous_card and previous_card != card and "workflow" in card:
                        command += ["-loop", "1", "-framerate", "30", "-i", str(work / (previous_card + ".png"))]
                        visual = f"[2:v][1:v]xfade=transition=fade:duration=0.25:offset=0,trim=end_frame={count},setpts=PTS-STARTPTS[v]"
                    else:
                        visual = f"[1:v]trim=end_frame={count},setpts=PTS-STARTPTS[v]"
                else:
                    visual = f"[0:v]trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS[v]"
                previous_card = card
                # 48000 / 30 = 1600 audio samples per video frame.
                audio = (
                    f"[0:a]atrim=start_sample={start * 1600}:"
                    f"end_sample={end * 1600},asetpts=PTS-STARTPTS[a]"
                )
                part = work / f"part-{len(parts):02d}.mkv"
                command += [
                    "-filter_complex",
                    visual + ";" + audio,
                    "-map",
                    "[v]",
                    "-map",
                    "[a]",
                    "-c:v",
                    "ffv1",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "pcm_s16le",
                    "-ar",
                    "48000",
                    "-ac",
                    "1",
                    "-map_metadata",
                    "-1",
                    str(part),
                ]
                run(*command)
                parts.append(part)
        concat = work / "parts.txt"
        # Generated filenames contain no user-controlled quote or newline.
        concat.write_text("".join(f"file '{part.name}'\n" for part in parts))
        run(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            str(concat),
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-preset",
            "fast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-fps_mode",
            "cfr",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-map_metadata",
            "-1",
            "-metadata",
            "title=Unstuck",
            "-metadata",
            "artist=Barath Ganesh",
            "-metadata",
            "comment=Recorded replay of synthetic evidence",
            "-movflags",
            "+faststart",
            str(output),
        )
    print(f"Created {output.name}: {plan['total_frames']} frames expected.")


if __name__ == "__main__":
    main()
