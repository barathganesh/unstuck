"""Obviously synthetic deployment placeholders for the telemetry suites.

Not one value here names a real account, Region, runtime, endpoint, model, log
group, or principal. They exist so a test can exercise the typed deployment
inputs without the suite itself becoming the place a real identifier is written
down, and every one of them is visibly a placeholder.

Account and Region are the owner's Isengard deployment inputs. They are read
from validated deployment configuration at runtime and never from a source
constant, so the only account and Region literals in this repository are the
zeroed placeholders below and the ones the cloud-platform suite already owns.
"""

#: An all-zero account is not assignable, so it cannot name anyone's account.
SYNTHETIC_ACCOUNT = "0" * 12
SYNTHETIC_REGION = "us-east-1"
SYNTHETIC_PARTITION = "aws"
SYNTHETIC_AGENT = "unstuck-synthetic-agent"
SYNTHETIC_RUNTIME = "unstuck-synthetic-runtime"
SYNTHETIC_ENDPOINT = "synthetic-endpoint"
SYNTHETIC_RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{SYNTHETIC_REGION}:{SYNTHETIC_ACCOUNT}"
    f":runtime/{SYNTHETIC_RUNTIME}"
)
SYNTHETIC_MODEL = "synthetic-model-v1"
SYNTHETIC_MODEL_REVISION = "scripted-v1"
SYNTHETIC_LOG_GROUP = (
    f"/aws/bedrock-agentcore/runtimes/{SYNTHETIC_AGENT}-{SYNTHETIC_ENDPOINT}"
)
SYNTHETIC_LOG_STREAM = "synthetic-runtime-logs"
SYNTHETIC_SERVICE = "unstuck-synthetic-controller"
SYNTHETIC_STAGE = "synthetic"

#: Unique sentinels for the public negative tests. Each is a value a public
#: artifact must never carry, chosen so that finding it in a serialized bundle
#: can only mean the field it sits in survived projection. They are distinct
#: from the placeholders above precisely so a leak names its own field: a
#: shared value would pass a scan on whichever copy happened to be removed.
STAGE_SENTINEL = "stage-sentinel-must-not-publish"
MODEL_SENTINEL = "model-sentinel-must-not-publish"
MODEL_REVISION_SENTINEL = "revision-sentinel-must-not-publish"
SOURCE_LABEL_SENTINEL = "label-sentinel-must-not-publish.json"
