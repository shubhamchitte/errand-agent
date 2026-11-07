# Bug log

Template per entry: **What broke** / **Why** / **Fix** / **Regression test**.

(No bugs logged yet.)

## Tracer redacted its own token-count fields
**What broke**: model_call events logged input_tokens/output_tokens/context_tokens as "[REDACTED]", and summary() crashed summing strings.
**Why**: sanitize() secret-key regex matched the substring "token" (also "pin"/"otp"/"card" in unrelated keys).
**Fix**: secret names are matched strictly (token/otp/pin/card only as the whole key or after `_`/`-` at the end; `tokens` no longer matches).
**Regression test**: tests/test_trace.py::test_jsonl_events, test_summary, test_sanitize.

## Executor marked an unverifiable booking as FAILED (could lose a real booking)
- **What broke:** `book` timed out (outcome unknown, actually booked) and `get_booking_status` also kept timing out. The executor treated "status check failed" the same as "verified not booked", re-issued `book` with the same key (mock replayed `confirmed`), failed to verify again and replied "status check disagrees; not treating it as booked", marking the row FAILED. The recovery pass skips FAILED rows, so a booking that really exists would never be reported.
- **Why:** `Executor.verify()` returned `None` for both "not booked" and "could not check".
- **Fix:** `verify()` is tri-state (done Outcome / `None` = verifiably not done / `UNVERIFIABLE`). Unverifiable => row `UNKNOWN`, workflow ESCALATED, user told nothing was confirmed; startup recovery re-checks `UNKNOWN` rows.
- **Regression tests:** `tests/test_agent_core.py::test_unverifiable_never_claims_success`, `::test_recovery_after_unknown_outcome`.

## Proactive event handler crashed on every provider cancellation
- **What broke:** `poll_and_handle_events()` raised `TypeError: Tracer.event() got multiple values for argument 'type'` as soon as a provider cancellation arrived.
- **Why:** the event dict from `env.poll_events()` has a `type` key and was splatted into `tracer.event("external_event", **ev)`, colliding with the event-type parameter.
- **Fix:** log it nested: `tracer.event("external_event", event=ev)`.
- **Regression tests:** `::test_provider_cancellation_replacement_needs_approval`, `::test_no_notification_for_user_cancelled_booking`.

## Small talk after a proactive replacement silently superseded it
- **What broke:** after a provider cancelled, the agent proposed a replacement (different provider). The user then said "hi"; the model loop still ran with the workflow intent `new_booking` and proposed the original (cancelling) provider again, superseding the replacement card.
- **Why:** the model only saw the persisted workflow intent, not what the current message was about, so an unrelated message re-ran the booking flow.
- **Fix:** the context state now carries `turn_intent`; on small talk with a pending approval the policy reminds the user of the pending card instead of re-proposing. The replacement search also skips unverified providers.
- **Regression test:** `::test_provider_cancellation_replacement_needs_approval` (asserts the pending card is not the cancelling provider after "hi").

## Context could exceed the token cap with many tool results
- **What broke:** `ContextBuilder.build()` returned a context of ~7.8k tokens against a 4k cap when the turn had hundreds of small tool calls/results.
- **Why:** truncation kept a 40-token floor per tool result and the final safety net only popped conversation history, never scratch entries or memory.
- **Fix:** after history trimming, elide oldest tool payloads, then drop oldest call/result entries, then drop memory facts, then truncate the current message, until `tokens <= cap`.
- **Regression test:** `tests/test_review_fixes.py::test_cap_enforced_with_large_scratch`.

## "yes" could approve a card the user never saw
- **What broke:** a provider-cancellation event polled at the start of the user's "yes" turn proposed a replacement card B, superseding card A; the "yes" (meant for A) then approved B.
- **Why:** `authorize()` approved whatever was latest PENDING, and events were handled before the message.
- **Fix:** the action id of the card visible in each assistant reply is persisted (`wf_shown`); an affirmative only authorizes that id, otherwise the new card is shown and nothing runs.
- **Regression test:** `tests/test_review_fixes.py::test_yes_cannot_approve_card_created_by_event_before_it_was_shown`.

## Executor ignored the wall-clock budget and retried without backoff
- **What broke:** a 503 storm during book/cancel retried back-to-back with no delay and no time limit (the agent's `wall_clock_s` only applied to the model loop).
- **Why:** `Executor` had no deadline or sleep.
- **Fix:** `execute(action, deadline=...)` with exponential backoff capped by the remaining time; on deadline the row goes UNKNOWN / workflow ESCALATED with an explicit "ran out of time, nothing confirmed" message.
- **Regression test:** `tests/test_review_fixes.py::test_executor_respects_deadline_with_backoff`.

## Provider ranking was duplicated and unspecified
- **What broke:** the scripted model and the proactive-replacement path each carried their own copy of the sort key, so the two could silently diverge, and nothing documented or tested which inputs could influence order (review text / sponsored flags were one careless edit away from leaking in).
- **Fix:** single `errand_agent/ranking.py` (price, rating, slot fit, user prefs only; documented order) used by both paths.
- **Regression test:** `evals/test_quality.py::test_injected_or_sponsored_review_cannot_change_ranking`.
