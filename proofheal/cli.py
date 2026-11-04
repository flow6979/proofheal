"""proofheal command line.

  proofheal record  [--variant v1]           run the suite on a green build, store success-run memory
  proofheal run     [--variant v2] [--heal]  run the suite; with --heal, propose + verify repairs
  proofheal report  [--open] [PATH]          summarize (and optionally open) the latest proof report
  proofheal check                            Meta Model API capability check only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from pathlib import Path

from .llm import LLM, IncompatibleProvider, LLMConfig, SetupError
from .memory import MemoryStore
from . import report as report_mod
from .runner import now_id, record, run_suite

REPORTS = Path("proof_reports")


def log(msg: str) -> None:
    print(msg, flush=True)


def _llm_or_exit() -> LLM:
    try:
        llm = LLM(LLMConfig.from_env())
    except SetupError as exc:
        log(f"✘ setup: {exc}")
        sys.exit(2)
    log(f"• capability check: {llm.config.model} @ {llm.config.base_url} (structured output, reasoning_effort=high, tools)")
    try:
        cap = llm.capability_check()
    except IncompatibleProvider as exc:
        log(f"✘ incompatible provider: {exc}")
        sys.exit(3)
    log("  ✔ supported")
    llm.capability = cap  # type: ignore[attr-defined]
    return llm


def cmd_check(_: argparse.Namespace) -> int:
    _llm_or_exit()
    return 0


def cmd_record(a: argparse.Namespace) -> int:
    mem = MemoryStore(a.memory)
    log(f"● record · variant {a.variant} · tests {a.tests}")
    results = record(a.variant, a.tests, mem, log)
    ok = all(r.status == "passed" for r in results)
    log(f"{'✔' if ok else '✘'} memory: {mem.count()} steps in {mem.path}")
    return 0 if ok else 1


def cmd_run(a: argparse.Namespace) -> int:
    mem = MemoryStore(a.memory)
    if a.heal and mem.count() == 0:
        log("✘ no success-run memory. Run `proofheal record` against a green build first.")
        return 2
    llm = _llm_or_exit() if a.heal else None
    run_id = now_id(a.variant)
    out_dir = Path(a.out) / run_id if a.out else REPORTS / run_id
    log(f"● run · variant {a.variant} · heal={'on' if a.heal else 'off'}")
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    results, repairs = run_suite(a.variant, a.tests, mem, heal=a.heal, llm=llm, img_dir=out_dir / "img", log=log)
    run = {
        "run_id": run_id, "variant": a.variant, "mode": "heal" if a.heal else "plain", "started": started,
        "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
        "llm": None if not llm else {
            "model": llm.config.model, "base_url": llm.config.base_url, "capability": llm.capability,  # type: ignore[attr-defined]
            "calls": [c.__dict__ for c in llm.calls],
        },
    }
    rep = report_mod.build(run, results, repairs, out_dir)
    jp, hp = report_mod.write(rep, out_dir)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "latest.json").write_text(json.dumps({"report": str(hp), "json": str(jp)}))
    s = rep["summary"]
    log(f"■ {s['tests']} tests: {s['passed']} passed, {s['healed']} healed, {s['failed']} failed · "
        f"{s['repairs_accepted']} proven repair(s), {s['repairs_refused']} refused")
    log(f"  report: {hp}")
    return 0 if s["failed"] == 0 else 1


def cmd_report(a: argparse.Namespace) -> int:
    path = Path(a.path) if a.path else None
    if path is None:
        latest = REPORTS / "latest.json"
        if not latest.exists():
            log("no reports yet")
            return 1
        path = Path(json.loads(latest.read_text())["report"])
    if path.is_dir():
        path = path / "report.html"
    rep = json.loads(path.with_name("report.json").read_text())
    s = rep["summary"]
    log(f"{rep['run_id']}: {s['tests']} tests · {s['passed']} passed · {s['healed']} healed · {s['failed']} failed")
    for r in rep["repairs"]:
        mark = "✔ PROVEN " if r["status"] == "accepted" else "✘ REFUSED"
        tail = f"→ {r['new_locator']}" if r.get("new_locator") else f"({(r.get('verdict') or {}).get('classification', 'no verdict')})"
        log(f"  {mark} {r['test_id']} · {r['step']}: {r['old_locator']} {tail}")
    log(f"  {path}")
    if a.open:
        webbrowser.open(path.resolve().as_uri())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="proofheal", description="Self-healing e2e tests that only accept proven repairs.")
    p.add_argument("--memory", default=".proofheal/memory.db")
    p.add_argument("--tests", default="tests_e2e")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="record success-run memory from a green build")
    r.add_argument("--variant", default=os.environ.get("SHOP_VARIANT", "v1"))
    r.set_defaults(fn=cmd_record)
    u = sub.add_parser("run", help="run the suite (add --heal to repair with proof)")
    u.add_argument("--variant", default=os.environ.get("SHOP_VARIANT", "v1"))
    u.add_argument("--heal", action="store_true")
    u.add_argument("--out", default=None, help="parent dir for the report (default proof_reports/)")
    u.set_defaults(fn=cmd_run)
    rp = sub.add_parser("report", help="summarize the latest (or given) proof report")
    rp.add_argument("path", nargs="?")
    rp.add_argument("--open", action="store_true")
    rp.set_defaults(fn=cmd_report)
    c = sub.add_parser("check", help="verify the LLM provider supports what ProofHeal needs")
    c.set_defaults(fn=cmd_check)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
