import json
from types import SimpleNamespace

import pytest

from demo_app.app import create_app, order_total_cents
from proofheal import fingerprints, report
from proofheal.llm import LLM, IncompatibleProvider, LLMConfig, SetupError
from proofheal.memory import MemoryStore, StepMemory, attach_outcomes
from proofheal.replay import Check, ReplayResult, url_pattern
from proofheal.runner import StepEvent, TestResult
from proofheal.verifier import Verdict, gate

BUTTON = {"tag": "button", "id": "add-mug", "classes": ["btn-add"], "role": "button", "name": "Add to cart",
          "text": "Add to cart", "attrs": {"type": "submit"}, "dom_path": "", "bbox": {"x": 40, "y": 200, "w": 100, "h": 30},
          "context": "Ceramic Mug $24.99 Add to cart"}


# ---------------------------------------------------------------- fingerprints
def test_similarity_identical_is_one():
    assert fingerprints.similarity(BUTTON, BUTTON)["total"] == 1.0


def test_similarity_prefers_restyled_counterpart_over_other_element():
    restyled = {**BUTTON, "id": None, "classes": ["cta"], "name": "Add to bag", "text": "Add to bag",
                "attrs": {"type": "submit", "data-testid": "bag-add-mug"}, "bbox": {"x": 260, "y": 150, "w": 110, "h": 38},
                "context": "Ceramic Mug $24.99 Add to bag"}
    heading = {"tag": "h1", "id": None, "classes": [], "role": "heading", "name": "Tiny Shop Catalog", "text": "Tiny Shop Catalog",
               "attrs": {}, "dom_path": "", "bbox": {"x": 260, "y": 40, "w": 400, "h": 40}, "context": ""}
    assert fingerprints.similarity(BUTTON, restyled)["total"] > fingerprints.similarity(BUTTON, heading)["total"] + 0.3


def test_similarity_context_distinguishes_products():
    mug = {**BUTTON, "context": "Ceramic Mug $24.99 Add to bag"}
    tee = {**BUTTON, "context": "Cotton Tee $34.99 Add to bag"}
    s_mug, s_tee = fingerprints.similarity(BUTTON, mug), fingerprints.similarity(BUTTON, tee)
    assert s_mug["context"] > s_tee["context"]


# ---------------------------------------------------------------- memory
def _mem(i, name, kind, loc=None, val=None):
    return StepMemory("t", name, i, kind, loc, val, None, None, "u", "u", True)


def test_attach_outcomes_groups_assertions_after_each_action():
    recs = [_mem(0, "open", "goto", val="/"), _mem(1, "click", "click", "#a"),
            _mem(2, "a1", "expect_text", "#x", "1"), _mem(3, "a2", "expect_text", "#y", "2"), _mem(4, "pay", "click", "#p")]
    attach_outcomes(recs)
    assert [o["step"] for o in recs[1].outcome] == ["a1", "a2"]
    assert recs[4].outcome == []


def test_memory_roundtrip(tmp_path):
    store = MemoryStore(tmp_path / "m.db")
    rec = StepMemory("t1", "add", 1, "click", "#a", None, BUTTON, None, "http://x/", "http://x/", True,
                     [{"step": "c", "locator": "#c", "expected_text": "1"}], "v1")
    store.save_test([rec])
    got = store.get("t1", "add")
    assert got.fingerprint == BUTTON and got.outcome == rec.outcome and got.dom_changed is True
    store.save_test([rec])  # re-record replaces, never duplicates
    assert store.count() == 1


# ---------------------------------------------------------------- verification gate
def _verdict(accepted=True, conf=0.9):
    return Verdict(accepted, conf, "cosmetic_ui_change", ["e"], "r")


def test_gate_accepts_only_when_hard_checks_and_verdict_agree():
    r = ReplayResult("#x", checks=[Check("original assertion", True, "ok"), Check("fingerprint similarity", False, "low", hard=False)])
    assert gate(r, _verdict()).accepted


def test_gate_model_cannot_override_failed_hard_check():
    r = ReplayResult("#x", checks=[Check("navigation outcome", False, "stayed on /checkout")])
    d = gate(r, _verdict(accepted=True, conf=1.0))
    assert not d.accepted and any("navigation outcome" in x for x in d.reasons)


