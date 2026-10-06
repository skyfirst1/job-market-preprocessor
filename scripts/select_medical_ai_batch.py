from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit


SOE = re.compile(r"央国企|国企|央企|国有|事业单位|政府")
MEDICAL = re.compile(r"医疗|医药|医学|生物|制药|药业|生命|诊断|健康|基因|影像|器械|疫苗|药物", re.I)
TARGET_LOCATION = re.compile(r"四川|成都|北京|上海|广州|全国")
ROLE_SPLIT = re.compile(r"[,，;；、\n]+")
PRIORITIES = (
    (
        1,
        "视觉相关AI/深度学习算法",
        re.compile(
            r"(?:计算机视觉|视觉算法|图像算法|图像处理|机器视觉|目标检测|图像分割|深度学习|CV算法|AI算法)",
            re.I,
        ),
    ),
    (
        2,
        "Agent/大模型开发",
        re.compile(r"Agent(?:\s*Infra)?(?:研发|开发)|智能体(?:研发|开发)|大模型(?:应用)?开发|LLM开发|RAG", re.I),
    ),
    (
        3,
        "Agent/大模型算法（次选）",
        re.compile(r"Agent算法|智能体算法|大模型(?:应用)?算法|LLM算法|NLP算法|自然语言处理", re.I),
    ),
)
MOJIBAKE_MARKERS = ("\ufffd", "Ã", "Â", "â€", "锟斤拷", "烫烫烫")


def classify_roles(text: str) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    for role in (part.strip() for part in ROLE_SPLIT.split(text or "")):
        if not role:
            continue
        for priority, label, pattern in PRIORITIES:
            if pattern.search(role) and (priority, role) not in seen:
                matches.append({"priority": priority, "category": label, "role": role})
                seen.add((priority, role))
                break
    return sorted(matches, key=lambda item: (int(item["priority"]), str(item["role"])))


def valid_url(value: str) -> bool:
    try:
        parts = urlsplit((value or "").strip())
        return parts.scheme in {"http", "https"} and bool(parts.netloc)
    except ValueError:
        return False


def is_wechat(value: str) -> bool:
    try:
        return (urlsplit(value).hostname or "").lower() == "mp.weixin.qq.com"
    except ValueError:
        return False


def choose_url(row: dict[str, str]) -> tuple[str, str]:
    delivery = (row.get("投递链接") or "").strip()
    announcement = (row.get("公告链接") or "").strip()
    for value in (delivery, announcement):
        if valid_url(value) and not is_wechat(value):
            return value, "web"
    for value in (announcement, delivery):
        if valid_url(value):
            return value, "wechat" if is_wechat(value) else "web"
    return "", "missing"


def looks_mojibake(value: str) -> bool:
    if any(marker in value for marker in MOJIBAKE_MARKERS):
        return True
    if any(0x80 <= ord(char) <= 0x9F for char in value):
        return True
    for encoding in ("latin1", "gb18030"):
        try:
            repaired = value.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired != value and any("\u4e00" <= char <= "\u9fff" for char in repaired):
            return True
    return False


def select_rows(
    source: Path,
    industry_pattern: re.Pattern[str] = MEDICAL,
    *,
    industry_only: bool = False,
) -> list[dict[str, object]]:
    selected: OrderedDict[str, dict[str, object]] = OrderedDict()
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        for source_row, row in enumerate(csv.DictReader(handle), start=2):
            company = (row.get("公司名称") or "").strip()
            nature = (row.get("企业性质") or "").strip()
            industry = (row.get("行业分类") or "").strip()
            location = (row.get("工作地点") or "").strip()
            if (
                not company
                or SOE.search(nature)
                or not industry_pattern.search(industry if industry_only else f"{industry} {company}")
                or not TARGET_LOCATION.search(location)
            ):
                continue
            roles = classify_roles(row.get("招聘岗位") or "")
            url, pool = choose_url(row)
            candidate = {
                "company": company,
                "enterprise_nature": nature or "未注明",
                "ownership_status": "非国企（CSV标注）" if nature in {"民企", "外企"} else "未发现国企标记，待核验",
                "industry": industry,
                "roles": roles,
                "best_priority": min((int(role["priority"]) for role in roles), default=None),
                "discovery_status": "CSV已有目标岗位信号" if roles else "待抓取页面发现JD",
                "announcement_url": (row.get("公告链接") or "").strip(),
                "application_url": (row.get("投递链接") or "").strip(),
                "acquisition_url": url,
                "source_pool": pool,
                "source_row": source_row,
                "graduation_year": (row.get("届次") or "").strip(),
                "location": location,
                "deadline": (row.get("截止时间") or "").strip(),
            }
            old = selected.get(company)
            candidate_sort = (candidate["best_priority"] is None, candidate["best_priority"] or 99, pool == "wechat")
            old_sort = ((old["best_priority"] is None), old["best_priority"] or 99, old["source_pool"] == "wechat") if old else None
            if old is None or candidate_sort < old_sort:
                selected[company] = candidate
    return sorted(
        selected.values(),
        key=lambda item: (
            item["best_priority"] is None,
            item["best_priority"] or 99,
            item["source_pool"] == "wechat",
            item["company"],
        ),
    )


