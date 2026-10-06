"""Bounded public extraction for WJX application forms."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup


WJX_HOSTS = {"wjx.cn", "wjx.top"}
ROLE_QUESTION = re.compile(r"(?:应聘|意向|志愿).{0,12}(?:岗位|职位)|(?:岗位|职位).{0,12}(?:选择|方向)")
DETAIL_MARKERS = ("岗位职责", "任职要求", "职位职责", "职位要求", "工作职责", "任职资格")


def _is_wjx_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in WJX_HOSTS)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _save(root: Path, body: bytes) -> str:
    target = root / (hashlib.sha256(body).hexdigest() + ".html")
    target.write_bytes(body)
    return str(target.resolve())


def _option_roles(scope) -> list[str]:
    roles: list[str] = []
    for field in scope.select(".field"):
        question = field.select_one(".topichtml")
        prompt = _clean(question.get_text(" ", strip=True) if question else "")
        if not ROLE_QUESTION.search(prompt):
            continue
        for node in field.select("label, .label, .ui-radio, .ui-checkbox"):
            title = _clean(node.get_text(" ", strip=True))
            title = re.sub(r"^[A-Za-zＡ-Ｚａ-ｚ0-9一二三四五六七八九十]+[、.．]\s*", "", title)
            if title and title not in roles:
                roles.append(title)
    return roles


def _listed_roles(scope) -> list[str]:
    roles: list[str] = []
    for field in scope.select(".cutfield"):
        lines = [_clean(value) for value in field.stripped_strings]
        lines = [value for value in lines if value]
        if not lines or "岗位列表" not in lines[0]:
            continue
        for value in lines[1:]:
            if value in {"岗位详情", "岗位职责", "任职要求"}:
                break
            if len(value) <= 80 and value not in roles:
                roles.append(value)
    return roles


def _job_text(full_text: str, title: str, next_title: str | None) -> str:
    start = full_text.find(title)
    if start < 0:
        return title
    # A role can appear first in the list and later in the detailed section.
    detailed = full_text.find(title, start + len(title))
    if detailed >= 0:
        start = detailed
    end = full_text.find(next_title, start + len(title)) if next_title else -1
    value = full_text[start:end if end >= 0 else None].strip()
    return value if len(value) <= 12000 else value[:12000]


def parse_form(html: str, url: str, html_path: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = _clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    scope = soup.select_one("#divQuestion") or soup.select_one("main") or soup.body or soup
    for node in scope.select("script, style, noscript"):
        node.decompose()
    text = "\n".join(_clean(value) for value in scope.stripped_strings if _clean(value))
    roles = _listed_roles(scope)
    for role in _option_roles(scope):
        if role not in roles:
            roles.append(role)
    has_details = any(marker in text for marker in DETAIL_MARKERS)
    jobs = []
    for index, role in enumerate(roles):
        next_role = roles[index + 1] if index + 1 < len(roles) else None
        body = _job_text(text, role, next_role) if has_details else role
        jobs.append({
            "id": hashlib.sha256(f"{url}\n{role}".encode("utf-8")).hexdigest()[:24],
            "title": role,
            "url": url,
            "text": body,
            "needs_details": False,
            "raw": {"source": "wjx_public_form", "html_path": html_path, "order": index},
        })
    list_complete = bool(jobs)
    jd_complete = bool(jobs) and has_details
    complete = list_complete and jd_complete
    status = "ok" if complete else "partial" if text else "error"
    stop_reason = "static_form_complete" if complete else (
        "role_details_not_exposed" if list_complete else "roles_not_found"
    )
    warnings = []
    if list_complete and not jd_complete:
        warnings.append("The public form exposes a complete role selector but no per-role JD body")
    if not list_complete:
        warnings.append("No explicit role list was found in the public form")
    return {
        "url": url, "final_url": url, "status": status, "title": title, "text": text,
        "method": "wjx-static", "html_path": html_path, "html_paths": [html_path],
        "json_paths": [], "images": [], "links": [], "jobs": jobs,
        "evidence": [{"kind": "public_form_html", "path": html_path}],
        "warnings": warnings, "retryable": False, "error_kind": None,
        "http_status": 200,
        "coverage": {
            "pages_seen": 1, "complete": complete, "list_complete": list_complete,
            "jd_complete": jd_complete, "stop_reason": stop_reason,
            "total_reported": len(jobs) if jobs else None, "detail_urls": [],
            "pagination_applied": False, "scope": "public_application_form",
        },
    }


async def collect(url: str, artifact_dir: Path, options: dict | None = None) -> dict:
    options = dict(options or {})
    timeout = options.get("timeout", 20)
    max_html_bytes = options.get("max_html_bytes", 5 * 1024**2)
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
        raise ValueError("WJX timeout must be between 1 and 120 seconds")
    if type(max_html_bytes) is not int or not 1024 <= max_html_bytes <= 10 * 1024**2:
        raise ValueError("WJX max_html_bytes must be between 1024 and 10485760")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or not _is_wjx_host(parts.hostname):
        raise ValueError("wjx_form requires a public *.wjx.cn or *.wjx.top URL")
    root = Path(artifact_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()
        final = urlsplit(str(response.url))
        if not final.hostname or not _is_wjx_host(final.hostname):
            raise ValueError("WJX redirect left the trusted host family")
        body = response.content
    if len(body) > max_html_bytes:
        raise ValueError("WJX HTML byte limit reached")
    encoding = response.encoding or "utf-8"
    html_path = _save(root, body)
    result = parse_form(body.decode(encoding, errors="replace"), url, html_path)
    result["final_url"] = str(response.url)
    result["http_status"] = response.status_code
    result["artifact_dir"] = str(root)
    return result
