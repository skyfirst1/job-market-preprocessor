# Project Agent Instructions

## Skill Use

Before starting work, every subagent must identify and read the skills that apply to its task. A subagent performing job-semantic screening, target-JD analysis, candidate-company analysis, incremental target refreshes, or screening audits must read and apply:

`D:\job_market\.codex\skills\medical-ai-job-screening\SKILL.md`

Treat a skill as project policy, not as content the subagent may freely rewrite. Do not expand, relax, or edit a skill unless the user explicitly requests that change. If a skill appears wrong or incomplete, identify the exact rule and provide verifiable contrary evidence; otherwise follow it as written.

## Required Pre-Delivery Check

Before writing or reporting a semantic-screening deliverable, the responsible subagent must check its own result against the applicable skill, the current user instructions, the source evidence, and the current canonical outputs.

The check must cover all of the following:

1. **Facts and evidence:** Every admitted role must be supported by the quoted title or JD evidence used for that decision. Keep verified facts separate from unverified inference. Do not infer a role from the employer's industry, another role at the same company, or a broad AI label.
2. **Category boundary:** On ordinary complete sources, generic image processing, medical-image processing, reconstruction, rendering, signal processing, or a title containing only `图像处理` is not visual AI without affirmative model or algorithm evidence required by the screening skill. A CSV declaration does not override this boundary. Likewise, generic NLP or `自然语言处理` is not by itself evidence of Agent/LLM algorithm work. The skill's explicit limited-source exception is narrower and takes precedence for WeChat, questionnaires, forms, and unprovable static sources: a role-local AI mention may enter only as `limited_source_ai_mention`, must retain uncertainty, and must never enter verified outputs.
3. **Completeness:** Recheck excluded and unclassified records for strong target signals and compare the analyzed population with the source population. Explicitly look for qualifying roles omitted because of truncation, pagination, stale fetch state, location parsing, or an overly narrow keyword rule.
4. **Consistency:** Check unique IDs and normalized company/title/URL duplicates; company-to-JD counts; `applicable`/`verified` subset relations; category and priority labels; evidence level and verification flags; and agreement between CSV totals and `summary.json`. A verified output must not silently rely on evidence absent from its canonical row.
5. **Scope fields:** Revalidate location, enterprise nature, recruitment type, graduation year, and role level against the user's latest criteria. Do not preserve an older location or seniority rule after the user has changed it.
6. **Canonical data quality:** Keep `role_title` to the actual title rather than concatenated page or JD text. Preserve a direct job URL when available. If the decision relies on evidence stored elsewhere, carry a concise evidence excerpt or a stable source reference into the canonical row.
7. **Conflicts and uncertainty:** Resolve conflicts between the current user instructions, the skill, reports, and canonical files before delivery. If the task does not authorize correcting the affected canonical file, report the exact affected rows and the unresolved consequence instead of presenting the result as clean.
8. **Industry scope:** Every canonical company and JD row must carry a nonempty `screening_scope` and the source `industry` text. Use stable scope names such as `medical` or `manufacturing`; do not infer the scope from the output path. When scopes are merged, deduplicate within each scope, preserve the scope on every row, report counts by scope, and verify each scope's `verified` rows remain a subset of its complete candidate rows.
9. **Machine identifiers:** Keep `company_id` and `jd_id` for joins, deduplication, and auditability, but do not treat them as user-facing evidence or a primary display field. Never replace company names, role titles, industry scope, or source URLs with opaque IDs in a human-facing deliverable.

## Deliverables

Create only files explicitly required for the task. Prefer updating the existing canonical outputs under `exports/targets` over creating parallel result sets.

Do not create simultaneous CSV, JSON, and Markdown copies merely for reporting, temporary audit reports, one-off status files, or unrelated artifacts. Add a test file only when it protects executable behavior that cannot be adequately checked by an existing test. Put concise findings in the final response when no persistent report was requested.
