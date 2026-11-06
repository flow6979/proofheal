# ProofHeal

**Self-healing end-to-end tests that only accept a repair they can prove.**

When the UI changes, ordinary AI test healers swap in whatever element looks right and turn the test green. But a green test isn't proof: the healer might have clicked the wrong button, or hidden a real bug. ProofHeal heals a renamed button, but refuses to hide a broken checkout.

> **Status: work in progress.** The record → replay → verification pipeline runs locally. The model-driven healing (Meta Model API, `muse-spark-1.3`) is implemented but has **not yet been run against the live API**. This README will be updated with real results and a committed example report from an actual run.

## How it works

1. **Record success-run memory.** On a green run, ProofHeal fingerprints every element a test touches (role, accessible name, text, attributes, DOM path, bounding box, surrounding context, screenshot crop). It also stores the outcome each step must preserve: the URL reached and the assertions that held.
2. **Propose repairs.** When a locator stops matching, an agent on Muse Spark 1.3 inspects the live page with read-only tools (`get_dom_snapshot`, `get_memory_for_step`, `try_locator`) and proposes candidate locators as structured JSON.
3. **Verify in isolation.** Each candidate is replayed from scratch in a fresh browser context. It is accepted only if the deterministic checks hold and a high-reasoning-effort verdict agrees. Deterministic checks: the original assertion still passes, the step reaches the same page as in the green run, the page reacts, and there are no JS errors.
4. **Ship proof.** Every accepted or refused repair is written to an HTML + JSON proof report with before/after screenshots, the old and new locator, each check result, and the model's reasoning.

```
 green run (v1)                       later run (UI changed)
 ──────────────                       ───────────────────────
 tests_e2e ──► RecordDriver           tests_e2e ──► HealDriver ── locator missing?
                 │ fingerprints,                         │
                 │ outcomes                              ▼
                 ▼                          HealingAgent (muse-spark-1.3, tools)
          .proofheal/memory.db  ◄────────── get_memory_for_step / get_dom_snapshot / try_locator
                                                          │ candidates (JSON schema)
                                                          ▼
                                            Isolated replay (fresh browser context)
                                                          │ hard checks
                                                          ▼
                                            Verifier (reasoning_effort=high, JSON verdict)
                                                          │
                                       accept + apply ◄───┴───► refuse + explain
                                                          ▼
                                            proof_reports/<run>/report.{html,json}
```

## Quickstart

Requires Python 3.11+ and a Meta Model API key.

```bash
git clone https://github.com/flow6979/proofheal && cd proofheal
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
export MODEL_API_KEY=<your Meta Model API key>   # or put it in .env (gitignored)

proofheal record --variant v1        # green run, captures success-run memory
proofheal run --variant v2 --heal    # restyled UI: heal with proof
proofheal run --variant v2-broken --heal   # broken checkout: must refuse
proofheal report --open
# or all at once:
./demo.sh
```

Without a key, `record`, plain `run`, and the unit tests still work: `pytest tests_unit`.

## The demo shop

`demo_app/` is a tiny synthetic Flask shop with three variants, selected via `SHOP_VARIANT`:

| Variant | What changed | Expected ProofHeal behavior |
|---|---|---|
| `v1` | original UI the tests target | green, record memory |
| `v2` | renamed ids/classes, new layout, "Add to bag", "Complete purchase" | heal every broken locator, with proof |
| `v2-broken` | v2 look, but Pay silently does nothing and the total is $58.00 instead of $59.98 | heal cosmetic changes, **refuse** the total and Pay repairs, exit non-zero |

## License

The license will be added by the owner.
