import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import JsonValue

import unstuck
from unstuck.clock import SystemClock
from unstuck.controller import Controller
from unstuck.decision import Decision, DecisionState, OptionProbability
from unstuck.domain import TaskSpec
from unstuck.fixtures import load_fixture
from unstuck.query import EvidenceEngine
from unstuck.storage import LocalRunRepository

if TYPE_CHECKING:
    from unstuck.cloud.preflight import SpendGateInputs
    from unstuck.cloud.synth import SynthesisInputs


class OfflineDecider:
    async def select(self, state: DecisionState) -> Decision:
        identifiers = tuple(probe.id for probe in state.candidates) + (
            "request_review",
        )
        selected = "inspect_schema_keys"
        return Decision(
            candidate_ids=identifiers,
            selected_id=selected,
            probabilities=tuple(
                OptionProbability(
                    id=identifier, probability=float(identifier == selected)
                )
                for identifier in identifiers
            ),
            confidence=0.95,
            validation_status="valid",
        )


async def skeleton(artifact_root: Path) -> dict[str, JsonValue]:
    fixture = load_fixture()
    clock = SystemClock()
    repository = LocalRunRepository(artifact_root, clock=clock)
    task = TaskSpec(
        dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
    )
    snapshot = repository.create_run(task, fixture.dataset_version, str(uuid4()))
    engine = EvidenceEngine(fixture, clock=clock)
    try:
        controller = Controller(repository, engine, OfflineDecider(), clock=clock)
        result = await controller.run(snapshot.state.run_id)
        repository.export_events(snapshot.state.run_id)
        return result
    finally:
        engine.close()


def _ensure_synthesis_path() -> None:
    """Make the repository-root CDK stacks importable.

    `infra/` is synthesis-only and is deliberately not an installed
    distribution package, so an installed console script cannot see it. In a
    checkout the stacks sit beside `src/`, so that one resolved root is added
    explicitly. The current working directory is never added to the path.
    """
    root = Path(str(unstuck.__file__)).resolve().parents[2]
    if (root / "infra" / "stack.py").is_file() and str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _synthesis_inputs(path: Path) -> "SynthesisInputs":
    from unstuck.cloud.synth import SynthesisInputs

    return SynthesisInputs.model_validate_json(path.read_text(encoding="utf-8"))


def _spend_inputs() -> "SpendGateInputs":
    """Read the owner spend ceiling. An absent ceiling refuses by itself."""
    from unstuck.cloud.preflight import SpendGateInputs

    return SpendGateInputs(spend_limit_usd=os.environ.get("AWS_SPEND_LIMIT_USD", ""))


def _refuse(reason: str, detail: str) -> int:
    print(json.dumps({"status": "refused", "reason_code": reason, "detail": detail}))
    return 2


def deploy(config_path: Path, out: Path, *, synth_only: bool) -> int:
    """Offline synthesis only. A live deploy needs the owner, not a flag."""
    from unstuck.cloud import CloudFault
    from unstuck.cloud.preflight import run_preflight

    if not synth_only:
        return _refuse(
            "owner_approval_required",
            "A live deploy needs explicit owner approval and PF6 through PF9. "
            "This unit synthesizes offline only; use --synth-only.",
        )
    _ensure_synthesis_path()
    try:
        inputs = _synthesis_inputs(config_path)
    except ModuleNotFoundError as error:
        return _refuse("synthesis_invalid", str(error))
    except (OSError, ValueError) as error:
        return _refuse("cloud_config_invalid", str(error))
    report = run_preflight(inputs.config, _spend_inputs())
    if not report.offline_passed:
        return _refuse(
            "preflight_required",
            "; ".join(check.id for check in report.unmet() if check.scope == "offline"),
        )
    from unstuck.cloud.synth import policy_diff, synthesize, template_for

    try:
        assembly = synthesize(inputs, out)
        diff = policy_diff(template_for(inputs, outdir=out / "assertions"))
    except CloudFault as fault:
        return _refuse(fault.reason.value, "synthesis refused")
    print(
        json.dumps(
            {
                "status": "synthesized",
                "mode": "offline",
                "assembly": str(assembly),
                "design_15_policy_diff": diff,
                "live_checks": [
                    {"id": check.id, "status": check.status}
                    for check in report.checks
                    if check.scope == "live"
                ],
                "deployment": "Pending owner approval and PF6 through PF9",
            },
            sort_keys=True,
        )
    )
    return 0


