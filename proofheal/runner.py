"""Suite orchestration: record success-run memory, or run with healing + verification."""

from __future__ import annotations

import importlib.util
import inspect
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError

from . import fingerprints
from .agent import HealingAgent
from .env import browser, new_context, shop_server
from .harness import AssertionMismatch, LocatorMissing, PageWatch, Step, Steps, StepFailed, TestFn, perform, resolve
from .llm import LLM
from .memory import MemoryStore, StepMemory, attach_outcomes
from .replay import ReplayResult, page_state, replay_candidate
from .verifier import Verdict, gate, judge

Log = Callable[[str], None]


def load_tests(tests_dir: str | Path) -> list[tuple[str, TestFn]]:
    tests: list[tuple[str, TestFn]] = []
    root = Path(tests_dir).resolve()
    if str(root.parent) not in sys.path:
        sys.path.insert(0, str(root.parent))
    for f in sorted(root.glob("test_*.py")):
        spec = importlib.util.spec_from_file_location(f"_ph_tests.{f.stem}", f)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(mod)
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if name.startswith("test_") and fn.__module__ == mod.__name__:
                tests.append((name, fn))
    tests.sort(key=lambda t: inspect.getsourcelines(t[1])[1])
    return tests


@dataclass
class StepEvent:
    step: str
    kind: str
    locator: str | None
    status: str  # passed | healed | refused | failed | not_run
    detail: str = ""
    repair_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class TestResult:
    __test__ = False  # not a pytest class
    test_id: str
    status: str = "passed"  # passed | healed | failed
    events: list[StepEvent] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"test_id": self.test_id, "status": self.status, "error": self.error,
                "events": [e.to_dict() for e in self.events]}


# --------------------------------------------------------------------------- record

class RecordDriver:
    def __init__(self, crops_dir: Path, variant: str):
        self.crops_dir, self.variant = crops_dir, variant
        self.records: list[StepMemory] = []
        self.events: list[StepEvent] = []

    def execute(self, ph: Steps, step: Step) -> None:
        page = ph.page
        fp = crop = None
        if step.locator:
            el = resolve(page, step.locator)
            fp = fingerprints.capture(el)
            crop_path = self.crops_dir / f"{step.test_id}__{step.index:02d}.png"
            el.screenshot(path=str(crop_path))
            crop = str(crop_path)
        before = page_state(page)
        perform(page, ph.base_url, step, step.locator)
        after = page_state(page)
        self.records.append(StepMemory(
            test_id=step.test_id, step_name=step.name, step_index=step.index, kind=step.kind,
            locator=step.locator, value=step.value, fingerprint=fp, crop_path=crop,
            url_before=before["url"], url_after=after["url"],
            dom_changed=before["dom_hash"] != after["dom_hash"] or before["url"] != after["url"],
            variant=self.variant,
        ))
        self.events.append(StepEvent(step.name, step.kind, step.locator, "passed"))


def record(variant: str, tests_dir: str, memory: MemoryStore, log: Log) -> list[TestResult]:
    crops = memory.path.parent / "crops"
    crops.mkdir(parents=True, exist_ok=True)
    results: list[TestResult] = []
    with shop_server(variant) as url, browser() as b:
        for test_id, fn in load_tests(tests_dir):
            ctx = new_context(b)
            page = ctx.new_page()
            drv = RecordDriver(crops, variant)
            res = TestResult(test_id)
            try:
                fn(Steps(page, url, test_id, driver=drv))
                attach_outcomes(drv.records)
                memory.save_test(drv.records)
                log(f"  ✔ {test_id}: {len(drv.records)} steps recorded")
            except (LocatorMissing, AssertionMismatch, PlaywrightError) as exc:
                res.status, res.error = "failed", str(exc)
                log(f"  ✘ {test_id}: {exc} (memory NOT updated: only green runs are recorded)")
            finally:
                ctx.close()
            res.events = drv.events
            results.append(res)
    return results


# --------------------------------------------------------------------------- heal

@dataclass
class HealContext:
    browser: Browser
    base_url: str
    memory: MemoryStore
    llm: LLM | None
    agent: HealingAgent | None
    img_dir: Path
    log: Log
    repairs: list[dict[str, Any]] = field(default_factory=list)
    # original locator -> accepted repair (reused across tests, but always re-verified by replay)
    cache: dict[str, dict[str, Any]] = field(default_factory=dict)


