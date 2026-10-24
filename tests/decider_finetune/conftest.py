"""One tiny frozen corpus, built through the real stack once per session.

Focused command:
python -m pytest tests/decider_finetune -q -p no:cacheprovider
"""

from pathlib import Path

import pytest

from experiments.decider_finetune.corpus import build
from experiments.decider_finetune.oracle import Config
from tests.decider_finetune.helpers import REVISION, TINY


@pytest.fixture(scope="session")
def corpus_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("finetune") / "corpus"
    build(root, Config(model_revision=REVISION), plan=TINY, workers=8)
    return root
