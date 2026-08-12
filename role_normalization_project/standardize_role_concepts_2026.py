"""岗位概念标准化审核与安全导出。

本脚本只生成审核/导出文件，不修改受控岗位库，也不写入 Neo4j。
候选审核表中的 decision 由人工填写；只有 APPROVE_NEW_ROLE 才会生成正式 Role 草案。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

CN_TZ = timezone(timedelta(hours=8))
KINDS = ("EXISTING_CANONICAL", "ALIAS", "SUBROLE_OF", "APPROVE_NEW_ROLE", "NOISE", "INSUFFICIENT_INFO")

def rid(name: str) -> str:
    return "role:" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]

def load_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def load_registry(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    roles = data.get("roles", data.get("role_registry", {}).get("roles", []))
    result = []
    for r in roles:
        name = r.get("canonical_name") or r.get("role_name") or r.get("name")
        if name:
            result.append({"role_id": r.get("role_id") or rid(name), "canonical_name": name,
                           "parent_role_id": r.get("parent_role_id", ""),
                           "aliases": r.get("aliases", []), "family": r.get("family") or r.get("family_id", "")})
    return result

def infer(name: str, registry):
    n = name.strip()
    if not n or len(n) < 2 or n in {"其他", "无", "未知", "N/A", "不限"}:
        return "NOISE", "", "明显为空值或信息不足"
    folded = n.lower().replace(" ", "")
    for r in registry:
        if n == r["canonical_name"]:
            return "EXISTING_CANONICAL", r["role_id"], "与受控岗位标准名完全一致"
        if folded in {a.lower().replace(" ", "") for a in r["aliases"]}:
            return "ALIAS", r["role_id"], "命中受控岗位别名"
    # 技术栈、行业、场景和等级通常是方向标签，不应自动平级建岗。
    direction = ("AI", "算法", "机器学习", "深度学习", "数据", "Java", "Python", "前端", "后端",
                 "嵌入式", "汽车", "芯片", "金融", "医疗", "平台", "行业", "高级", "资深", "实习")
    for r in registry:
        base = r["canonical_name"]
        if any(x.lower() in folded for x in direction) and (base in n or n in base):
            return "SUBROLE_OF", r["role_id"], "名称含技术/行业/等级方向，建议挂靠上级岗位并使用标签"
    return "INSUFFICIENT_INFO", "", "名称本身不足以证明独立岗位概念，需结合 JD 证据人工判断"

def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=root / "2026数据51job" / "新岗位发现结果" / "new_role_review.csv")
    parser.add_argument("--registry", type=Path, default=Path(__file__).resolve().parents[1] / "trusted_graph_agent" / "it_role_taxonomy.json")
    parser.add_argument("--output-dir", type=Path, default=root / "2026数据51job" / "新岗位发现结果")
    args = parser.parse_args()
    registry = load_registry(args.registry)
    rows = load_csv(args.input)
    review_path = args.output_dir / "role_concept_review_2026.csv"
    prior = {r.get("candidate_id"): r for r in load_csv(review_path)} if review_path.exists() else {}
    now = datetime.now(CN_TZ).isoformat(timespec="seconds")
    out = []
    for row in rows:
        name = (row.get("人工岗位名称") or row.get("算法岗位名称") or "").strip()
        kind, parent, note = infer(name, registry)
        confidence = "0.90" if kind == "EXISTING_CANONICAL" else ("0.85" if kind == "ALIAS" else "0.50")
        item = {"candidate_id": row.get("候选ID", ""), "original_name": name, "concept_type": kind,
                    "role_id": rid(name) if kind == "APPROVE_NEW_ROLE" else "", "canonical_name": name if kind == "APPROVE_NEW_ROLE" else "",
                    "parent_role_id": parent, "confidence": confidence, "definition_version": "1",
                    "evidence_jds": row.get("JD数", ""), "evidence_companies": row.get("企业数", ""),
                    "evidence_templates": row.get("模板数", ""), "decision": "PENDING", "review_note": note,
                    "source_version": row.get("定义版本", ""), "updated_at": now}
        # 允许反复刷新证据而不覆盖人工填写的 decision / canonical / parent / note。
        old = prior.get(item["candidate_id"], {})
        for key in ("decision", "concept_type", "role_id", "canonical_name", "parent_role_id", "review_note", "definition_version"):
            if old.get(key): item[key] = old[key]
        out.append(item)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    fields = list(out[0]) if out else ["candidate_id"]
    with review_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(out)
    approved = [x for x in out if x["decision"] == "APPROVE_NEW_ROLE" and x["canonical_name"] and x["parent_role_id"] != "BLOCKED"]
    (args.output_dir / "role_concept_export_draft.json").write_text(json.dumps({"format_version": "1.0.0", "formal_role_count": len(approved), "roles": approved}, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"input_rows": len(rows), "review_file": str(args.output_dir / "role_concept_review_2026.csv"), "formal_role_count": len(approved), "pending_count": len(out)}
    (args.output_dir / "role_concept_manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