class _AbortTest(Exception):
    pass


class HealDriver:
    def __init__(self, hc: HealContext, test_fn: TestFn, result: TestResult, heal: bool):
        self.hc, self.test_fn, self.result, self.heal = hc, test_fn, result, heal
        self.overrides: dict[str, str] = {}
        self.skip: set[str] = set()

    def _fail(self, step: Step, status: str, detail: str, repair_index: int | None = None) -> None:
        self.result.status = "failed"
        self.result.events.append(StepEvent(step.name, step.kind, step.locator, status, detail, repair_index))

    def execute(self, ph: Steps, step: Step) -> None:
        loc = self.overrides.get(step.name, step.locator)
        try:
            perform(ph.page, ph.base_url, step, loc)
            self.result.events.append(StepEvent(step.name, step.kind, step.locator, "passed"))
            return
        except AssertionMismatch as exc:
            # Locator is intact but the value is wrong: a real failure. Never healed.
            self._fail(step, "failed", f"assertion failed with locator intact (not healable): {exc}")
            self.hc.log(f"    ✘ {step.name}: {exc}")
            self.skip.add(step.name)
            if not self.heal:
                raise _AbortTest()
            return
        except LocatorMissing as exc:
            if not self.heal:
                self._fail(step, "failed", str(exc))
                self.hc.log(f"    ✘ {step.name}: {exc}")
                raise _AbortTest()
            self.hc.log(f"    ⚠ {step.name}: {exc} → healing")
            self._heal(ph, step, str(exc))

    def _heal(self, ph: Steps, step: Step, error: str) -> None:
        hc = self.hc
        mem = hc.memory.get(step.test_id, step.name)
        if mem is None:
            self._fail(step, "failed", "no success-run memory for this step; run `proofheal record` on a green build")
            raise _AbortTest()

        tag_base = f"{step.test_id}__{step.index:02d}"
        attempts: list[ReplayResult] = []
        proposal = None
        verdict: Verdict | None = None
        accepted: tuple[str, ReplayResult, Verdict, str] | None = None

        cached = hc.cache.get(step.locator or "")
        candidate_lists: list[tuple[str, list[dict[str, str]]]] = []
        if cached:
            candidate_lists.append(("reused", [{"locator": cached["new_locator"],
                                                "rationale": f"reuse of repair verified at {cached['origin']}"}]))
        candidate_lists.append(("agent", []))

        for source, cands in candidate_lists:
            if source == "agent":
                hc.log("      agent: proposing candidates with the model…")
                proposal = hc.agent.propose(ph.page, step, mem, error)
                cands = proposal.candidates
                hc.log(f"      agent: {len(cands)} candidate(s): " + ", ".join(c["locator"] for c in cands))
            for c in cands:
                n = len(attempts)
                r = replay_candidate(hc.browser, hc.base_url, self.test_fn, step.test_id, mem, c["locator"],
                                     dict(self.overrides), set(self.skip), hc.img_dir, f"{tag_base}-c{n}")
                attempts.append(r)
                failed = [ch.name for ch in r.checks if ch.hard and not ch.passed] + ([r.error] if r.error else [])
                hc.log(f"      replay {c['locator']!r}: " + ("hard checks ✔" if r.hard_passed else f"✘ {', '.join(failed)}"))
                if not r.hard_passed:
                    continue
                if source == "reused":
                    v = Verdict(**{k: cached["verdict"][k] for k in ("accepted", "confidence", "classification", "evidence", "reasoning")})
                    v.source = "reused"
                    v.evidence = v.evidence + [f"verdict reused from {cached['origin']}; replay re-verified here"]
                else:
                    hc.log("      verifier: judging at reasoning_effort=high…")
                    v = judge(hc.llm, step, mem, attempts, proposal.diagnosis if proposal else None)
                verdict = v
                d = gate(r, v)
                if d.accepted:
                    accepted = (c["locator"], r, v, source)
                    break
                hc.log(f"      verifier rejected: {v.reasoning[:120]}")
            if accepted:
                break

        if accepted is None and attempts and not any(a.hard_passed for a in attempts):
            # Every candidate failed hard checks: ask the judge to explain (repair is refused regardless).
            hc.log("      verifier: all candidates failed hard checks; asking for a diagnosis…")
            verdict = judge(hc.llm, step, mem, attempts, proposal.diagnosis if proposal else None, all_failed=True)

        repair: dict[str, Any] = {
            "test_id": step.test_id, "step": step.name, "step_index": step.index, "kind": step.kind,
            "old_locator": step.locator, "expected_text": step.value, "error": error,
            "recorded": {"fingerprint": mem.fingerprint, "url_after": mem.url_after, "outcome": mem.outcome,
                         "crop": _copy_into(mem.crop_path, hc.img_dir, f"{tag_base}-recorded.png")},
            "proposal": proposal.to_dict() if proposal else None,
            "attempts": [a.to_dict() for a in attempts],
            "verdict": verdict.to_dict() if verdict else None,
        }

        if accepted:
            new_loc, r, v, source = accepted
            try:
                perform(ph.page, ph.base_url, step, new_loc)  # apply the proven repair to the live run
            except (LocatorMissing, AssertionMismatch, PlaywrightError) as exc:
                accepted = None
                repair["live_apply_error"] = str(exc)
            else:
                d = gate(r, v)
                repair.update(status="accepted", new_locator=new_loc, source=source,
                              accepted_attempt=attempts.index(r), decision=d.to_dict())
                hc.repairs.append(repair)
                self.overrides[step.name] = new_loc
                hc.cache.setdefault(step.locator or "", {"new_locator": new_loc, "verdict": v.to_dict(),
                                                         "origin": f"{step.test_id}::{step.name}"})
                if self.result.status == "passed":
                    self.result.status = "healed"
                self.result.events.append(StepEvent(step.name, step.kind, step.locator, "healed",
                                                    f"{step.locator} → {new_loc}", len(hc.repairs) - 1))
                hc.log(f"    ✔ healed {step.name}: {step.locator} → {new_loc} (confidence {v.confidence:.2f})")
                return

        reasons = []
        for a in attempts:
            reasons += [f"{a.candidate}: {x}" for x in gate(a, None).reasons if not x.startswith("no verification verdict")]
        if verdict and not verdict.accepted:
            reasons.append(f"verifier ({verdict.classification}): {verdict.reasoning}")
        if not attempts:
            reasons.append("the agent proposed no candidates")
        repair.update(status="refused", new_locator=None, decision={"accepted": False, "reasons": reasons})
        hc.repairs.append(repair)
        self.skip.add(step.name)
        self._fail(step, "refused", "repair refused: " + (verdict.reasoning if verdict else "; ".join(reasons)),
                   len(hc.repairs) - 1)
        hc.log(f"    ✘ REFUSED {step.name}" + (f" — {verdict.classification}" if verdict else ""))
        if step.kind != "expect_text":
            raise _AbortTest()  # an unproven action leaves the page in an unknown state


