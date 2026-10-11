"""Optional trace logging to Braintrust and LangSmith so the same trajectories can be inspected and scored
in those platforms. Skipped silently when the key is not set; never blocks the matrix."""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("sandbox.tracing")


class Sinks:
    def __init__(self, project: str = "commitlayer-agent-benchmark"):
        self.project = project
        self.bt = None
        self.ls = None
        if os.environ.get("BRAINTRUST_API_KEY"):
            try:
                import braintrust

                self.bt = braintrust.init(project=project, api_key=os.environ["BRAINTRUST_API_KEY"])
            except Exception as e:  # pragma: no cover - network
                log.warning("braintrust init failed: %s", e)
        if os.environ.get("LANGSMITH_API_KEY"):
            try:
                from langsmith import Client

                self.ls = Client(api_key=os.environ["LANGSMITH_API_KEY"])
            except Exception as e:  # pragma: no cover - network
                log.warning("langsmith init failed: %s", e)

    def log(
        self, row: dict[str, Any], task: str, calls: list[dict[str, Any]], final: dict[str, Any] | None
    ) -> None:
        scores = {
            "commitlayer_policy_match": 1.0 if row["policy_match"] else 0.0,
            "agent_claimed_success": 1.0 if row["agent_claimed_success"] else 0.0,
        }
        if row.get("trace_judge"):
            scores["trace_judge_match"] = (
                1.0 if row["trace_judge"]["verdict"] == row["truth"]["policy"] else 0.0
            )
        if self.bt is not None:
            try:
                self.bt.log(
                    input={"scenario": row["id"], "task": task},
                    output={"calls": calls, "agent_report": final, "commitlayer": row["commitlayer"]},
                    expected=row["truth"],
                    scores=scores,
                    metadata={"failure": row["failure"], "provider": row.get("provider")},
                )
            except Exception as e:  # pragma: no cover - network
                log.warning("braintrust log failed: %s", e)
        if self.ls is not None:
            try:
                import uuid

                run_id = uuid.uuid4()
                self.ls.create_run(
                    id=run_id,
                    name=row["id"],
                    run_type="chain",
                    project_name=self.project,
                    inputs={"task": task, "scenario": row["id"]},
                    outputs={
                        "calls": calls,
                        "agent_report": final,
                        "commitlayer": row["commitlayer"],
                        "truth": row["truth"],
                    },
                    extra={"metadata": {"failure": row["failure"], "provider": row.get("provider")}},
                )
                for k, v in scores.items():
                    self.ls.create_feedback(run_id, key=k, score=v)
            except Exception as e:  # pragma: no cover - network
                log.warning("langsmith log failed: %s", e)

    def finish(self) -> dict[str, str]:
        out = {}
        if self.bt is not None:
            try:
                summary = self.bt.summarize()
                out["braintrust"] = getattr(summary, "experiment_url", "") or str(summary)
            except Exception as e:  # pragma: no cover
                out["braintrust"] = f"summarize failed: {e}"
        return out
