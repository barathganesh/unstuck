"""Content and checkpoint identities. Standard library only.

Both the project environment and the isolated checkpoint environment import
this module, so the prompt identity an answer is filed under and the one the
harness looks it up by are computed by one function.
"""

import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path


def prompt_digest(
    question: str, state_text: str, options: Sequence[Sequence[str]]
) -> str:
    """Identity of what the model reads, independent of the call identity."""
    encoded = json.dumps(
        {
            "question": question,
            "state_text": state_text,
            "options": [[str(o[0]), str(o[1])] for o in options],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checkpoint_digest(directory: Path) -> tuple[str, dict[str, str]]:
    """Identity of every file a checkpoint directory resolves to.

    Symlinks are followed, so a Hub snapshot and a copied directory with the
    same bytes have the same identity. Bytecode caches are not part of it.
    """
    root = Path(directory)
    files: dict[str, str] = {}
    for current, dirs, names in os.walk(root, followlinks=True):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(names):
            path = Path(current) / name
            files[str(path.relative_to(root))] = file_sha256(path.resolve())
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest(), files
