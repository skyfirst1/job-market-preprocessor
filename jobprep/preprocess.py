"""Build a derived review view without changing collection/audit evidence."""

from __future__ import annotations

from copy import deepcopy
import re

from bs4 import BeautifulSoup, Comment
from readability import Document


_HTML = re.compile(r"</?[a-zA-Z][^>]*>")
_PLACEHOLDER = re.compile(r"详见|岗位意向|待补充|待确认|to be confirmed", re.I)
_SIGNALS = {
    "recruitment": r"招聘|岗位|职位|工程师|招聘要求|job|vacancy",
    "ai": r"大模型|人工智能|算法|机器学习|\bAI\b|\bagent\b",
    "access_challenge": r"请登录|安全验证|环境异常|访问过于频繁|微信客户端|captcha|access denied",
}
_FIELDS = {
    "title": ("jobName", "jobNameNew", "positionName", "招聘岗位"),
    "description": ("jobDesc", "mainBusiness", "jobDescription", "description", "岗位职责", "工作职责"),
    "requirements": ("jobRequire", "jobRequirements", "requirements", "任职要求", "岗位要求", "专业要求"),
    "location": ("workPlace", "workArea", "cityName", "工作地点"),
    "department": ("deptName", "firstDeptName", "部门"),
    "company": ("companyName", "公司名称"),
}


def _clean(value: object, *, article: bool = False) -> tuple[str, bool, bool]:
    """Use readability for documents, structural cleanup for short job fields."""
    if not isinstance(value, str):
        return "", False, False
    value = value.strip()
    if not _HTML.search(value):
        return "\n".join(" ".join(line.split()) for line in value.splitlines() if line.strip()), False, False
    soup = BeautifulSoup(value, "html.parser")
    removed = False
    for node in soup.find_all(string=lambda node: isinstance(node, Comment)):
        node.extract()
        removed = True
    for node in list(soup.select("script, style, noscript, template, nav, header, footer, [hidden], [aria-hidden='true']")):
        node.decompose()
        removed = True
    for node in list(soup.select("[style]")):
        if re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", str(node.get("style")), re.I):
            node.decompose()
            removed = True
    root = soup.select_one("#js_content, article, main") or soup.body or soup
    fallback = root.get_text("\n", strip=True)
    uncertain = False
    if article and fallback:
        try:
            extracted = BeautifulSoup(Document(str(soup)).summary(), "html.parser").get_text("\n", strip=True)
            # Short lists and explicit article roots can be damaged by article scoring.
            if len(extracted) < 80 or (root is not soup and root is not soup.body and extracted != fallback):
                uncertain = True
            else:
                removed |= extracted != fallback
                fallback = extracted
        except Exception:
            uncertain = True
    return _clean(fallback)[0], removed, uncertain


