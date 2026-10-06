"""Read the private Markdown application log for local result de-duplication."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import urlsplit


PENDING_MARKERS = ("还没投", "尚未投", "未投", "准备投", "待投", "还没开")
CORPORATE_SUFFIXES = (
    "股份有限公司", "有限责任公司", "有限公司", "集团", "医疗", "科技",
)


@dataclass(frozen=True)
class ApplicationRecord:
    company: str
    status: str
    urls: tuple[str, ...] = ()

    @property
    def applied(self) -> bool:
        status = self.status.replace(" ", "")
        return not any(marker in status for marker in PENDING_MARKERS)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def read_application_history(path: Path) -> list[ApplicationRecord]:
    if not path.is_file():
        return []
    records: list[ApplicationRecord] = []
    company_index = status_index = None
    url_indices: list[int] = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = _cells(line)
        if company_index is None:
            lowered = [cell.casefold() for cell in cells]
            if "company" not in lowered and "公司" not in cells:
                continue
            company_index = lowered.index("company") if "company" in lowered else cells.index("公司")
            status_index = cells.index("状态") if "状态" in cells else None
            url_indices = [
                index for index, name in enumerate(lowered)
                if name in {"index", "url", "link", "链接", "网址", "投递链接"}
            ]
            continue
        if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
            continue
        if company_index >= len(cells):
            continue
        company = cells[company_index].strip()
        if not company:
            continue
        status = cells[status_index].strip() if status_index is not None and status_index < len(cells) else ""
        urls = tuple(
            match.group(0).rstrip(">),，。")
            for index in url_indices
            if index < len(cells)
            for match in re.finditer(r"https?://[^\s\])]+", cells[index])
        )
        records.append(ApplicationRecord(company=company, status=status, urls=urls))
    return records


def _company_tokens(value: str) -> set[str]:
    parenthetical = re.findall(r"[（(]([^）)]*)[）)]", value)
    value = re.sub(r"[（(][^）)]*[）)]", "", value).casefold()
    parts = re.split(r"[、·•,，/／;；|&＆+＋]+", value)
    parts.extend(parenthetical)
    tokens: set[str] = set()
    for part in parts:
        token = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", part.casefold())
        changed = True
        while changed and token:
            changed = False
            for suffix in CORPORATE_SUFFIXES:
                if token.endswith(suffix) and len(token) > len(suffix) + 1:
                    token = token[: -len(suffix)]
                    changed = True
                    break
        if len(token) >= 2:
            tokens.add(token)
    return tokens


def applied_company_tokens(path: Path) -> set[str]:
    return {
        token
        for record in read_application_history(path)
        if record.applied
        for token in _company_tokens(record.company)
    }


def company_was_applied(company: str, applied_tokens: set[str]) -> bool:
    for candidate in _company_tokens(company):
        for recorded in applied_tokens:
            if candidate == recorded:
                return True
            if min(len(candidate), len(recorded)) >= 2 and (
                candidate.startswith(recorded) or recorded.startswith(candidate)
            ):
                return True
            if len(recorded) >= 3 and recorded in candidate:
                return True
    return False


_SHARED_HOSTS = {
    "app.mokahr.com", "mp.weixin.qq.com", "wecruit.hotjob.cn",
    "jobs.51job.com", "campus.51job.com", "m.liepin.com", "www.liepin.com",
    "m.zhaopin.com", "q.yingjiesheng.com",
}


def _url_keys(value: str) -> set[str]:
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return set()
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if not host:
        return set()
    path = re.sub(r"/+", "/", parsed.path).rstrip("/").casefold()
    keys = {f"exact:{host}{path}"}
    segments = [segment for segment in path.split("/") if segment]
    if host == "app.mokahr.com":
        for marker in ("campus_apply", "campus-recruitment", "social-recruitment"):
            if marker in segments and segments.index(marker) + 1 < len(segments):
                keys.add(f"tenant:mokahr:{segments[segments.index(marker) + 1]}")
    elif host == "wecruit.hotjob.cn" and segments:
        keys.add(f"tenant:hotjob:{segments[0]}")
    elif host not in _SHARED_HOSTS:
        keys.add(f"host:{host}")
    return keys


def applied_url_keys(path: Path) -> set[str]:
    return {
        key
        for record in read_application_history(path)
        if record.applied
        for url in record.urls
        for key in _url_keys(url)
    }


def url_was_applied(url: str, applied_keys: set[str]) -> bool:
    return bool(_url_keys(url) & applied_keys)
