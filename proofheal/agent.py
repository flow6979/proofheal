"""Healing agent: proposes candidate repairs for a failed step using tools on the live page.

Tools (read-only, they never act on the page):
  get_dom_snapshot     compact list of visible elements with suggested selectors
  get_memory_for_step  the success-run memory for the failing step
  try_locator          resolve a selector on the live page, report matches + fingerprint similarity
The final answer is a structured (JSON-schema) list of candidate locators.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from . import fingerprints
from .harness import Step
from .llm import LLM
from .memory import StepMemory

MAX_TOOL_TURNS = 8
MAX_CANDIDATES = 3

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string", "description": "What changed on the page and why the step failed."},
        "likely_cause": {"type": "string", "enum": ["ui_change", "product_regression", "unknown"]},
        "candidates": {
            "type": "array",
            "description": f"Up to {MAX_CANDIDATES} CSS selectors, best first.",
            "items": {
                "type": "object",
                "properties": {"locator": {"type": "string"}, "rationale": {"type": "string"}},
                "required": ["locator", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["diagnosis", "likely_cause", "candidates"],
    "additionalProperties": False,
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_dom_snapshot",
            "description": "List visible elements on the current page (tag, role, text, key attributes, a suggested unique CSS selector).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_memory_for_step",
            "description": "Success-run memory for the failing step: recorded element fingerprint and the outcome it must preserve.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "try_locator",
            "description": "Resolve a CSS selector on the live page without acting. Returns match count, the element's fingerprint, and similarity to the recorded element.",
            "parameters": {
                "type": "object",
                "properties": {"locator": {"type": "string", "description": "CSS selector"}},
                "required": ["locator"],
            },
        },
    },
]

SYSTEM = """You are ProofHeal's repair agent. An end-to-end UI test step failed because its locator no longer matches anything.
Your job: find the element on the CURRENT page that plays the same role the recorded element played in the green run, and propose CSS selectors for it.

Rules:
- Use the tools. Inspect memory first, then the DOM, then confirm each candidate with try_locator (it must match exactly one element).
- Prefer stable selectors: id, data-testid, then distinctive attribute combos. No :nth-child chains unless unavoidable.
- Match on purpose, not on surface: the same action (e.g. "add this product", "pay") or the same displayed value role (e.g. "the order total").
- Never propose an element just because it happens to display the expected text; it must be the counterpart of the recorded element.
- If the page suggests the product itself is broken rather than restyled, say so in the diagnosis and set likely_cause accordingly. Still propose the closest counterpart if one exists; a separate verification gate decides.
- Your candidates will be replayed in isolation and verified against the recorded outcome. You cannot make a test pass by proposal alone."""


@dataclass
class Proposal:
    diagnosis: str
    likely_cause: str
    candidates: list[dict[str, str]]
    tool_trace: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"diagnosis": self.diagnosis, "likely_cause": self.likely_cause,
                "candidates": self.candidates, "tool_trace": self.tool_trace}


_SNAPSHOT_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const sel = (e) => {
    if (e.id) return '#' + CSS.escape(e.id);
    const tid = e.getAttribute('data-testid');
    if (tid) return `[data-testid="${tid}"]`;
    let s = e.tagName.toLowerCase();
    if (e.classList.length) s += '.' + Array.from(e.classList).map(c => CSS.escape(c)).join('.');
    if (document.querySelectorAll(s).length === 1) return s;
    return null;
  };
  const out = [];
  const els = document.querySelectorAll('h1,h2,h3,h4,button,a,input,select,textarea,label,strong,b,span,td,p,[data-testid],[role]');
  for (const e of els) {
    const r = e.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (e.type === 'hidden') continue;
    const text = norm(e.innerText).slice(0, 80);
    const attrs = {};
    for (const a of e.attributes) if (['data-testid','name','type','href','aria-label','role','disabled','value'].includes(a.name)) attrs[a.name] = a.value.slice(0, 60);
    out.push({ selector: sel(e), tag: e.tagName.toLowerCase(), id: e.id || null, classes: Array.from(e.classList), text, attrs,
               pos: [Math.round(r.x), Math.round(r.y)] });
    if (out.length >= 150) break;
  }
  return { url: location.pathname, title: document.title, elements: out };
}
"""


