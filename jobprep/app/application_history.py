"""Read the private Markdown application log for local result de-duplication."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


PENDING_MARKERS = ("还没投", "尚未投", "未投", "准备投", "待投", "还没开")
CORPORATE_SUFFIXES = (
    "股份有限公司", "有限责任公司", "有限公司", "集团", "医疗", "科技",
)


@dataclass(frozen=True)
class ApplicationRecord:
    company: str
    status: str

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
            continue
        if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
            continue
        if company_index >= len(cells):
            continue
        company = cells[company_index].strip()
        if not company:
            continue
        status = cells[status_index].strip() if status_index is not None and status_index < len(cells) else ""
        records.append(ApplicationRecord(company=company, status=status))
    return records


def _company_tokens(value: str) -> set[str]:
    value = re.sub(r"[（(][^）)]*[）)]", "", value).casefold()
    parts = re.split(r"[、·•,，/／]+", value)
    tokens: set[str] = set()
    for part in parts:
        token = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", part)
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
    return False
