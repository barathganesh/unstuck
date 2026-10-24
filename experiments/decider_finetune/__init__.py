"""Decider 2B domain fine-tune experiment: corpus, evaluation harness, configs.

Offline and synthetic only. Decider 2B stays the only model tool selector: this
package adds no classifier, gate, router or second selector, changes no runtime
behaviour, and is imported by nothing under `src/unstuck`.

This file imports nothing on purpose. `infer.py` runs in the isolated
checkpoint environment, which cannot import `unstuck`, so the package itself
must import cleanly there.
"""
