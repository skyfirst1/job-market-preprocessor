from __future__ import annotations

import argparse
import csv
import html
import io
import json
import mimetypes
import os
import re
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from ..app.application_history import applied_company_tokens, company_was_applied


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_ROOT = PROJECT_ROOT / "exports" / "operations_dashboard"
URL_FILE = PROJECT_ROOT / "data" / "operations_dashboard_url.txt"
APPLICATION_HISTORY = PROJECT_ROOT / "data" / "private" / "工作.md"
ALLOWED_EXTENSIONS = {".csv", ".json", ".txt", ".html", ".md"}
FIRST_BATCH_REPORTS = {
    "batch_summary.json",
    "first_batch_candidates.csv",
    "ocr_general_basic_trial.json",
    "web_increment_report.json",
    "web_pool_guard_report.json",
    "web_run_report.json",
    "wechat_pool_report.json",
    "wechat_pool_rerun_guard_report.json",
}
PURPOSES = {
    "applicable_companies.csv": "发现目标岗位的可投公司清单",
    "job_targets.csv": "逐岗位 JD 清单与优先级",
    "verified_companies.csv": "网页或独立审核已核验的公司子集",
    "verified_job_targets.csv": "网页或独立审核已核验的 JD 子集",
    "pending_discovery_companies.csv": "尚待继续发现目标岗位的公司",
    "error_companies.csv": "抓取错误诊断清单；CSV 明确岗位仍可同时保留为候选",
    "summary.json": "本批分析数量、状态与口径摘要",
    "remediation_report.json": "14 项未完成任务的定向修复完整报告",
    "remediation_report.csv": "定向修复逐项结果表",
    "special_adapter_report.json": "专站 adapter 修复完整报告",
    "special_adapter_report.csv": "专站 adapter 修复逐项结果表",
    "job51_static_report.json": "51job 静态专题页与 Zhiye 交接验证报告",
    "job51_xyz_report.json": "51job XYZ 公共岗位 API 验证报告",
    "continuous_adapter_report.json": "连续批次专站适配分析报告",
    "continuous_adapter_report.csv": "连续批次专站适配问题明细",
    "continuous_adapter_remediation.json": "连续批次 Zhiye/Mokahr 修复完整报告",
    "continuous_adapter_remediation.csv": "连续批次 Zhiye/Mokahr 逐 URL 修复明细",
    "recovered_jobs_target_delta.json": "恢复 structured jobs 的三档岗位增量审核完整报告",
    "recovered_jobs_target_delta.csv": "恢复岗位逐条去重、分类、地点与纳入决策",
    "recovered_jobs_target_delta.md": "恢复岗位目标命中结论与证据摘要",
    "wechat_resume_usage.md": "batch_009 微信断点安全续跑说明",
    "wechat_resume_report.json": "batch_009 微信断点续跑状态与预算预检报告",
    "nonwechat_gap_remediation.json": "非微信真实缺口最高收益平台修复报告",
    "nonwechat_gap_remediation.csv": "非微信真实缺口逐 URL 修复明细",
    "final_acceptance.json": "修复前最终验收报告",
    "final_acceptance.md": "修复前最终验收摘要",
    "final_acceptance_fix.json": "三个最终验收阻断项的修复与复验报告",
    "batch009_image_gap_recovery.json": "batch_009 缓存微信图片缺口恢复与 OCR 审计报告",
    "batch009_image_gap_recovery.csv": "batch_009 缓存微信图片逐项下载、校验与 OCR 结果",
    "first_batch_candidates.csv": "首批 20 家医疗/医药候选公司",
    "batch_summary.json": "首批公司、优先级与抓取池统计",
    "ocr_general_basic_trial.json": "百度通用文字识别标准版效果与调用验证",
    "web_increment_report.json": "普通网页增量抓取结果",
    "web_pool_guard_report.json": "普通网页成功跳过与断点守护验证",
    "web_run_report.json": "普通网页首轮运行明细",
    "wechat_pool_report.json": "独立微信池运行结果",
    "wechat_pool_rerun_guard_report.json": "微信成功链接永久跳过验证",
    "document.json": "微信文章结构化抓取与 OCR 数据",
    "article.txt": "微信文章可读文本",
    "subagent1.json": "抓取、监控与断点续跑状态",
    "subagent2.json": "公司和 JD 分析状态",
    "subagent4.json": "第三批起的持续增量分析状态",
    "analysis_summary.json": "历史 AI 岗位筛选摘要",
    "report.json": "岗位列表发现阶段报告",
}

