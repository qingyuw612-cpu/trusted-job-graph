"""手动运行新增数据的增量知识图谱流水线。

该入口只编排已有模块，不复制清洗、能力分析、关键词归一化或 Neo4j
写入逻辑。默认完成原始数据导入、能力处理和归一化检查；加 --publish
才会把归一化结果发布为 Neo4j 当前活动版本。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable


def run_step(label: str, script: Path, *args: str) -> None:
    print(f"\n===== {label} =====", flush=True)
    command = [PYTHON, "-B", str(script), *args]
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="增量运行：原始数据导入 → 能力分析回标 → 关键词归一化 → Neo4j发布"
    )
    parser.add_argument("--source", type=Path, help="新增 CSV/JSON/JSONL 文件或所在目录；默认使用项目配置目录")
    parser.add_argument("--neo4j-config", type=Path, help="Neo4j 连接配置文件")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条当前版本；0 表示全部")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--llm-endpoint", default="", help="可选：已有能力分析缺失时使用的兼容 Webhook")
    parser.add_argument("--skip-import", action="store_true", help="数据已经在原始层时跳过导入")
    parser.add_argument("--force-import", action="store_true", help="同一文件曾按旧字段规则导入时强制重新适配")
    parser.add_argument("--skip-normalization", action="store_true", help="只导入并处理能力，不做向量归一化")
    parser.add_argument("--publish", action="store_true", help="发布归一化结果并切换 Neo4j 活动版本")
    parser.add_argument("--work-dir", type=Path, default=PROJECT_ROOT / "output" / "processed_normalization_incremental")
    args = parser.parse_args()

    neo4j_config = args.neo4j_config.resolve() if args.neo4j_config else PROJECT_ROOT / "config" / "neo4j_connection.json"
    common = ("--neo4j-config", str(neo4j_config))

    if not args.skip_import:
        importer_args = list(common)
        importer_args += ["--batch-size", str(max(1, min(args.batch_size, 1000)))]
        if args.source:
            importer_args += ["--source", str(args.source.resolve())]
        if args.force_import:
            importer_args.append("--force")
        run_step("1/6 原始数据增量导入", PROJECT_ROOT / "raw_jd_layer" / "importer.py", *importer_args)

    ingest_run_id = ""
    ingestion_report = PROJECT_ROOT / "output" / "raw_jd_ingestion" / "last_run.json"
    if not args.skip_import and ingestion_report.exists():
        ingest_run_id = str(
            json.loads(ingestion_report.read_text(encoding="utf-8")).get("run_id") or ""
        )

    process_args = [*common, "--batch-size", str(args.batch_size)]
    if args.limit:
        process_args += ["--limit", str(args.limit)]
    if args.llm_endpoint:
        process_args += ["--llm-endpoint", args.llm_endpoint]
    if ingest_run_id:
        process_args += ["--ingest-run-id", ingest_run_id, "--force"]
    run_step("2/6 复用能力分析清洗与原文回标", PROJECT_ROOT / "processing_layer" / "processor.py", *process_args)

    domain_args = [*common, "--batch-size", str(max(1, min(args.batch_size, 500)))]
    if args.limit:
        domain_args += ["--limit", str(args.limit)]
    run_step("3/6 信息技术岗位准入", PROJECT_ROOT / "processing_layer" / "domain_filter.py", *domain_args)

    run_step(
        "4/6 映射到受控岗位分类",
        PROJECT_ROOT / "processing_layer" / "backfill_it_roles.py",
        *common,
        "--batch-size",
        str(max(1, min(args.batch_size, 2000))),
    )

    if args.skip_normalization:
        print("\n已完成原始层和能力证据层；按参数跳过归一化与发布。", flush=True)
        return

    normalize_args = [
        "--work-dir", str(args.work_dir.resolve()),
        "--batch-size", str(max(1, min(args.batch_size, 500))),
        "--neo4j-config", str(neo4j_config),
    ]
    # 新数据需要重新导出当前完整快照；旧目录仅作为可覆盖的中间产物。
    if (args.work_dir / "knowledge_graph.db").exists():
        normalize_args.append("--overwrite")
    if args.limit:
        normalize_args += ["--limit", str(args.limit)]
    run_step("5/6 复用关键词归一化与知识图谱候选生成", PROJECT_ROOT / "processing_layer" / "normalize_with_demo.py", *normalize_args)

    publish_args = [
        "--database", str((args.work_dir / "knowledge_graph.db").resolve()),
        "--normalization-dir", str((args.work_dir / "skill_reports").resolve()),
        "--neo4j-config", str(neo4j_config),
    ]
    if args.publish:
        publish_args.append("--publish")
        label = "6/6 发布到 Neo4j 并切换活动版本"
    else:
        label = "6/6 只读校验（未发布；加 --publish 才写入活动版本）"
    run_step(label, PROJECT_ROOT / "processing_layer" / "publish_normalization.py", *publish_args)


if __name__ == "__main__":
    main()
