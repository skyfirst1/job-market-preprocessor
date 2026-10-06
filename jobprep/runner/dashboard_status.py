from __future__ import annotations

import argparse
from datetime import datetime
from html import escape
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
from jobprep.app.fileio import atomic_write_text, read_json_object
OUTPUT = ROOT / "exports" / "operations_dashboard"
AGENTS = ("subagent1", "subagent2")
COUNTS = ("candidates", "processed", "success", "partial", "failed", "errors",
          "total_candidates", "processed_companies", "remaining_companies",
          "recovered_partial", "remaining_partial")


def atomic_write(path: Path, content: str):
    atomic_write_text(path, content)


def ensure_index(output: Path):
    target = output / "index.html"
    if target.exists():
        return
    content = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>抓取运行看板</title>
<style>
html,body{height:100%;margin:0;background:#eef1f5;font-family:Segoe UI,Microsoft YaHei,sans-serif}
header{height:52px;box-sizing:border-box;padding:13px 20px;background:#17202a;color:#fff;font-weight:700}
main{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:12px;height:calc(100% - 52px);box-sizing:border-box}
iframe{width:100%;height:100%;border:1px solid #c9d1d9;background:#fff}
@media(max-width:900px){main{grid-template-columns:1fr;grid-template-rows:1fr 1fr}}
</style></head><body><header>招聘预处理运行看板</header><main>
<iframe src="subagent1.html" title="subagent1"></iframe>
<iframe src="subagent2.html" title="subagent2"></iframe>
</main></body></html>"""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError:
        pass


def render(status):
    def value(name, fallback="-"):
        raw = status.get(name)
        return escape(str(raw if raw not in (None, "") else fallback))

    artifacts = status.get("artifacts") or []
    artifacts_html = "".join(f"<li>{escape(str(item))}</li>" for item in artifacts) or "<li>-</li>"
    reasons = status.get("partial_by_reason") or {}
    reasons_html = "".join(
        f"<li><code>{escape(str(reason))}</code>: {escape(str(count))}</li>"
        for reason, count in sorted(reasons.items())
    ) or "<li>-</li>"
    circuit = bool(status.get("circuit_open"))
    circuit_class = "bad" if circuit else "good"
    state_class = "bad" if status.get("status") in {"failed", "blocked"} else "good"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta http-equiv="refresh" content="10">
<title>{value('agent')} 状态</title><style>
body{{margin:0;padding:18px;color:#17202a;background:#fff;font:14px/1.45 Segoe UI,Microsoft YaHei,sans-serif}}
h1{{font-size:20px;margin:0 0 4px}} .muted{{color:#637083}} .grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:14px 0}}
.metric{{border:1px solid #d8dee6;padding:9px;background:#f8fafc}} .metric b{{display:block;font-size:19px}}
.policies{{display:flex;gap:7px;flex-wrap:wrap;margin:12px 0}} .pill{{padding:5px 8px;border:1px solid #b8c1cc;background:#f3f5f7}}
.good{{color:#126b3a}} .bad{{color:#a32424}} table{{border-collapse:collapse;width:100%}} td{{border-top:1px solid #e1e5ea;padding:7px 4px;vertical-align:top}} td:first-child{{width:92px;color:#637083}}
ul{{margin:4px 0;padding-left:20px}} code{{white-space:normal;overflow-wrap:anywhere}}
</style></head><body>
<h1>{value('agent')} <span class="{state_class}">{value('status')}</span></h1>
<div class="muted">{value('role')} · 每 10 秒自动刷新</div>
<div class="policies"><span class="pill good">微信单并发：启用</span><span class="pill good">启动间隔 ≥10秒：启用</span><span class="pill {circuit_class}">失败第11篇熔断：{'已熔断' if circuit else '监控中'}</span><span class="pill good">成功链接永久跳过：启用</span></div>
<div class="grid">
<div class="metric">候选<b>{value('candidates','0')}</b></div><div class="metric">处理<b>{value('processed','0')}</b></div><div class="metric">成功<b>{value('success','0')}</b></div>
<div class="metric">部分成功<b>{value('partial','0')}</b></div><div class="metric">失败<b>{value('failed','0')}</b></div><div class="metric">本批错误<b>{value('errors','0')}</b></div>
</div>
<div class="grid">
<div class="metric">候选全集<b>{value('total_candidates','0')}</b></div><div class="metric">已处理公司<b>{value('processed_companies','0')}</b></div><div class="metric">剩余公司<b>{value('remaining_companies','0')}</b></div>
<div class="metric">恢复 partial<b>{value('recovered_partial','0')}</b></div><div class="metric">剩余 partial<b>{value('remaining_partial','0')}</b></div><div class="metric">当前批次<b>{value('batch')}</b></div>
</div>
<table><tr><td>阶段</td><td>{value('phase')}</td></tr><tr><td>批次</td><td>{value('batch')}</td></tr>
<tr><td>Partial 根因</td><td><ul>{reasons_html}</ul></td></tr>
<tr><td>熔断</td><td class="{circuit_class}">{'是' if circuit else '否'}</td></tr><tr><td>最近错误</td><td><code>{value('last_error')}</code></td></tr>
<tr><td>成果文件</td><td><ul>{artifacts_html}</ul></td></tr><tr><td>更新时间</td><td>{value('updated_at')}</td></tr>
<tr><td>下一步</td><td>{value('next_step')}</td></tr></table>
</body></html>"""


def update(agent, values, output=OUTPUT):
    if agent not in AGENTS:
        raise ValueError("agent must be subagent1 or subagent2")
    output = Path(output)
    ensure_index(output)
    json_path = output / f"{agent}.json"
    current = read_json_object(json_path)
    current["agent"] = agent
    for key, item in values.items():
        if item is not None:
            current[key] = item
    current.setdefault("artifacts", [])
    current["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_write(json_path, json.dumps(current, ensure_ascii=False, indent=2))
    atomic_write(output / f"{agent}.html", render(current))
    return current


def main():
    parser = argparse.ArgumentParser(description="Update one agent-owned operations dashboard page")
    parser.add_argument("--agent", required=True, choices=AGENTS)
    parser.add_argument("--role")
    parser.add_argument("--phase")
    parser.add_argument("--status")
    parser.add_argument("--batch")
    for field in COUNTS:
        parser.add_argument(f"--{field}", type=int)
    circuit = parser.add_mutually_exclusive_group()
    circuit.add_argument("--circuit-open", dest="circuit_open", action="store_true")
    circuit.add_argument("--no-circuit-open", dest="circuit_open", action="store_false")
    parser.set_defaults(circuit_open=None)
    parser.add_argument("--last-error")
    parser.add_argument("--artifact", action="append")
    parser.add_argument("--next-step")
    parser.add_argument("--output", type=Path, default=OUTPUT, help=argparse.SUPPRESS)
    args = parser.parse_args()
    values = vars(args)
    agent = values.pop("agent")
    output = values.pop("output")
    if values.get("artifact") is not None:
        values["artifacts"] = values.pop("artifact")
    else:
        values.pop("artifact")
    status = update(agent, values, output)
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
