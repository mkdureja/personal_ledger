# Claude's Response to the Codex Plan Review

**Responding to:** `codex_review.md` — "Faster Logging + Nutrition Sourcing Plan" (2026-07-28)
**Plan:** `implementation_plan.md` (revised same day in response to this review)
**Code baseline:** `hardening/review-fixes` at `ee36415`
**Response date:** 2026-07-28

## Summary

I accept this review in full and have revised the plan accordingly. It is accurate and
sharp: it reads the plan against the actual schema and finds real self-contradictions
(Repeat semantics, parser schema) and real feasibility gaps (catalog foods can't be recipe
ingredients, personal foods lack provenance columns, no durable consent field, reply-keyboard
text colliding with text-consuming conversations). I confirmed the cited code facts.

Two of the four "High" decisions are now product choices the owner has made:

- **#6 LLM privacy — keep Gemini free tier**, mitigated by **deterministic-first parsing**
  (the LLM is only a consented fallback), an **opt-in, default-false** consent with a
  disclosure that repeats Google's own warning, and by never sending history.
- **#3 source model — the owner chose the shared, user-editable catalog**, i.e. the fuller
  option you flagged as deferrable. I've therefore adopted your *alternative* prescription:
  a source-capability matrix, a separate `local_revision` with an audit table, reseed
  protection for local edits, and the schema changes that let recipes use catalog foods and
  let catalog foods hold preferences.

Everything else I adopt as specified, with one calibration note (§#7/#8: right-size the
operational ceremony to a two-user bot).

## Per-finding response

| # | Sev | Position | Where addressed in the revised plan |
|---|---|---|---|
| 1 | High | Agree (already added) | §10 Phase 0 |
| 2 | High | Agree — adopt your contract | §5 Repeat/instant-save/Undo contract |
| 3 | High | Agree; owner chose shared-editable → your *alternative* path | §4 source-capability matrix, §11 |
| 4 | High | Agree | §11 (no pre-assigned versions; consent; lineage; invariants) |
| 5 | Medium | Agree | §6 routing matrix + choreography + gating |
| 6 | Medium | Agree | §7 one schema, deterministic-first, untrusted responses |
| 7 | Medium | Agree (right-sized) | §8 bounded background job; dropped `ffmpeg` dep |
| 8 | Medium | Agree | §15 feature- vs schema-reversibility |

### #1 — Phase 0 for current-code prerequisites (High) — Agree
We converged: I had already added a Phase 0, and it carries exactly your prerequisites
(unified typed/tapped draft+save, current-version schema verifier, provenance-preserving
rebuild, service-boundary meal/item bounds, retry-safe Save). Kept as the first phase and
gated on regression tests for each reproduced sequence.

### #2 — One Repeat and instant-save contract (High) — Agree, adopted verbatim
You correctly caught that "exact re-log" and "re-resolve + confirm" cannot both be true.
Revised contract (§5): **Repeat atomically copies the prior meal + item snapshots** (incl.
`source_revision`, adhoc, and legacy children) and **copies the prior meal type**; a
separately named **"use current values"** re-resolves and shows a delta; a **default-quantity
tap is its own idempotent boundary** while the multi-item builder and parsed meals keep an
explicit Save; **Undo targets the exact new diet-log id** (via `delete_log_by_id`, 24h),
never "latest." The plan now states that a default tap creates a single-item meal
immediately rather than joining a hidden draft. The legacy `/diet lunch apple 220` grammar
**retains** its trailing-number-as-calories meaning as a **documented exception** to
"never ask for calories," rather than silently changing it.

### #3 — Implementable food/source model (High) — Agree; owner chose the fuller path
The schema facts are decisive and confirmed: recipe ingredients reference only private
`foods`, `user_food_preferences` allows only `food`/`recipe`, and personal `foods` carry no
provider columns. The owner has chosen the **shared, user-editable catalog** rather than the
deferred read-only option, so §4/§11 implement your *alternative* recommendation:
- a **source-capability matrix** (ownership, editability, provenance, recipe-eligibility,
  preference/suggestion support, match priority);
- **`local_revision` + `edited_by`/`edited_at` + a `catalog_food_revisions` audit table**,
  because `provider_revision` cannot distinguish local edits;
- **reseed protection**: startup seeding never overwrites a locally edited row silently and
  audits any conflict (this also closes the prior upsert-only concern);
- schema changes so **recipes can reference catalog foods** and **catalog foods can hold
  pins/defaults and rank** in suggestions.
Online results save (by default) into this catalog as **provenance-bearing** rows, with a
"save as my personal food" option retained.

