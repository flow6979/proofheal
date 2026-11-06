# ProofHeal build brief (source of truth)

This is the original build spec for the Meta Global AI Developer Hackathon entry. `CLAUDE.md` tracks what is done; this file says what "done" means.

## What ProofHeal is

ProofHeal is a self-healing test runner with a verification gate. Ordinary self-healing tools make a failing test pass by swapping in whatever element the AI guesses, but a green test is not enough evidence that the repair is correct. ProofHeal instead:

1. **Records success-run memory.** During green runs it captures a fingerprint of every element the test interacts with (role, accessible name, text, key attributes, DOM path, bounding box, screenshot crop) plus the expected outcome of each step (the assertions that held).
2. **Proposes repairs on failure.** When a test later fails because the UI changed, an agent retrieves the relevant success-run memory plus the live failure context (error, DOM snapshot, screenshot) and proposes candidate repairs (new locators).
3. **Verifies before accepting.** Each candidate is executed in an isolated replay. A repair is accepted only if the step's expected outcome is verified. Weak evidence leaves the test failed, and the runner explains why.
4. **Ships proof with every repair.** Before/after screenshots, old and new locator, verification results, and the model's rationale, written to a proof report (HTML + JSON).

Demo story: **heal a renamed button, but refuse to hide a broken checkout.**

## Hard rules

- **Original work only.** All demo code and data are synthetic and created in this repo. Nothing from the owner's employer or any other third party may be copied, adapted, or referenced.
- **No secrets** in the repo or logs. The key comes from `MODEL_API_KEY` (or `PROOFHEAL_API_KEY`). Never hardcode, print, or commit it.
- **No fabricated evidence.** The committed example report (`proof_reports/example/`) must be the real output of an actual run. Never hand-write or mock it.
- **No silent downgrades.** If the provider lacks structured output (JSON schema), `reasoning_effort`, or tool calling, fail with a clear message. Never emulate them.
- **If an external input is missing** (e.g. the API key), stop and say what is needed. Don't mock around it.
- **No LICENSE file.** The owner adds it. The README carries a one-line note.
- **Report honestly.** Never claim an acceptance criterion passes unless it was actually verified.

## Constraints

- Python 3.11+, pinned deps, `pip install -e .` on a clean venv, Playwright Chromium only (`playwright install chromium`).
- LLM: Meta Model API, model `muse-spark-1.3`, OpenAI-compatible base URL `https://api.meta.ai/v1`, bearer auth. `reasoning_effort: high` for verification judgments. JSON-schema structured output for every agent decision. Capability check at startup.
- Runtime network: only the configured LLM endpoint and the local demo app (the browser blocks every other host). Baseten is allowed only if the stretch is enabled.
- Setup target: clone → passing demo in ~10 minutes, following the README.

## Acceptance criteria

1. Clean venv: `pip install -e .` + `playwright install chromium` + README quickstart works end to end.
2. `proofheal record` on v1: suite green, memory populated.
3. `proofheal run --heal` on v2: suite passes after healing. Every healed locator has a proof entry, and HTML + JSON reports exist in `proof_reports/`.
4. `proofheal run --heal` on v2-broken: the checkout test is refused with clear evidence (Pay no-ops, wrong total) and exits non-zero. The report shows the rejection. Genuinely cosmetic failures may still heal.
5. `pytest tests_unit` passes.
6. No secrets anywhere in the repo or logs.
7. Anything unverifiable is stated plainly.

## Demo-readiness

- `./demo.sh`: record v1 → heal v2 → refuse v2-broken → summary → open the report.
- README: what it is, why "a green test isn't proof", ASCII architecture diagram, quickstart, demo script, a "Built with Meta Model API" section (only what the code really does), and a 3-minute video outline:
  - 0:00–0:20 problem
  - 0:20–0:50 record
  - 0:50–1:40 heal + proof report
  - 1:40–2:20 refuse broken checkout (linger here)
  - 2:20–3:00 architecture, Muse Spark 1.3 / Meta Model API, wrap-up

## Stretch (only after every criterion verifiably passes)

Pluggable LLM via `PROOFHEAL_BASE_URL`, `PROOFHEAL_MODEL`, `PROOFHEAL_API_KEY` (already wired), to run the same loop on Muse Spark Open hosted on Baseten (`meta-models/muse-spark-open-1026`, OpenAI-compatible). Run the same capability check against it. Document it in the README only if it actually works.
