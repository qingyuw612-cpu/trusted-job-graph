"""Run one bounded new-role discovery job against the live Neo4j graph."""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime
from pathlib import Path

from .neo4j_source import inspect_neo4j_source
from .workbench import EvolutionWorkbenchService


FEATURE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = FEATURE_DIR.parent


def _date(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%d")
    return value


def _default_window(config_path: Path) -> tuple[str, str]:
    source = inspect_neo4j_source(config_path)
    as_of = datetime.strptime(
        str(source["max_posted_at"])[:10],
        "%Y-%m-%d",
    ).date()
    month_index = as_of.year * 12 + as_of.month - 1 - 3
    cutoff = date(month_index // 12, month_index % 12 + 1, 1)
    return cutoff.isoformat(), as_of.isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="直接使用 Neo4j 全局图谱运行一次新岗位涌现 Demo"
    )
    parser.add_argument("--cutoff", type=_date)
    parser.add_argument("--as-of", type=_date)
    parser.add_argument(
        "--llm-mode",
        choices=("off", "auto", "required"),
        default="auto",
    )
    parser.add_argument("--role-limit", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument(
        "--neo4j-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "neo4j_connection.json",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=PROJECT_ROOT / "output" / "role_evolution_workbench",
    )
    args = parser.parse_args()

    config_path = args.neo4j_config.expanduser().resolve()
    cutoff, as_of = _default_window(config_path)
    if args.cutoff:
        cutoff = args.cutoff
    if args.as_of:
        as_of = args.as_of
    if cutoff >= as_of:
        parser.error("cutoff 必须早于 as-of")

    service = EvolutionWorkbenchService(config_path, args.data_root)
    try:
        task = service.create_run(
            {
                "cutoff": cutoff,
                "as_of": as_of,
                "baseline_days": 0,
                "current_days": (
                    datetime.strptime(as_of, "%Y-%m-%d")
                    - datetime.strptime(cutoff, "%Y-%m-%d")
                ).days,
                "role_review_limit": max(0, min(args.role_limit, 5)),
                "llm_mode": args.llm_mode,
            }
        )
        deadline = time.monotonic() + max(30, args.timeout_seconds)
        last_progress = -1
        while time.monotonic() < deadline:
            task = service.get_run(task["task_id"])
            progress = int(task.get("progress") or 0)
            if progress != last_progress:
                print(
                    f"[{progress:3d}%] {task.get('message') or task.get('stage')}",
                    flush=True,
                )
                last_progress = progress
            if task["status"] in {"REVIEW_READY", "FAILED", "INTERRUPTED"}:
                break
            time.sleep(1)
        if task["status"] != "REVIEW_READY":
            print(
                json.dumps(task, ensure_ascii=False, indent=2),
                flush=True,
            )
            return 1
        result = service.get_result(task["task_id"])
        candidates = result.get("new_role_candidates") or []
        summary = result.get("manifest", {}).get("summary") or task.get(
            "summary",
            {},
        )
        output = {
            "task_id": task["task_id"],
            "run_id": task.get("run_id"),
            "window": {"cutoff": cutoff, "as_of": as_of},
            "summary": summary,
            "top_candidates": [
                {
                    "candidate_id": candidate.get("candidate_id"),
                    "title": candidate.get("candidate_title"),
                    "rule_state": candidate.get("rule_state"),
                    "emergence_score": candidate.get("emergence_score"),
                    "growth_windows": candidate.get("growth_windows"),
                    "independent_sources": candidate.get(
                        "independent_source_count"
                    ),
                    "confirmation_state": candidate.get(
                        "confirmation_state"
                    ),
                }
                for candidate in candidates[:5]
            ],
            "warnings": task.get("warnings") or [],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2), flush=True)
        return 0
    finally:
        service.close(wait=True)


if __name__ == "__main__":
    raise SystemExit(main())
