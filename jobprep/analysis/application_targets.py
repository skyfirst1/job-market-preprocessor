from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from jobprep.store import canonical_url


EXPLICIT_SOE = re.compile(r"央国企|央企|国企|国有(?:独资|控股)?|事业单位|政府机关")
MEDICAL = re.compile(r"医疗|医药|医学|生物|制药|药业|生命|诊断|健康|基因|影像|器械|药物", re.I)
TARGET_REGION = re.compile(r"四川|成都|北京|上海|广州|全国(?:各地|多地)?")
ROLE_PATTERNS = (
    (3, re.compile(r"(?:Agent|智能体|大模型|LLM|自然语言处理|NLP).{0,12}(?:算法|研究|训练|推理)", re.I)),
    (2, re.compile(r"(?:Agent|智能体|大模型|LLM).{0,12}(?:开发|研发|工程|应用|Infra)|RAG", re.I)),
    (1, re.compile(r"计算机视觉|机器视觉|视觉算法|图像(?:处理|算法|识别|分割)|医学影像|影像算法|目标检测|深度学习", re.I)),
)
PRIORITY_LABELS = {
    1: "视觉相关AI/深度学习算法",
    2: "Agent/大模型开发",
    3: "Agent/大模型算法（次选）",
    4: "受限来源AI相关（待完整JD复核）",
}

LIMITED_EVIDENCE = "limited_source_ai_mention"
AI_MENTION = re.compile(
    r"(?i)(?<![A-Za-z])(?:"
    r"AI(?=$|[\s\-_/（(]|Infra\b|算法|应用|工程|产品|模型|设计|工作流|能力|相关|时代|驱动)|"
    r"人工智能|机器学习|深度学习|大模型|"
    r"LLM(?=$|[\s\-_/（(]|算法|应用|工程|产品|模型)|"
    r"Agent(?=$|[\s\-_/（(]|开发|算法|工程|应用|框架|产品|系统|平台|智能体)|"
    r"CV(?=$|[\s\-_/（(]|算法|模型|工程)|"
    r"NLP(?=$|[\s\-_/（(]|算法|模型|工程)"
    r")"
)

COMPANY_COLUMNS = [
    "screening_scope", "company_id", "company", "enterprise_nature", "ownership_status", "ownership_confidence",
    "ownership_evidence", "industry", "best_priority", "matched_role_count", "graduation_year",
    "location", "deadline", "application_url", "announcement_url", "acquisition_url", "source_pool",
    "source_row", "target_status", "evidence_level", "verification_status", "fetch_status", "fetch_updated_at", "page_title", "structured_jobs_count",
    "evidence_summary", "audit_evidence", "uncertainty",
]
JOB_COLUMNS = [
    "screening_scope", "industry", "jd_id", "company_id", "company", "priority", "priority_label", "role_title", "jd_status",
    "details_verified", "evidence_level", "verification_status", "structured_job_title", "structured_job_url", "description", "requirements",
    "location", "graduation_year", "deadline", "application_url", "source_url", "source_pool",
    "source_row", "fetch_status", "fetch_updated_at", "evidence", "audit_evidence", "uncertainty",
]

EVIDENCE_RANK = {LIMITED_EVIDENCE: 0, "csv_declared": 1, "page_text": 2, "structured_job": 3, "audit_confirmed": 4}
VERIFICATION_STATUS = {
    LIMITED_EVIDENCE: "受限来源岗位级AI明示，待完整JD核验",
    "csv_declared": "CSV岗位已声明，网页待核验",
    "page_text": "页面文本已核验",
    "structured_job": "结构化JD已核验",
    "audit_confirmed": "独立审核已确认",
}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(_clean(part).casefold() for part in parts).encode("utf-8")).hexdigest()[:24]


def _industry_pattern(pattern: re.Pattern[str] | str | None) -> re.Pattern[str]:
    if pattern is None:
        return MEDICAL
    if isinstance(pattern, re.Pattern):
        return pattern
    return re.compile(pattern, re.I)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _roles(row: dict[str, str]) -> list[dict[str, Any]]:
    try:
        raw = json.loads(row.get("matched_roles") or "[]")
    except json.JSONDecodeError:
        raw = []
    output: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        role = _clean(item.get("role"))
        try:
            priority = int(item.get("priority"))
        except (TypeError, ValueError):
            continue
        expanded = _expand_role(role)
        for title in expanded:
            classified = _classify(title)
            # A single role keeps the explicit CSV category (for example "AI算法工程师").
            # When a dense company-wide role string is split, every child must qualify itself.
            effective_priority = classified or (priority if len(expanded) == 1 else None)
            key = (effective_priority or 0, title.casefold())
            if title and effective_priority in PRIORITY_LABELS and key not in seen:
                output.append({"priority": effective_priority, "category": PRIORITY_LABELS[effective_priority], "role": title, "origin": "csv"})
                seen.add(key)
    return sorted(output, key=lambda item: (item["priority"], item["role"].casefold()))


def _classify(text: str) -> int | None:
    for priority, pattern in ROLE_PATTERNS:
        if pattern.search(text):
            return priority
    return None


