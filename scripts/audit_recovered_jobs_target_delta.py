from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REMEDIATION = Path("data/audits/continuous_adapter_remediation.json")
DATABASE = Path("data/workstation.sqlite3")
OUTPUT_JSON = Path("data/audits/recovered_jobs_target_delta.json")
OUTPUT_CSV = Path("data/audits/recovered_jobs_target_delta.csv")
OUTPUT_MD = Path("data/audits/recovered_jobs_target_delta.md")

AGENT_PATTERNS = (
    (
        2,
        "Agent/大模型开发",
        re.compile(
            r"(?:Agent|智能体|大模型|LLM).{0,12}"
            r"(?:开发|研发|工程|应用|Infra)|RAG",
            re.I,
        ),
    ),
    (
        3,
        "Agent/大模型算法（次选）",
        re.compile(
            r"(?:Agent|智能体|大模型|LLM|自然语言处理|NLP).{0,12}"
            r"(?:算法|研究|训练|推理)",
            re.I,
        ),
    ),
)
VISUAL_AFFIRMATIVE = (
    re.compile(
        r"计算机视觉|机器视觉|视觉\s*AI|AI\s*视觉|视觉算法|"
        r"深度学习算法|CNN|卷积神经网络|Vision\s*Transformer|\bViT\b|"
        r"多模态视觉模型|视觉大模型|图像生成模型|"
        r"(?:目标检测|图像分割|图像分类|图像识别|视觉识别|影像识别|目标跟踪|视觉跟踪|姿态估计)"
        r".{0,12}(?:算法|模型|网络|训练|开发|优化|研究|工程师)|"
        r"(?:训练|开发|优化|研究).{0,12}(?:目标检测|图像分割|图像分类|图像识别|视觉识别|影像识别|目标跟踪|视觉跟踪|姿态估计)",
        re.I,
    ),
    re.compile(
        r"(?:训练|微调|评估|部署|优化|开发|构建).{0,28}(?:深度学习|机器学习|视觉|图像|影像).{0,16}(?:模型|算法)|"
        r"(?:深度学习|机器学习|视觉|图像|影像).{0,16}(?:模型|算法).{0,28}(?:训练|微调|评估|部署|优化|开发|构建)|"
        r"(?:深度学习|机器学习).{0,16}(?:训练|微调|评估|部署|优化|开发|构建).{0,20}(?:模型|算法)|"
        r"(?:模型|算法模型).{0,20}(?:训练|微调|评估|部署|优化).{0,50}(?:深度学习|机器学习)",
        re.I,
    ),
)
IMAGE_PROCESSING_WEAK = re.compile(
    r"图像处理|图像增强|图像配准|图像重建|图像拼接|图像去噪|图像压缩|"
    r"图像渲染|计算机图形学|图形学|\bISP\b|OpenCV|医学影像(?:处理)?|"
    r"显微(?:镜)?图像(?:处理)?|影像处理|影像重建|影像配准|图像算法|"
    r"目标检测|图像分割|图像分类|图像识别|视觉识别|影像识别|目标跟踪|视觉跟踪|姿态估计|"
    r"预训练(?:视觉|图像|影像)?模型|图像API|视觉API|识别API",
    re.I,
)
IMAGE_INTEGRATION_ONLY = re.compile(
    r"(?:调用|接入|集成|使用).{0,24}(?:预训练(?:视觉|图像|影像)?模型|图像API|视觉API|识别API)|"
    r"预训练(?:视觉|图像|影像)?模型.{0,24}(?:调用|接入|集成|使用)",
    re.I,
)
MODEL_OWNERSHIP = re.compile(
    r"(?:负责|主导|开发|(?<!预)训练|微调|评估|部署|优化|构建|设计).{0,24}(?:模型|算法|检测|分割|分类|识别|跟踪|姿态估计)|"
    r"(?:模型|算法|检测|分割|分类|识别|跟踪|姿态估计).{0,24}(?:负责|主导|开发|(?<!预)训练|微调|评估|部署|优化|构建|设计)",
    re.I,
)
TARGET_LOCATION = re.compile(r"四川(?:省)?|成都(?:市)?|北京(?:市)?|上海(?:市)?|广州(?:市)?|全国(?:各地|多地)?")
LOCATION = re.compile(
    r"全国(?:各地|多地)?|(?:北京|上海|天津|重庆)市?|四川省?|成都(?:市)?|广州(?:市)?|"
    r"(?:河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|广东|"
    r"海南|贵州|云南|陕西|甘肃|青海|台湾)(?:省)?[·・][\u4e00-\u9fff]{2,8}市?|"
    r"(?:内蒙古|广西|西藏|宁夏|新疆)[\u4e00-\u9fff]{0,6}[·・][\u4e00-\u9fff]{2,8}市?",
)
BROAD_AI = re.compile(
    r"Agent|智能体|大模型|LLM|RAG|自然语言处理|NLP|计算机视觉|机器视觉|"
    r"视觉算法|图像(?:处理|算法|识别|分割)|医学影像|影像算法|目标检测|深度学习|"
    r"多模态|人工智能|(?<![A-Za-z])AI(?![A-Za-z])|算法",
    re.I,
)
COLUMNS = (
    "company",
    "job_title",
    "location",
    "recruitment_type",
    "priority",
    "category",
    "role_evidence",
    "broad_ai_signal",
    "exclusion_code",
    "exclusion_evidence",
    "location_evidence",
    "job_url",
    "should_include",
    "decision_reason",
    "structured_job_id",
    "source_task_id",
    "batch",
    "source_entry_url",
)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def structured_text(job: dict[str, Any]) -> str:
    return clean(
        " ".join(
            str(job.get(field) or "")
            for field in ("title", "description", "requirements", "text", "category", "department")
        )
    )