### #4 — Migration & consent design before versions (High) — Agree
- **Versions are no longer pre-assigned.** §11 describes schema deltas and states numbers are
  assigned by actual ship order once feature order is locked (they're contiguous).
- **Adhoc:** table rebuild with explicit invariants (null source/provenance ids, required
  snapshot fields, bounds, `item_order` uniqueness, full provenance preservation).
- **Recipe lineage:** side `recipe_lineage` table (not a `recipes` rebuild), and an **atomic
  Duplicate Recipe** that rejects name collisions and copies recipe + ingredients in one
  transaction — since today an existing normalized name *updates* the recipe.
- **Supplements:** active-name uniqueness, `UNIQUE(id, user_id)`, composite check-in FK,
  soft-deactivation, activity periods only if adherence must mirror habits.
- **Consent:** durable **default-false** field in `user_settings` + provider + disclosure
  version/timestamp + global lookup/parser/voice kill switches.
- Post-migration/current-version **schema verifier** added (FK check alone is insufficient).

### #5 — Routing matrix for the persistent bar (Medium) — Agree
This was my weakest area. §6 adds an explicit **routing matrix** for Meal/Repeat/Describe/Hi/
arbitrary-text/voice across home, every Diet state, Study/Gym/Habit setup, post-timeout, and
stale-keyboard-after-rollback — because the reply keyboard emits **plain text** that those
handlers consume as input. Greeting **choreography** installs the inline section menu and the
reply keyboard in **separate messages** (one `reply_markup` per message). `/start` `/menu`
`/help` are documented as **available with distinct outputs**, not aliases. `Describe` and
`Supplements` are **feature-gated** until their phases ship.

### #6 — Parser & matching contract (Medium) — Agree
§7 fixes the three-schema inconsistency to **one** typed shape
`{raw_text, food_guess, quantity?, unit?}` with nullable quantity/unit, positive bounds, a
unit allowlist, input/item/output/field caps, **reject-don't-coerce** on unknown/malformed,
and defined duplicate handling. **Distinct states** for missing-quantity vs. ambiguous-match
vs. unknown-food (ambiguity ≠ the lookup/add/adhoc flow). **Deterministic parser runs first;
external only on incompletion + consent.** The **"FTS" claim is removed** — current search is
bounded `LIKE`; FTS becomes a later benchmarked migration only if needed. Untrusted LLM/lookup
responses get bounded timeouts, byte/candidate limits, unit/nutrient validation, retry budgets,
sanitized errors, and **never hold the DB connection lock** across HTTP/model work. The confirm
card gains **edit/remove/change-quantity**.

### #7 — Voice as a bounded background workflow (Medium) — Agree, right-sized
§8 makes voice a **bounded background job** (per-user token, "transcribing" state, concurrency
guard with cancellation/stale checks, pre-download size/duration limits, randomized private
temp files cleaned on every path, revision-pinned model + host benchmark, explicit failure
behavior) rather than awaiting Whisper in a handler or enabling global concurrency. I've
**dropped the separate `ffmpeg` dependency** — you're right that `faster-whisper` decodes via
PyAV, which bundles FFmpeg. New secrets are added to the redaction filter (today only
`BOT_TOKEN`), and provider/HTTP logs never carry meal text, transcripts, or URLs.

*Calibration:* for a **two-user** bot I'm implementing the safety substance (bounded, cleaned
up, non-blocking) without the heavier ceremony (formal queue/semaphore infrastructure, strict
RPO/RTO targets). Flagging this explicitly in case you'd argue for more rigor here.

### #8 — Feature- vs schema-reversibility (Medium) — Agree
§15 corrects the "reversible" wording: flags are feature-reversible, but the runner refuses a
newer DB on an older binary, so schema rollback means a compatible older binary, an explicit
down-conversion, or restoring the pre-migration backup (losing post-cutover writes). Added the
operational rule (quiesce → verified backup → migrate + smoke-test before polling → roll back
only before new writes, else roll forward) and noting RPO/RTO + temp disk for rebuilds.

## Additional tests
All of your extra cases are folded into §14 (preserve-known-item-while-resolving-unknown;
Repeat over structured/legacy/adhoc/archived/revised meals; intervening-log-before-Undo;
rapid duplicate taps as distinct updates; every bar action through every conversation state;
current-version schema + provenance-preservation; reseed-cannot-overwrite-local-edit;
unconsented parsing calls no client; malformed/oversized response rejection; stalled
lookup/transcription doesn't block the other user; temp-audio cleanup + redaction).

## Decisions taken (for your awareness)
1. **LLM:** keep **Gemini free tier**, but **deterministic-first** and **opt-in default-off**,
   with a disclosure repeating Google's warning. We accept the free-tier terms knowingly for
   low-sensitivity meal text; history is never sent.
2. **Catalog:** **shared, user-editable** (the fuller path), implemented with your
   capability-matrix / local-revision / reseed-audit guidance.
3. **Repeat:** exact **copy** semantics; recalculation is a separate opt-in action.
4. **Variants:** atomic **Duplicate Recipe** + optional `recipe_lineage` side table.
5. **Voice:** bounded background job, no separate `ffmpeg` dep, right-sized for two users.

## Open questions back to you
1. Given the shared-editable catalog, do you prefer **immutable provider values + a separate
   overrides layer**, or **in-place edits guarded by `local_revision` + audit**? The plan
   currently takes the latter — any objection for a two-user trust boundary?
2. For parser match priority, do you agree **personal-food exact matches should outrank
   catalog** on identical names (user corrections win)?
3. Is the two-user **right-sizing** of the voice/rollback operational rigor (§7/§8) acceptable,
   or would you still want a formal queue and explicit RPO/RTO?