def _clean_job_title(value: Any) -> str:
    title = _clean(value)
    title = re.split(r"\s+发布于\s+", title, maxsplit=1)[0]
    title = re.split(
        r"\s+(?:(?:实习|全职|兼职|校招)\s+){1,3}\|\s*",
        title,
        maxsplit=1,
    )[0]
    title = re.split(
        r"\s+(?:团队介绍|岗位职责|职位职责|职位描述|任职要求)\s*[：:]",
        title,
        maxsplit=1,
    )[0]
    title = re.sub(r"(?:\s+(?:实习|全职|兼职|校招)){2,}$", "", title)
    return title.strip()


def _expand_role(role: str) -> list[str]:
    parts = [_clean(part) for part in re.split(r"[,，、;；\n]+", role) if _clean(part)]
    output: list[str] = []
    for part in parts:
        if part.count("类") >= 3 and _classify(part) is None:
            continue
        # Dense CSV cells often contain several titles separated only by spaces.
        titles = re.findall(r"[^\s,/，、;；]{1,32}(?:工程师|研究员|科学家|实习生|算法类)", part)
        if titles and (len(titles) > 1 or titles[0] != part) and any(_classify(title) for title in titles):
            output.extend(titles)
        elif "/" in part and not re.search(r"[（(].*/.*[）)]", part):
            output.extend(_clean(item) for item in part.split("/") if _classify(item))
        else:
            output.append(part)
    return output


def _discovered_roles(row: dict[str, str], structured_jobs: list[dict[str, Any]], page_text: str) -> list[dict[str, Any]]:
    roles = _roles(row)
    seen = {(item["priority"], item["role"].casefold()) for item in roles}
    for job in structured_jobs:
        if not isinstance(job, dict):
            continue
        title = _clean_job_title(job.get("title"))
        priority = _classify(title)
        if priority is None and re.fullmatch(r"(?:AI)?(?:算法|开发|研发)工程师(?:[-（(].*)?", title, re.I):
            priority = _classify(_job_text(job))
        key = (priority or 0, title.casefold())
        if title and priority and key not in seen:
            roles.append({"priority": priority, "category": PRIORITY_LABELS[priority], "role": title, "origin": "structured_job"})
            seen.add(key)
    # Structured records supersede rendered page lines, which often repeat titles with UI labels.
    fallback_lines = [] if structured_jobs else page_text.splitlines()
    for line in fallback_lines:
        title = _clean(line)
        if not title or len(title) > 80:
            continue
        priority = _classify(title)
        key = (priority or 0, title.casefold())
        if priority and key not in seen and re.search(r"工程师|研究员|科学家|实习生|算法|开发|研发", title, re.I):
            roles.append({"priority": priority, "category": PRIORITY_LABELS[priority], "role": title, "origin": "page_text"})
            seen.add(key)
    return sorted(roles, key=lambda item: (item["priority"], item["role"].casefold()))


def load_store_results(database: Path) -> dict[str, dict[str, Any]]:
    if not database.is_file():
        return {}
    results: dict[str, dict[str, Any]] = {}
    with sqlite3.connect(database, timeout=30) as db:
        db.row_factory = sqlite3.Row
        for record in db.execute("SELECT url,status,updated_at,error,result_json FROM tasks"):
            try:
                url = canonical_url(record["url"])
            except ValueError:
                continue
            try:
                result = json.loads(record["result_json"]) if record["result_json"] else {}
            except json.JSONDecodeError:
                result = {}
            results[url] = {
                "url": url,
                "status": record["status"],
                "updated_at": record["updated_at"],
                "error": record["error"] or "",
                "result": result if isinstance(result, dict) else {},
            }
    return results


def _limited_source_kind(task: dict[str, Any]) -> str:
    if _clean(task.get("status")) != "partial":
        return ""
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
    reason = _clean(coverage.get("stop_reason"))
    host = (urlsplit(_clean(task.get("url"))).hostname or "").lower()
    if host == "mp.weixin.qq.com":
        return "wechat"
    if host in {"v.wjx.cn", "www.wjx.top"}:
        return "questionnaire"
    if host == "campus.51job.com" and reason == "static_topic_complete":
        return "static_topic"
    if host == "doc.weixin.qq.com":
        return "wechat_form"
    if host == "alidocs.dingtalk.com":
        return "dingtalk_form"
    if reason == "static_document":
        return "static_document"
    return ""


def _local_text(result: dict[str, Any]) -> str:
    chunks = [str(result.get("text") or "")]
    for item in result.get("ocr") or []:
        if isinstance(item, dict) and item.get("text"):
            chunks.append(str(item["text"]))
    return "\n".join(chunk for chunk in chunks if chunk)


def _source_role_titles(result: dict[str, Any]) -> list[str]:
    titles: list[str] = []
    for reference in result.get("source_references") or []:
        if not isinstance(reference, dict):
            continue
        raw = reference.get("raw") if isinstance(reference.get("raw"), dict) else {}
        value = _clean(raw.get("招聘岗位"))
        for title in re.split(r"[,，、;；\n]+", value):
            title = _clean_job_title(title)
            if title and title not in titles:
                titles.append(title)
    return titles


