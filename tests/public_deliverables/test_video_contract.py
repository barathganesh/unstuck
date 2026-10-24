"""Public ten-scene contract: preserve the receipt story and bound ML claims."""

import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ORIGINAL_SCENES = (
    (
        "1. The hook\n"
        "\n"
        "**0:00 to 0:29.** Planning budget: 28 s speech, 1 s held.\n"
        "\n"
        "*Screen:* `submission/pitch/frames/slide-01.png`.\n"
        "\n"
        "> Unstuck gets Decider 2B out of a decision loop. Strands Decider 2B makes "
        "tool decisions. But a new call can repeat a decision already proven useless. "
        "Unstuck turns that verified dead end into a Decision Receipt. The same "
        "Decider then chooses a new path, and no larger model takes over. Run 1 pays "
        "the learning cost once. Run 2 uses the receipt before selection. The "
        "memoryless control stays stuck."
    ),
    (
        "2. The control stays stuck\n"
        "\n"
        "**0:29 to 0:45.** Planning budget: 15 s speech, 1 s held.\n"
        "\n"
        "*Screen:* `submission/pitch/frames/slide-02.png`, followed by demo steps 1, "
        "9.\n"
        "\n"
        "> This is a recorded replay of the measured synthetic run, not a new run. A "
        "report returned zero rows. Without receipts, Decider picked inspect pipeline "
        "on three calls. Each spent its one read and ended unresolved."
    ),
    (
        "3. Run 1 pays the learning cost\n"
        "\n"
        "**0:45 to 1:03.** Planning budget: 17 s speech, 1 s held.\n"
        "\n"
        "*Screen:* `submission/pitch/frames/slide-03.png`, followed by demo steps 2, "
        "3, 4.\n"
        "\n"
        "> Run 1 happens once: one inference, 21.6 seconds of client time, one read, "
        "unresolved. Run 1 is not saved. The deterministic verifier stored a Decision "
        "Receipt: real data, no decision moved. The model is unchanged. The system "
        "recorded a fact."
    ),
    (
        "4. Run 2 escapes\n"
        "\n"
        "**1:03 to 1:27.** Planning budget: 23 s speech, 1 s held.\n"
        "\n"
        "*Screen:* `submission/pitch/frames/slide-04.png`, followed by demo steps 5, "
        "6, 7, 8.\n"
        "\n"
        "> Run 2 is a new call, and the exact receipt matches. Before the first "
        "Decider request and before any read, the controller precludes only the "
        "proven dead end. The three remaining diagnostics keep their original order. "
        "Decider makes one fresh choice, inspect schema keys. One inference, one "
        "read, a verified repair, completed in 26.6 seconds."
    ),
    (
        "5. The proof\n"
        "\n"
        "**1:27 to 1:50.** Planning budget: 22 s speech, 1 s held.\n"
        "\n"
        "*Screen:* `submission/pitch/frames/slide-05.png`, followed by demo steps "
        "10.\n"
        "\n"
        "> Old Run 2 took 54.8 seconds and two inferences. Optimized Run 2 takes 26.6 "
        "seconds and one inference. That is one fewer inference and an observed "
        "difference of 28.2 seconds, or 51.4 percent lower client latency. One "
        "synthetic case, each proof once. These are single observations. Old Run 2 is "
        "not the memoryless control."
    ),
    (
        "6. The private AWS path\n"
        "\n"
        "**1:50 to 2:07.** Planning budget: 16 s speech, 1 s held.\n"
        "\n"
        "*Screen:* `submission/pitch/frames/slide-06.png`.\n"
        "\n"
        "> Decider serves from a private, always-on Amazon SageMaker endpoint. It is "
        "billed by uptime, not requests. One fewer inference is not a dollar saving. "
        "Amazon Bedrock Nova Lite narrates only after completion. Narration was "
        "refused on Run 1."
    ),
    (
        "7. The reusable pattern\n"
        "\n"
        "**2:07 to 2:21.** Planning budget: 13 s speech, 1 s held.\n"
        "\n"
        "*Screen:* `submission/pitch/frames/slide-07.png`.\n"
        "\n"
        "> The reusable recipe sends current state and matching history to Decider. "
        "Only a proven exact repeat is withheld before selection. A receipt cannot "
        "pick a tool. Decider chooses from the remaining tools."
    ),
    (
        "8. The boundary, restated\n"
        "\n"
        "**2:21 to 2:35.** Planning budget: 13 s speech, 1 s held.\n"
        "\n"
        "*Screen:* `submission/pitch/frames/slide-08.png`.\n"
        "\n"
        "> One failed decision becomes a boundary. History can rule out one proven "
        "execution under one identical context. It cannot choose the next tool. "
        "Decider chooses. The verifier decides what the read found."
    ),
)


