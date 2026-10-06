import argparse
import csv
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


AI_TERMS = [
    "agent",
    "ai",
    "aigc",
    "cv",
    "llm",
    "人工智能",
    "算法",
    "计算机视觉",
    "机器视觉",
    "视觉",
    "图像",
    "图形",
    "机器学习",
    "深度学习",
    "强化学习",
    "大模型",
    "多模态",
    "自然语言",
    "nlp",
    "生成式",
    "机器人",
    "自动驾驶",
    "感知",
    "规划控制",
    "SLAM",
    "点云",
]

STRONG_ROLE_TERMS = [
    "算法工程师",
    "算法研究",
    "视觉算法",
    "计算机视觉",
    "机器视觉",
    "图像算法",
    "感知算法",
    "多模态",
    "大模型",
    "Agent",
    "AI算法",
    "AI Infra",
    "AIInfra",
    "机器学习",
    "深度学习",
    "强化学习",
]

NOISE_TERMS = [
    "营销",
    "销售",
    "柜面",
    "客户经理",
    "管培",
    "运营",
    "人力",
    "财务",
    "会计",
    "法务",
    "行政",
]

TODAY = date(2026, 10, 5)


def read_rows(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("更新时间")]


def haystack(row):
    return " ".join(
        str(row.get(col, "") or "")
        for col in ["公司名称", "行业分类", "招聘岗位", "专业要求", "公告来源"]
    )


def score_row(row):
    text = haystack(row)
    lower = text.lower()
    terms = []
    score = 0
    for term in AI_TERMS:
        if term.lower() in lower:
            terms.append(term)
            score += 1
    for term in STRONG_ROLE_TERMS:
        if term.lower() in lower:
            score += 3
    role = row.get("招聘岗位", "") or ""
    if any(term.lower() in role.lower() for term in STRONG_ROLE_TERMS):
        score += 4
    if any(term in role for term in NOISE_TERMS) and not any(
        term.lower() in role.lower() for term in STRONG_ROLE_TERMS
    ):
        score -= 2
    if (row.get("投递链接") or "").strip() and "mp.weixin.qq.com" not in (
        row.get("投递链接") or ""
    ):
        score += 1
    return score, terms


def deadline_status(value):
    value = (value or "").strip()
    if not value or value == "/":
        return "unknown"
    if "尽快" in value or "持续" in value:
        return "open"
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            d = datetime.strptime(value[:10], fmt).date()
            return "open" if d >= TODAY else "expired"
        except ValueError:
            pass
    return "unknown"


def priority_bucket(row):
    role = row.get("招聘岗位", "") or ""
    lower = role.lower()
    if any(term.lower() in lower for term in ["agent", "大模型", "多模态", "llm"]):
        return "A-Agent/大模型"
    if any(term.lower() in lower for term in ["计算机视觉", "机器视觉", "视觉算法", "图像算法", "slam", "感知算法"]):
        return "A-CV/视觉"
    if any(term.lower() in lower for term in ["算法工程师", "算法研究", "ai算法", "机器学习", "深度学习"]):
        return "B-算法"
    return "C-泛AI"


def domain(url):
    match = re.search(r"https?://([^/]+)", url or "")
    return match.group(1).lower() if match else ""


def fetch_url(url, timeout=15):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(800_000)
            ctype = resp.headers.get("content-type", "")
            status = resp.status
            final_url = resp.geturl()
    except urllib.error.HTTPError as e:
        body = e.read(200_000)
        ctype = e.headers.get("content-type", "")
        status = e.code
        final_url = e.geturl()
    except Exception as e:
        return {"ok": False, "error": repr(e)}

    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    if m:
        charset = m.group(1)
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    return {
        "ok": 200 <= status < 400,
        "status": status,
        "content_type": ctype,
        "final_url": final_url,
        "text": text,
        "bytes": len(body),
    }


def html_summary(text):
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if m:
        title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    m = re.search(r'<h1[^>]*id=["\']activity-name["\'][^>]*>(.*?)</h1>', text, re.I | re.S)
    if m:
        title = html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
        title = re.sub(r"\s+", " ", title).strip()
    desc = ""
    m = re.search(
        r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\'](.*?)["\']',
        text,
        re.I | re.S,
    )
    if m:
        desc = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    article_text_len = 0
    article_text_sample = ""
    m = re.search(r'<div[^>]*id=["\']js_content["\'][^>]*>(.*?)</div>', text, re.I | re.S)
    if m:
        fragment = re.sub(r"<script.*?</script>", " ", m.group(1), flags=re.I | re.S)
        fragment = re.sub(r"<style.*?</style>", " ", fragment, flags=re.I | re.S)
        plain = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
        plain = re.sub(r"\s+", " ", plain).strip()
        article_text_len = len(plain)
        article_text_sample = plain[:300]
    img_count = len(re.findall(r"<img\b", text, re.I))
    image_only_hint = article_text_len < 80 and img_count >= 3
    blocked_hint = any(
        s in text
        for s in ["环境异常", "访问过于频繁", "请在微信客户端打开", "verify", "captcha"]
    )
    return {
        "title": title,
        "description": desc[:300],
        "article_text_len": article_text_len,
        "article_text_sample": article_text_sample,
        "img_count": img_count,
        "image_only_hint": image_only_hint,
        "blocked_hint": blocked_hint,
    }


