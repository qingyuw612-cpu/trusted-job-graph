"""从已归一化招聘 CSV 中发现新岗位并维护可审核的岗位定义。"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from cli import extract_responsibilities, template_id
from normalize_jobs_2026_csv import parse_ability_profile, read_header
from role_normalizer.io import iter_csv_rows, write_csv_atomic
from role_normalizer.registry import normalize_lookup_key
from role_normalizer.taxonomy_adapter import load_role_registry


GENERIC_TITLES = {
    "工程师", "开发工程师", "技术工程师", "技术员", "经理", "主管", "总监",
    "专员", "助理", "顾问", "实习生", "兼职", "全职", "销售", "运营",
}
REVIEW_STATUSES = {"待审核", "已通过", "已拒绝", "观察"}
QUALIFICATION_SENTENCE = re.compile(
    r"(?:任职要求|岗位要求|职位要求|招聘要求|学历|经验|优先考虑|任职资格|我们希望|你需要)"
)
SENTENCE_SPLIT = re.compile(r"[。！？!?；;\n\r]+|(?=\s+\d+[、.）)])")
LEADING_MARKER = re.compile(r"^(?:\d+[.、)）]|[（(]?\d+[）)]|[-—•●◆※]+)\s*")
REVIEW_COLUMNS = [
    "候选ID", "算法岗位名称", "算法核心职责", "算法必备技能", "算法加分技能",
    "算法典型行业应用场景", "人工岗位名称", "人工核心职责", "人工必备技能",
    "人工加分技能", "人工典型行业应用场景", "审核状态", "审核备注", "定义版本",
    "变更类型", "JD数", "企业数", "模板数", "更新时间",
]
DEFINITION_COLUMNS = [
    "候选ID", "岗位名称", "核心职责", "必备技能", "加分技能", "典型行业应用场景",
    "审核状态", "定义版本", "变更类型", "JD数", "企业数", "模板数", "技能数",
    "证据指纹", "更新时间",
]


@dataclass(frozen=True)
class DiscoveryConfig:
    """新岗位证据门槛与岗位定义生成参数。"""

    min_jds: int = 3
    min_companies: int = 3
    min_templates: int = 3
    min_skills: int = 3
    min_shared_skills: int = 2
    required_skill_coverage: float = 0.35
    min_bonus_skill_companies: int = 2
    max_required_skills: int = 8
    max_bonus_skills: int = 8
    max_responsibilities: int = 5
    max_industries: int = 5

    def validate(self) -> None:
        """校验所有门槛，避免静默产生不可信候选。"""

        for name in (
            "min_jds", "min_companies", "min_templates", "min_skills",
            "min_shared_skills",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 必须大于等于 1")
        if not 0 < self.required_skill_coverage <= 1:
            raise ValueError("required_skill_coverage 必须位于 0 到 1 之间")


@dataclass
class _EvidenceGroup:
    """同一归一化岗位名称的原始招聘证据。"""

    name: str
    rows: list[dict[str, str]]


def _candidate_id(name: str) -> str:
    """根据规范化名称生成稳定候选 ID。"""

    digest = hashlib.sha256(normalize_lookup_key(name).encode("utf-8")).hexdigest()[:16]
    return f"new-role:{digest}"


def _json_fingerprint(value: Any) -> str:
    """为候选证据和定义生成稳定版本指纹。"""

    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_items(value: str) -> list[str]:
    """把人工编辑的顿号、逗号或换行列表转换为去重条目。"""

    return list(
        dict.fromkeys(
            item.strip()
            for item in re.split(r"[、,，;；|\n\r]+", str(value or ""))
            if item.strip()
        )
    )


def _join_items(values: Iterable[str]) -> str:
    """把岗位定义列表转换为便于在 CSV 中编辑的中文文本。"""

    return "；".join(str(item).strip() for item in values if str(item).strip())


def _atomic_text(path: Path, text: str) -> None:
    """在目标目录中原子写入文本，避免中断留下半文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _read_existing_csv(path: Path, key: str) -> dict[str, dict[str, str]]:
    """读取上轮人工审核或岗位定义，并按稳定标识建立索引。"""

    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return {
            str(row.get(key) or "").strip(): dict(row)
            for row in csv.DictReader(stream)
            if str(row.get(key) or "").strip()
        }


def _known_names(registry_path: Path) -> set[str]:
    """读取现有岗位标准名和别名，作为新岗位排除集合。"""

    registry = load_role_registry(registry_path)
    values: set[str] = set()
    for role in registry:
        values.add(normalize_lookup_key(role.canonical_name))
        values.update(normalize_lookup_key(alias) for alias in role.aliases)
    return values


