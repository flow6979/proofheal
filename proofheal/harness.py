"""The `ph` step API that e2e tests are written against, plus pluggable drivers.

A test is a plain function `def test_x(ph): ...` that calls named steps:

    ph.goto("open catalog", "/")
    ph.click("add mug", "#add-mug")
    ph.expect_text("cart shows 1", "#cart-count", "1")

Because tests are deterministic step sequences, ProofHeal can re-execute a test
in a fresh browser context (isolated replay) with candidate locator overrides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

DEFAULT_TIMEOUT_MS = 2500
ACTIONS = {"goto", "click"}


@dataclass
class Step:
    test_id: str
    index: int
    name: str
    kind: str  # goto | click | expect_text
    locator: str | None = None
    value: str | None = None

    @property
    def is_action(self) -> bool:
        return self.kind in ACTIONS


class LocatorMissing(Exception):
    """The step's locator matched no visible element: a candidate for healing."""


class AssertionMismatch(AssertionError):
    """The element was found but its value differs: never healed."""


class StepFailed(Exception):
    """A step could not be completed (and was not healed)."""


class Driver(Protocol):
    def execute(self, ph: "Steps", step: Step) -> None: ...


@dataclass
class PageWatch:
    """Collects JS errors from a page so verification can assert there were none."""

    errors: list[str] = field(default_factory=list)

    def attach(self, page: Page) -> "PageWatch":
        page.on("pageerror", lambda exc: self.errors.append(f"pageerror: {exc}"))
        page.on("console", lambda msg: msg.type == "error" and self.errors.append(f"console.error: {msg.text}"))
        return self


def resolve(page: Page, locator: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Locator:
    loc = page.locator(locator)
    try:
        loc.first.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightError as exc:
        raise LocatorMissing(f"no visible element for {locator!r}") from exc
    return loc.first


def settle(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=3000)
    except PlaywrightError:
        pass
    page.wait_for_timeout(150)


def element_text(el: Locator) -> str:
    return re.sub(r"\s+", " ", el.inner_text()).strip()


def perform(page: Page, base_url: str, step: Step, locator: str | None, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    """Execute one step using `locator` (which may differ from step.locator when healed)."""
    if step.kind == "goto":
        page.goto(base_url + (step.value or "/"))
        settle(page)
        return
    assert locator is not None
    el = resolve(page, locator, timeout_ms)
    if step.kind == "click":
        el.click(timeout=timeout_ms)
        settle(page)
    elif step.kind == "expect_text":
        expected = step.value or ""
        deadline_checks = max(1, timeout_ms // 250)
        actual = element_text(el)
        for _ in range(deadline_checks):
            if actual == expected:
                return
            page.wait_for_timeout(250)
            actual = element_text(el)
        raise AssertionMismatch(f"{locator!r} shows {actual!r}, expected {expected!r}")
    else:
        raise ValueError(f"unknown step kind {step.kind}")


class PlainDriver:
    """Runs steps exactly as written. Used by pytest and by `proofheal run` without --heal."""

    def execute(self, ph: "Steps", step: Step) -> None:
        perform(ph.page, ph.base_url, step, step.locator)


class Steps:
    """The `ph` object handed to each test function."""

    def __init__(self, page: Page, base_url: str, test_id: str, driver: Driver | None = None):
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.test_id = test_id
        self.driver: Driver = driver or PlainDriver()
        self._index = 0
        self._names: set[str] = set()

    def _run(self, kind: str, name: str, locator: str | None, value: str | None) -> None:
        if name in self._names:
            raise ValueError(f"duplicate step name {name!r} in {self.test_id}")
        self._names.add(name)
        step = Step(self.test_id, self._index, name, kind, locator, value)
        self._index += 1
        self.driver.execute(self, step)

    def goto(self, name: str, path: str) -> None:
        self._run("goto", name, None, path)

    def click(self, name: str, locator: str) -> None:
        self._run("click", name, locator, None)

    def expect_text(self, name: str, locator: str, expected: str) -> None:
        self._run("expect_text", name, locator, expected)


TestFn = Callable[[Steps], None]