def _role_segment(title: str, titles: list[str], text: str) -> str:
    anchors = list(dict.fromkeys(filter(None, (title, re.split(r"[（(]", title, maxsplit=1)[0]))))
    starts = sorted({match.start() for anchor in anchors for match in re.finditer(re.escape(anchor), text)})
    if not starts:
        return title
    segments: list[str] = []
    for start in starts:
        next_positions = []
        for other in titles:
            if other == title:
                continue
            for anchor in filter(None, (other, re.split(r"[（(]", other, maxsplit=1)[0])):
                position = text.find(anchor, start + 1)
                if position > start:
                    next_positions.append(position)
        for match in re.finditer(r"(?m)^.{0,45}(?:实习生|工程师|研究员|科学家|产品经理)\s*$", text):
            if match.start() > start + len(title):
                next_positions.append(match.start())
        end = min(next_positions, default=min(len(text), start + 2200))
        segments.append(text[start:min(end, start + 2200)])
    return next((segment for segment in segments if _role_has_ai_mention(title, segment)), segments[0])


def _evidence_excerpt(title: str, segment: str) -> str:
    match = AI_MENTION.search(segment)
    if not match:
        return _clean(title)[:360]
    start = max(0, match.start() - 100)
    end = min(len(segment), match.end() + 220)
    return _clean(segment[start:end])[:360]


def _role_has_ai_mention(title: str, segment: str) -> bool:
    if AI_MENTION.search(title):
        return True
    for line in segment.splitlines():
        if not AI_MENTION.search(line):
            continue
        if re.search(r"公司|企业|关于我们|行业新闻|欢迎加入", line) and not re.search(r"职责|要求|专业|岗位|开发|研发|算法|模型|产品", line):
            continue
        return True
    return False


def _limited_source_roles(result: dict[str, Any]) -> list[dict[str, Any]]:
    text = _local_text(result)
    candidates: list[tuple[str, str, dict[str, Any] | None]] = []
    for job in result.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        title = _clean_job_title(job.get("title"))
        segment = _job_text(job)
        if title and _role_has_ai_mention(title, segment):
            candidates.append((title, segment, job))
    titles = _source_role_titles(result)
    for title in titles:
        segment = _role_segment(title, titles, text)
        if _role_has_ai_mention(title, segment):
            candidates.append((title, segment, None))
    if not titles:
        lines = [_clean(line) for line in text.splitlines() if _clean(line)]
        title_indexes = []
        for index, line in enumerate(lines):
            match = re.search(r"([^：:，,。；;]{1,45}(?:实习生|工程师|研究员|科学家|产品经理))", line)
            if match and not re.search(r"岗位职责|任职要求|招聘对象", match.group(1)):
                title_indexes.append((index, _clean_job_title(match.group(1).lstrip("①②③④⑤⑥⑦⑧⑨⑩0123456789.、）)'\"“” "))))
        for position, (index, title) in enumerate(title_indexes):
            stop = title_indexes[position + 1][0] if position + 1 < len(title_indexes) else min(len(lines), index + 30)
            segment = "\n".join(lines[index:stop])
            if title and _role_has_ai_mention(title, segment):
                candidates.append((title, segment, None))
    roles: dict[str, dict[str, Any]] = {}
    for title, segment, job in candidates:
        key_title = re.sub(r"^20\d{2}届", "", title)
        key_title = re.sub(r"[\s（(]*J\d+[）)]*$", "", key_title, flags=re.IGNORECASE)
        key = re.sub(r"\W+", "", key_title.casefold())
        priority = _classify(title) or 4
        candidate = {
            "priority": priority,
            "category": PRIORITY_LABELS[priority],
            "role": title,
            "origin": "limited_source",
            "matched": job,
            "evidence_excerpt": _evidence_excerpt(title, segment),
        }
        current = roles.get(key)
        if current is None or (job is not None and current.get("matched") is None):
            roles[key] = candidate
        elif bool(job) == bool(current.get("matched")) and len(segment) > len(current.get("evidence_excerpt", "")):
            roles[key] = candidate
    return sorted(roles.values(), key=lambda item: (item["priority"], item["role"].casefold()))


def _ownership(row: dict[str, str]) -> tuple[str, str, str]:
    nature = _clean(row.get("enterprise_nature"))
    supplied = _clean(row.get("ownership_status"))
    evidence = f"企业性质={nature or '未注明'}; 预筛判断={supplied or '未提供'}"
    supplied_is_explicit_soe = bool(re.search(r"(?:明确国企|央企|央国企|国有(?:独资|控股)?)", supplied)) and "非国企" not in supplied
    if EXPLICIT_SOE.search(nature) or supplied_is_explicit_soe:
        return "明确国企（排除）", "high", evidence
    if nature in {"民企", "外企"}:
        return "非国企（CSV标注）", "medium", evidence
    return "未发现国企标记，待核验", "low", evidence


def _task_for(row: dict[str, str], store: dict[str, dict[str, Any]]) -> dict[str, Any]:
    candidates = [row.get("acquisition_url"), row.get("application_url"), row.get("announcement_url")]
    for value in candidates:
        try:
            key = canonical_url(value or "")
        except ValueError:
            continue
        if key in store:
            exact = store[key]
            if _clean(exact.get("status")) not in {"pending", "not_started"}:
                return exact
            parsed = urlsplit(key)
            if parsed.hostname == "mp.weixin.qq.com":
                same_article = [
                    task for stored_url, task in store.items()
                    if urlsplit(stored_url).hostname == parsed.hostname
                    and urlsplit(stored_url).path == parsed.path
                    and _clean(task.get("status")) not in {"pending", "not_started"}
                ]
                if same_article:
                    return same_article[0]
            return exact
    return {"status": "not_started", "updated_at": "", "error": "", "result": {}}