def _responsibility_sentences(text: str) -> list[str]:
    """从 JD 职责段提取适合岗位定义的简洁职责句。"""

    section = extract_responsibilities(text, limit=2500)
    output: list[str] = []
    for raw in SENTENCE_SPLIT.split(section):
        sentence = re.sub(r"^[\s【】\[\]]+", "", re.sub(r"\s+", " ", raw))
        sentence = LEADING_MARKER.sub("", sentence).strip(" ：:，,【】[]")
        sentence = re.sub(r"^(?:岗位描述|职位描述|工作描述)\s*[:：]\s*", "", sentence)
        if not 8 <= len(sentence) <= 220 or QUALIFICATION_SENTENCE.search(sentence):
            continue
        if sentence not in output:
            output.append(sentence)
    return output


def _build_definition(group: _EvidenceGroup, config: DiscoveryConfig) -> dict[str, Any]:
    """聚合企业、技能、职责和行业证据，生成结构化岗位定义草案。"""

    companies = {
        str(row.get("公司全称") or "").strip() for row in group.rows
        if str(row.get("公司全称") or "").strip()
    }
    templates = {template_id(str(row.get("JD全文") or "")) for row in group.rows}
    skill_jds: Counter[str] = Counter()
    skill_companies: defaultdict[str, set[str]] = defaultdict(set)
    for row in group.rows:
        skills = parse_ability_profile(str(row.get("能力提取结果") or ""), ("技术", "知识"))
        company = str(row.get("公司全称") or "").strip()
        for skill in set(skills):
            skill_jds[skill] += 1
            if company:
                skill_companies[skill].add(company)
    ranked_skills = sorted(
        skill_jds,
        key=lambda skill: (-len(skill_companies[skill]), -skill_jds[skill], skill.casefold()),
    )
    required_company_count = max(2, math.ceil(len(companies) * config.required_skill_coverage))
    required = [
        skill for skill in ranked_skills
        if len(skill_companies[skill]) >= required_company_count
    ][: config.max_required_skills]
    shared_skills = [skill for skill in ranked_skills if len(skill_companies[skill]) >= 2]
    # 宽岗位的技能分布可能较分散；若没有技能达到固定覆盖率，则以跨企业
    # 支持度最高的技能补齐最小必备技能草案，留给人工审核确认。
    for skill in shared_skills:
        if skill not in required:
            required.append(skill)
        if len(required) >= min(config.min_shared_skills, config.max_required_skills):
            break
    bonus = [
        skill for skill in ranked_skills
        if skill not in required
        and len(skill_companies[skill]) >= config.min_bonus_skill_companies
    ][: config.max_bonus_skills]
    if not bonus:
        bonus = [skill for skill in ranked_skills if skill not in required][
            : config.max_bonus_skills
        ]
    if not bonus and len(required) > config.min_shared_skills:
        # 所有能力都达到高覆盖时，保留最稳定的必备项，并把支持度最低的
        # 一项作为加分项，避免两个定义字段机械重复或无依据留空。
        bonus = [required.pop()]

    responsibilities: list[str] = []
    responsibility_evidence: list[dict[str, str]] = []
    seen_companies: set[str] = set()
    for row in group.rows:
        company = str(row.get("公司全称") or "").strip()
        if company and company in seen_companies:
            continue
        sentences = _responsibility_sentences(str(row.get("JD全文") or ""))
        if not sentences:
            continue
        sentence = sentences[0]
        if any(normalize_lookup_key(sentence) == normalize_lookup_key(item) for item in responsibilities):
            continue
        responsibilities.append(sentence)
        responsibility_evidence.append(
            {
                "职位ID": str(row.get("职位ID") or ""),
                "公司": company,
                "职责": sentence,
            }
        )
        if company:
            seen_companies.add(company)
        if len(responsibilities) >= config.max_responsibilities:
            break

    industries = Counter(
        str(row.get("公司行业") or "").strip() for row in group.rows
        if str(row.get("公司行业") or "").strip()
    )
    industry_scenarios = [name for name, _count in industries.most_common(config.max_industries)]
    ids = sorted({str(row.get("职位ID") or "").strip() for row in group.rows})
    definition = {
        "name": group.name,
        "core_responsibilities": responsibilities,
        "required_skills": required,
        "bonus_skills": bonus,
        "industry_scenarios": industry_scenarios,
    }
    return {
        "candidate_id": _candidate_id(group.name),
        "definition": definition,
        "metrics": {
            "jd_count": len(ids),
            "company_count": len(companies),
            "template_count": len(templates),
            "skill_count": len(ranked_skills),
            "shared_skill_count": len(shared_skills),
        },
        "evidence": {
            "record_ids": ids,
            "responsibilities": responsibility_evidence,
            "skill_support": [
                {
                    "skill": skill,
                    "jd_count": skill_jds[skill],
                    "company_count": len(skill_companies[skill]),
                }
                for skill in ranked_skills[:30]
            ],
            "industry_distribution": dict(industries.most_common(20)),
        },
    }