def run_suite(
    variant: str, tests_dir: str, memory: MemoryStore, *, heal: bool, llm: LLM | None, img_dir: Path, log: Log
) -> tuple[list[TestResult], list[dict[str, Any]]]:
    results: list[TestResult] = []
    with shop_server(variant) as url, browser() as b:
        hc = HealContext(b, url, memory, llm, HealingAgent(llm) if llm else None, img_dir, log)
        for test_id, fn in load_tests(tests_dir):
            log(f"  ▶ {test_id}")
            res = TestResult(test_id)
            ctx = new_context(b)
            page = ctx.new_page()
            PageWatch().attach(page)
            drv = HealDriver(hc, fn, res, heal)
            try:
                fn(Steps(page, url, test_id, driver=drv))
            except _AbortTest:
                done = {e.step for e in res.events}
                for m in memory.for_test(test_id):
                    if m.step_name not in done:
                        res.events.append(StepEvent(m.step_name, m.kind, m.locator, "not_run", "blocked by earlier failure"))
            except (StepFailed, PlaywrightError) as exc:
                res.status, res.error = "failed", str(exc)
            finally:
                ctx.close()
            icon = {"passed": "✔", "healed": "✚", "failed": "✘"}[res.status]
            log(f"  {icon} {test_id}: {res.status.upper()}")
            results.append(res)
    return results, hc.repairs


def _copy_into(src: str | None, dst_dir: Path, name: str) -> str | None:
    if not src or not Path(src).exists():
        return None
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst_dir / name)
    return str(dst_dir / name)


def now_id(variant: str) -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"-{variant}"
