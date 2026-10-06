from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Iterable, Mapping, Sequence
import uuid


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Replace a text artifact atomically in its destination directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(content, encoding=encoding)
    os.replace(temporary, path)


def read_json_object(path: Path) -> dict:
    """Read a JSON object, returning an empty object for absent or invalid files."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json_atomic(
    path: Path,
    value: object,
    *,
    ensure_ascii: bool = False,
    trailing_newline: bool = False,
) -> None:
    content = json.dumps(value, ensure_ascii=ensure_ascii, indent=2)
    atomic_write_text(path, content + ("\n" if trailing_newline else ""))


def write_csv_atomic(
    path: Path,
    rows: Iterable[Mapping[str, object]],
    fieldnames: Sequence[str],
) -> None:
    """Write a UTF-8 BOM CSV atomically for Excel and dashboard consumers."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