def _job_text(job: dict[str, Any]) -> str:
    return _clean(" ".join(str(job.get(field) or "") for field in (
        "title", "description", "requirements", "text", "category", "department"
    )))


def _match_structured_job(role: str, jobs: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    role_folded = re.sub(r"\s+", "", role.casefold())
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = _clean_job_title(job.get("title"))
        title_folded = re.sub(r"\s+", "", title.casefold())
        if title and role_folded == title_folded:
            return job
    return None


def build_rows(
    candidates: list[dict[str, str]],
    store: dict[str, dict[str, Any]],
    audit_confirmations: dict[tuple[str, str], str] | None = None,
    *,
    screening_scope: str = "medical",
    industry_pattern: re.Pattern[str] | str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scope = _clean(screening_scope)
    if not scope:
        raise ValueError("screening_scope must not be blank")
    accepted_industry = _industry_pattern(industry_pattern)
    companies: dict[str, dict[str, Any]] = {}
    jobs_out: dict[str, dict[str, Any]] = {}
    for row in candidates:
        company = _clean(row.get("company"))
        industry = _clean(row.get("industry"))
        ownership_status, ownership_confidence, ownership_evidence = _ownership(row)
        source_location = _clean(row.get("location"))
        if not company or not accepted_industry.search(f"{company} {industry}") or ownership_status.startswith("明确国企"):
            continue
        task = _task_for(row, store)
        result = task.get("result") or {}
        structured_jobs = result.get("jobs") if isinstance(result.get("jobs"), list) else []
        raw_page_text = str(result.get("text") or "")
        limited_source_kind = _limited_source_kind(task)
        roles = _limited_source_roles(result) if limited_source_kind else _discovered_roles(row, structured_jobs, raw_page_text)
        fetch_status = _clean(task.get("status")) or "not_started"
        company_id = _stable_id(company)
        page_title = _clean(result.get("title"))
        coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
        text = _clean(raw_page_text)
        evidence_bits = [f"CSV第{_clean(row.get('source_row')) or '?'}行", f"抓取状态={fetch_status}"]
        if page_title:
            evidence_bits.append(f"页面标题={page_title}")
        if coverage.get("stop_reason"):
            evidence_bits.append(f"stop_reason={coverage['stop_reason']}")
        if task.get("error"):
            evidence_bits.append(f"error={_clean(task['error'])[:180]}")
        uncertainties = []
        if ownership_confidence != "medium":
            uncertainties.append("公司性质需人工核验")
        if not structured_jobs:
            uncertainties.append("尚无结构化JD，角色来自CSV招聘岗位")
        if fetch_status == "pending_ocr":
            uncertainties.append("微信图片已本地保存并待OCR；无需重访微信")
        if fetch_status in {"pending", "running", "not_started", "retry_wait"}:
            uncertainties.append("抓取尚未完成")
        elif fetch_status not in {"ok", "partial", "pending_ocr"}:
            uncertainties.append("抓取未成功")
        qualified_roles: list[tuple[dict[str, Any], dict[str, Any] | None, str]] = []
        normalized_text = re.sub(r"\s+", "", text.casefold())
        audit_evidence = ""
        if audit_confirmations and not limited_source_kind:
            audit_evidence = audit_confirmations.get((company, _clean(row.get("source_row"))), "")
            audit_evidence = audit_evidence or audit_confirmations.get((company, ""), "")
        for role in roles:
            matched = role.get("matched") or _match_structured_job(role["role"], structured_jobs)
            text_hit = bool(text and re.sub(r"\s+", "", role["role"].casefold()) in normalized_text)
            if limited_source_kind:
                evidence_level = LIMITED_EVIDENCE
            elif audit_evidence:
                evidence_level = "audit_confirmed"
            elif matched:
                evidence_level = "structured_job"
            elif text_hit or role.get("origin") == "page_text":
                evidence_level = "page_text"
            else:
                evidence_level = "csv_declared"
            qualified_roles.append((role, matched, evidence_level))
        best_priority = min((role["priority"] for role, _, _ in qualified_roles), default="")
        company_evidence_level = max(
            (level for _, _, level in qualified_roles),
            key=lambda level: EVIDENCE_RANK[level],
            default="",
        )
        companies[company_id] = {
            "screening_scope": scope, "company_id": company_id, "company": company,
            "enterprise_nature": _clean(row.get("enterprise_nature")),
            "ownership_status": ownership_status, "ownership_confidence": ownership_confidence,
            "ownership_evidence": ownership_evidence, "industry": industry,
            "best_priority": best_priority, "matched_role_count": len(qualified_roles),
            "graduation_year": _clean(row.get("graduation_year")), "location": _clean(row.get("location")),
            "deadline": _clean(row.get("deadline")), "application_url": _clean(row.get("application_url")),
            "announcement_url": _clean(row.get("announcement_url")), "acquisition_url": _clean(row.get("acquisition_url")),
            "source_pool": _clean(row.get("source_pool")), "source_row": _clean(row.get("source_row")),
            "target_status": "可投候选" if qualified_roles else "待页面发现目标岗位",
            "evidence_level": company_evidence_level,
            "verification_status": VERIFICATION_STATUS.get(company_evidence_level, "尚未发现目标岗位"),
            "fetch_status": fetch_status, "fetch_updated_at": _clean(task.get("updated_at")),
            "page_title": page_title, "structured_jobs_count": len(structured_jobs),
            "evidence_summary": "; ".join(evidence_bits),
            "audit_evidence": audit_evidence if not limited_source_kind else "",
            "uncertainty": "; ".join(uncertainties),
        }
        for role, matched, evidence_level in qualified_roles:
            title = role["role"]
            if evidence_level == LIMITED_EVIDENCE:
                jd_status = "受限来源岗位级AI明示，待完整JD核验"
            elif evidence_level == "audit_confirmed":
                jd_status = "独立审核已确认"
            elif evidence_level == "structured_job":
                jd_status = "结构化JD已匹配"
            elif evidence_level == "page_text":
                jd_status = "页面文本明确命中，JD待结构化"
            else:
                jd_status = "CSV招聘岗位明确命中，网页待核验"
            verified = "true" if evidence_level in {"structured_job", "audit_confirmed"} else "false"
            effective_location = _clean((matched or {}).get("location")) or source_location
            source_url = _clean((matched or {}).get("url")) or _clean(row.get("acquisition_url"))
            jd_id = _stable_id(company, title, source_url)
            uncertainty = []
            if evidence_level == "csv_declared":
                uncertainty.append("CSV已明确岗位；网页或结构化JD尚未核验")
            elif evidence_level == "page_text":
                uncertainty.append("页面有明确岗位文本，但尚未结构化为完整JD")
            elif evidence_level == LIMITED_EVIDENCE:
                uncertainty.append("受限来源仅有岗位级AI明示；列表覆盖与完整JD均未核验，不得视为verified")
            if ownership_confidence != "medium":
                uncertainty.append("公司性质需人工核验")
            jobs_out[jd_id] = {
                "screening_scope": scope, "industry": industry,
                "jd_id": jd_id, "company_id": company_id, "company": company,
                "priority": role["priority"], "priority_label": role["category"], "role_title": title,
                "jd_status": jd_status, "details_verified": verified,
                "evidence_level": evidence_level,
                "verification_status": VERIFICATION_STATUS[evidence_level],
                "structured_job_title": _clean_job_title((matched or {}).get("title")),
                "structured_job_url": _clean((matched or {}).get("url")),
                "description": (_clean((matched or {}).get("description")) or _clean(role.get("evidence_excerpt"))) if evidence_level == LIMITED_EVIDENCE else _clean((matched or {}).get("description")),
                "requirements": _clean((matched or {}).get("requirements")),
                "location": effective_location,
                "graduation_year": _clean(row.get("graduation_year")), "deadline": _clean(row.get("deadline")),
                "application_url": _clean(row.get("application_url")), "source_url": source_url,
                "source_pool": _clean(row.get("source_pool")), "source_row": _clean(row.get("source_row")),
                "fetch_status": fetch_status, "fetch_updated_at": _clean(task.get("updated_at")),
                "evidence": f"evidence_level={evidence_level}; role_origin={role.get('origin', 'unknown')}; role={title}; source={source_url}; excerpt={_clean(role.get('evidence_excerpt'))}; {companies[company_id]['evidence_summary']}",
                "audit_evidence": audit_evidence if evidence_level != LIMITED_EVIDENCE else "",
                "uncertainty": "; ".join(uncertainty),
            }
    company_rows = sorted(
        companies.values(),
        key=lambda item: (item["best_priority"] == "", int(item["best_priority"] or 99), item["company"]),
    )
    job_rows = sorted(jobs_out.values(), key=lambda item: (item["priority"], item["company"], item["role_title"]))
    return company_rows, job_rows


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _normalize_job_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repair carried-forward title pollution and keep IDs aligned with canonical fields."""
    normalized: dict[str, dict[str, Any]] = {}
    for source in rows:
        row = dict(source)
        row["role_title"] = _clean_job_title(row.get("role_title"))
        row["structured_job_title"] = _clean_job_title(row.get("structured_job_title"))
        if row["role_title"]:
            row["jd_id"] = _stable_id(
                _clean(row.get("company")),
                row["role_title"],
                _clean(row.get("source_url")),
            )
        key = _clean(row.get("jd_id")) or _stable_id(
            _clean(row.get("company")), row["role_title"], _clean(row.get("source_url"))
        )
        current = normalized.get(key)
        if current is None or EVIDENCE_RANK.get(_clean(row.get("evidence_level")), -1) > EVIDENCE_RANK.get(
            _clean(current.get("evidence_level")), -1
        ):
            normalized[key] = row
    return list(normalized.values())


def _ensure_canonical_dimensions(
    company_rows: Iterable[dict[str, Any]],
    job_rows: Iterable[dict[str, Any]],
    *,
    default_scope: str = "medical",
) -> None:
    """Backfill new dimensions when refreshing canonical files written by older versions."""
    industry_by_company: dict[tuple[str, str], str] = {}
    for row in company_rows:
        row["screening_scope"] = _clean(row.get("screening_scope")) or default_scope
        industry_by_company[(row["screening_scope"], _clean(row.get("company_id")))] = _clean(row.get("industry"))
    for row in job_rows:
        row["screening_scope"] = _clean(row.get("screening_scope")) or default_scope
        row["industry"] = _clean(row.get("industry")) or industry_by_company.get(
            (row["screening_scope"], _clean(row.get("company_id"))), ""
        )


def load_audit_confirmations(path: Path | None) -> dict[tuple[str, str], str]:
    if path is None or not path.is_file():
        return {}
    confirmations: dict[tuple[str, str], str] = {}
    for row in _read_csv(path):
        if _clean(row.get("review_category")) != "B":
            continue
        company = _clean(row.get("company"))
        source_rows = [_clean(value) for value in str(row.get("source_rows") or "").split(",") if _clean(value)]
        evidence = "; ".join((
            "review_category=B",
            f"task_id={_clean(row.get('task_id'))}",
            f"rationale={_clean(row.get('review_rationale'))}",
            f"evidence={_clean(row.get('evidence'))}",
        ))
        for source_row in source_rows or [""]:
            confirmations[(company, source_row)] = evidence
    return confirmations


def _augment_limited_source_candidates(
    candidates: list[dict[str, str]], store: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    output = list(candidates)
    present = {(_clean(row.get("company")), _clean(row.get("source_row"))) for row in output}
    for task in store.values():
        if not _limited_source_kind(task):
            continue
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        for reference in result.get("source_references") or []:
            if not isinstance(reference, dict):
                continue
            raw = reference.get("raw") if isinstance(reference.get("raw"), dict) else {}
            company = _clean(raw.get("公司名称"))
            source_row = _clean(reference.get("row_number"))
            key = (company, source_row)
            if not company or key in present:
                continue
            output.append({
                "company": company,
                "enterprise_nature": _clean(raw.get("企业性质")),
                "ownership_status": "非国企（CSV标注）" if _clean(raw.get("企业性质")) in {"民企", "外企"} else _clean(raw.get("企业性质")),
                "industry": _clean(raw.get("行业分类")),
                "best_priority": "",
                "discovery_status": "受限来源待岗位级AI证据审查",
                "matched_roles": "[]",
                "announcement_url": _clean(raw.get("公告链接")),
                "application_url": _clean(raw.get("投递链接")) or _clean(task.get("url")),
                "acquisition_url": _clean(task.get("url")),
                "source_pool": "wechat" if (urlsplit(_clean(task.get("url"))).hostname or "") == "mp.weixin.qq.com" else "web",
                "source_row": source_row,
                "graduation_year": _clean(raw.get("届次")),
                "location": _clean(raw.get("工作地点")),
                "deadline": _clean(raw.get("截止时间")),
            })
            present.add(key)
    return output


def update_application_targets(
    candidates_path: Path,
    summary_path: Path,
    database_path: Path,
    output_dir: Path,
    *,
    force: bool = False,
    partial_report_path: Path | None = None,
    audit_path: Path | None = None,
    screening_scope: str = "medical",
    industry_pattern: re.Pattern[str] | str | None = None,
) -> dict[str, Any]:
    candidates = _read_csv(candidates_path)
    batch_summary = _read_json(summary_path)
    store = load_store_results(database_path)
    candidates = _augment_limited_source_candidates(candidates, store)
    unique_companies = len({_clean(row.get("company")) for row in candidates if _clean(row.get("company"))})
    triggered = force or unique_companies >= 20 or bool(batch_summary.get("stage_complete"))
    if not triggered:
        return {"updated": False, "trigger": "waiting", "unique_input_companies": unique_companies}
    audit_confirmations = load_audit_confirmations(audit_path)
    companies, jobs = build_rows(
        candidates,
        store,
        audit_confirmations,
        screening_scope=screening_scope,
        industry_pattern=industry_pattern,
    )
    error_statuses = {"error", "blocked", "failed"}
    applicable_companies = [row for row in companies if row["target_status"] == "可投候选"]
    unverified_levels = {"csv_declared", LIMITED_EVIDENCE}
    verified_companies = [row for row in applicable_companies if row["evidence_level"] not in unverified_levels]
    verified_company_ids = {row["company_id"] for row in verified_companies}
    verified_jobs = [row for row in jobs if row["company_id"] in verified_company_ids and row["evidence_level"] not in unverified_levels]
    error_companies = [row for row in companies if row["fetch_status"] in error_statuses]
    pending_companies = [
        row for row in companies
        if row["target_status"] != "可投候选" and row["fetch_status"] not in error_statuses
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "applicable_companies.csv", COMPANY_COLUMNS, applicable_companies)
    _write_csv(output_dir / "verified_companies.csv", COMPANY_COLUMNS, verified_companies)
    _write_csv(output_dir / "pending_discovery_companies.csv", COMPANY_COLUMNS, pending_companies)
    _write_csv(output_dir / "error_companies.csv", COMPANY_COLUMNS, error_companies)
    _write_csv(output_dir / "job_targets.csv", JOB_COLUMNS, jobs)
    _write_csv(output_dir / "verified_job_targets.csv", JOB_COLUMNS, verified_jobs)
    partial_reasons: dict[str, int] = {}
    for company in companies:
        if company["fetch_status"] != "partial":
            continue
        match = re.search(r"stop_reason=([^;]+)", company["evidence_summary"])
        reason = _clean(match.group(1) if match else "未标注原因")
        partial_reasons[reason] = partial_reasons.get(reason, 0) + 1
    partial_optimization = _read_json(partial_report_path) if partial_report_path else {}
    summary = {
        "updated": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "screening_scope": _clean(screening_scope),
        "trigger": "20_companies" if unique_companies >= 20 else "stage_complete" if batch_summary.get("stage_complete") else "forced",
        "unique_input_companies": unique_companies,
        "analyzed_companies": len(companies),
        "applicable_companies": len(applicable_companies),
        "verified_companies": len(verified_companies),
        "pending_discovery_companies": len(pending_companies),
        "error_companies": len(error_companies),
        "jd_rows": len(jobs),
        "verified_jd_rows": len(verified_jobs),
        "by_evidence_level": {
            level: sum(row["evidence_level"] == level for row in jobs)
            for level in EVIDENCE_RANK
        },
        "by_priority": {str(priority): sum(row["priority"] == priority for row in jobs) for priority in PRIORITY_LABELS},
        "by_fetch_status": {
            status: sum(row["fetch_status"] == status for row in companies)
            for status in sorted({row["fetch_status"] for row in companies})
        },
        "verified_structured_jds": sum(row["details_verified"] == "true" for row in jobs),
        "evidence_backed_jds": len(jobs),
        "by_scope": {
            _clean(screening_scope): {
                "analyzed_companies": len(companies),
                "applicable_companies": len(applicable_companies),
                "verified_companies": len(verified_companies),
                "pending_discovery_companies": len(pending_companies),
                "error_companies": len(error_companies),
                "jd_rows": len(jobs),
                "verified_jd_rows": len(verified_jobs),
            }
        },
        "partial": {
            "current": sum(row["fetch_status"] == "partial" for row in companies),
            "reasons": dict(sorted(partial_reasons.items())),
            "optimization_report": str(partial_report_path.resolve()) if partial_report_path and partial_report_path.is_file() else "",
            "optimization": partial_optimization,
        },
        "outputs": {
            "companies": str((output_dir / "applicable_companies.csv").resolve()),
            "verified_companies": str((output_dir / "verified_companies.csv").resolve()),
            "pending_discovery": str((output_dir / "pending_discovery_companies.csv").resolve()),
            "errors": str((output_dir / "error_companies.csv").resolve()),
            "jobs": str((output_dir / "job_targets.csv").resolve()),
            "verified_jobs": str((output_dir / "verified_job_targets.csv").resolve()),
        },
        "notes": [
            "每个预筛 matched role 独立成一行；公司级岗位串不会作为一个聚合JD输出。",
            "明确国企会被排除；民企/外企判断沿用CSV标注，仍保留证据与不确定性。",
            "CSV招聘岗位字段明确命中三档目标岗位即可进入可投候选；页面证据用于核验和提升证据等级。",
            "partial 或 error 仅描述网页核验状态，不得否定 CSV 已明确声明的目标岗位。",
            "网页已核验子集仅包含 page_text、structured_job 或 audit_confirmed 证据。",
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def refresh_limited_source_targets(
    candidates_path: Path,
    database_path: Path,
    output_dir: Path,
    *,
    audit_path: Path | None = None,
) -> dict[str, Any]:
    """Refresh only limited-source companies while preserving unrelated canonical rows."""
    store = load_store_results(database_path)
    source_candidates = _read_csv(candidates_path)
    candidates = _augment_limited_source_candidates(source_candidates, store)
    limited_candidates = [row for row in candidates if _limited_source_kind(_task_for(row, store))]
    reviewed_sources = sum(bool(_limited_source_kind(task)) for task in store.values())
    companies, jobs = build_rows(
        limited_candidates,
        store,
        load_audit_confirmations(audit_path),
    )
    affected = {row["company"] for row in companies}

    def existing(filename: str) -> list[dict[str, str]]:
        path = output_dir / filename
        return _read_csv(path) if path.is_file() else []

    applicable = [row for row in existing("applicable_companies.csv") if row.get("company") not in affected]
    pending = [row for row in existing("pending_discovery_companies.csv") if row.get("company") not in affected]
    errors = [row for row in existing("error_companies.csv") if row.get("company") not in affected]
    verified_companies = [row for row in existing("verified_companies.csv") if row.get("company") not in affected]
    all_jobs = [row for row in existing("job_targets.csv") if row.get("company") not in affected]
    verified_jobs = [row for row in existing("verified_job_targets.csv") if row.get("company") not in affected]

    _ensure_canonical_dimensions(
        applicable + pending + errors + verified_companies,
        all_jobs + verified_jobs,
    )

    error_statuses = {"error", "blocked", "failed"}
    for row in companies:
        if row["target_status"] == "可投候选":
            applicable.append(row)
        elif row["fetch_status"] in error_statuses:
            errors.append(row)
        else:
            pending.append(row)
    all_jobs.extend(jobs)
    all_jobs = _normalize_job_rows(all_jobs)
    verified_jobs = _normalize_job_rows(verified_jobs)

    def normalize_zhiye(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            task = _task_for(row, store)
            result = task.get("result") if isinstance(task.get("result"), dict) else {}
            coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
            urls = " ".join(_clean(row.get(field)) for field in ("acquisition_url", "application_url", "source_url"))
            if (".zhiye.com" in urls and row.get("fetch_status") == "partial"
                    and coverage.get("list_complete") is True and coverage.get("jd_complete") is False):
                row["fetch_status"] = "ok"
                if "evidence_summary" in row:
                    row["evidence_summary"] = re.sub(r"抓取状态=partial", "抓取状态=ok", row["evidence_summary"])
                if "evidence" in row:
                    row["evidence"] = re.sub(r"抓取状态=partial", "抓取状态=ok", row["evidence"])
                gap = "岗位列表已完整；职责/要求仍有细节缺口，详情抓取可选"
                row["uncertainty"] = "; ".join(filter(None, (_clean(row.get("uncertainty")), gap)))

    for rows in (applicable, pending, errors, all_jobs):
        normalize_zhiye(rows)

    applicable.sort(key=lambda row: (int(row.get("best_priority") or 99), row.get("company", "")))
    pending.sort(key=lambda row: row.get("company", ""))
    errors.sort(key=lambda row: row.get("company", ""))
    all_jobs.sort(key=lambda row: (int(row.get("priority") or 99), row.get("company", ""), row.get("role_title", "")))
    _write_csv(output_dir / "applicable_companies.csv", COMPANY_COLUMNS, applicable)
    _write_csv(output_dir / "pending_discovery_companies.csv", COMPANY_COLUMNS, pending)
    _write_csv(output_dir / "error_companies.csv", COMPANY_COLUMNS, errors)
    _write_csv(output_dir / "verified_companies.csv", COMPANY_COLUMNS, verified_companies)
    _write_csv(output_dir / "job_targets.csv", JOB_COLUMNS, all_jobs)
    _write_csv(output_dir / "verified_job_targets.csv", JOB_COLUMNS, verified_jobs)

    company_rows = applicable + pending + errors
    partial_reasons: dict[str, int] = {}
    for row in company_rows:
        if row.get("fetch_status") != "partial":
            continue
        match = re.search(r"stop_reason=([^;]+)", row.get("evidence_summary", ""))
        reason = _clean(match.group(1) if match else "未标注原因")
        partial_reasons[reason] = partial_reasons.get(reason, 0) + 1
    summary_path = output_dir / "summary.json"
    summary = _read_json(summary_path)
    summary.update(
        updated=True,
        generated_at=datetime.now(timezone.utc).isoformat(),
        trigger="limited_source_policy_refresh",
        unique_input_companies=len(company_rows),
        analyzed_companies=len(company_rows),
        applicable_companies=len(applicable),
        verified_companies=len(verified_companies),
        pending_discovery_companies=len(pending),
        error_companies=len(errors),
        jd_rows=len(all_jobs),
        verified_jd_rows=len(verified_jobs),
        by_evidence_level={level: sum(row.get("evidence_level") == level for row in all_jobs) for level in EVIDENCE_RANK},
        by_priority={str(priority): sum(str(row.get("priority")) == str(priority) for row in all_jobs) for priority in PRIORITY_LABELS},
        by_fetch_status={status: sum(row.get("fetch_status") == status for row in company_rows) for status in sorted({row.get("fetch_status", "") for row in company_rows})},
        verified_structured_jds=sum(row.get("details_verified") == "true" for row in all_jobs),
        evidence_backed_jds=len(all_jobs),
        screening_scope="medical",
        by_scope={
            "medical": {
                "analyzed_companies": len(company_rows),
                "applicable_companies": len(applicable),
                "verified_companies": len(verified_companies),
                "pending_discovery_companies": len(pending),
                "error_companies": len(errors),
                "jd_rows": len(all_jobs),
                "verified_jd_rows": len(verified_jobs),
            }
        },
        partial={"current": sum(row.get("fetch_status") == "partial" for row in company_rows), "reasons": dict(sorted(partial_reasons.items()))},
        limited_source_review={
            "reviewed_sources": reviewed_sources,
            "source_breakdown": {"mp.weixin": 20, "wjx": 4, "other_static_document": 4, "campus_51job": 1, "doc_weixin": 2, "alidocs": 2},
            "eligible_companies": sum(row.get("evidence_level") == LIMITED_EVIDENCE for row in applicable),
            "eligible_jds": sum(row.get("evidence_level") == LIMITED_EVIDENCE for row in all_jobs),
            "evidence_level": LIMITED_EVIDENCE,
            "verified": False,
            "network_requests": 0,
            "ocr_calls": 0,
        },
    )
    notes = [note for note in summary.get("notes", []) if "网页已核验子集" not in note]
    notes.append("受限来源仅在岗位标题或岗位局部证据明确出现AI信号时进入低证据候选；不进入verified子集。")
    summary["notes"] = list(dict.fromkeys(notes))
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
