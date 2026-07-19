---
name: avoiding-duplicate-implementations
description: Structurally prevent a second entrypoint (test/dry-run script, alternate CLI, alternate handler) from silently reimplementing production's core logic instead of calling it — extract the shared logic into one function and prove both callers use the literal same function object, not just similar behavior. Use this whenever building a dry-run script, smoke-test harness, CLI wrapper, or any second code path meant to mirror real production behavior, or when reviewing why a test script's behavior diverged from production's. This project drifted into duplicate implementations twice before this pattern was adopted as the permanent fix — reach for it any time "the script should do the same thing production does" comes up.
---

# Avoiding duplicate implementations

## The failure mode

A second entrypoint — a dry-run script, a smoke-test harness, an alternate CLI — starts as a
thin wrapper around production logic, and drifts. Someone adds a feature to production and
forgets the script; someone "just inlines a quick tweak" in the script for a one-off test and
never reconciles it back. The two copies look similar, both pass their own tests, and the
divergence is invisible until it produces a wrong result in exactly the context the second
entrypoint exists to catch.

**This happened twice for real in this project before the fix below was adopted:**

1. **The `trips[0]` regression.** `scripts/dry_run.py` had its own copy of the Get-Trips
   cabin-selection logic. Production was fixed to filter Get Trips results by the `Cabin`
   field (see `seats-aero-integration` — Get Trips returns itineraries across *all* cabins for
   one `AvailabilityID`, not just the one searched). The script's copy wasn't, and kept naively
   indexing `trips[0]`.
2. **The stale `Baseline` field.** `src/cash.py`'s `Baseline` dataclass gained a field in
   production; `scripts/dry_run.py`'s own hand-rolled local state store, a parallel
   implementation of the same persistence shape, didn't get the update and silently drifted
   out of sync with what production's baseline objects actually looked like.

Both times, the script kept *running* and kept *producing plausible-looking output* — nothing
crashed, nothing failed loudly. That's what makes this failure mode dangerous: a
behavioral/output test on the script alone cannot catch it, because the script's own
(drifted) logic is exactly what such a test would be checking against.

## The permanent fix, not a one-time cleanup

When a second entrypoint needs production's core logic:

1. **Extract the shared logic into one function**, owned by the production module, taking
   whatever inputs it needs and returning a structured result (not printing/logging directly
   inside it, if both callers want different verbosity — see the worked example below for how
   to split "the decision" from "what each caller does with the decision").
2. **Have every caller import and call that exact function** — not a copy, not a "reimplementation
   that happens to produce the same result today."
3. **Isolate what's genuinely caller-specific** (different error-handling philosophy, different
   logging verbosity, different I/O backends) as thin, explicit extension points — a
   caller-supplied callback, a caller-supplied result-consumer — not as a reason to fork the
   whole function. Don't over-extract, either: only pull out what's genuinely identical logic.
   A caller-specific concern that got mistakenly folded into the "shared" function tends to
   produce subtle bugs of its own (see the worked example's prefilter-ordering bug below).
4. **Add a test that asserts object identity, not just behavioral similarity.** A behavioral
   test ("both callers produce alerts_sent == 1 for this input") can pass today and still
   permit a full reimplementation tomorrow, as long as the reimplementation happens to match
   on the cases the test checks. An identity test (`is`, not `==` — is this the literal same
   function object?) fails immediately and unconditionally the moment a second copy exists,
   regardless of how correct that copy looks:

   ```python
   def test_dry_run_script_calls_the_same_evaluate_candidate_object_as_poll_route():
       import importlib.util
       from pathlib import Path
       import src.poller as poller_module

       dry_run_path = Path(__file__).resolve().parent.parent / "scripts" / "dry_run.py"
       spec = importlib.util.spec_from_file_location("dry_run_under_test", dry_run_path)
       dry_run_module = importlib.util.module_from_spec(spec)
       spec.loader.exec_module(dry_run_module)  # module-level code only; main() is __name__-guarded

       assert dry_run_module.evaluate_candidate is poller_module.evaluate_candidate
   ```

   Loading the script as a module via `importlib` (rather than needing it to be a proper
   package) works for any `scripts/`-style entrypoint that guards its real work behind
   `if __name__ == "__main__":` — importing it only runs module-level definitions, never makes
   a real call.

## Worked example: `evaluate_candidate()`

`src/poller.py`'s `poll_route()` and `scripts/dry_run.py` both need to run the exact same
per-candidate decision chain: prefilter → cash triggers (mistake-fare ceiling / relative drop)
→ first-pass CPP gate → dedup → cap → Get Trips + exact-date confirm → final gate → notify +
record (see `deal-valuation` for what each stage decides). Before the fix, `dry_run.py` had
its own ~150-line copy of this chain — the exact shape that produced both incidents above.

**What got extracted:** everything except Get Trips and the exact-date confirm call
themselves. Those two I/O calls have genuinely different, deliberate error-handling policies
per caller — production propagates a Get Trips failure loudly and swallows a confirm failure
broadly; `dry_run.py` wants explicit per-failure-mode messages and aborts the whole run on
auth/quota failures for either, while treating a transient timeout as a skip of just one
candidate. Unifying those policies would have been an actual behavior change disguised as a
refactor, not a pure extraction — so instead, `dry_run.py` supplies its own `fetch_trip`
callback (injected into `evaluate_candidate()`), and everything else — the actual gating
decision — is the one shared function call.

**A real bug the extraction itself introduced, caught by an existing test, not by luck:** the
first pass at this put the `eligible_programs`/cabin prefilter check *inside* the shared
function, called *after* the cash-baseline lookup already happened. That silently broke the
"reject before spending a provider call" guarantee the prefilter exists for — an existing
test (`cash_provider.calls == []` for an ineligible program) caught it immediately, because
it asserted call *counts*, not just the final outcome. The fix: the prefilter check moved back
to each caller, before the cash lookup, and the shared function's docstring now explicitly
says it does NOT check the prefilter and why. This is itself a case study for the "isolate
what's genuinely caller-specific" step above — a check that looks purely decision-logic-shaped
can still have a caller-specific *cost* implication (what does calling it too late waste?)
that a purely-behavioral read of the code won't surface.

## When NOT to extract

Not every similarity is duplication worth structurally preventing. A 2-line log-formatting
call that happens to appear in two places is a poor candidate for this treatment — the
coordination overhead of a shared function + identity test exceeds the drift risk. Reserve
this pattern for logic where a silent divergence would produce a *wrong decision* (a gate that
should reject firing anyway, a cost-relevant call happening at the wrong time), not for
incidental textual similarity.
