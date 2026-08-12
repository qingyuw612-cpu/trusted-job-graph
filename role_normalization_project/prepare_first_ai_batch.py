"""从全量证据包中抽取原有176个高证据候选，避免把全部名称交给AI。"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


PROJECT = Path(__file__).resolve().parent
WORKSPACE = PROJECT.parents[1]
DEFAULT_RESULTS = WORKSPACE / "2026数据51job" / "岗位概念标准化结果"
DEFAULT_DISCOVERY = WORKSPACE / "2026数据51job" / "新岗位发现结果" / "new_role_review.csv"


def main() -> int:
    parser = argparse.ArgumentParser(description="准备第一批高证据AI任务")
    parser.add_argument("--payload", type=Path, default=DEFAULT_RESULTS / "ai_review_payload.jsonl")
    parser.add_argument("--priority-review", type=Path, default=DEFAULT_DISCOVERY)
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULTS / "ai_batch_1_high_evidence.jsonl")
    args = parser.parse_args()
    with args.priority_review.open("r", encoding="utf-8-sig", newline="") as stream:
        priority = {str(row.get("人工岗位名称") or row.get("算法岗位名称") or "").strip()
                    for row in csv.DictReader(stream)}
    selected = []
    with args.payload.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            item = json.loads(line)
            if str(item.get("source_name") or "").strip() in priority:
                selected.append(item)
    selected.sort(key=lambda x: (-int(x.get("jd_count") or 0), str(x.get("source_name") or "")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as stream:
        for item in selected:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(json.dumps({"priority_names": len(priority), "ai_tasks": len(selected), "output": str(args.output)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