def _apply_human_review(
    generated: dict[str, Any], previous_review: Mapping[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    """保留人工字段，并在审核通过时生成最终生效定义。"""

    algorithm = generated["definition"]
    status = str(previous_review.get("审核状态") or "待审核").strip()
    if status not in REVIEW_STATUSES:
        status = "待审核"
    human = {
        "name": str(previous_review.get("人工岗位名称") or "").strip(),
        "core_responsibilities": _split_items(previous_review.get("人工核心职责", "")),
        "required_skills": _split_items(previous_review.get("人工必备技能", "")),
        "bonus_skills": _split_items(previous_review.get("人工加分技能", "")),
        "industry_scenarios": _split_items(previous_review.get("人工典型行业应用场景", "")),
    }
    effective = dict(algorithm)
    if status == "已通过":
        for field, value in human.items():
            if value:
                effective[field] = value
    review = {
        "候选ID": generated["candidate_id"],
        "算法岗位名称": algorithm["name"],
        "算法核心职责": _join_items(algorithm["core_responsibilities"]),
        "算法必备技能": _join_items(algorithm["required_skills"]),
        "算法加分技能": _join_items(algorithm["bonus_skills"]),
        "算法典型行业应用场景": _join_items(algorithm["industry_scenarios"]),
        "人工岗位名称": str(previous_review.get("人工岗位名称") or ""),
        "人工核心职责": str(previous_review.get("人工核心职责") or ""),
        "人工必备技能": str(previous_review.get("人工必备技能") or ""),
        "人工加分技能": str(previous_review.get("人工加分技能") or ""),
        "人工典型行业应用场景": str(previous_review.get("人工典型行业应用场景") or ""),
        "审核状态": status,
        "审核备注": str(previous_review.get("审核备注") or ""),
    }
    return effective, review


def discover_new_roles(
    input_csv: Path,
    registry_path: Path,
    output_dir: Path,
    config: DiscoveryConfig | None = None,
) -> dict[str, Any]:
    """扫描已归一化 CSV，输出新岗位定义、审核表、证据和动态版本。"""

    selected = config or DiscoveryConfig()
    selected.validate()
    input_csv, registry_path, output_dir = (
        Path(input_csv).resolve(), Path(registry_path).resolve(), Path(output_dir).resolve()
    )
    if not input_csv.is_file() or not registry_path.is_file():
        raise FileNotFoundError("输入 CSV 或岗位注册表不存在")
    required_columns = {
        "职位ID", "岗位名称", "JD全文", "公司全称", "公司行业", "能力提取结果"
    }
    missing = sorted(required_columns - set(read_header(input_csv)))
    if missing:
        raise KeyError(f"输入 CSV 缺少列：{'、'.join(missing)}")
    output_dir.mkdir(parents=True, exist_ok=True)
    known = _known_names(registry_path)
    grouped: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    observed_rows: list[dict[str, str]] = []
    total_rows = 0
    for row in iter_csv_rows(input_csv):
        total_rows += 1
        name = str(row.get("岗位名称") or "").strip()
        if name and normalize_lookup_key(name) not in known:
            grouped[name].append(row)

    review_path = output_dir / "new_role_review.csv"
    definitions_path = output_dir / "new_role_definitions.csv"
    previous_reviews = _read_existing_csv(review_path, "候选ID")
    previous_definitions = _read_existing_csv(definitions_path, "候选ID")
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    definitions: list[dict[str, Any]] = []
    review_rows: list[dict[str, str]] = []
    evidence_rows: list[dict[str, Any]] = []
    history_path = output_dir / "definition_history.jsonl"
    history: list[dict[str, Any]] = []
    if history_path.is_file():
        for line in history_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                history.append(json.loads(line))

    for name in sorted(grouped, key=lambda value: normalize_lookup_key(value)):
        generated = _build_definition(_EvidenceGroup(name, grouped[name]), selected)
        metrics = generated["metrics"]
        gate_reasons = []
        if name in GENERIC_TITLES:
            gate_reasons.append("岗位名称过于宽泛")
        for label, actual, minimum in (
            ("独立JD", metrics["jd_count"], selected.min_jds),
            ("独立企业", metrics["company_count"], selected.min_companies),
            ("独立模板", metrics["template_count"], selected.min_templates),
            ("能力项", metrics["skill_count"], selected.min_skills),
            ("跨企业共享能力项", metrics["shared_skill_count"], selected.min_shared_skills),
        ):
            if actual < minimum:
                gate_reasons.append(f"{label}不足：{actual} < {minimum}")
        if gate_reasons:
            observed_rows.append(
                {
                    "候选ID": generated["candidate_id"], "岗位名称": name,
                    "JD数": str(metrics["jd_count"]), "企业数": str(metrics["company_count"]),
                    "模板数": str(metrics["template_count"]), "技能数": str(metrics["skill_count"]),
                    "未通过原因": "；".join(gate_reasons),
                }
            )
            continue

        candidate_id = generated["candidate_id"]
        effective, review = _apply_human_review(generated, previous_reviews.get(candidate_id, {}))
        fingerprint = _json_fingerprint(
            {"definition": generated["definition"], "record_ids": generated["evidence"]["record_ids"]}
        )
        previous = previous_definitions.get(candidate_id, {})
        previous_fingerprint = str(previous.get("证据指纹") or "")
        previous_version = int(str(previous.get("定义版本") or "0") or 0)
        if not previous:
            change_type, version = "NEW", 1
        elif fingerprint != previous_fingerprint:
            change_type, version = "UPDATED", previous_version + 1
        else:
            change_type, version = "UNCHANGED", max(1, previous_version)
        review.update(
            {
                "定义版本": str(version), "变更类型": change_type,
                "JD数": str(metrics["jd_count"]), "企业数": str(metrics["company_count"]),
                "模板数": str(metrics["template_count"]), "更新时间": generated_at,
            }
        )
        definitions.append({**generated, "effective_definition": effective, "review_status": review["审核状态"], "version": version, "change_type": change_type, "fingerprint": fingerprint})
        review_rows.append(review)
        evidence_rows.append({"candidate_id": candidate_id, **generated["evidence"]})
        if change_type != "UNCHANGED":
            history.append({"candidate_id": candidate_id, "version": version, "created_at": generated_at, "change_type": change_type, "definition": effective, "metrics": metrics, "fingerprint": fingerprint})
    # definitions 中保留结构化数据；CSV 行从最终字段稳定重建。
    definition_rows = []
    for item, review in zip(definitions, review_rows):
        effective, metrics = item["effective_definition"], item["metrics"]
        definition_rows.append(
            {
                "候选ID": item["candidate_id"], "岗位名称": effective["name"],
                "核心职责": _join_items(effective["core_responsibilities"]),
                "必备技能": _join_items(effective["required_skills"]),
                "加分技能": _join_items(effective["bonus_skills"]),
                "典型行业应用场景": _join_items(effective["industry_scenarios"]),
                "审核状态": review["审核状态"], "定义版本": str(item["version"]),
                "变更类型": item["change_type"], "JD数": str(metrics["jd_count"]),
                "企业数": str(metrics["company_count"]), "模板数": str(metrics["template_count"]),
                "技能数": str(metrics["skill_count"]), "证据指纹": item["fingerprint"],
                "更新时间": generated_at,
            }
        )

    write_csv_atomic(definitions_path, definition_rows, DEFINITION_COLUMNS)
    write_csv_atomic(review_path, review_rows, REVIEW_COLUMNS)
    write_csv_atomic(
        output_dir / "observation_pool.csv",
        observed_rows,
        ["候选ID", "岗位名称", "JD数", "企业数", "模板数", "技能数", "未通过原因"],
    )
    _atomic_text(
        output_dir / "new_role_definitions.json",
        json.dumps(definitions, ensure_ascii=False, indent=2) + "\n",
    )
    _atomic_text(
        output_dir / "new_role_evidence.jsonl",
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in evidence_rows),
    )
    _atomic_text(
        history_path,
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in history),
    )
    approved_roles = []
    for item in definitions:
        if item["review_status"] != "已通过":
            continue
        definition = item["effective_definition"]
        approved_roles.append(
            {
                "role_id": item["candidate_id"].replace("new-role:", "role:new:"),
                "canonical_name": definition["name"],
                "aliases": [item["definition"]["name"]] if definition["name"] != item["definition"]["name"] else [],
                "description": "；".join(definition["core_responsibilities"]),
                "skills": list(dict.fromkeys(definition["required_skills"] + definition["bonus_skills"])),
                "metadata": {"required_skills": definition["required_skills"], "bonus_skills": definition["bonus_skills"], "industry_scenarios": definition["industry_scenarios"], "definition_version": item["version"]},
            }
        )
    _atomic_text(
        output_dir / "approved_registry_update_draft.json",
        json.dumps({"roles": approved_roles}, ensure_ascii=False, indent=2) + "\n",
    )
    summary = {
        "mode": "CSV_NEW_ROLE_DISCOVERY_NO_GRAPH_WRITE",
        "input": str(input_csv), "registry": str(registry_path), "output_dir": str(output_dir),
        "total_rows": total_rows, "unknown_unique_names": len(grouped),
        "new_role_candidates": len(definitions), "observation_pool": len(observed_rows),
        "approved_candidates": len(approved_roles), "generated_at": generated_at,
        "config": selected.__dict__,
    }
    _atomic_text(output_dir / "manifest.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary
