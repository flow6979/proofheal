"""Proof reports: JSON (machine-checkable) + HTML (human-readable) for one run."""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
from typing import Any

from .runner import TestResult

STATUS_COLORS = {"passed": "#1f7a4d", "healed": "#2459c4", "failed": "#b42318",
                 "refused": "#b42318", "not_run": "#777", "accepted": "#1f7a4d"}


def build(run: dict[str, Any], results: list[TestResult], repairs: list[dict[str, Any]], out_dir: Path) -> dict[str, Any]:
    def rel(p: str | None) -> str | None:
        return os.path.relpath(p, out_dir) if p else None

    for r in repairs:
        if r["recorded"].get("crop"):
            r["recorded"]["crop"] = rel(r["recorded"]["crop"])
        for a in r["attempts"]:
            a["screenshots"] = {k: rel(v) for k, v in a["screenshots"].items()}
    summary = {
        "tests": len(results),
        "passed": sum(t.status == "passed" for t in results),
        "healed": sum(t.status == "healed" for t in results),
        "failed": sum(t.status == "failed" for t in results),
        "repairs_accepted": sum(r["status"] == "accepted" for r in repairs),
        "repairs_refused": sum(r["status"] == "refused" for r in repairs),
    }
    return {**run, "summary": summary, "tests": [t.to_dict() for t in results], "repairs": repairs}


def write(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jp, hp = out_dir / "report.json", out_dir / "report.html"
    jp.write_text(json.dumps(report, indent=2))
    hp.write_text(render_html(report))
    return jp, hp


def _e(x: Any) -> str:
    return html.escape(str(x)) if x is not None else ""


def _badge(status: str) -> str:
    label = {"accepted": "PROVEN REPAIR", "refused": "REFUSED"}.get(status, status.upper())
    return f'<span class="badge" style="background:{STATUS_COLORS.get(status, "#555")}">{_e(label)}</span>'


def _img(src: str | None, caption: str) -> str:
    if not src:
        return ""
    return f'<figure><img src="{_e(src)}" alt="{_e(caption)}"><figcaption>{_e(caption)}</figcaption></figure>'


def _repair_card(i: int, r: dict[str, Any]) -> str:
    acc = r.get("accepted_attempt")
    shown = r["attempts"][acc] if acc is not None else (r["attempts"][-1] if r["attempts"] else None)
    v = r.get("verdict") or {}
    head = (f'<h3>#{i + 1} {_badge(r["status"])} <code>{_e(r["test_id"])}</code> · step “{_e(r["step"])}”</h3>'
            f'<p class="loc"><code class="old">{_e(r["old_locator"])}</code> → '
            + (f'<code class="new">{_e(r["new_locator"])}</code>' if r.get("new_locator") else '<em>no repair applied</em>')
            + "</p>")
    if r["kind"] == "expect_text":
        head += f'<p>Original assertion: text must equal <b>{_e(r["expected_text"])}</b></p>'
    figs = _img(r["recorded"].get("crop"), "Before: element in the green run (success-run memory)")
    if shown:
        figs += _img(shown["screenshots"].get("element"), f"Candidate element {shown['candidate']}")
        figs += _img(shown["screenshots"].get("after"), "Replay: page after the step" if r["kind"] == "click" else "Replay: page at assertion")
    checks = ""
    for a in r["attempts"]:
        rows = "".join(
            f'<tr><td>{"✔" if c["passed"] else "✘"}</td><td>{_e(c["name"])}{"" if c["hard"] else " <small>(evidence)</small>"}</td><td>{_e(c["detail"])}</td></tr>'
            for c in a["checks"]
        )
        extra = ""
        if a.get("error"):
            extra += f'<p class="err">Replay error: {_e(a["error"])}</p>'
        if r["kind"] == "click":
            extra += f'<p><small>Requests to /api during the step: {_e(", ".join(a["network"]) or "none")}</small></p>'
        checks += (f'<details {"open" if a is shown else ""}><summary>Isolated replay of <code>{_e(a["candidate"])}</code> — '
                   f'{"hard checks passed" if a["hard_checks_passed"] else "hard checks FAILED"}</summary>'
                   f'<table>{rows}</table>{extra}</details>')
    verdict = ""
    if v:
        ev = "".join(f"<li>{_e(x)}</li>" for x in v.get("evidence", []))
        verdict = (f'<div class="verdict"><b>Verifier verdict</b> ({_e(v.get("source"))}, reasoning effort high): '
                   f'accepted={_e(v.get("accepted"))}, confidence={_e(v.get("confidence"))}, '
                   f'classification=<b>{_e(v.get("classification"))}</b><p>{_e(v.get("reasoning"))}</p><ul>{ev}</ul></div>')
    decision = "".join(f"<li>{_e(x)}</li>" for x in r["decision"]["reasons"])
    prop = ""
    if r.get("proposal"):
        p = r["proposal"]
        cands = "".join(f'<li><code>{_e(c["locator"])}</code> — {_e(c["rationale"])}</li>' for c in p["candidates"])
        trace = "".join(f'<li><code>{_e(t["tool"])}({_e(json.dumps(t["args"]))})</code> → {_e(t["result_summary"])}</li>'
                        for t in p["tool_trace"])
        prop = (f'<details><summary>Agent rationale (likely cause: {_e(p["likely_cause"])})</summary>'
                f'<p>{_e(p["diagnosis"])}</p><ol>{cands}</ol><p><b>Tool calls</b></p><ul>{trace}</ul></details>')
    return (f'<section class="card {r["status"]}">{head}<div class="figs">{figs}</div>'
            f'<p><b>Gate decision:</b></p><ul>{decision}</ul>{verdict}{checks}{prop}</section>')


def render_html(rep: dict[str, Any]) -> str:
    s = rep["summary"]
    tests = ""
    for t in rep["tests"]:
        rows = "".join(
            f'<tr><td>{_badge(e["status"])}</td><td>{_e(e["step"])}</td><td><code>{_e(e["locator"])}</code></td><td>{_e(e["detail"])}</td></tr>'
            for e in t["events"]
        )
        tests += f'<h3>{_badge(t["status"])} {_e(t["test_id"])}</h3><table>{rows}</table>'
    cards = "".join(_repair_card(i, r) for i, r in enumerate(rep["repairs"])) or "<p>No repairs were needed.</p>"
    llm = rep.get("llm") or {}
    calls = llm.get("calls", [])
    llm_html = (f'<p>Model <code>{_e(llm.get("model"))}</code> via <code>{_e(llm.get("base_url"))}</code> · '
                f'{len(calls)} calls · capability check: {_e(llm.get("capability"))}</p>') if llm else "<p>No model calls (no healing needed or healing disabled).</p>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>ProofHeal report · {_e(rep['run_id'])}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#1b1f2a;background:#fafbfc}}
h1{{margin-bottom:4px}} .sub{{color:#555}} code{{background:#eef0f4;padding:1px 5px;border-radius:4px}}
.badge{{color:#fff;font-size:12px;font-weight:700;padding:2px 8px;border-radius:999px;letter-spacing:.03em}}
.stats{{display:flex;gap:12px;margin:16px 0}} .stat{{background:#fff;border:1px solid #e3e6ec;border-radius:10px;padding:10px 16px}}
.stat b{{font-size:22px;display:block}} table{{border-collapse:collapse;width:100%;margin:6px 0;background:#fff}}
td{{border-top:1px solid #eceef2;padding:5px 8px;vertical-align:top;font-size:14px}}
.card{{background:#fff;border:1px solid #e3e6ec;border-left:6px solid #1f7a4d;border-radius:10px;padding:8px 18px;margin:18px 0}}
.card.refused{{border-left-color:#b42318}} .loc code.old{{text-decoration:line-through;color:#b42318}} .loc code.new{{color:#1f7a4d}}
.figs{{display:flex;gap:14px;flex-wrap:wrap}} figure{{margin:0;max-width:340px}} figure img{{max-width:340px;max-height:220px;border:1px solid #ddd;border-radius:6px}}
figcaption{{font-size:12px;color:#555}} .verdict{{background:#f3f6fd;border-radius:8px;padding:8px 12px;margin:8px 0}}
.err{{color:#b42318}} details{{margin:6px 0}} summary{{cursor:pointer}}
</style></head><body>
<h1>ProofHeal proof report</h1>
<p class="sub">Run <code>{_e(rep['run_id'])}</code> · shop variant <b>{_e(rep['variant'])}</b> · mode <b>{_e(rep['mode'])}</b> · {_e(rep['started'])}</p>
<div class="stats">
<div class="stat"><b>{s['tests']}</b>tests</div><div class="stat"><b>{s['passed']}</b>passed</div>
<div class="stat"><b>{s['healed']}</b>healed</div><div class="stat"><b>{s['failed']}</b>failed</div>
<div class="stat"><b>{s['repairs_accepted']}</b>proven repairs</div><div class="stat"><b>{s['repairs_refused']}</b>refused</div></div>
<p><i>A green test is not proof.</i> Each repair below was replayed in an isolated browser context and accepted only if the step's recorded outcome held and the verifier agreed.</p>
<h2>Tests</h2>{tests}
<h2>Repairs</h2>{cards}
<h2>Model</h2>{llm_html}
</body></html>"""