def test_gate_model_can_veto_and_low_confidence_rejects():
    r = ReplayResult("#x", checks=[Check("original assertion", True, "ok")])
    assert not gate(r, _verdict(accepted=False)).accepted
    assert not gate(r, _verdict(conf=0.5)).accepted
    assert not gate(r, None).accepted


def test_gate_rejects_replay_error_and_empty_checks():
    assert not gate(ReplayResult("#x", error="prefix failed"), _verdict()).accepted
    assert not gate(ReplayResult("#x"), _verdict()).accepted


def test_url_pattern_normalizes_ids():
    assert url_pattern("http://127.0.0.1:5000/confirmation/1001?x=1") == "/confirmation/{n}"
    assert url_pattern("http://127.0.0.1:5000") == "/"


# ---------------------------------------------------------------- llm config / schema enforcement
def test_missing_key_is_a_clear_setup_error(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("PROOFHEAL_API_KEY", raising=False)
    with pytest.raises(SetupError, match="MODEL_API_KEY"):
        LLMConfig.from_env()


def test_key_is_not_in_config_repr(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "sk-test-not-real")
    assert "sk-test-not-real" not in repr(LLMConfig.from_env())


def test_structured_output_that_violates_schema_is_rejected_not_repaired(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "sk-test-not-real")
    llm = LLM(LLMConfig.from_env())
    bad = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"ok": "yes"})))], usage=None)
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: bad)))
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    with pytest.raises(IncompatibleProvider):
        llm.structured("t", [{"role": "user", "content": "x"}], "s", schema, reasoning_effort="high")


# ---------------------------------------------------------------- report
def test_report_build_and_render(tmp_path):
    res = [TestResult("test_a", "healed", [StepEvent("s", "click", "#a", "healed", "#a → #b", 0)]),
           TestResult("test_b", "failed", [StepEvent("t", "expect_text", "#t", "refused", "wrong total", 1)])]
    rep_attempt = ReplayResult("#b", checks=[Check("navigation outcome", True, "ok")]).to_dict()
    repairs = [
        {"test_id": "test_a", "step": "s", "kind": "click", "old_locator": "#a", "new_locator": "#b", "status": "accepted",
         "expected_text": None, "recorded": {"crop": None}, "attempts": [rep_attempt], "accepted_attempt": 0,
         "verdict": _verdict().to_dict(), "decision": {"accepted": True, "reasons": ["ok"]}, "proposal": None},
        {"test_id": "test_b", "step": "t", "kind": "expect_text", "old_locator": "#t", "new_locator": None, "status": "refused",
         "expected_text": "$59.98", "recorded": {"crop": None}, "attempts": [], "verdict": None,
         "decision": {"accepted": False, "reasons": ["shows $58.00"]}, "proposal": None},
    ]
    rep = report.build({"run_id": "r1", "variant": "v2", "mode": "heal", "started": "now", "llm": None}, res, repairs, tmp_path)
    assert rep["summary"] == {"tests": 2, "passed": 0, "healed": 1, "failed": 1, "repairs_accepted": 1, "repairs_refused": 1}
    jp, hp = report.write(rep, tmp_path)
    page = hp.read_text()
    assert "PROVEN REPAIR" in page and "REFUSED" in page and "$59.98" in page
    assert json.loads(jp.read_text())["run_id"] == "r1"


# ---------------------------------------------------------------- demo shop
def test_shop_totals_correct_and_broken():
    cart = {"mug": 1, "tee": 1}
    assert order_total_cents(cart, broken=False) == 5998
    assert order_total_cents(cart, broken=True) == 5800


@pytest.mark.parametrize("variant,ids", [("v1", ["add-mug", "pay-btn"]), ("v2", ["bag-add-mug", "complete-purchase"])])
def test_shop_variants_render(variant, ids):
    client = create_app(variant).test_client()
    client.post("/cart/add", data={"sku": "mug"})
    html = client.get("/").data.decode() + client.get("/checkout").data.decode()
    assert all(i in html for i in ids)
