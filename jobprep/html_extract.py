"""DOM evidence extraction shared by HTTP and browser collection."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag


JOB_RE = re.compile(r"job|position|vacanc|career|recruit|engineer|scientist|developer|analyst|招聘|职位|岗位|工程师|算法|开发|研发|研究员", re.I)
NAV_LABELS = {"careers", "career", "jobs", "job", "positions", "recruitment", "search jobs", "view all jobs", "招聘", "招聘职位", "职位", "岗位", "社会招聘", "校园招聘", "加入我们", "全部职位"}
NEXT_RE = re.compile(r"^(?:next(?:\s+page)?|下一页|下页|后一页|›|»|>)$", re.I)
DELETED_RE = re.compile(r"该内容已被发布者删除|此内容因违规无法查看|内容已删除|该文章已被删除|the (?:article|page) (?:has been|was) (?:deleted|removed)", re.I)
BLOCKED_RE = re.compile(r"访问过于频繁|环境异常|请完成安全验证|请进行验证|安全验证|人机验证|拖动.*滑块|请登录后查看|内容暂时无法访问|请在微信客户端打开|verify you are human|checking your browser|just a moment|access denied|please (?:log|sign) in to (?:continue|view)", re.I)


def absolute_url(value: str, base: str) -> str | None:
    """Resolve real web URLs without discarding SPA fragments."""
    value = (value or "").strip()
    if not value or value == "#" or value.lower().startswith(("javascript:", "mailto:", "tel:", "data:")):
        return None
    result = urljoin(base, value)
    return result if urlsplit(result).scheme in {"http", "https"} else None


def is_detail_url(url: str) -> bool:
    """Recognize observed job routes, excluding common listing/navigation routes."""
    if re.search(r"(?:job|position|vacancy)(?:Id|_id|id)=", url, re.I):
        return True
    if re.search(r"(?:job|position|vacancy|recruitment)[-_/](?:detail|view)(?:[/?.#=]|$)", url, re.I):
        return True
    match = re.search(r"(?:job|position|vacancy)s?/([^/?#]+)", url, re.I)
    if not match:
        return False
    segment = match.group(1).lower()
    return segment not in {"list", "listing", "search", "all", "index", "home", "openings", "categories", "category", "campus", "social", "apply", "application"} and bool(re.search(r"\d", segment) or "-" in segment)


def _disabled(tag: Tag) -> bool:
    classes = " ".join(tag.get("class", []))
    return tag.has_attr("disabled") or str(tag.get("aria-disabled", "")).lower() == "true" or bool(re.search(r"(?:^|[\s_-])disabled(?:$|[\s_-])", classes))


def _image_url(tag: Tag, base: str) -> str | None:
    for key in ("data-src", "data-original", "data-lazy-src"):
        resolved = absolute_url(str(tag.get(key, "")), base)
        if resolved:
            return resolved
    candidates = []
    for candidate in str(tag.get("srcset", tag.get("data-srcset", ""))).split(","):
        parts = candidate.strip().split()
        if parts:
            descriptor = parts[1] if len(parts) > 1 else "1x"
            try:
                weight = float(descriptor.rstrip("wx"))
            except ValueError:
                weight = 0
            candidates.append((weight, parts[0]))
    for _, candidate in sorted(candidates, reverse=True):
        resolved = absolute_url(candidate, base)
        if resolved:
            return resolved
    return absolute_url(str(tag.get("src", "")), base)


def extract_html(html_text: str | bytes, url: str, http_status: int = 200) -> dict:
    """Return JSON-safe evidence plus collection hints; never execute page scripts."""
    soup = BeautifulSoup(html_text, "html.parser")
    # Capture framework markers before scripts are removed. Some recruitment
    # portals return a short shell whose literal closing-tag text otherwise
    # looks like a valid static document.
    spa_mount = soup.select_one("#app, #root, #__next, [data-reactroot]")
    script_src_count = len(soup.select("script[src]"))
    article = soup.select_one("#js_content") or soup.find("article")
    title_node = soup.select_one("#activity-name") or soup.find("title") or soup.find("h1")
    title = title_node.get_text(" ", strip=True) if title_node else ""
    challenge_node = soup.select_one("#js_verify, #captcha, .captcha-container, #challenge-form, #cf-challenge-running, .weui-msg__title, .weui-msg__desc")
    challenge_text = challenge_node.get_text(" ", strip=True) if challenge_node else ""
    challenge_marker = soup.select_one("#js_verify, #challenge-form, #cf-challenge-running") is not None
    login_form = soup.select_one("form input[type='password']") is not None
    for tag in soup.select("script, style, noscript, template, [hidden], [aria-hidden='true']"):
        if tag is not article:
            tag.decompose()
    for tag in list(soup.select("[style]")):
        if tag is not article and re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", tag.get("style", ""), re.I):
            tag.decompose()
    visible = soup.get_text(" ", strip=True)
    status = "ok"
    error = None
    if http_status in {404, 410} or (DELETED_RE.search(visible) and (article is None or len(visible) < 500)):
        status, error = "deleted", "Page is deleted or unavailable"
    elif http_status in {401, 403, 429} or (BLOCKED_RE.search(title + " " + challenge_text) and len(visible) < 500) or (article is None and len(visible) < 500 and (BLOCKED_RE.search(visible) or challenge_marker or login_form)):
        status, error = "blocked", "Authentication or page challenge requires manual access"
    elif http_status >= 400:
        status, error = "error", f"HTTP {http_status}"

    root = article or soup.find("article") or soup.find("main") or soup.body or soup
    evidence = []
    image_urls = []
    for node in root.descendants:
        if isinstance(node, NavigableString) and not isinstance(node, Comment):
            value = " ".join(str(node).split())
            if value:
                evidence.append({"kind": "text", "text": value, "order": len(evidence)})
        elif isinstance(node, Tag) and node.name == "img":
            source = _image_url(node, url)
            if source:
                image_urls.append({"url": source, "order": len(evidence)})
                evidence.append({"kind": "image", "url": source, "order": len(evidence)})
    text = "\n".join(item["text"] for item in evidence if item["kind"] == "text")
    links, jobs, seen = [], [], set()
    terminal = False
    has_next = False
    for tag in soup.select("a[href], button"):
        label = tag.get_text(" ", strip=True) or str(tag.get("aria-label", ""))
        rel = tag.get("rel", [])
        pagination = "next" in rel or bool(NEXT_RE.fullmatch(label.strip()))
        if pagination:
            terminal |= _disabled(tag)
            has_next |= not _disabled(tag)
        target = absolute_url(str(tag.get("href", "")), url)
        if not target or target in seen:
            continue
        seen.add(target)
        parts = urlsplit(target)
        route = parts.path + " " + parts.query + " " + parts.fragment
        kind = "pagination" if pagination else "job" if JOB_RE.search(route + " " + label) else "other"
        links.append({"url": target, "text": label, "kind": kind})
        parent = tag.find_parent(["li", "tr", "article"])
        entry = parent is not None and tag.find_parent(["nav", "header", "footer"]) is None and label.lower().strip() not in NAV_LABELS and bool(JOB_RE.search(label))
        entry |= any(tag.has_attr(key) for key in ("data-job-id", "data-position-id"))
        if kind == "job" and (is_detail_url(target) or entry):
            context = parent.get_text(" ", strip=True) if parent else label
            jobs.append({"id": hashlib.sha256(target.encode()).hexdigest()[:24], "title": label, "url": target, "text": context, "raw": {"href": str(tag.get("href", "")), "source": "dom"}})
    total_match = re.search(r"(?:共(?:计)?\s*|total\s*[:：]?\s*)([\d,]+)\s*(?:个|条|项|jobs?|positions?|职位|岗位)", visible, re.I)
    total = int(total_match.group(1).replace(",", "")) if total_match else None
    terminal |= any(re.fullmatch(r"(?:没有更多|暂无更多|已全部加载|no more (?:jobs|results)|end of results)[。.!\s]*", line, re.I) for line in text.splitlines())
    empty = not text and not image_urls
    shell_text = re.sub(r"[\s<>/!-]+", "", text).lower()
    spa_shell = (
        not jobs
        and article is None
        and (
            (spa_mount is not None and len(text) < 500)
            or (script_src_count > 0 and len(text) < 160)
            or ("projectconfigstart" in shell_text and len(text) < 500)
        )
    )
    if status == "ok" and empty:
        status = "partial"
    details = list(dict.fromkeys([item["url"] for item in jobs] + [item["url"] for item in links if is_detail_url(item["url"])]))
    document = not jobs and not has_next and not empty and not spa_shell
    complete = status == "ok" and document and not details and not image_urls
    if status == "ok" and not complete:
        status = "partial"
    warnings = []
    if image_urls:
        warnings.append("Images are discovered but not downloaded by HTML extraction")
    if details:
        warnings.append("Job detail URLs require separate collection")
    return {"url": url, "final_url": url, "method": "http", "html_path": "",
            "images": [{"url": item["url"], "path": "", "sha256": "", "width": None, "height": None,
                        "status": "pending", "error": None, "order": item["order"]} for item in image_urls],
            "coverage": {"pages_seen": 1, "complete": complete, "list_complete": bool(terminal and not has_next and status in {"ok", "partial"}),
                         "stop_reason": status if status in {"blocked", "deleted", "error"} else "static_document" if document else "unknown_pagination",
                         "search_terms": [], "search_applied": None, "search_scope": "source", "searches": [],
                         "total_reported": total, "detail_urls": details},
            "warnings": warnings, "title": title, "text": text, "status": status, "error": error,
            "image_urls": image_urls, "links": links, "jobs": jobs, "evidence": evidence,
            "is_article": article is not None, "has_next": has_next,
            "terminal": terminal and not has_next, "total_reported": total,
            "requires_browser": empty or spa_shell or (article is None and root.name != "article" and len(text) < 100 and not jobs)}