class HealingAgent:
    def __init__(self, llm: LLM):
        self.llm = llm

    def propose(self, page: Page, step: Step, mem: StepMemory, error: str) -> Proposal:
        trace: list[dict[str, Any]] = []
        mem_view = _memory_view(mem)

        def run_tool(name: str, args: dict[str, Any]) -> Any:
            if name == "get_dom_snapshot":
                return page.evaluate(_SNAPSHOT_JS)
            if name == "get_memory_for_step":
                return mem_view
            if name == "try_locator":
                return _try_locator(page, args.get("locator", ""), mem)
            return {"error": f"unknown tool {name}"}

        shot = base64.b64encode(page.screenshot(type="jpeg", quality=55)).decode()
        user_text = (
            f"Test `{step.test_id}`, step {step.index} `{step.name}` ({step.kind}) failed.\n"
            f"Original locator: `{step.locator}`\n"
            + (f"Original assertion: element text must equal {step.value!r}\n" if step.kind == "expect_text" else "")
            + f"Error: {error}\nCurrent URL: {page.url}\n"
            "A screenshot of the current page is attached. Investigate with the tools, then answer."
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{shot}"}},
            ]},
        ]
        for _ in range(MAX_TOOL_TURNS):
            resp = self.llm.chat(f"propose:{step.name}", messages, tools=TOOLS, tool_choice="auto", reasoning_effort="medium")
            msg = resp.choices[0].message
            if not msg.tool_calls:
                break
            messages.append({
                "role": "assistant", "content": msg.content or "",
                "tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                               for tc in msg.tool_calls],
            })
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = run_tool(tc.function.name, args)
                trace.append({"tool": tc.function.name, "args": args, "result_summary": _summarize(tc.function.name, out)})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(out)[:30000]})

        messages.append({"role": "user", "content": "Give your final answer now as JSON matching the schema."})
        data = self.llm.structured(
            f"propose-final:{step.name}", messages, "repair_candidates", PROPOSAL_SCHEMA, reasoning_effort="medium"
        )
        cands = [c for c in data["candidates"] if c["locator"].strip()][:MAX_CANDIDATES]
        return Proposal(data["diagnosis"], data["likely_cause"], cands, trace)


def _memory_view(mem: StepMemory) -> dict[str, Any]:
    return {
        "step": mem.step_name, "kind": mem.kind, "original_locator": mem.locator, "expected_text": mem.value,
        "recorded_fingerprint": mem.fingerprint, "url_before": mem.url_before, "url_after": mem.url_after,
        "page_changed_after_step": mem.dom_changed, "assertions_that_held_after_step": mem.outcome,
    }


def _try_locator(page: Page, locator: str, mem: StepMemory) -> dict[str, Any]:
    try:
        loc = page.locator(locator)
        n = loc.count()
    except PlaywrightError as exc:
        return {"locator": locator, "error": f"invalid selector: {str(exc)[:200]}"}
    out: dict[str, Any] = {"locator": locator, "count": n}
    if n:
        fp = fingerprints.capture(loc.first)
        out["visible"] = loc.first.is_visible()
        out["fingerprint"] = {k: fp[k] for k in ("tag", "id", "classes", "role", "name", "text", "attrs", "context")}
        if mem.fingerprint:
            out["similarity_to_recorded"] = fingerprints.similarity(mem.fingerprint, fp)
    return out


def _summarize(tool: str, out: Any) -> str:
    if tool == "get_dom_snapshot":
        return f"{len(out.get('elements', []))} elements on {out.get('url')}"
    if tool == "get_memory_for_step":
        return "recorded fingerprint + outcome"
    if tool == "try_locator":
        if "error" in out:
            return out["error"]
        sim = out.get("similarity_to_recorded", {}).get("total")
        return f"{out['count']} match(es)" + (f", similarity {sim}" if sim is not None else "")
    return ""