def write_outputs(
    rows: list[dict[str, object]],
    output_dir: Path,
    *,
    screening_scope: str = "medical",
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    columns = [
        "company", "enterprise_nature", "ownership_status", "industry", "best_priority", "discovery_status",
        "matched_roles", "announcement_url", "application_url", "acquisition_url", "source_pool",
        "source_row", "graduation_year", "location", "deadline",
    ]
    flattened = []
    for row in rows:
        flat = dict(row)
        flat["matched_roles"] = json.dumps(row["roles"], ensure_ascii=False)
        flat.pop("roles")
        flattened.append(flat)
    with (output_dir / "first_batch_candidates.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(flattened)
    for pool in ("web", "wechat"):
        with (output_dir / f"{pool}_candidates.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(row for row in flattened if row["source_pool"] == pool)
        with (output_dir / f"{pool}_queue.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                if row["source_pool"] == pool and row["acquisition_url"]:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "screening_scope": screening_scope,
        "companies": len(rows),
        "by_priority": {str(p): sum(row["best_priority"] == p for row in rows) for p in (1, 2, 3)},
        "awaiting_page_discovery": sum(row["best_priority"] is None for row in rows),
        "by_pool": {pool: sum(row["source_pool"] == pool for row in rows) for pool in ("web", "wechat", "missing")},
        "stage_complete": True,
        "analysis_trigger": "stage_complete" if len(rows) < 20 else "20_companies",
    }
    (output_dir / "batch_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def audit_output(source: Path, candidate_path: Path, expected_count: int | None = None) -> dict[str, int]:
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        canonical = list(csv.DictReader(handle))
    with candidate_path.open("r", encoding="utf-8-sig", newline="") as handle:
        candidates = list(csv.DictReader(handle))
    if expected_count is not None and len(candidates) != expected_count:
        raise ValueError(f"candidate count changed: expected {expected_count}, got {len(candidates)}")
    seen = set()
    for candidate in candidates:
        source_row = int(candidate["source_row"])
        if source_row < 2 or source_row - 2 >= len(canonical):
            raise ValueError(f"invalid source_row: {source_row}")
        raw = canonical[source_row - 2]
        expected = (raw.get("公司名称") or "").strip()
        actual = (candidate.get("company") or "").strip()
        if not expected or actual != expected:
            raise ValueError(f"company mismatch at source_row {source_row}")
        if looks_mojibake(actual):
            raise ValueError(f"company mojibake at source_row {source_row}")
        if candidate.get("announcement_url", "") != (raw.get("公告链接") or "").strip():
            raise ValueError(f"announcement URL mismatch at source_row {source_row}")
        if candidate.get("application_url", "") != (raw.get("投递链接") or "").strip():
            raise ValueError(f"application URL mismatch at source_row {source_row}")
        expected_acquisition, _ = choose_url(raw)
        if candidate.get("acquisition_url", "") != expected_acquisition:
            raise ValueError(f"acquisition URL mismatch at source_row {source_row}")
        if actual in seen:
            raise ValueError(f"duplicate company: {actual}")
        seen.add(actual)
    return {"companies": len(candidates), "canonical_rows_checked": len(candidates),
            "mojibake_names": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description="按行业筛选非国企 AI 岗位候选公司。")
    parser.add_argument("--input", type=Path, default=Path("job_market_raw.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/first_batch"))
    parser.add_argument("--limit", type=int, default=20, help="本批公司上限；0 表示导出全部候选。")
    parser.add_argument("--screening-scope", default="medical", help="稳定的行业筛选标识。")
    parser.add_argument("--industry-pattern", default=MEDICAL.pattern, help="行业筛选正则。")
    parser.add_argument(
        "--industry-only",
        action="store_true",
        help="只在行业分类中匹配；默认兼容旧医疗逻辑，也会参考公司名。",
    )
    args = parser.parse_args()
    if not args.screening_scope.strip():
        parser.error("--screening-scope must not be blank")
    try:
        industry_pattern = re.compile(args.industry_pattern, re.I)
    except re.error as exc:
        parser.error(f"invalid --industry-pattern: {exc}")
    rows = select_rows(args.input, industry_pattern, industry_only=args.industry_only)
    selected = rows if args.limit == 0 else rows[: args.limit]
    write_outputs(selected, args.output_dir, screening_scope=args.screening_scope.strip())
    audit = audit_output(args.input, args.output_dir / "first_batch_candidates.csv",
                         expected_count=len(selected))
    sample_size = min(12, len(selected))
    sample = random.Random(20261006).sample([str(row["company"]) for row in selected], sample_size)
    summary_path = args.output_dir / "batch_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["encoding_audit"] = {
        **audit,
        "canonical_source": str(args.input),
        "random_seed": 20261006,
        "sampled_companies": sample,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output_dir": str(args.output_dir), "companies": len(selected),
                      "total_candidates": len(rows), "audit": audit,
                      "sampled_companies": sample}, ensure_ascii=False))


if __name__ == "__main__":
    main()