def preprocess_document(result: dict) -> dict:
    """Return compact_text/jobs/signals/quality; no I/O, fetches or eligibility verdicts.

    Accepts collector results and codex_review packets. HTML must be supplied in
    html/html_text; audit file paths are references only and are never opened.
    """
    removed = False
    extraction_uncertain = False
    texts = []
    evidence_refs = []
    dedup_events = []
    html_key = next((key for key in ("html_text", "html") if result.get(key)), None)
    html_reliable = False
    source_text_retained_due_fallback = False
    if html_key:
        text, cleaned, uncertain = _clean(result[html_key], article=True)
        removed |= cleaned
        extraction_uncertain |= uncertain
        html_reliable = len(text) >= 80 and not uncertain
        evidence_refs.append({"field": html_key, "kind": "html", "reliable": html_reliable})
        texts.append((text, evidence_refs[-1]))
        if not html_reliable:
            extraction_uncertain = True
            source_text_retained_due_fallback = bool(result.get("text"))
    for key in ("text", "ocr_text"):
        if key == "text" and html_reliable:
            continue
        if result.get(key):
            text, cleaned, uncertain = _clean(result[key])
            removed |= cleaned
            extraction_uncertain |= uncertain
            evidence_refs.append({"field": key, "kind": "ocr" if key == "ocr_text" else "html"})
            texts.append((text, evidence_refs[-1]))
    for collection in ("evidence", "ocr"):
        for index, item in enumerate(result.get(collection) or []):
            if isinstance(item, dict):
                if collection == "evidence" and item.get("kind") == "html" and html_reliable:
                    evidence_refs.append({"field": collection, "kind": "html", "index": index,
                                          "id": item.get("id"), "source_url": item.get("source_url"),
                                          "skip_reason": "superseded_by_reliable_html",
                                          "superseded_by": html_key})
                    continue
                value = item.get("text") or ""
                line_refs = []
                for line_index, line in enumerate(item.get("lines") or []):
                    line_text = line.get("text", "") if isinstance(line, dict) else line
                    if isinstance(line_text, str) and line_text.strip():
                        line_refs.append(line_index)
                        if line_text.strip() not in value.splitlines():
                            value += "\n" + line_text
                if not value:
                    continue
                if collection == "evidence" and item.get("kind") == "html" and html_key and not html_reliable:
                    source_text_retained_due_fallback = True
                text, cleaned, _ = _clean(value)
                removed |= cleaned
                evidence_refs.append({"field": collection, "kind": item.get("kind", collection),
                                      "index": index, "id": item.get("id"), "line_indices": line_refs,
                                      "image_index": item.get("image_index"),
                                      "image_sha256": item.get("image_sha256")})
                texts.append((text, evidence_refs[-1]))

    jobs = []
    seen_jobs = {}
    duplicate_jobs = 0
    for index, original in enumerate(result.get("jobs") or []):
        if not isinstance(original, dict):
            continue
        raw = original.get("raw") if isinstance(original.get("raw"), dict) else {}
        job = {"id": original.get("id"), "url": original.get("url", ""),
               "source_ids": [original["id"]] if original.get("id") is not None else [],
               "raw_job_id": raw.get("jobId"), "field_sources": {}, "evidence_sources": [],
               "needs_confirmation": False}
        for field, aliases in {**_FIELDS, "text": ()}.items():
            candidates = [(f"jobs[{index}].{field}", original.get(field))]
            candidates += [(f"jobs[{index}].raw.{key}", raw.get(key)) for key in aliases]
            values = []
            sources = []
            for path, value in candidates:
                clean, cleaned, _ = _clean(value)
                removed |= cleaned
                if clean:
                    sources.append(path)
                    if clean not in values:
                        values.append(clean)
            job[field] = "\n".join(values)
            if sources:
                job["field_sources"][field] = sources
        job["needs_confirmation"] = (not job["description"] or not job["requirements"] or
                                       bool(_PLACEHOLDER.search(job["description"] + job["requirements"])))
        job["needs_details"] = job["needs_confirmation"]
        job["evidence_sources"].append({"job_index": index, "original_job_id": original.get("id"),
                                        "kind": "json" if raw and raw.get("source") != "dom" else "html",
                                        "raw_job_id": raw.get("jobId"),
                                        "source_url": result.get("source_url") or result.get("final_url") or result.get("url"),
                                        "raw_json_reference": "provenance.raw_json_paths"})
        # No reliable identity means no merge, even for identical titles/text.
        identity = (original.get("id"), job["raw_job_id"], job["url"])
        key = (*identity, *(job[f] for f in (*_FIELDS, "text")))
        if any(value is not None and value != "" for value in identity) and key in seen_jobs:
            existing = seen_jobs[key]
            existing["source_ids"] = list(dict.fromkeys(existing["source_ids"] + job["source_ids"]))
            existing["evidence_sources"].extend(job["evidence_sources"])
            for field, sources in job["field_sources"].items():
                existing["field_sources"].setdefault(field, []).extend(sources)
            duplicate_jobs += 1
            dedup_events.append({"kind": "job", "removed_job_index": index,
                                 "retained_job_index": existing["evidence_sources"][0]["job_index"]})
        else:
            seen_jobs[key] = job
            jobs.append(job)

    lines = []
    duplicate_lines = 0
    for text, source in texts:
        seen_lines = {}
        for line_index, line in enumerate(text.splitlines()):
            if len(line) >= 20 and line in seen_lines:
                duplicate_lines += 1
                dedup_events.append({"kind": "line", "source": deepcopy(source),
                                     "removed_line_index": line_index, "retained_line_index": seen_lines[line]})
                continue
            seen_lines[line] = line_index
            lines.append(line)
    body = "\n".join(lines)
    compact_parts = [body] if body else []
    for job in jobs:
        compact_parts.append("\n".join(f"{field}: {job[field]}" for field in (*_FIELDS, "text") if job[field]))
    compact_text = "\n\n".join(part for part in compact_parts if part)
    coverage = result.get("coverage") or {}
    refs = result.get("source_references") or []
    signal_text = compact_text + "\n" + "\n".join(
        str(ref.get("raw", {}).get("公司名称", "")) for ref in refs if isinstance(ref, dict))
    signals = {key: sorted({m.group(0) for m in re.finditer(pattern, signal_text, re.I)})
               for key, pattern in _SIGNALS.items()}
    signals["keyword_matches_are_not_exclusions"] = True
    insufficient = len(body) < 80
    reasons = []
    if insufficient:
        reasons.append("insufficient_body")
    if extraction_uncertain:
        reasons.append("article_extraction_fallback")
    if source_text_retained_due_fallback:
        reasons.append("source_text_retained_due_fallback")
    if any(job["needs_confirmation"] for job in jobs):
        reasons.append("missing_or_placeholder_job_details")
    if coverage.get("complete") is not True:
        reasons.append("coverage_unconfirmed")
    if coverage.get("ocr_gaps") or result.get("ocr_gaps") or coverage.get("unresolved_details"):
        reasons.append("unresolved_evidence")
    if result.get("status") in {"blocked", "deleted", "error", "partial", "pending_ocr"} or signals["access_challenge"]:
        reasons.append("acquisition_needs_confirmation")
    return {"compact_text": compact_text, "jobs": jobs, "signals": signals,
            "quality": {"boilerplate_removed": removed, "dedup_count": duplicate_jobs + duplicate_lines,
                        "changes": [name for name, changed in (("boilerplate_removed", removed),
                                    ("jobs_deduplicated", duplicate_jobs), ("lines_deduplicated", duplicate_lines)) if changed],
                        "semantic_verified": False,
                        "job_dedup_count": duplicate_jobs, "line_dedup_count": duplicate_lines,
                        "body_insufficient": insufficient, "needs_confirmation": bool(reasons),
                        "needs_details": any(job["needs_details"] for job in jobs),
                        "confirmation_reasons": reasons, "no_jobs_proven": False,
                        "input_job_count": len(result.get("jobs") or []), "body_chars": len(body),
                        "compact_chars": len(compact_text)},
            "provenance": {"task_id": result.get("task_id"), "evidence_sources": evidence_refs,
                           "source_text_retained_due_fallback": source_text_retained_due_fallback,
                           "source_text_superseded_by_html": bool(html_reliable and result.get("text")),
                           "dedup_events": dedup_events,
                           "source_references": [{"source_id": ref.get("source_id"), "row_number": ref.get("row_number"),
                                                  "field": ref.get("field"), "company": ref.get("raw", {}).get("公司名称")}
                                                 for ref in refs if isinstance(ref, dict)],
                           "coverage": deepcopy(coverage),
                           "warnings": deepcopy(result.get("warnings", [])),
                           "raw_html_path": result.get("raw_html_path", result.get("html_path")),
                           "raw_html_paths": deepcopy(result.get("raw_html_paths", result.get("html_paths", []))),
                           "raw_json_paths": deepcopy(result.get("raw_json_paths", result.get("json_paths", [])))}}
