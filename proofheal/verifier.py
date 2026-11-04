"""Verification gate: decide whether a replayed candidate repair is proven.

A repair is accepted only when BOTH hold:
  1. every hard deterministic check from the isolated replay passed
     (original assertion / recorded navigation outcome / page reacted / unique match / no page errors), and
  2. the model's verification judgment (reasoning_effort="high", JSON-schema verdict)
     accepts it with confidence >= MIN_CONFIDENCE.
The model can veto a repair that passed replay; it can never override a failed hard check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .harness import Step
from .llm import LLM
from .memory import StepMemory
from .replay import ReplayResult

MIN_CONFIDENCE = 0.7

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "accepted": {"type": "boolean"},
        "confidence": {"type": "number", "description": "0.0 to 1.0"},
        "classification": {"type": "string", "enum": ["cosmetic_ui_change", "genuine_product_regression", "inconclusive"]},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": ["accepted", "confidence", "classification", "evidence", "reasoning"],
    "additionalProperties": False,
}

SYSTEM = """You are ProofHeal's verification judge. A UI test step broke and a repair (new locator) was replayed in an isolated browser.
Decide whether the repair is PROVEN correct: the new element is the true counterpart of the recorded one AND the step's recorded outcome still holds.
A green test is not proof. Reject when evidence is weak, when the candidate merely displays the expected text without being the same element's counterpart,
or when the outcome differs from the green run (e.g. no navigation, no request sent, wrong amount). If the evidence shows the product itself is broken,
classify as genuine_product_regression and explain concretely what is broken. Cite specific evidence."""


@dataclass
class Verdict:
    accepted: bool
    confidence: float
    classification: str
    evidence: list[str]
    reasoning: str
    source: str = "model"  # model | reused

    def to_dict(self) -> dict[str, Any]:
        return {"accepted": self.accepted, "confidence": self.confidence, "classification": self.classification,
                "evidence": self.evidence, "reasoning": self.reasoning, "source": self.source}


@dataclass
class Decision:
    accepted: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"accepted": self.accepted, "reasons": self.reasons}


def gate(replay: ReplayResult, verdict: Verdict | None) -> Decision:
    reasons: list[str] = []
    if replay.error:
        reasons.append(f"replay incomplete: {replay.error}")
    for c in replay.checks:
        if c.hard and not c.passed:
            reasons.append(f"hard check failed - {c.name}: {c.detail}")
    if not replay.checks:
        reasons.append("no verification checks ran")
    if verdict is None:
        reasons.append("no verification verdict")
    else:
        if not verdict.accepted:
            reasons.append(f"verifier rejected ({verdict.classification}): {verdict.reasoning}")
        elif verdict.confidence < MIN_CONFIDENCE:
            reasons.append(f"verifier confidence {verdict.confidence:.2f} < {MIN_CONFIDENCE}")
    return Decision(accepted=not reasons, reasons=reasons or ["all hard checks passed and verifier accepted"])


def judge(
    llm: LLM, step: Step, mem: StepMemory, results: list[ReplayResult], diagnosis: str | None, *, all_failed: bool = False
) -> Verdict:
    payload = {
        "step": {"test": step.test_id, "name": step.name, "kind": step.kind, "original_locator": step.locator,
                 "original_expected_text": step.value},
        "success_run_memory": {"fingerprint": mem.fingerprint, "url_before": mem.url_before,
                               "url_after": mem.url_after, "page_changed_after_step": mem.dom_changed,
                               "assertions_that_held_after_step": mem.outcome},
        "agent_diagnosis": diagnosis,
        "replayed_candidates": [
            {k: v for k, v in r.to_dict().items() if k != "screenshots"} | {"live_fingerprint": r.live_fingerprint}
            for r in results
        ],
        "instruction": (
            "Every candidate failed at least one hard deterministic check, so no repair will be applied. "
            "Explain from the evidence whether this is a UI change the agent failed to locate or a genuine product regression."
            if all_failed else
            "Judge the LAST candidate in replayed_candidates. Earlier entries are context."
            if len(results) > 1 else "Judge the candidate."
        ),
    }
    data = llm.structured(
        f"verify:{step.name}",
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(payload, indent=1)}],
        "repair_verdict", VERDICT_SCHEMA, reasoning_effort="high",
    )
    return Verdict(data["accepted"], float(data["confidence"]), data["classification"], data["evidence"], data["reasoning"])