def scenes() -> list[str]:
    script = (ROOT / "docs/video-script.md").read_text()
    return [
        block.split("\n## ", 1)[0].strip() for block in script.split("\n## Scene ")[1:]
    ]


@pytest.mark.parametrize("index", range(8))
def test_original_receipt_scenes_are_unchanged(index: int) -> None:
    actual = scenes()[index if index == 0 else index + 2]
    original = ORIGINAL_SCENES[index]
    assert (
        actual.splitlines()[0].split(". ", 1)[1]
        == original.splitlines()[0].split(". ", 1)[1]
    )
    assert actual.split("*Screen:*", 1)[1] == original.split("*Screen:*", 1)[1]


def test_ten_scenes_put_process_before_validation() -> None:
    blocks = scenes()
    assert len(blocks) == 10
    assert blocks[1].startswith("2. Why LoRA is in the decision path")
    assert blocks[2].startswith("3. What validation showed")
    assert blocks[-1].startswith("10. The boundary, restated")
    assert [int(block.split(".", 1)[0]) for block in blocks] == list(range(1, 11))


def test_process_explains_training_and_runtime_authority() -> None:
    scene = scenes()[1]
    assert scene.split("> ", 1)[1] == (
        "Offline, we ran every diagnostic and trained LoRA plus Decider's "
        "choice head on verifier labels. At runtime, the receipt removes "
        "one repeat; LoRA ranks what remains; the verifier owns truth."
    )
    assert len(scene.split("> ", 1)[1].split()) == 30
    for phrase in (
        "A Decision Receipt exact-filters one proven repeat",
        "It cannot rank the remaining tools",
        "Execute every offered diagnostic; deterministic verifier outcomes become labels",
        "Train LoRA adapters plus the Decider choice head; freeze the checkpoint",
        "Receipt filters first",
        "LoRA-adapted Decider 2B ranks what remains",
        "The verifier still decides truth and repair",
        "No second selector",
        "No online learning",
        "not the current hosted selector",
    ):
        assert phrase in scene, phrase


def test_validation_has_complete_labeled_evidence_and_limits() -> None:
    scene = scenes()[2]
    assert scene.split("> ", 1)[1] == (
        "Across 160 decisions, correct first choices rose from 97 to 129. "
        "Across 64 calls, wasted first reads fell from 20 to 15 and completions "
        "rose from 44 to 49; CPU replay confirmed direction, one seed, "
        "experimental checkpoint."
    )
    sentences = scene.split("> ", 1)[1].split(". ")
    assert [len(sentence.split()) for sentence in sentences] == [11, 26]
    for phrase in (
        "Controlled synthetic in-distribution evidence",
        "One seed",
        "Correct first choices: 129 out of 160 decisions (80.6%); up from 97 (60.6%)",
        "32 more correct decisions",
        "Wasted first reads: 15 out of 64 full calls (23.4%)",
        "down from 20 (31.3%)",
        "5 fewer wasted calls; 25% reduction",
        "Verified completions: 49 out of 64 calls (76.6%)",
        "44 out of 64 calls (68.8%); 5 more completed calls",
        "HEAD-ONLY REJECTED",
        "107 correct out of 160 decisions",
        "wasted reads stayed at 20 out of 64 calls",
        "SAME-DEVICE CPU REPLAY CONFIRMED DIRECTION",
        "Correct choices: 132 out of 160 versus 96",
        "Wasted reads: 14 out of 64 versus 20",
        "Verified completions: 50 out of 64 calls versus 44",
        "CPU and CUDA differed on 3 out of 160 near-tie decisions",
        "Private hosted canary missed exact parity",
        "checkpoint remains experimental",
        "Exact paired p=0.0625 is not significance",
        "False repair/preclusion zeros are not safety evidence",
        "No unseen-generalization, hosted ML success, latency or cost claim",
        "Not the current hosted selector",
    ):
        assert phrase in scene, phrase


@pytest.mark.parametrize(
    "count,total,percent",
    [
        (129, 160, "80.6"),
        (97, 160, "60.6"),
        (15, 64, "23.4"),
        (20, 64, "31.3"),
        (49, 64, "76.6"),
        (44, 64, "68.8"),
    ],
)
def test_displayed_percentages_recompute(count: int, total: int, percent: str) -> None:
    actual = (Decimal(count) * 100 / total).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    assert str(actual) == percent
    assert f"({percent}%)" in scenes()[2]
    assert 129 - 97 == 32 and 20 - 15 == 5 and 49 - 44 == 5
    assert Decimal(20 - 15) * 100 / 20 == 25


def test_ml_copy_never_uses_bare_fractions_or_addendum_labels() -> None:
    script = (ROOT / "docs/video-script.md").read_text()
    assert "ml addendum" not in script.lower()
    assert not re.search(r"\b\d+\s*/\s*\d+\b", " ".join(scenes()[1:3]))
    for scene in scenes()[1:3]:
        assert "0 s held" in scene