def teardown(
    config_path: Path, out: Path, *, retain_artifacts: bool, dry_run: bool
) -> int:
    """Plan only. Executing a teardown is an owner action, not a flag."""
    from unstuck.cloud import CloudFault
    from unstuck.cloud.preflight import run_preflight

    if not dry_run:
        return _refuse(
            "owner_approval_required",
            "Teardown execution needs explicit owner approval. This unit plans "
            "offline only; use --dry-run.",
        )
    _ensure_synthesis_path()
    try:
        inputs = _synthesis_inputs(config_path)
    except ModuleNotFoundError as error:
        return _refuse("synthesis_invalid", str(error))
    except (OSError, ValueError) as error:
        return _refuse("cloud_config_invalid", str(error))
    report = run_preflight(inputs.config, _spend_inputs())
    if not report.offline_passed:
        return _refuse(
            "preflight_required",
            "; ".join(check.id for check in report.unmet() if check.scope == "offline"),
        )
    from unstuck.cloud.synth import template_for
    from unstuck.cloud.teardown import plan_teardown, protected_resources

    template = template_for(inputs, outdir=out / "teardown")
    try:
        plan = plan_teardown(template, retain_artifacts=retain_artifacts)
    except CloudFault as fault:
        return _refuse(
            fault.reason.value,
            "; ".join(
                f"{kind} {logical} {reason}"
                for kind, logical, reason in protected_resources(template)
            ),
        )
    print(
        json.dumps(
            {
                "status": "planned",
                "mode": "offline",
                "steps": [
                    {
                        "order": step.order,
                        "resource_kind": step.resource_kind,
                        "logical_id": step.logical_id,
                    }
                    for step in plan.steps
                ],
                "retained": list(plan.retained),
                "execution": "Pending owner approval",
            },
            sort_keys=True,
        )
    )
    return 0


def serve(host: str, port: int, artifact_root: Path, *, run: bool = True) -> int:
    """Serve the operator UI on loopback. A non-loopback host is refused."""
    from unstuck.operator import OperatorFault
    from unstuck.operator.app import create_app
    from unstuck.operator.security import loopback_bind

    try:
        bind = loopback_bind(host, port)
    except OperatorFault as fault:
        return _refuse(fault.reason.value, fault.detail)
    repository = LocalRunRepository(artifact_root, clock=SystemClock())
    app = create_app(repository=repository, bind=bind, clock=SystemClock())
    print(
        json.dumps(
            {
                "status": "serving",
                "url": f"http://{bind.authority}/",
                "bind": bind.authority,
                "mode": "offline",
                "provider": "mock",
                "exposure": "loopback only; no host port is published",
                "live_checks": "Pending",
            },
            sort_keys=True,
        )
    )
    if not run:
        return 0
    import uvicorn

    uvicorn.run(app, host=bind.host, port=bind.port, log_level="info")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synthetic offline core-runtime skeleton; live evidence Pending"
    )
    parser.add_argument(
        "command", choices=("skeleton", "doctor", "deploy", "teardown", "serve")
    )
    parser.add_argument("--mode", choices=("offline",), default="offline")
    parser.add_argument(
        "--artifact-root", type=Path, default=Path(".venv/unstuck-runs")
    )
    parser.add_argument("--synth-only", action="store_true")
    parser.add_argument("--retain-artifacts", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--cloud-config", type=Path, default=Path("deployment/cloud-config.json")
    )
    parser.add_argument("--out", type=Path, default=Path(".venv/cdk.out"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    if args.command == "serve":
        return serve(args.host, args.port, args.artifact_root)
    if args.command == "deploy":
        return deploy(args.cloud_config, args.out, synth_only=args.synth_only)
    if args.command == "teardown":
        return teardown(
            args.cloud_config,
            args.out,
            retain_artifacts=args.retain_artifacts,
            dry_run=args.dry_run,
        )
    if args.command == "doctor":
        load_fixture()
        print(
            json.dumps(
                {
                    "status": "passed",
                    "mode": "offline",
                    "provider": "mock",
                    "live_checks": "Pending",
                }
            )
        )
        return 0
    result = asyncio.run(skeleton(args.artifact_root))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"completed", "empty_verified"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