def display_job_title(value: Any) -> str:
    title = clean(value)
    title = re.split(r"\s+发布于\s+", title, maxsplit=1)[0]
    title = title.split(" | ", 1)[0]
    # Mokahr list records often append the same department or legal entity twice.
    repeated_suffix = re.match(r"^(.+?)\s+(.{2,100})\s+\2$", title)
    if repeated_suffix:
        title = repeated_suffix.group(1)
    return title.strip()


def job_key(job: dict[str, Any]) -> str:
    identifier = clean(job.get("id"))
    if identifier:
        return "id:" + identifier.casefold()
    url = clean(job.get("url"))
    if url:
        return "url:" + url.casefold()
    return "text:" + clean(job.get("title")).casefold() + "\x1f" + clean(job.get("text")).casefold()


def evidence_window(text: str, match: re.Match[str], radius: int = 48) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return text[start:end].strip()


def classify_visual(text: str) -> tuple[re.Match[str] | None, re.Match[str] | None]:
    affirmative = next((match for pattern in VISUAL_AFFIRMATIVE if (match := pattern.search(text))), None)
    weak = IMAGE_PROCESSING_WEAK.search(text)
    integration_only = IMAGE_INTEGRATION_ONLY.search(text)
    if integration_only and not MODEL_OWNERSHIP.search(text):
        return None, weak or integration_only
    return affirmative, weak


def classify(text: str) -> tuple[int | None, str, str, str, str]:
    visual_match, weak_image_match = classify_visual(text)
    if visual_match:
        return (
            1,
            "视觉相关AI/深度学习算法",
            evidence_window(text, visual_match),
            "",
            "",
        )
    for priority, category, pattern in AGENT_PATTERNS:
        match = pattern.search(text)
        if match:
            return priority, category, evidence_window(text, match), "", ""
    if weak_image_match:
        return (
            None,
            "未命中既定三档",
            "",
            "image_processing_without_ai_evidence",
            evidence_window(text, weak_image_match),
        )
    return None, "未命中既定三档", "", "", ""


def locations(text: str) -> tuple[str, str, bool]:
    found: list[str] = []
    for match in LOCATION.finditer(text):
        value = match.group(0)
        if value not in found:
            found.append(value)
    found = [value for value in found if not any(value != other and other.startswith(value) for other in found)]
    target = TARGET_LOCATION.search(text)
    return "、".join(found) if found else "未明确", target.group(0) if target else "", bool(target)


def recruitment_type(source_url: str, text: str) -> str:
    combined = f"{source_url} {text}"
    if re.search(r"campus|校招|校园|应届|20\d{2}届", combined, re.I):
        return "校招/实习"
    if re.search(r"social|社招", combined, re.I):
        return "社招"
    return "未明确"


