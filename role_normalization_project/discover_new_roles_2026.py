"""2026 已归一化 CSV 的新岗位发现与岗位定义生成入口。"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from csv_new_role_discovery import DiscoveryConfig, discover_new_roles


PROJECT_DIR = Path(__file__).resolve().parent
REPOSITORY_DIR = PROJECT_DIR.parent
WORKSPACE_DIR = REPOSITORY_DIR.parent
DEFAULT_INPUT = WORKSPACE_DIR / "2026数据51job" / "jobs_2026_it_含能力提取结果_岗位归一化.csv"
DEFAULT_REGISTRY = REPOSITORY_DIR / "trusted_graph_agent" / "it_role_taxonomy.json"
DEFAULT_OUTPUT = WORKSPACE_DIR / "2026数据51job" / "新岗位发现结果"


def build_parser() -> argparse.ArgumentParser:
    """定义可调证据门槛，默认不需要任何参数。"""

    parser = argparse.ArgumentParser(description="从已归一化 CSV 发现新岗位并生成岗位定义")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--min-jds", type=int, default=3)
    parser.add_argument("--min-companies", type=int, default=3)
    parser.add_argument("--min-templates", type=int, default=3)
    parser.add_argument("--min-skills", type=int, default=3)
    parser.add_argument("--min-shared-skills", type=int, default=2)
    parser.add_argument("--required-skill-coverage", type=float, default=0.35)
    parser.add_argument("--display-limit", type=int, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行发现任务并在控制台显示结果文件和候选数量。"""

    args = build_parser().parse_args(argv)
    try:
        summary = discover_new_roles(
            args.input,
            args.registry,
            args.output_dir,
            DiscoveryConfig(
                min_jds=args.min_jds,
                min_companies=args.min_companies,
                min_templates=args.min_templates,
                min_skills=args.min_skills,
                min_shared_skills=args.min_shared_skills,
                required_skill_coverage=args.required_skill_coverage,
            ),
        )
    except Exception as exc:
        print(f"NEW_ROLE_DISCOVERY_FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    definitions_path = Path(summary["output_dir"]) / "new_role_definitions.csv"
    print(f"\n新岗位定义：{definitions_path}")
    print(f"人工审核表：{Path(summary['output_dir']) / 'new_role_review.csv'}")
    if args.display_limit > 0 and definitions_path.is_file():
        with definitions_path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = sorted(
                csv.DictReader(stream),
                key=lambda row: (-int(row["企业数"]), -int(row["JD数"]), row["岗位名称"]),
            )[: args.display_limit]
        if rows:
            print(f"\n当前新岗位候选（按企业数显示前 {len(rows)} 个）：")
            for row in rows:
                print(
                    f"- {row['岗位名称']}｜JD {row['JD数']}｜企业 {row['企业数']}｜"
                    f"版本 {row['定义版本']}｜{row['审核状态']}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
