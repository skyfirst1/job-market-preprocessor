from __future__ import annotations

import argparse
import json
from pathlib import Path

from jobprep.analysis.application_targets import update_application_targets


def main() -> None:
    parser = argparse.ArgumentParser(description="增量更新医疗AI可投公司与逐角色JD清单。")
    parser.add_argument("--candidates", type=Path, default=Path("data/first_batch/first_batch_candidates.csv"))
    parser.add_argument("--batch-summary", type=Path, default=Path("data/first_batch/batch_summary.json"))
    parser.add_argument("--database", type=Path, default=Path("data/workstation.sqlite3"))
    parser.add_argument("--output-dir", type=Path, default=Path("exports/targets"))
    parser.add_argument("--partial-report", type=Path)
    parser.add_argument("--audit", type=Path, default=Path("data/audits/incomplete_review.csv"))
    parser.add_argument("--screening-scope", default="medical")
    parser.add_argument("--industry-pattern")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = update_application_targets(
        args.candidates, args.batch_summary, args.database, args.output_dir,
        force=args.force, partial_report_path=args.partial_report, audit_path=args.audit,
        screening_scope=args.screening_scope, industry_pattern=args.industry_pattern,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
