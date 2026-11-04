"""Isolated replay: re-run a test in a fresh browser context with a candidate repair.

The test function is executed again from the start in a brand-new context (own
cookies, own cart). Earlier steps use already-accepted repairs; the target step
uses the candidate locator. Execution stops right after the target step, and the
step's recorded outcome is checked against what actually happened.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from . import fingerprints
from .env import new_context
from .harness import (
    AssertionMismatch,
    LocatorMissing,
    PageWatch,
    Step,
    Steps,
    TestFn,
    element_text,
    perform,
    settle,
)
from .memory import StepMemory


def url_pattern(url: str | None) -> str | None:
    if url is None:
        return None
    path = re.sub(r"^https?://[^/]+", "", url).split("?")[0] or "/"
    return re.sub(r"\d+", "{n}", path)


def page_state(page: Page) -> dict[str, str]:
    text = page.evaluate("() => document.body ? document.body.innerText : ''")
    return {"url": page.url, "url_pattern": url_pattern(page.url), "dom_hash": hashlib.sha1(text.encode()).hexdigest()[:12]}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    hard: bool = True  # hard checks gate acceptance; soft checks are evidence only

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail, "hard": self.hard}


@dataclass
class ReplayResult:
    candidate: str
    checks: list[Check] = field(default_factory=list)
    error: str | None = None
    live_fingerprint: dict[str, Any] | None = None
    similarity: dict[str, float] | None = None
    observed_text: str | None = None
    network: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    before_png: str | None = None
    after_png: str | None = None
    crop_png: str | None = None

    @property
    def hard_passed(self) -> bool:
        return self.error is None and bool(self.checks) and all(c.passed for c in self.checks if c.hard)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "hard_checks_passed": self.hard_passed,
            "checks": [c.to_dict() for c in self.checks],
            "error": self.error,
            "observed_text": self.observed_text,
            "similarity": self.similarity,
            "network": self.network,
            "page_errors": self.page_errors,
            "screenshots": {"before": self.before_png, "after": self.after_png, "element": self.crop_png},
        }


class _Stop(Exception):
    pass


class _PrefixFailed(Exception):
    pass


class ReplayDriver:
    def __init__(self, target: str, overrides: dict[str, str], skip: set[str], on_target):
        self.target, self.overrides, self.skip, self.on_target = target, overrides, skip, on_target

    def execute(self, ph: Steps, step: Step) -> None:
        if step.name == self.target:
            self.on_target(ph, step)
            raise _Stop()
        if step.name in self.skip:
            return
        try:
            perform(ph.page, ph.base_url, step, self.overrides.get(step.name, step.locator))
        except (LocatorMissing, AssertionMismatch, PlaywrightError) as exc:
            raise _PrefixFailed(f"prefix step {step.name!r} failed during replay: {exc}") from exc


def replay_candidate(
    browser: Browser,
    base_url: str,
    test_fn: TestFn,
    test_id: str,
    mem: StepMemory,
    candidate: str,
    overrides: dict[str, str],
    skip: set[str],
    out_dir: Path,
    tag: str,
) -> ReplayResult:
    result = ReplayResult(candidate=candidate)
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = new_context(browser)
    page = ctx.new_page()
    watch = PageWatch()
    requests: list[str] = []

    def on_target(ph: Steps, step: Step) -> None:
        loc = page.locator(candidate)
        try:
            count = loc.count()
        except PlaywrightError as exc:
            result.error = f"invalid locator: {exc}"
            return
        result.checks.append(Check("unique match", count == 1, f"{count} element(s) match {candidate!r}"))
        if count == 0:
            result.error = "candidate matched nothing in replay"
            return
        el = loc.first
        try:
            el.wait_for(state="visible", timeout=2500)
        except PlaywrightError:
            result.checks.append(Check("visible", False, "candidate element is not visible"))
            return
        result.live_fingerprint = fingerprints.capture(el)
        if mem.fingerprint:
            result.similarity = fingerprints.similarity(mem.fingerprint, result.live_fingerprint)
            result.checks.append(Check(
                "fingerprint similarity", result.similarity["total"] >= 0.5,
                f"{result.similarity['total']:.2f} vs recorded element (role {result.live_fingerprint['role']!r}, "
                f"name {result.live_fingerprint['name']!r})", hard=False,
            ))
        result.crop_png = _shot(el.screenshot, out_dir / f"{tag}-element.png")
        result.before_png = _shot(page.screenshot, out_dir / f"{tag}-before.png")
        before = page_state(page)
        watch.errors.clear()
        page.on("request", lambda r: r.url.startswith(base_url + "/api") and requests.append(f"{r.method} {r.url[len(base_url):]}"))

        if step.kind == "click":
            try:
                el.click(timeout=2500)
                settle(page)
                page.wait_for_timeout(400)
                settle(page)
                result.checks.append(Check("action performed", True, "click dispatched"))
            except PlaywrightError as exc:
                result.checks.append(Check("action performed", False, f"click failed: {exc}"))
                return
            after = page_state(page)
            result.after_png = _shot(page.screenshot, out_dir / f"{tag}-after.png")
            exp = url_pattern(mem.url_after)
            result.checks.append(Check(
                "navigation outcome", after["url_pattern"] == exp,
                f"page is {after['url_pattern']} after the action; green run reached {exp}",
            ))
            if mem.dom_changed:
                changed = after["dom_hash"] != before["dom_hash"] or after["url"] != before["url"]
                result.checks.append(Check(
                    "page reacted", changed,
                    "page content changed after the action" if changed else "page content did not change at all",
                ))
            # Follow-up assertions are evidence here; each is enforced exactly (hard) at its own step,
            # so a test can only go green if every original assertion holds.
            for o in mem.outcome:
                visible = _text_visible(page, o["expected_text"])
                result.checks.append(Check(
                    f"outcome preview: {o['step']}", visible,
                    f"text {o['expected_text']!r} {'is' if visible else 'is NOT'} visible after the action",
                    hard=False,
                ))
        elif step.kind == "expect_text":
            actual = element_text(el)
            result.observed_text = actual
            result.after_png = result.before_png
            result.checks.append(Check(
                "original assertion", actual == step.value,
                f"element shows {actual!r}; original test expects {step.value!r}",
            ))
        result.network = list(requests)
        result.page_errors = list(watch.errors)
        result.checks.append(Check(
            "no page errors", not watch.errors, "no JS errors" if not watch.errors else "; ".join(watch.errors)[:300]
        ))

    watch.attach(page)
    try:
        test_fn(Steps(page, base_url, test_id, driver=ReplayDriver(mem.step_name, overrides, skip, on_target)))
        result.error = result.error or "target step was never reached"
    except _Stop:
        pass
    except _PrefixFailed as exc:
        result.error = str(exc)
    except PlaywrightError as exc:
        result.error = f"replay error: {exc}"
    finally:
        ctx.close()
    return result


def _text_visible(page: Page, text: str) -> bool:
    try:
        loc = page.get_by_text(text, exact=True)
        return any(loc.nth(i).is_visible() for i in range(min(loc.count(), 5)))
    except PlaywrightError:
        return False


def _shot(fn, path: Path) -> str | None:
    try:
        fn(path=str(path))
        return str(path)
    except PlaywrightError:
        return None
