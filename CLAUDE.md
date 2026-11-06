# ProofHeal: context for Claude sessions

Hackathon entry (Meta Global AI Developer Hackathon, Indie Developer track). Self-healing e2e test runner that accepts a locator repair only when an isolated replay proves the step's recorded outcome still holds. Demo story: **heal a renamed button, refuse to hide a broken checkout.**

The spec, hard rules, and acceptance criteria are in **`docs/BRIEF.md`**. Read it before changing behavior.

## Hard rules (short form)

- Original synthetic work only: nothing from the owner's employer or any third party.
- Never print, log, hardcode, or commit the API key. `.env` is gitignored.
- `proof_reports/example/` must be copied from a **real** run. Never hand-write it.
- No silent fallback if the provider lacks JSON-schema output, `reasoning_effort`, or tools: fail loudly.
- No LICENSE file (the owner adds it).
- Report what was actually verified. Don't claim unchecked success.
- Git identity: the owner chose to keep the existing global git author for now. Don't change git config without asking. GitHub remote: `flow6979/proofheal` (public).

## Setup in a new session

```bash
cd proofheal
python3.11 -m venv .venv && .venv/bin/pip install -e . && .venv/bin/playwright install chromium
# API key: the owner puts MODEL_API_KEY=... in ./.env (gitignored). Load without printing:
set -a; . ./.env; set +a
.venv/bin/proofheal check          # capability probe: structured output + reasoning_effort=high + tools
```

## Commands

| Command | What it does |
|---|---|
| `proofheal record --variant v1` | Runs the suite on a green build and stores memory in `.proofheal/memory.db` (+ crops) |
| `proofheal run --variant v2 [--heal]` | Plain run, or heal with proof. Writes `proof_reports/<run_id>/report.{html,json}` |
| `proofheal report [--open] [path]` | Summarizes the latest report (`proof_reports/latest.json`) or a given one |
| `proofheal check` | LLM capability check only |
| `./demo.sh` | record v1 → heal v2 → refuse v2-broken → summary → opens the reports (`NO_OPEN=1` to skip) |
| `SHOP_VARIANT=v1 .venv/bin/pytest tests_e2e` | Plain e2e suite through pytest |
| `.venv/bin/pytest tests_unit` | Unit tests |

## Layout

- `demo_app/`: Flask shop. `SHOP_VARIANT` = `v1` (original), `v2` (renamed ids, sidebar layout, same behavior), or `v2-broken` (v2 templates, but the Pay handler silently returns and the total truncates cents: $58.00 instead of $59.98).
- `tests_e2e/test_shop.py`: two tests written against v1 using the `ph` step API (`ph.goto/click/expect_text(name, locator, value)`).
- `proofheal/harness.py`: `Steps` (the `ph` object), `perform()`, and the exceptions. `LocatorMissing` is healable; `AssertionMismatch` (locator intact, value wrong) is **never** healed.
- `proofheal/fingerprints.py`: JS capture (tag, id, classes, role, name, text, attrs, dom_path, bbox, context = nearest ancestor with more text, e.g. the product card) + weighted similarity.
- `proofheal/memory.py`: SQLite store. Only fully green tests are written. `attach_outcomes` links each action to the assertions that follow it.
- `proofheal/replay.py`: re-runs the test function from scratch in a fresh browser context with the accepted overrides and the candidate. Stops after the target step and checks the outcome.
- `proofheal/agent.py`: tool-calling loop (`get_dom_snapshot`, `get_memory_for_step`, `try_locator`, all read-only) with a page screenshot, then a final JSON-schema call → up to 3 candidates. `reasoning_effort=medium`.
- `proofheal/verifier.py`: `judge()` (`reasoning_effort=high`, JSON-schema verdict `{accepted, confidence, classification, evidence, reasoning}`) and `gate()`.
- `proofheal/runner.py`: `RecordDriver`, `HealDriver`, `run_suite`.
- `proofheal/report.py`: JSON + HTML proof report.
- `proofheal/llm.py`: OpenAI SDK client against the Meta base URL, capability check, strict schema validation.
- `proofheal/cli.py`, `proofheal/env.py`: CLI, the shop subprocess on a free port, and the browser network allowlist.

## Key design decisions

- **Gate** = all *hard* replay checks pass **and** the model verdict is accepted with confidence ≥ 0.7. The model can veto a repair, but can never override a failed hard check.
- **Hard checks.**
  - click: unique match, click performed, URL pattern after the step equals the green run's (`/confirmation/{n}`), page reacted (if it did in the green run), no JS errors.
  - expect_text: unique match + the **original assertion text matches exactly**.
- **Follow-up assertions are soft evidence at action steps.** Each one is enforced exactly at its own step. This is deliberate: otherwise the renamed checkout *link* in v2-broken would be refused because of the downstream total bug. A test still only goes green if every original assertion holds.
- **After a refused assertion** the test is failed but keeps running (diagnostic mode) to gather more evidence. **After a refused action** it aborts, and the remaining steps show as `not_run`. In v2-broken this yields two refusals: total ($58.00 ≠ $59.98) and Pay (stays on /checkout, no `POST /api/order`).
- **Run-level reuse cache.** A repair accepted for an original locator (e.g. `#add-mug`) is reused in later tests. It is re-verified by replay, and the verdict is marked `source: reused`.

## Status (as of 2026-11-06)

Deadlines from the brief:
- Submission: Thu Nov 5, 7:30am IST.
- Credits claim: Wed Nov 4, 9pm ET.
- Judging: Nov 5–8. Keep the repo public through judging.

Check with the owner whether the submission went in before resuming the hackathon-specific steps.

Verified without a key:
- v1 e2e green
- v2 and v2-broken fail
- `record` populates 13 steps
- `run --heal` without a key exits 2 with a setup message
- unit tests 17/17
- Manual replay probe: correct v2 candidates pass hard checks. v2-broken total and Pay are refused with the evidence above. A wrong candidate (`#to-bag` as Pay) is refused.

**Not yet verified (needs `MODEL_API_KEY`):**
- capability check against real `muse-spark-1.3` (the exact `response_format` and `reasoning_effort` acceptance)
- agent loop
- verifier verdicts
- heal on v2 / refuse on v2-broken end to end
- example report
- clean-venv README walkthrough
- Baseten stretch

Known risk: proposing the tee's "Add to bag" button for the mug step passes the hard checks (cart count 1 either way). Only the verifier (product context in the fingerprint) and the checkout total catch it.

Possible API friction to check on the first real call:
- whether tools + a tool-message history are accepted on the final `response_format` call (`agent.py` sends the final call without `tools`)
- whether image input is accepted in the agent prompt

Fix compatibly. Don't remove the capability requirements.

## Next steps, in order

1. Owner provides the key in `.env`. Run `proofheal check`.
2. `./demo.sh` (`NO_OPEN=1`). Fix issues. Expect v2 exit 0 and v2-broken exit ≠ 0 with refusals on the total and Pay.
3. Copy one real run (ideally the v2-broken run, or both into `example/v2` and `example/v2-broken`) to `proof_reports/example/` and commit it.
4. Fill in README sections marked TODO with real results: architecture diagram, demo script, "Built with Meta Model API", 3-min video outline, token usage/cost from `report.json` → `llm.calls`.
5. Validate the README quickstart in a fresh clone + fresh venv. Note the real friction.
6. Stretch: Baseten (`PROOFHEAL_BASE_URL/MODEL/API_KEY`). Document it only if the capability check passes.
7. Owner: add a LICENSE, record the video, submit on Cerebral Valley. Submission-form drafts live outside the repo (see the owner's brief).
