"""两个招聘平台数据的一键增量入图入口。

流程：字段预检 -> 两个平台分别导入 -> 分批能力处理 -> IT 准入 -> 岗位映射
-> 全量技能归一化 -> 只读验证/正式发布。底层节点均使用稳定 ID，失败后可安全重跑。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable
SUPPORTED_EXTENSIONS = {".csv", ".json", ".jsonl"}
REQUIRED_SAMPLE_FIELDS = ("title", "description_length")


@dataclass(frozen=True, slots=True)
class SourceSpec:
    name: str
    path: Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(value: str) -> str:
    safe = "".join(character.lower() if character.isalnum() else "_" for character in value)
    return "_".join(part for part in safe.split("_") if part) or "platform"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_sources(config_path: Path) -> list[SourceSpec]:
    payload = load_json(config_path)
    rows = payload.get("platforms")
    if not isinstance(rows, list) or len(rows) != 2:
        raise ValueError("配置文件 platforms 必须恰好包含两个平台。")
    sources: list[SourceSpec] = []
    names: set[str] = set()
    paths: set[Path] = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 个平台配置不是对象。")
        name = str(row.get("name") or "").strip()
        raw_path = str(row.get("path") or "").strip()
        if not name or not raw_path:
            raise ValueError(f"第 {index} 个平台必须同时填写 name 和 path。")
        path = Path(raw_path).expanduser().resolve()
        if name in names:
            raise ValueError(f"平台名重复：{name}")
        if path in paths:
            raise ValueError(f"两个平台不能指向同一路径：{path}")
        if not path.exists():
            raise FileNotFoundError(f"平台数据不存在：{path}")
        if path.is_file() and path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"不支持的数据格式：{path.suffix}；只接受 CSV/JSON/JSONL。")
        names.add(name)
        paths.add(path)
        sources.append(SourceSpec(name=name, path=path))
    return sources


class PipelineRun:
    def __init__(self, report_dir: Path, dry_run: bool) -> None:
        self.report_dir = report_dir
        self.dry_run = dry_run
        self.steps: list[dict[str, Any]] = []

    def run(self, label: str, script: Path, *arguments: str) -> None:
        command = [str(PYTHON), "-B", str(script), *arguments]
        started = time.monotonic()
        print(f"\n===== {label} =====", flush=True)
        completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        step = {
            "label": label,
            "command": command,
            "exit_code": completed.returncode,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
        self.steps.append(step)
        if completed.returncode:
            raise subprocess.CalledProcessError(completed.returncode, command)


def validate_preflight(report: dict[str, Any], source: SourceSpec) -> None:
    metrics = report.get("metrics") or {}
    errors = list(metrics.get("errors") or [])
    files_found = int(metrics.get("files_found") or 0)
    rows_valid = int(metrics.get("rows_valid") or 0)
    samples = list(report.get("samples") or [])
    if errors:
        raise ValueError(f"{source.name} 字段预检失败：{errors}")
    if files_found <= 0 or rows_valid <= 0 or not samples:
        raise ValueError(f"{source.name} 没有找到有效的 CSV/JSON/JSONL 记录。")
    for sample in samples:
        missing = []
        if not str(sample.get(REQUIRED_SAMPLE_FIELDS[0]) or "").strip():
            missing.append("岗位名称")
        if int(sample.get(REQUIRED_SAMPLE_FIELDS[1]) or 0) <= 0:
            missing.append("职位描述/JD")
        if missing:
            raise ValueError(
                f"{source.name} 的样例记录缺少 {', '.join(missing)}；"
                "请补字段别名后再正式导入。"
            )
    detected = {str(sample.get("source_platform") or "") for sample in samples}
    if detected != {source.name}:
        raise ValueError(
            f"{source.name} 平台标识不一致，检测到：{sorted(detected)}。"
            "请检查源文件 source/platform 字段或配置名称。"
        )


def require_completed(path: Path, label: str) -> dict[str, Any]:
    payload = load_json(path)
    if payload.get("status") != "COMPLETED":
        raise RuntimeError(f"{label} 未完成：{payload}")
    metrics = payload.get("metrics") or {}
    if metrics.get("errors"):
        raise RuntimeError(f"{label} 含错误：{metrics['errors']}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="两个平台一步完成：预检、增量导入、清洗、归一化、验证和可选发布"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "two_platform_import.json",
        help="两个平台名称与数据路径配置",
    )
    parser.add_argument(
        "--neo4j-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "neo4j_connection.json",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--llm-endpoint", default="")
    parser.add_argument("--force-import", action="store_true")
    parser.add_argument("--publish", action="store_true", help="验证成功后切换正式活动图谱")
    parser.add_argument("--dry-run", action="store_true", help="只做两个平台的字段和格式预检")
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=PROJECT_ROOT / "output" / "two_platform_imports",
        help="运行报告目录",
    )
    args = parser.parse_args()

    sources = load_sources(args.config.resolve())
    neo4j_config = args.neo4j_config.resolve()
    if not neo4j_config.exists():
        raise FileNotFoundError(f"Neo4j 配置不存在：{neo4j_config}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    report_dir = args.report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    runner = PipelineRun(report_dir, args.dry_run)
    manifest: dict[str, Any] = {
        "run_id": f"two-platform:{timestamp}",
        "status": "RUNNING",
        "started_at": utc_now(),
        "publish_requested": bool(args.publish),
        "dry_run": bool(args.dry_run),
        "sources": [asdict(source) | {"path": str(source.path)} for source in sources],
        "reports": {},
    }
    manifest_path = report_dir / "latest_manifest.json"
    write_json(manifest_path, manifest)

    try:
        # 预检不连接 Neo4j，先阻止字段不兼容的数据进入原始层。
        for index, source in enumerate(sources, 1):
            key = f"platform_{index}"
            preflight = report_dir / f"{key}_preflight.json"
            runner.run(
                f"预检 {index}/2：{source.name}",
                PROJECT_ROOT / "raw_jd_layer" / "importer.py",
                "--source", str(source.path),
                "--default-platform", source.name,
                "--source-id-prefix", slug(source.name),
                "--check-only",
                "--sample-rows-per-file", "25",
                "--report", str(preflight),
            )
            validate_preflight(load_json(preflight), source)
            manifest["reports"][f"{key}_preflight"] = str(preflight)

        if args.dry_run:
            manifest.update(status="PREFLIGHT_OK", finished_at=utc_now(), steps=runner.steps)
            write_json(manifest_path, manifest)
            print(f"\nPREFLIGHT_OK 报告：{manifest_path}")
            return

        # 每个平台单独形成导入批次和能力处理报告，避免第二个平台覆盖第一个。
        for index, source in enumerate(sources, 1):
            key = f"platform_{index}"
            ingest_report = report_dir / f"{key}_ingest.json"
            ingest_arguments = [
                "--source", str(source.path),
                "--default-platform", source.name,
                "--source-id-prefix", slug(source.name),
                "--neo4j-config", str(neo4j_config),
                "--batch-size", str(max(1, min(args.batch_size, 1000))),
                "--report", str(ingest_report),
            ]
            if args.force_import:
                ingest_arguments.append("--force")
            runner.run(
                f"导入 {index}/2：{source.name}",
                PROJECT_ROOT / "raw_jd_layer" / "importer.py",
                *ingest_arguments,
            )
            ingest = require_completed(ingest_report, f"{source.name} 原始导入")
            manifest["reports"][f"{key}_ingest"] = str(ingest_report)

            # 文件未变化时 importer 会跳过；无需对空批次重复强制处理。
            if int((ingest.get("metrics") or {}).get("files_imported") or 0) == 0:
                continue
            process_report = report_dir / f"{key}_process.json"
            process_arguments = [
                "--neo4j-config", str(neo4j_config),
                "--batch-size", str(max(1, args.batch_size)),
                "--ingest-run-id", str(ingest["run_id"]),
                "--force",
                "--report", str(process_report),
            ]
            if args.llm_endpoint:
                process_arguments.extend(("--llm-endpoint", args.llm_endpoint))
            runner.run(
                f"能力处理 {index}/2：{source.name}",
                PROJECT_ROOT / "processing_layer" / "processor.py",
                *process_arguments,
            )
            process = require_completed(process_report, f"{source.name} 能力处理")
            if int((process.get("metrics") or {}).get("needs_llm") or 0) > 0:
                raise RuntimeError(
                    f"{source.name} 有 {process['metrics']['needs_llm']} 条记录缺少能力分析；"
                    "请配置 --llm-endpoint 或先补齐能力提取结果。正式图谱尚未发布。"
                )
            manifest["reports"][f"{key}_process"] = str(process_report)

        domain_report = report_dir / "domain_filter.json"
        runner.run(
            "信息技术领域准入",
            PROJECT_ROOT / "processing_layer" / "domain_filter.py",
            "--neo4j-config", str(neo4j_config),
            "--batch-size", str(max(1, min(args.batch_size, 500))),
            "--report", str(domain_report),
        )
        runner.run(
            "映射受控岗位",
            PROJECT_ROOT / "processing_layer" / "backfill_it_roles.py",
            "--neo4j-config", str(neo4j_config),
            "--batch-size", str(max(1, min(args.batch_size, 2000))),
        )

        work_dir = PROJECT_ROOT / "output" / "processed_normalization_incremental"
        runner.run(
            "生成当前完整归一化快照",
            PROJECT_ROOT / "processing_layer" / "normalize_with_demo.py",
            "--work-dir", str(work_dir),
            "--neo4j-config", str(neo4j_config),
            "--batch-size", str(max(1, min(args.batch_size, 500))),
            "--overwrite",
        )
        publish_arguments = [
            "--database", str(work_dir / "knowledge_graph.db"),
            "--normalization-dir", str(work_dir / "skill_reports"),
            "--neo4j-config", str(neo4j_config),
        ]
        if args.publish:
            publish_arguments.append("--publish")
            publish_label = "验证并发布正式活动图谱"
        else:
            publish_label = "只读验证（未发布）"
        runner.run(
            publish_label,
            PROJECT_ROOT / "processing_layer" / "publish_normalization.py",
            *publish_arguments,
        )

        # 保留一份固定位置的最近报告，便于双击运行后快速查看。
        manifest.update(
            status="PUBLISHED" if args.publish else "VALIDATED_NOT_PUBLISHED",
            finished_at=utc_now(),
            steps=runner.steps,
            normalization_dir=str(work_dir),
        )
        write_json(manifest_path, manifest)
        print(f"\n{manifest['status']}\n完整报告：{manifest_path}")
    except Exception as error:
        manifest.update(
            status="FAILED",
            finished_at=utc_now(),
            error=f"{type(error).__name__}: {error}",
            steps=runner.steps,
        )
        write_json(manifest_path, manifest)
        print(
            f"\nPIPELINE_FAILED；活动图谱只会在最后发布步骤切换。"
            f"请结合报告确认状态：{manifest_path}",
            file=sys.stderr,
        )
        raise


if __name__ == "__main__":
    main()