def write_csv(path, rows, fieldnames):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="job_market_raw.csv")
    parser.add_argument("--outdir", default="out")
    parser.add_argument("--probe", type=int, default=10)
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(exist_ok=True)

    rows = read_rows(Path(args.input))
    enriched = []
    for row in rows:
        score, terms = score_row(row)
        if terms:
            item = dict(row)
            item["匹配分"] = score
            item["匹配词"] = "、".join(sorted(set(terms)))
            item["公告域名"] = domain(row.get("公告链接", ""))
            item["投递域名"] = domain(row.get("投递链接", ""))
            enriched.append(item)
    enriched.sort(key=lambda r: int(r["匹配分"]), reverse=True)

    fields = [
        "匹配分",
        "匹配词",
        "更新时间",
        "公司名称",
        "企业性质",
        "行业分类",
        "招聘岗位",
        "工作地点",
        "截止时间",
        "届次",
        "学历要求",
        "批次",
        "公告来源",
        "公告域名",
        "公告链接",
        "投递域名",
        "投递链接",
        "专业要求",
        "是否笔试",
        "备注",
    ]
    write_csv(outdir / "ai_algorithm_candidates.csv", enriched, fields)

    shortlist = []
    seen_company = set()
    for row in enriched:
        status = deadline_status(row.get("截止时间", ""))
        if status == "expired":
            continue
        if int(row["匹配分"]) < 10:
            continue
        bucket = priority_bucket(row)
        if bucket == "C-泛AI" and int(row["匹配分"]) < 16:
            continue
        key = (row.get("公司名称", ""), row.get("投递链接", ""))
        if key in seen_company:
            continue
        seen_company.add(key)
        item = dict(row)
        item["投递状态"] = status
        item["优先方向"] = bucket
        item["投递便利度"] = (
            "direct" if item.get("投递域名") and item.get("投递域名") != "mp.weixin.qq.com" else "wechat/manual"
        )
        shortlist.append(item)
    shortlist.sort(
        key=lambda r: (
            0 if r["优先方向"].startswith("A-") else 1,
            0 if r["投递便利度"] == "direct" else 1,
            -int(r["匹配分"]),
        )
    )
    shortlist_fields = [
        "优先方向",
        "匹配分",
        "匹配词",
        "投递便利度",
        "投递状态",
        "更新时间",
        "公司名称",
        "企业性质",
        "行业分类",
        "招聘岗位",
        "工作地点",
        "截止时间",
        "届次",
        "学历要求",
        "公告链接",
        "投递链接",
        "专业要求",
    ]
    write_csv(outdir / "applyable_ai_algorithm_shortlist.csv", shortlist[:300], shortlist_fields)

    summary = {
        "valid_rows": len(rows),
        "ai_keyword_rows": len(enriched),
        "top_announcement_domains": Counter(r["公告域名"] for r in enriched).most_common(20),
        "top_apply_domains": Counter(r["投递域名"] for r in enriched).most_common(20),
        "shortlist_rows": len(shortlist),
        "top_candidates": [
            {k: r.get(k, "") for k in fields[:18]} for r in enriched[:30]
        ],
    }

    probe_rows = []
    seen = set()
    for row in enriched:
        for field in ["公告链接", "投递链接"]:
            url = (row.get(field) or "").strip()
            if not url or url in seen:
                continue
            if len(probe_rows) >= args.probe:
                break
            seen.add(url)
            result = fetch_url(url)
            item = {
                "公司名称": row.get("公司名称", ""),
                "招聘岗位": row.get("招聘岗位", ""),
                "链接字段": field,
                "url": url,
                "domain": domain(url),
                "ok": result.get("ok"),
                "status": result.get("status", ""),
                "bytes": result.get("bytes", ""),
                "final_url": result.get("final_url", ""),
                "error": result.get("error", ""),
            }
            if result.get("text"):
                item.update(html_summary(result["text"]))
            probe_rows.append(item)
            time.sleep(0.8)
        if len(probe_rows) >= args.probe:
            break

    summary["probes"] = probe_rows
    (outdir / "analysis_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    probe_fields = [
        "公司名称",
        "招聘岗位",
        "链接字段",
        "domain",
        "url",
        "ok",
        "status",
        "bytes",
        "title",
        "description",
        "article_text_len",
        "img_count",
        "image_only_hint",
        "blocked_hint",
        "article_text_sample",
        "final_url",
        "error",
    ]
    write_csv(outdir / "link_probe_sample.csv", probe_rows, probe_fields)
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
