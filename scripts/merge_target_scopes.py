"""Merge canonical target directories without losing their screening scope."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.analysis.application_targets import COMPANY_COLUMNS, JOB_COLUMNS


COMPANY_FILES = (
    "applicable_companies.csv",
    "verified_companies.csv",
    "pending_discovery_companies.csv",
    "error_companies.csv",
)
JOB_FILES = ("job_targets.csv", "verified_job_targets.csv")


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, columns: list[str], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _key(row: dict[str, Any], id_field: str) -> tuple[str, str]:
    return (str(row.get("screening_scope") or "").strip(), str(row.get(id_field) or "").strip())


def _dedupe(rows: Iterable[dict[str, str]], id_field: str) -> list[dict[str, str]]:
    selected: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = _key(row, id_field)
        if not all(key):
            raise ValueError(f"canonical row is missing screening_scope or {id_field}: {row}")
        current = selected.get(key)
        if current is None or sum(bool(value) for value in row.values()) > sum(bool(value) for value in current.values()):
            selected[key] = row
    return sorted(selected.values(), key=lambda row: (_key(row, id_field), row.get("company", "")))


def _validate_output(inputs: Iterable[Path], output_dir: Path) -> None:
    output = output_dir.resolve()
    for input_dir in inputs:
        source = input_dir.resolve()
        if output == source or source in output.parents or output in source.parents:
            raise ValueError(f"output directory must be independent of input directory: {source}")


def merge_target_scopes(scopes: dict[str, Path], output_dir: Path) -> dict[str, Any]:
    if not scopes:
        raise ValueError("at least one screening scope is required")
    normalized_scopes = {scope.strip(): Path(path) for scope, path in scopes.items()}
    if any(not scope for scope in normalized_scopes):
        raise ValueError("screening scope must not be blank")
    _validate_output(normalized_scopes.values(), output_dir)

    merged: dict[str, list[dict[str, str]]] = {name: [] for name in COMPANY_FILES + JOB_FILES}
    for scope, source_dir in normalized_scopes.items():
        if not source_dir.is_dir():
            raise FileNotFoundError(source_dir)
        for filename in merged:
            for source in _read_csv(source_dir / filename):
                existing_scope = str(source.get("screening_scope") or "").strip()
                if existing_scope and existing_scope != scope:
                    raise ValueError(
                        f"{source_dir / filename} contains scope {existing_scope!r}, expected {scope!r}"
                    )
                row = dict(source)
                row["screening_scope"] = scope
                merged[filename].append(row)

    company_rows = [row for filename in COMPANY_FILES for row in merged[filename]]
    industry_by_company = {
        _key(row, "company_id"): str(row.get("industry") or "").strip()
        for row in company_rows
        if all(_key(row, "company_id"))
    }
    for filename in JOB_FILES:
        for row in merged[filename]:
            row["industry"] = str(row.get("industry") or "").strip() or industry_by_company.get(
                _key(row, "company_id"), ""
            )

    for filename in COMPANY_FILES:
        merged[filename] = _dedupe(merged[filename], "company_id")
    for filename in JOB_FILES:
        merged[filename] = _dedupe(merged[filename], "jd_id")

    applicable_keys = {_key(row, "company_id") for row in merged["applicable_companies.csv"]}
    verified_company_keys = {_key(row, "company_id") for row in merged["verified_companies.csv"]}
    job_keys = {_key(row, "jd_id") for row in merged["job_targets.csv"]}
    verified_job_keys = {_key(row, "jd_id") for row in merged["verified_job_targets.csv"]}
    if not verified_company_keys <= applicable_keys:
        raise ValueError("verified_companies must be a subset of applicable_companies by scope and company_id")
    if not verified_job_keys <= job_keys:
        raise ValueError("verified_job_targets must be a subset of job_targets by scope and jd_id")

    by_scope: dict[str, dict[str, int]] = {}
    for scope in normalized_scopes:
        in_scope = lambda rows: [row for row in rows if row["screening_scope"] == scope]
        analyzed_keys = {
            _key(row, "company_id")
            for filename in ("applicable_companies.csv", "pending_discovery_companies.csv", "error_companies.csv")
            for row in in_scope(merged[filename])
        }
        by_scope[scope] = {
            "analyzed_companies": len(analyzed_keys),
            "applicable_companies": len(in_scope(merged["applicable_companies.csv"])),
            "verified_companies": len(in_scope(merged["verified_companies.csv"])),
            "pending_discovery_companies": len(in_scope(merged["pending_discovery_companies.csv"])),
            "error_companies": len(in_scope(merged["error_companies.csv"])),
            "jd_rows": len(in_scope(merged["job_targets.csv"])),
            "verified_jd_rows": len(in_scope(merged["verified_job_targets.csv"])),
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    for filename in COMPANY_FILES:
        _write_csv(output_dir / filename, COMPANY_COLUMNS, merged[filename])
    for filename in JOB_FILES:
        _write_csv(output_dir / filename, JOB_COLUMNS, merged[filename])
    summary = {
        "updated": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "screening_scope": "multiple",
        "scopes": list(normalized_scopes),
        "by_scope": by_scope,
        "applicable_companies": len(merged["applicable_companies.csv"]),
        "verified_companies": len(merged["verified_companies.csv"]),
        "pending_discovery_companies": len(merged["pending_discovery_companies.csv"]),
        "error_companies": len(merged["error_companies.csv"]),
        "jd_rows": len(merged["job_targets.csv"]),
        "verified_jd_rows": len(merged["verified_job_targets.csv"]),
        "outputs": {filename.removesuffix(".csv"): str((output_dir / filename).resolve()) for filename in merged},
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def _scope_argument(value: str) -> tuple[str, Path]:
    scope, separator, directory = value.partition("=")
    if not separator or not scope.strip() or not directory.strip():
        raise argparse.ArgumentTypeError("scope must use NAME=DIRECTORY")
    return scope.strip(), Path(directory)


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge independently generated target scopes.")
    parser.add_argument("--scope", action="append", required=True, type=_scope_argument, metavar="NAME=DIRECTORY")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    scopes = dict(args.scope)
    if len(scopes) != len(args.scope):
        parser.error("each scope name may be supplied only once")
    print(json.dumps(merge_target_scopes(scopes, args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
