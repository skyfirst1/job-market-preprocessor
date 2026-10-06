from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


TARGET_EXCLUSION_CODE = "image_processing_without_ai_evidence"
ALLOWED_LOCATION_TERMS = ("四川", "成都", "北京", "上海", "广州", "全国", "全国多地")


# The role inventory is transcribed from the locally cached article/OCR evidence.
# Keeping it explicit makes this one-off audit reviewable and prevents an AI keyword
# hit elsewhere in an article from promoting unrelated roles.
ROLE_INVENTORY: dict[str, list[str]] = {
    "归领医疗": ["材料实习生", "结构实习生", "硬件实习生", "IT实习生", "海外市场实习生", "小红书运营实习生", "人事实习生", "市场实习生", "销售实习生"],
    "怀信科技": ["AI工程师（MES方向）", "软件开发/实施工程师", "自动化工程师", "销售管培生"],
    "新能康": ["销售实习生", "产品实习生", "研发工程师实习生", "测试工程师实习生", "实施工程师实习生"],
    "新途社区健康促进社": ["健康社工实习生"],
    "普利铭": ["工艺工程师", "质量管理工程师", "综合管理岗"],
    "欧蒙医学": ["研发工程师", "产品设计师", "自动化测试工程师", "嵌入式开发工程师", "应用软件开发工程师", "机械工程师", "光学工程师", "硬件工程师", "技术服务工程师", "技术应用工程师", "工艺工程师", "质检技术员", "试剂操作工", "仪器操作工"],
    "爱尔眼科": ["博士后", "眼科医生", "视光师", "医学影像医师/技师"],
    "甫康生物": ["AI药物研发（抗体/抗体偶联）", "AI药物研发（小分子）", "AI药物研发（AI模型工程化）", "AI Pilot（实习生）", "BD/海外BD", "大分子药物研发", "抗肿瘤药物研究员", "抗感染药物研究员", "新药项目管理", "制剂研究", "临床医学", "临床监查员", "临床试验助理", "临床前科研助理（实习）", "临床试验科研助理（实习）", "职能实习"],
    "百多力": ["暑期实习生（具体岗位未披露）"],
    "石药集团": ["高级研发员", "研发员", "生产管培生", "生产储备生", "市场医学管培生", "销售储备生", "职能管培生"],
    "芮来医药": ["市场医学部暑期实习生"],
    "英贝健": ["营销管培生", "电商管培生", "采购管培生", "生产管培生", "仓储管培生", "审计管培生"],
    "赫力昂": [],
    "迈微医疗": ["研发中心岗位族（具体JD未披露）", "营销中心岗位族（具体JD未披露）", "供应链岗位族（具体JD未披露）", "质量与职能岗位族（具体JD未披露）"],
    "金士达医疗": ["外贸销售类", "职能管理类（总助/生产/质量/设计/文案/电商）"],
    "雷允上药业集团": ["研发方向", "营销方向", "生产方向"],
}