VISIBLE_DELIVERIES = {
    "核心目标结果": (
        "exports/targets/summary.json",
        "exports/targets/applicable_companies.csv",
        "exports/targets/job_targets.csv",
        "exports/targets/verified_companies.csv",
        "exports/targets/verified_job_targets.csv",
        "exports/targets/pending_discovery_companies.csv",
        "exports/targets/error_companies.csv",
    ),
    "当前运行状态": (
        "exports/operations_dashboard/subagent1.json",
        "exports/operations_dashboard/subagent2.json",
    ),
    "最终审计": (
        "data/audits/final_acceptance_fix.json",
        "data/audits/batch009_target_analysis.md",
        "data/audits/batch009_target_consistency.json",
    ),
}

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _html_page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>{html.escape(title)}</title><style>
body{{margin:0;color:#17202a;background:#f4f6f8;font:14px/1.5 \"Segoe UI\",\"Microsoft YaHei\",sans-serif}}
header{{position:sticky;top:0;display:flex;align-items:center;gap:14px;padding:11px 16px;background:#17202a;color:#fff;z-index:2}}
header a{{color:#fff;font-weight:700;text-decoration:none}} header span{{overflow-wrap:anywhere}}
main{{padding:14px}} pre{{margin:0;padding:14px;background:#fff;border:1px solid #d0d5dd;white-space:pre-wrap;overflow-wrap:anywhere}}
.csv-tools{{position:sticky;top:45px;z-index:3;display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:10px;background:#fff;border:1px solid #d0d5dd;border-bottom:0}}
.csv-tools input{{flex:1 1 320px;min-width:180px;padding:7px 9px;border:1px solid #98a2b3;border-radius:4px;font:inherit}}
.csv-count{{color:#475467;white-space:nowrap}}.csv-tools a{{color:#087ea4;text-decoration:none;font-weight:600}}.csv-tools label{{display:flex;align-items:center;gap:6px;white-space:nowrap}}
.table-wrap{{max-height:calc(100vh - 122px);overflow:auto;background:#fff;border:1px solid #d0d5dd}} table{{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%;table-layout:fixed;font-size:12px}}
th,td{{border-right:1px solid #e1e5ea;border-bottom:1px solid #e1e5ea;padding:7px 8px;text-align:left;vertical-align:top;min-width:110px;max-width:360px;white-space:normal;overflow-wrap:anywhere}}
th{{position:sticky;top:0;z-index:2;background:#eef2f3;cursor:pointer;user-select:none;box-shadow:0 1px #d0d5dd}}th:hover{{background:#e1e8ec}}th::after{{content:" ↕";color:#98a2b3}}th[data-sort="asc"]::after{{content:" ↑";color:#087ea4}}th[data-sort="desc"]::after{{content:" ↓";color:#087ea4}}
td a{{color:#087ea4;overflow-wrap:anywhere}}td details{{max-width:100%}}td summary{{cursor:pointer;color:#344054}}.cell-full{{margin-top:6px;padding-top:6px;border-top:1px solid #eaecf0}}tr[hidden]{{display:none}}
iframe{{width:100%;height:calc(100vh - 78px);border:1px solid #d0d5dd;background:#fff}}
</style></head><body><header><a href=\"/\">返回看板</a><span>{html.escape(title)}</span></header><main>{body}</main></body></html>""".encode("utf-8")


def _linkify(value: str) -> str:
    output: list[str] = []
    cursor = 0
    for match in URL_RE.finditer(value):
        url = match.group(0).rstrip(".,;:!?)]}，。；：！？）】")
        if not url:
            continue
        end = match.start() + len(url)
        output.append(html.escape(value[cursor:match.start()]))
        escaped_url = html.escape(url, quote=True)
        output.append(
            f'<a href="{escaped_url}" target="_blank" rel="noopener noreferrer">{html.escape(url)}</a>'
        )
        cursor = end
    output.append(html.escape(value[cursor:]))
    return "".join(output).replace("\n", "<br>")


def _csv_cell(value: str) -> str:
    rendered = _linkify(value)
    if len(value) <= 180 and "\n" not in value:
        return rendered
    preview = value.replace("\r", " ").replace("\n", " ")[:140].rstrip()
    return (
        f'<details><summary>{html.escape(preview)}…</summary>'
        f'<div class="cell-full">{rendered}</div></details>'
    )


def _csv_preview(path: Path, relative: str | None = None) -> str:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return "<p>空 CSV 文件</p>"
    width = len(rows[0])
    relative = relative or _relative(path)
    company_column = next(
        (index for index, value in enumerate(rows[0]) if value.strip().casefold() in {"company", "company_name", "公司"}),
        None,
    )
    applied_tokens = (
        applied_company_tokens(APPLICATION_HISTORY)
        if relative.startswith("exports/targets/") and company_column is not None
        else set()
    )
    applied_count = (
        sum(
            company_was_applied(row[company_column] if company_column < len(row) else "", applied_tokens)
            for row in rows[1:]
        )
        if company_column is not None
        else 0
    )
    head = "".join(
        f'<th scope="col" data-column="{index}" tabindex="0">{html.escape(value)}</th>'
        for index, value in enumerate(rows[0])
    )
    body = "".join(
        "<tr data-search=\"{}\" data-applied=\"{}\">{}</tr>".format(
            html.escape(" ".join(row).casefold(), quote=True),
            "true" if company_column is not None and company_was_applied(
                row[company_column] if company_column < len(row) else "", applied_tokens
            ) else "false",
            "".join(
                f'<td data-value="{html.escape(value, quote=True)}">{_csv_cell(value)}</td>'
                for value in row + [""] * (width - len(row))
            ),
        )
        for row in rows[1:]
    )
    raw_href = "/raw/" + quote(relative, safe="/")
    applied_control = (
        f'<label><input id="show-applied" type="checkbox">显示已投（{applied_count}）</label>'
        if applied_count else ""
    )
    return f'''<div class="csv-tools">
<input id="csv-search" type="search" placeholder="搜索当前 CSV…" aria-label="搜索当前 CSV">
{applied_control}
<span id="csv-count" class="csv-count">显示 {len(rows) - 1 - applied_count} / {len(rows) - 1} 行</span>
<a href="{html.escape(raw_href, quote=True)}">下载原始 CSV（含已投）</a>
</div>
<div class="table-wrap"><table id="csv-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>
<script>
(() => {{
  const table = document.getElementById('csv-table');
  const tbody = table.tBodies[0];
  const rows = Array.from(tbody.rows);
  const search = document.getElementById('csv-search');
  const showApplied = document.getElementById('show-applied');
  const count = document.getElementById('csv-count');
  const updateFilter = () => {{
    const query = search.value.trim().toLocaleLowerCase('zh-CN');
    let visible = 0;
    rows.forEach(row => {{
      const filteredBySearch = query !== '' && !row.dataset.search.includes(query);
      const filteredByHistory = row.dataset.applied === 'true' && !(showApplied?.checked);
      row.hidden = filteredBySearch || filteredByHistory;
      if (!row.hidden) visible += 1;
    }});
    count.textContent = `显示 ${{visible}} / ${{rows.length}} 行`;
  }};
  search.addEventListener('input', updateFilter);
  showApplied?.addEventListener('change', updateFilter);
  const sortColumn = column => {{
    const header = table.tHead.rows[0].cells[column];
    const direction = header.dataset.sort === 'asc' ? 'desc' : 'asc';
    Array.from(table.tHead.rows[0].cells).forEach(cell => delete cell.dataset.sort);
    header.dataset.sort = direction;
    const number = value => {{ const parsed = Number(value.replace(/,/g, '')); return Number.isFinite(parsed) ? parsed : null; }};
    rows.sort((left, right) => {{
      const a = left.cells[column]?.dataset.value ?? '';
      const b = right.cells[column]?.dataset.value ?? '';
      const an = number(a), bn = number(b);
      const compared = an !== null && bn !== null ? an - bn : a.localeCompare(b, 'zh-CN', {{numeric:true, sensitivity:'base'}});
      return direction === 'asc' ? compared : -compared;
    }}).forEach(row => tbody.appendChild(row));
  }};
  Array.from(table.tHead.rows[0].cells).forEach((header, column) => {{
    header.addEventListener('click', () => sortColumn(column));
    header.addEventListener('keydown', event => {{ if (event.key === 'Enter' || event.key === ' ') {{ event.preventDefault(); sortColumn(column); }} }});
  }});
}})();
</script>'''


def _relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def _first_batch_wechat_urls() -> dict[str, str]:
    queue = PROJECT_ROOT / "data" / "first_batch" / "wechat_queue.jsonl"
    urls: dict[str, str] = {}
    if not queue.is_file():
        return urls
    for line in queue.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        url = record.get("acquisition_url") or record.get("announcement_url")
        if url:
            urls[str(url)] = str(record.get("company") or "微信文章")
    return urls


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def build_catalog() -> tuple[dict[str, Path], list[dict[str, object]]]:
    catalog: dict[str, Path] = {}

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved.is_file() and resolved.suffix.lower() in ALLOWED_EXTENSIONS:
            catalog[_relative(resolved)] = resolved

    for name in ("index.html", "subagent1.html", "subagent2.html", "subagent1.json", "subagent2.json"):
        add(DASHBOARD_ROOT / name)
    add(PROJECT_ROOT / "data" / "agent_reports" / "subagent2.json")
    add(PROJECT_ROOT / "data" / "agent_reports" / "subagent4.json")
    for path in (PROJECT_ROOT / "exports" / "targets").glob("*"):
        add(path)
    for name in FIRST_BATCH_REPORTS:
        add(PROJECT_ROOT / "data" / "first_batch" / name)
    for path in (PROJECT_ROOT / "data" / "batches").glob("**/*"):
        add(path)
    # Audit reports are append-only deliverables. Discover allowed file types under
    # this fixed root so new reports do not require a code allowlist edit.
    for path in (PROJECT_ROOT / "data" / "audits").glob("**/*"):
        add(path)

    first_batch_urls = _first_batch_wechat_urls()
    for document in (PROJECT_ROOT / "exports" / "wechat").glob("*/document.json"):
        try:
            payload = json.loads(document.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(payload.get("url") or "") in first_batch_urls:
            add(document)
            add(document.with_name("article.txt"))

    add(PROJECT_ROOT / "out" / "analysis_summary.json")
    add(PROJECT_ROOT / "exports" / "list_discovery" / "report.json")
    add(PROJECT_ROOT / "exports" / "full_lists" / "summary.json")

    def file_node(relative: str) -> dict[str, object]:
        path = catalog[relative]
        stat = path.stat()
        return {
            "type": "file",
            "name": path.name,
            "extension": path.suffix[1:].upper(),
            "href": "/view/" + quote(relative, safe="/"),
            "purpose": PURPOSES.get(path.name, "重要交付文件"),
            "updated": datetime.fromtimestamp(stat.st_mtime).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "size": format_size(stat.st_size),
        }

    groups = [
        {
            "type": "directory",
            "name": name,
            "children": [file_node(key) for key in keys if key in catalog],
        }
        for name, keys in VISIBLE_DELIVERIES.items()
    ]
    return catalog, groups


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "JobMarketDashboard/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._serve_file(DASHBOARD_ROOT / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/api/tree":
            self.server.refresh_catalog()
            body = json.dumps({"groups": self.server.groups}, ensure_ascii=False).encode("utf-8")
            self._send(body, "application/json; charset=utf-8")
            return
        if parsed.path.startswith("/view/"):
            self._serve_allowed(parsed.path[len("/view/") :], preview=True)
            return
        if parsed.path.startswith("/raw/"):
            self._serve_allowed(parsed.path[len("/raw/") :], preview=False)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _serve_allowed(self, encoded_relative: str, *, preview: bool) -> None:
        self.server.refresh_catalog()
        relative = unquote(encoded_relative)
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        path = self.server.catalog.get(pure.as_posix())
        if path is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        resolved = path.resolve()
        try:
            resolved.relative_to(PROJECT_ROOT)
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if preview:
            title = _relative(resolved)
            suffix = resolved.suffix.lower()
            if suffix == ".csv":
                body = _html_page(title, _csv_preview(resolved, title))
            else:
                text = resolved.read_text(encoding="utf-8-sig", errors="replace")
                if suffix == ".json":
                    try:
                        text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
                    except json.JSONDecodeError:
                        pass
                body = _html_page(title, f"<pre>{html.escape(text)}</pre>")
            self._send(body, "text/html; charset=utf-8")
            return
        content_type = (
            "text/csv"
            if resolved.suffix.lower() == ".csv"
            else mimetypes.guess_type(resolved.name)[0] or "text/plain"
        )
        if content_type.startswith("text/") or resolved.suffix.lower() in {".json", ".csv"}:
            content_type += "; charset=utf-8"
        self._serve_file(resolved, content_type)

    def _serve_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send(body, content_type)

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", "inline")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}", flush=True)


class DashboardServer(ThreadingHTTPServer):
    catalog: dict[str, Path]
    groups: list[dict[str, object]]

    def refresh_catalog(self) -> None:
        self.catalog, self.groups = build_catalog()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the read-only operations dashboard on localhost.")
    parser.add_argument("--port", type=int, default=0, help="Preferred port; 0 safely selects a free port.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    catalog, groups = build_catalog()
    server = DashboardServer(("127.0.0.1", args.port), DashboardHandler)
    server.catalog = catalog
    server.groups = groups
    url = f"http://127.0.0.1:{server.server_port}/"
    URL_FILE.parent.mkdir(parents=True, exist_ok=True)
    URL_FILE.write_text(url + os.linesep, encoding="utf-8")
    print(f"Operations dashboard: {url}", flush=True)
    print(f"Allowed files: {len(catalog)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