def load_task_result(db: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = db.execute("SELECT result_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"task missing from store: {task_id}")
    try:
        result = json.loads(row[0] or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid result_json for task: {task_id}") from exc
    if not isinstance(result, dict):
        raise ValueError(f"result_json is not an object: {task_id}")
    return result


def audit(remediation_path: Path, database_path: Path) -> dict[str, Any]:
    remediation = read_json(remediation_path)
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    task_counts: list[dict[str, Any]] = []
    raw_delta_count = 0

    with sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True) as db:
        for item in remediation.get("items") or []:
            if not item.get("recovered") or int(item.get("added_jobs") or 0) <= 0:
                continue
            task_id = clean(item.get("task_id"))
            result = load_task_result(db, task_id)
            jobs = result.get("jobs") if isinstance(result.get("jobs"), list) else []
            before_jobs = int(item.get("before_jobs") or 0)
            after_jobs = int(item.get("after_jobs") or 0)
            if len(jobs) != after_jobs:
                raise ValueError(
                    f"store changed after remediation for {task_id}: expected {after_jobs}, got {len(jobs)}"
                )
            delta = jobs[before_jobs:]
            if len(delta) != int(item.get("added_jobs") or 0):
                raise ValueError(f"delta boundary mismatch for {task_id}")
            raw_delta_count += len(delta)
            unique_from_task = 0
            duplicate_from_task = 0

            for job in delta:
                if not isinstance(job, dict):
                    continue
                key = job_key(job)
                if key in seen:
                    duplicate_from_task += 1
                    continue
                seen.add(key)
                unique_from_task += 1
                text = structured_text(job)
                priority, category, role_evidence, exclusion_code, exclusion_evidence = classify(text)
                location, location_evidence, location_ok = locations(text)
                should_include = priority is not None and location_ok
                broad_match = BROAD_AI.search(text)
                if should_include:
                    decision_reason = "岗位证据命中既定类别，且地点命中四川/北上广/全国"
                elif exclusion_code == "image_processing_without_ai_evidence":
                    decision_reason = "普通图像/影像处理缺少模型训练优化、CV/深度学习架构或检测分割识别等肯定证据"
                elif priority is None and broad_match:
                    decision_reason = "仅命中泛 AI/算法词，未明确命中视觉/深度学习或 Agent/大模型三档"
                elif priority is None:
                    decision_reason = "未命中既定三档岗位证据"
                else:
                    decision_reason = "岗位类别命中，但地点未命中四川/北上广/全国"
                    exclusion_code = "target_location_not_confirmed"
                    exclusion_evidence = location or "未明确"
                rows.append(
                    {
                        "company": clean(item.get("company")),
                        "job_title": display_job_title(job.get("title")),
                        "location": location,
                        "recruitment_type": recruitment_type(clean(item.get("url")), text),
                        "priority": priority if priority is not None else "",
                        "category": category,
                        "role_evidence": role_evidence,
                        "broad_ai_signal": bool(broad_match),
                        "exclusion_code": exclusion_code,
                        "exclusion_evidence": exclusion_evidence,
                        "location_evidence": location_evidence,
                        "job_url": clean(job.get("url")),
                        "should_include": should_include,
                        "decision_reason": decision_reason,
                        "structured_job_id": clean(job.get("id")),
                        "source_task_id": task_id,
                        "batch": clean(item.get("batch")),
                        "source_entry_url": clean(item.get("url")),
                    }
                )
            task_counts.append(
                {
                    "company": clean(item.get("company")),
                    "task_id": task_id,
                    "reported_added": int(item.get("added_jobs") or 0),
                    "unique_contributed": unique_from_task,
                    "cross_task_duplicates_removed": duplicate_from_task,
                }
            )

    rows.sort(
        key=lambda row: (
            not bool(row["should_include"]),
            int(row["priority"] or 99),
            str(row["company"]).casefold(),
            str(row["job_title"]).casefold(),
        )
    )
    included = [row for row in rows if row["should_include"]]
    role_matched = [row for row in rows if row["priority"] != ""]
    location_excluded = [row for row in role_matched if not row["should_include"]]
    near_misses = [
        row
        for row in rows
        if not row["should_include"] and row["broad_ai_signal"]
    ]
    return {
        "state": "completed",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "policy": {
            "scope": "only ordered structured jobs added by recovered tasks in continuous_adapter_remediation.json",
            "delta_boundary": "jobs[before_jobs:after_jobs] for each recovered task",
            "dedupe_key": "structured job id; URL fallback; title+text final fallback",
            "target_locations": "四川、成都、北京、上海、广州、全国各地/全国多地",
            "recruitment_preference": "校招优先；社招亦可，不作为硬排除条件",
            "visual_ai_rule": "普通图像处理不属于1档；须有CV/机器视觉/视觉AI、模型训练优化、深度学习架构或检测分割识别等肯定证据",
            "screening_skill": ".codex/skills/medical-ai-job-screening/SKILL.md",
            "network_requests": 0,
            "ocr_calls": 0,
            "exports_targets_written": False,
        },
        "summary": {
            "remediation_reported_added": raw_delta_count,
            "unique_recovered_jobs": len(rows),
            "duplicates_removed": raw_delta_count - len(rows),
            "included_jobs": len(included),
            "excluded_jobs": len(rows) - len(included),
            "near_miss_jobs": len(near_misses),
            "role_matched_jobs_before_location_filter": len(role_matched),
            "role_matched_but_location_excluded": len(location_excluded),
            "broad_ai_only_jobs": len(near_misses) - len(location_excluded),
            "image_processing_without_ai_evidence": sum(
                row["exclusion_code"] == "image_processing_without_ai_evidence" for row in rows
            ),
            "companies_with_included_jobs": len({row["company"] for row in included}),
            "by_priority": {
                str(priority): sum(row["priority"] == priority and row["should_include"] for row in rows)
                for priority in (1, 2, 3)
            },
            "by_recruitment_type": dict(Counter(str(row["recruitment_type"]) for row in rows)),
        },
        "task_delta_audit": task_counts,
        "included_jobs": included,
        "near_misses": near_misses,
        "jobs": rows,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def markdown_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "无。"
    lines = [
        "| 公司 | 岗位 | 地点 | 类别 | 证据 | 应否纳入 | URL |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        title = str(row["job_title"]).replace("|", "\\|")
        evidence = str(row["role_evidence"] or row["exclusion_evidence"] or row["decision_reason"]).replace("|", "\\|")
        url = str(row["job_url"])
        lines.append(
            f"| {row['company']} | {title} | {row['location']} | {row['category']} | "
            f"{evidence} | {'是' if row['should_include'] else '否'} | [岗位链接]({url}) |"
        )
    return "\n".join(lines)


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report["summary"]
    included = report["included_jobs"]
    near_misses = report["near_misses"]
    text = f"""# 恢复岗位目标增量审核

## 结论

- 修复报告新增计数：{summary['remediation_reported_added']} 条。
- 按 structured job id 全局去重：{summary['unique_recovered_jobs']} 条，移除重复 {summary['duplicates_removed']} 条。
- 严格命中既定三档且地点合格：{summary['included_jobs']} 条，涉及 {summary['companies_with_included_jobs']} 家公司。
- 三档分布：1档 {summary['by_priority']['1']} 条；2档 {summary['by_priority']['2']} 条；3档 {summary['by_priority']['3']} 条。
- 岗位证据命中三档共 {summary['role_matched_jobs_before_location_filter']} 条，其中 {summary['role_matched_but_location_excluded']} 条因地点未明确或超出范围而不纳入。
- 其余泛 AI/算法近邻但不满足既定三档：{summary['broad_ai_only_jobs']} 条。
- 因普通图像/影像处理缺少肯定 AI 模型证据而排除：{summary['image_processing_without_ai_evidence']} 条；逐条记录 `image_processing_without_ai_evidence`。
- 全程只读 SQLite 中的恢复任务 structured jobs；网络请求 0，OCR 调用 0，未写 `exports/targets`。

## 纳入岗位

{markdown_table(included)}

## 需复核但不纳入

{markdown_table(near_misses)}

## 增量口径

每个恢复任务按修复报告的 `before_jobs` 和 `after_jobs` 截取 `jobs[before_jobs:after_jobs]`；随后使用 structured job id 全局去重。药明生物的两个入口包含同一批岗位，重复项只保留首次出现的“药明生物全球数智科技部”来源。校招优先、社招亦可，但地点必须明确命中四川/成都、北京、上海、广州或全国范围。1档遵循 `medical-ai-job-screening` skill：普通图像处理、增强、配准、重建、拼接、去噪、压缩、渲染、ISP、OpenCV 或医学影像处理不能单独作为视觉 AI 证据。
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit target-role delta from recovered structured jobs")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--remediation", type=Path, default=REMEDIATION)
    parser.add_argument("--database", type=Path, default=DATABASE)
    parser.add_argument("--json", type=Path, default=OUTPUT_JSON)
    parser.add_argument("--csv", type=Path, default=OUTPUT_CSV)
    parser.add_argument("--markdown", type=Path, default=OUTPUT_MD)
    args = parser.parse_args()
    root = args.root.resolve()
    resolve = lambda path: path if path.is_absolute() else root / path
    report = audit(resolve(args.remediation), resolve(args.database))
    json_path = resolve(args.json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(resolve(args.csv), report["jobs"])
    write_markdown(resolve(args.markdown), report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