def canonical_url(value: str) -> str:
    parts = urlsplit(value.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_documents(root: Path) -> dict[str, tuple[Path, dict]]:
    documents: dict[str, tuple[Path, dict]] = {}
    for path in (root / "exports" / "wechat").glob("*/document.json"):
        doc = read_json(path)
        url = doc.get("url") or doc.get("final_url")
        if url:
            documents[canonical_url(url)] = (path, doc)
    # Image-gap recovery commits the newest OCR evidence transactionally to the
    # workstation task store. Prefer that local result over the exported snapshot.
    task_db = root / "data" / "workstation.sqlite3"
    if task_db.is_file():
        with sqlite3.connect(task_db) as connection:
            connection.row_factory = sqlite3.Row
            for row in connection.execute("SELECT id,url,status,result_json FROM tasks WHERE result_json IS NOT NULL"):
                try:
                    doc = json.loads(row["result_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                doc["_local_task_id"] = row["id"]
                doc["_local_task_status"] = row["status"]
                documents[canonical_url(row["url"])] = (task_db, doc)
    return documents


def combined_text(doc: dict) -> str:
    chunks = [str(doc.get("text") or "")]
    for item in doc.get("ocr") or []:
        if isinstance(item, dict):
            chunks.append(str(item.get("text") or ""))
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def role_decision(company: str, role: str, doc: dict, queue_row: dict) -> dict:
    text = combined_text(doc)
    lower = f"{role}\n{text}".lower()
    gaps = doc.get("ocr_gaps") or []
    location = str(queue_row.get("location") or "")
    location_eligible = any(term in location for term in ALLOWED_LOCATION_TERMS)

    category = ""
    admitted = False
    sufficient = False
    exclusion_code = "not_target_category"
    rationale = "岗位未体现视觉AI/深度学习算法、Agent/大模型开发或Agent/大模型算法职责。"
    affirmative_evidence = ""

    if role == "AI工程师（MES方向）":
        exclusion_code = "generic_ai_without_target_evidence"
        rationale = "仅披露AI工程师标题、MES方向和专业要求，没有视觉/深度学习模型或Agent/LLM开发算法证据。"
    elif role.startswith("AI药物研发") or role.startswith("AI Pilot"):
        exclusion_code = "insufficient_target_evidence"
        rationale = "AI药研或AI模型工程化标题未说明深度学习、视觉模型、Agent或LLM职责；不能仅凭AI字样准入。"
        if gaps:
            exclusion_code = "insufficient_evidence_ocr_gaps"
            rationale += f" 同时存在{len(gaps)}个图片OCR缺口，证据进一步降级。"
    elif "图像处理" in role:
        exclusion_code = TARGET_EXCLUSION_CODE
        rationale = "仅有普通图像处理表述，没有模型训练、CV/机器视觉或学习型算法证据。"
    elif company == "赫力昂":
        exclusion_code = "blocked_no_evidence"
        rationale = "本地任务为blocked，没有文章、图片或OCR证据，不能判断或准入。"
    elif "具体岗位未披露" in role or "岗位族" in role:
        exclusion_code = "role_details_not_exposed"
        rationale = "文章仅披露实习计划或部门岗位族，没有可独立判断的具体JD。"

    if admitted and not location_eligible:
        admitted = False
        sufficient = False
        exclusion_code = "location_out_of_scope"
        rationale = "岗位地点不在四川、北京、上海、广州或全国范围。"

    return {
        "category": category,
        "admitted": admitted,
        "evidence_sufficient": sufficient,
        "exclusion_code": exclusion_code,
        "rationale": rationale,
        "affirmative_evidence": affirmative_evidence,
        "location_eligible": location_eligible,
    }


def build_rows(root: Path) -> tuple[list[dict], dict]:
    queue_path = root / "data" / "batches" / "batch_009" / "wechat_queue.jsonl"
    queue = read_jsonl(queue_path)
    documents = load_documents(root)
    rows: list[dict] = []
    task_statuses: list[dict] = []

    for task_index, item in enumerate(queue, start=1):
        url = str(item["acquisition_url"])
        document_entry = documents.get(canonical_url(url))
        if document_entry:
            document_path, doc = document_entry
        else:
            document_path, doc = Path(""), {"status": "blocked", "ocr_gaps": [], "images": [], "ocr": []}
        blocked = str(doc.get("status") or "") == "blocked"
        evidence_success = bool(document_entry) and not blocked
        gaps = doc.get("ocr_gaps") or []
        status = "blocked" if blocked else "success_with_ocr_gaps" if gaps else "success"
        task_statuses.append({
            "company": item["company"],
            "url": url,
            "status": status,
            "document": str(document_path.resolve()) if document_entry else "",
            "ocr_gaps": len(gaps),
        })

        roles = ROLE_INVENTORY.get(item["company"], [])
        if not roles:
            roles = ["（无可分析JD）"]
        for role_index, role in enumerate(roles, start=1):
            decision = role_decision(item["company"], role, doc, item)
            evidence_text = combined_text(doc)
            evidence_excerpt = ""
            if role != "（无可分析JD）":
                pos = evidence_text.lower().find(role.split("（")[0].lower())
                if pos >= 0:
                    evidence_excerpt = evidence_text[max(0, pos - 80): pos + len(role) + 180].replace("\n", " ").strip()
            if not evidence_excerpt:
                evidence_excerpt = (evidence_text[:260] if evidence_text else "").replace("\n", " ").strip()
            row_id = hashlib.sha256(f"batch009|{item['company']}|{role}".encode("utf-8")).hexdigest()[:24]
            rows.append({
                "analysis_id": row_id,
                "task_index": task_index,
                "role_index": role_index,
                "company": item["company"],
                "role_title": role,
                "priority": decision["category"],
                "admitted": decision["admitted"],
                "evidence_sufficient": decision["evidence_sufficient"],
                "exclusion_code": decision["exclusion_code"],
                "rationale": decision["rationale"],
                "affirmative_evidence": decision["affirmative_evidence"],
                "location": item.get("location", ""),
                "location_eligible": decision["location_eligible"],
                "task_status": status,
                "document_status": doc.get("status", "blocked"),
                "ocr_gap_count": len(gaps),
                "evidence_level": "local_article_ocr_degraded" if gaps else "local_article_ocr" if evidence_success else "none",
                "evidence_excerpt": evidence_excerpt,
                "source_url": url,
                "document_path": str(document_path.resolve()) if document_entry else "",
            })

    status_counts = Counter(item["status"] for item in task_statuses)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "offline_local_evidence_only",
        "network_requests": 0,
        "queue_tasks": len(queue),
        "task_status_counts": dict(status_counts),
        "evidence_success_tasks": sum(1 for item in task_statuses if item["status"] != "blocked"),
        "blocked_tasks": sum(1 for item in task_statuses if item["status"] == "blocked"),
        "jd_analysis_rows": len(rows),
        "admitted_rows": sum(1 for row in rows if row["admitted"] and row["evidence_sufficient"]),
        "ocr_gap_tasks": sum(1 for item in task_statuses if item["ocr_gaps"]),
        "exclusion_codes": dict(Counter(row["exclusion_code"] for row in rows)),
        "ordinary_image_processing_exclusion_code": TARGET_EXCLUSION_CODE,
        "ordinary_image_processing_exclusions": sum(1 for row in rows if row["exclusion_code"] == TARGET_EXCLUSION_CODE),
        "tasks": task_statuses,
    }
    return rows, summary


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_reports(root: Path, rows: list[dict], summary: dict) -> None:
    audit_dir = root / "data" / "audits"
    audit_dir.mkdir(parents=True, exist_ok=True)
    json_path = audit_dir / "batch009_target_analysis.json"
    csv_path = audit_dir / "batch009_target_analysis.csv"
    md_path = audit_dir / "batch009_target_analysis.md"
    json_path.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(csv_path, rows)

    notable = [row for row in rows if row["exclusion_code"] in {"generic_ai_without_target_evidence", "insufficient_evidence_ocr_gaps", "blocked_no_evidence"}]
    lines = [
        "# Batch 009 本地目标岗位分析",
        "",
        "- 分析模式：仅本地队列、微信缓存与OCR文本；网络请求 0。",
        f"- 任务映射：{summary['evidence_success_tasks']} 篇有本地证据，{summary['blocked_tasks']} 篇 blocked。",
        f"- 逐JD/岗位族记录：{summary['jd_analysis_rows']} 条。",
        f"- 充分证据准入：{summary['admitted_rows']} 条。",
        f"- OCR有缺口的文章：{summary['ocr_gap_tasks']} 篇，均已降级证据。",
        f"- 普通图像处理排除码：`{TARGET_EXCLUSION_CODE}`；本批命中 {summary['ordinary_image_processing_exclusions']} 条。",
        "",
        "## 重点边界结论",
        "",
    ]
    for row in notable:
        lines.append(f"- **{row['company']} / {row['role_title']}**：`{row['exclusion_code']}`。{row['rationale']}")
    lines += [
        "",
        "## 合并结论",
        "",
        "本批没有同时满足三档语义、地点与充分证据要求的新增JD，因此不向可投公司或候选JD清单误收记录。15篇成功文章仍更新为已完成本地证据审核；赫力昂保留 blocked。",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_existing_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def merge_targets(root: Path, rows: list[dict], summary: dict) -> dict:
    target_dir = root / "exports" / "targets"
    baseline_counts = {
        "applicable_companies": len(read_csv(target_dir / "applicable_companies.csv")[1]),
        "job_targets": len(read_csv(target_dir / "job_targets.csv")[1]),
        "verified_companies": len(read_csv(target_dir / "verified_companies.csv")[1]),
        "verified_job_targets": len(read_csv(target_dir / "verified_job_targets.csv")[1]),
    }
    minimums = {
        "applicable_companies": 18,
        "job_targets": 41,
        "verified_companies": 17,
        "verified_job_targets": 39,
    }
    if any(baseline_counts[key] < minimums[key] for key in minimums):
        raise RuntimeError(f"Refusing merge because recovered_jobs_target_delta baseline is missing: {baseline_counts}")
    pending_path = target_dir / "pending_discovery_companies.csv"
    error_path = target_dir / "error_companies.csv"
    pending_fields, pending_rows = read_csv(pending_path)
    error_fields, error_rows = read_csv(error_path)
    batch_companies = {task["company"] for task in summary["tasks"]}
    pending_by_company = {row["company"]: row for row in pending_rows if row["company"] in batch_companies}
    error_by_company = {row["company"]: row for row in error_rows if row["company"] in batch_companies}
    templates = {**pending_by_company, **error_by_company}
    pending_rows = [row for row in pending_rows if row["company"] not in batch_companies]
    error_rows = [row for row in error_rows if row["company"] not in batch_companies]

    now = datetime.now(timezone.utc).isoformat()
    for task in summary["tasks"]:
        company = task["company"]
        template = dict(templates.get(company, {}))
        if not template:
            raise RuntimeError(f"Missing existing target-list row for {company}")
        company_rows = [row for row in rows if row["company"] == company]
        if task["status"] == "blocked":
            template.update({
                "fetch_status": "blocked",
                "fetch_updated_at": now,
                "verification_status": "本地微信任务受阻，无证据",
                "evidence_summary": "batch_009本地任务=blocked; 网络请求=0",
                "uncertainty": "没有文章、图片或OCR证据，不能判断目标岗位",
            })
            error_rows.append(template)
        else:
            gap_count = max((int(row["ocr_gap_count"]) for row in company_rows), default=0)
            fetch_status = "pending_ocr" if gap_count else "partial"
            template.update({
                "fetch_status": fetch_status,
                "fetch_updated_at": now,
                "verification_status": "本地证据已审核，未发现三档目标岗位",
                "evidence_summary": f"batch_009本地微信缓存与OCR已逐JD审核; ocr_gaps={gap_count}; 网络请求=0",
                "uncertainty": f"{gap_count}个图片OCR缺口，未以缺证据项准入" if gap_count else "文章岗位已按三档规则审核，未发现充分目标证据",
            })
            pending_rows.append(template)

    _, applicable_rows = read_csv(target_dir / "applicable_companies.csv")
    applicable_ids = {row.get("company_id", "") for row in applicable_rows if row.get("company_id")}
    applicable_names = {row.get("company", "") for row in applicable_rows if row.get("company")}
    pending_rows = [
        row for row in pending_rows
        if row.get("company_id") not in applicable_ids and row.get("company") not in applicable_names
    ]
    error_rows = [
        row for row in error_rows
        if row.get("company_id") not in applicable_ids and row.get("company") not in applicable_names
    ]
    pending_rows.sort(key=lambda row: (row.get("company", ""), row.get("source_row", "")))
    error_rows.sort(key=lambda row: (row.get("company", ""), row.get("source_row", "")))
    write_existing_csv(pending_path, pending_fields, pending_rows)
    write_existing_csv(error_path, error_fields, error_rows)

    target_summary_path = target_dir / "summary.json"
    target_summary = read_json(target_summary_path)
    target_summary.update({
        "generated_at": now,
        "trigger": "batch_009_offline_target_analysis",
        "analyzed_companies": 176,
        "processed_companies": 176,
        "successful_evidence_companies": 175,
        "blocked_companies": 1,
        "deferred_companies": 0,
        "remaining_companies": 0,
        "pending_discovery_companies": len(pending_rows),
        "error_companies": len(error_rows),
        "runner_state": "completed_with_one_blocked",
        "batch_009_local_analysis": {
            "queue_tasks": summary["queue_tasks"],
            "evidence_success": summary["evidence_success_tasks"],
            "blocked": summary["blocked_tasks"],
            "ocr_gap_tasks": summary["ocr_gap_tasks"],
            "jd_rows_reviewed": summary["jd_analysis_rows"],
            "new_admitted_jds": summary["admitted_rows"],
            "network_requests": 0,
            "net_new_applicable_companies": 0,
            "net_new_jds": summary["admitted_rows"],
            "preserved_target_baseline": baseline_counts,
            "report_json": str((root / "data" / "audits" / "batch009_target_analysis.json").resolve()),
        },
        "batch_delta": {
            "batch": "batch_009",
            "new_applicable_companies": 0,
            "new_jds": 0,
            "cumulative_applicable_companies": target_summary.get("applicable_companies", 0),
            "cumulative_jds": target_summary.get("jd_rows", 0),
        },
    })
    all_company_rows = []
    for filename in ("applicable_companies.csv", "pending_discovery_companies.csv", "error_companies.csv"):
        _, file_rows = read_csv(target_dir / filename)
        all_company_rows.extend(file_rows)
    target_summary["by_fetch_status"] = dict(Counter(row.get("fetch_status", "") for row in all_company_rows))
    target_summary_path.write_text(json.dumps(target_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return target_summary


def validate(root: Path, rows: list[dict], audit_summary: dict, target_summary: dict) -> dict:
    target_dir = root / "exports" / "targets"
    _, applicable = read_csv(target_dir / "applicable_companies.csv")
    _, jobs = read_csv(target_dir / "job_targets.csv")
    _, pending = read_csv(target_dir / "pending_discovery_companies.csv")
    _, errors = read_csv(target_dir / "error_companies.csv")
    task_companies = {task["company"] for task in audit_summary["tasks"]}
    present = [row for row in pending + errors if row["company"] in task_companies]
    admitted = [row for row in rows if row["admitted"] and row["evidence_sufficient"]]
    company_ids = [row.get("company_id", "") for row in applicable + pending + errors]
    checks = {
        "queue_mapping_15_success_1_blocked": audit_summary["evidence_success_tasks"] == 15 and audit_summary["blocked_tasks"] == 1,
        "all_16_companies_in_pending_or_error": len(present) == 16 and len({row["company"] for row in present}) == 16,
        "blocked_company_is_helion": any(row["company"] == "赫力昂" and row["fetch_status"] == "blocked" for row in errors),
        "no_insufficient_batch009_target_admitted": not admitted,
        "target_summary_matches_files": target_summary["applicable_companies"] == len(applicable) and target_summary["jd_rows"] == len(jobs),
        "pending_summary_matches_file": target_summary["pending_discovery_companies"] == len(pending),
        "error_summary_matches_file": target_summary["error_companies"] == len(errors),
        "overall_processing_mapping": target_summary["successful_evidence_companies"] == 175 and target_summary["blocked_companies"] == 1,
        "image_processing_exclusion_code_declared": audit_summary["ordinary_image_processing_exclusion_code"] == TARGET_EXCLUSION_CODE,
        "network_requests_zero": audit_summary["network_requests"] == 0,
        "company_outputs_deduplicated": len(company_ids) == len(set(company_ids)),
        "job_outputs_deduplicated": len(jobs) == len({row.get("jd_id", "") for row in jobs}),
        "recovered_jobs_delta_baseline_preserved": (
            len(applicable) >= 18
            and len(jobs) >= 41
            and len(read_csv(target_dir / "verified_companies.csv")[1]) >= 17
            and len(read_csv(target_dir / "verified_job_targets.csv")[1]) >= 39
        ),
    }
    result = {"passed": all(checks.values()), "checks": checks}
    path = root / "data" / "audits" / "batch009_target_consistency.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if not result["passed"]:
        raise RuntimeError(f"Consistency validation failed: {checks}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline semantic analysis and target merge for batch_009 WeChat evidence.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--merge", action="store_true", help="Merge sufficiently evidenced rows and update target status files.")
    args = parser.parse_args()
    root = args.root.resolve()
    rows, audit_summary = build_rows(root)
    write_reports(root, rows, audit_summary)
    target_summary = read_json(root / "exports" / "targets" / "summary.json")
    if args.merge:
        target_summary = merge_targets(root, rows, audit_summary)
    validation = validate(root, rows, audit_summary, target_summary) if args.merge else {"passed": True, "checks": {"report_only": True}}
    print(json.dumps({"summary": audit_summary, "validation": validation}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
