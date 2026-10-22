"""The private SageMaker real-time serving package for the pinned Decider.

This package is the model host and nothing else. It holds the strict internal
serving contract, the single-process checkpoint engine, the two required HTTP
paths, and the container entrypoint.

It is deliberately not part of the `unstuck` distribution and imports nothing
from `unstuck.cloud`, `unstuck.storage` or `evaluation`. The boundary is the
point: a role that serves a checkpoint has no business being able to name run
state, receipts, artifacts or evaluator truth, so the code that runs on the
model host cannot reach any of them.

Nothing here deploys, builds, pushes, or runs an image, and no claim about the
resulting endpoint is established: a container in a repository is not an
endpoint in an account.
"""
