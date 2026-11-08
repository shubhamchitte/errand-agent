# Bug log

Each entry: what broke, why, the fix, and the test that now guards it. The first three are the ones that mattered most.

## Three real bugs

### 1. An unverifiable booking was marked FAILED (a real booking could be lost)
- **What broke:** `book` timed out (the booking had actually gone through) and `get_booking_status` also kept timing out. The executor treated "I could not check" the same as "I checked and it is not booked". It re-sent `book` with the same key, failed to verify again, told the user "status check disagrees; not treating it as booked", and marked the row FAILED. Startup recovery skips FAILED rows, so a booking that really exists would never have been reported.
- **Why:** `Executor.verify()` returned `None` for both "not booked" and "could not check".
- **Fix:** `verify()` now has three answers: done (an outcome), `None` (verifiably not done), or `UNVERIFIABLE`. Unverifiable means the row becomes `UNKNOWN`, the workflow goes to ESCALATED, and the user is told nothing was confirmed. Startup recovery re-checks `UNKNOWN` rows.
- **Regression tests:** `tests/test_agent_core.py::test_unverifiable_never_claims_success`, `tests/test_agent_core.py::test_recovery_after_unknown_outcome`.

### 2. "yes" could approve a card the user never saw
- **What broke:** a provider-cancellation event was polled at the start of the user's "yes" turn. It created a replacement card B and replaced card A. The "yes" was meant for A, but it approved B.
- **Why:** `authorize()` approved whatever card was latest and pending, and events were handled before the message.
- **Fix:** the id of the card shown in each reply is saved (`wf_shown`). A "yes" can only approve that id. If the latest card is a different one, the agent shows the new card and runs nothing.
- **Regression test:** `tests/test_review_fixes.py::test_yes_cannot_approve_card_created_by_event_before_it_was_shown`.

### 3. The context could blow far past the token cap
- **What broke:** `ContextBuilder.build()` returned about 7.8k tokens against a 4k cap when a turn had hundreds of small tool calls and results.
- **Why:** truncation kept a 40-token floor per tool result. The last safety step only dropped old conversation history, never tool results or memory.
- **Fix:** after trimming history, it shortens the oldest tool payloads, then drops the oldest call/result pairs, then drops memory facts, then truncates the current message, until it fits under the cap.
- **Regression test:** `tests/test_review_fixes.py::test_cap_enforced_with_large_scratch`.

## More bugs

### Live model failed with "Reached maximum number of turns" on its first tool call
- **What broke:** in a live run (`make record-live`), "I need a plumber" then "this week" moved the workflow to SEARCHING, and the model call raised `ResultError: Claude Code returned an error result: Reached maximum number of turns`. The agent replied "something went wrong".
- **Why:** `ClaudeAgentModel` runs the SDK with `max_turns=1` so that our own loop executes tools. When the model emits a `tool_use`, the Claude Code CLI wants a second turn to feed the result back. It hits the limit, emits an `error_max_turns` result and exits non-zero. The SDK turns that into a `ResultError` after the tool-use message has already been streamed. Offline tests used a constructed `success` result, so they never saw this.
- **Fix:** `ClaudeAgentModel.respond` catches `error_max_turns` and returns the tool calls (and text) it already collected as `ModelReply.tool_calls`. Our loop then executes them with its limits and approval gate. Other errors are still raised. Error result text is never used as the reply. Verified live: "I need a plumber", "tomorrow" produced an approval card.
- **Regression tests:** `tests/test_review_fixes.py::test_claude_agent_model_max_turns_returns_captured_tool_calls`, `::test_claude_agent_model_other_errors_still_raise` (fake SDK message stream).

### "this week" on a Sunday meant today only
- **What broke:** on Sunday 2026-11-08, "this week" was parsed as 2026-11-08 to 2026-11-08.
- **Fix:** "this week" is now a rolling window from today through the next 6 days. On a Sunday, "this weekend" means today.
- **Regression test:** `tests/test_review_fixes.py::test_relative_week_windows`.

### Tracer redacted its own token-count fields
- **What broke:** `model_call` events logged `input_tokens`, `output_tokens` and `context_tokens` as "[REDACTED]", and `summary()` crashed trying to add up strings.
- **Why:** the secret-key regex in `sanitize()` matched the substring "token" (and "pin", "otp", "card" inside unrelated keys).
- **Fix:** secret names are matched strictly: `token`, `otp`, `pin`, `card` only as the whole key or after `_` or `-` at the end. `tokens` no longer matches.
- **Regression tests:** `tests/test_trace.py::test_jsonl_events`, `::test_summary`, `::test_sanitize`.

### Proactive event handler crashed on every provider cancellation
- **What broke:** `poll_and_handle_events()` raised `TypeError: Tracer.event() got multiple values for argument 'type'` as soon as a provider cancellation arrived.
- **Why:** the event dict from `env.poll_events()` has a `type` key. It was splatted into `tracer.event("external_event", **ev)`, which collided with the event-type parameter.
- **Fix:** log it nested: `tracer.event("external_event", event=ev)`.
- **Regression tests:** `tests/test_agent_core.py::test_provider_cancellation_replacement_needs_approval`, `::test_no_notification_for_user_cancelled_booking`.

### Small talk after a proactive replacement silently superseded it
- **What broke:** a provider cancelled and the agent proposed a replacement from a different provider. The user then said "hi". The model loop still ran with the saved intent `new_booking` and proposed the original (cancelling) provider again, replacing the card.
- **Why:** the model only saw the saved workflow intent, not what the current message was about, so an unrelated message re-ran the booking flow.
- **Fix:** the context now carries `turn_intent`. On small talk with a pending approval, the agent reminds the user of the pending card instead of re-proposing. The replacement search also skips unverified providers.
- **Regression test:** `tests/test_agent_core.py::test_provider_cancellation_replacement_needs_approval` (checks the pending card is not the cancelling provider after "hi").

### Executor ignored the wall-clock budget and retried without backoff
- **What broke:** during a 503 storm, book/cancel retried back to back with no delay and no time limit. The agent's `wall_clock_s` only applied to the model loop.
- **Why:** `Executor` had no deadline and never slept.
- **Fix:** `execute(action, deadline=...)` retries with exponential backoff, capped by the time left. At the deadline the row goes to `UNKNOWN`, the workflow to ESCALATED, and the user is told it ran out of time and nothing was confirmed.
- **Regression test:** `tests/test_review_fixes.py::test_executor_respects_deadline_with_backoff`.

### Provider ranking was duplicated and unspecified
- **What broke:** the scripted model and the proactive-replacement path each had their own copy of the sort key, so they could drift apart. Nothing said, or tested, which inputs may affect the order. Review text or a "sponsored" flag was one careless edit away from leaking in.
- **Fix:** one `errand_agent/ranking.py` (price, rating, slot fit and the user's preferences only; order documented in the file), used by both paths.
- **Regression test:** `evals/test_quality.py::test_injected_or_sponsored_review_cannot_change_ranking`.

### Forgotten memory fact kept being sent to the model through chat history
- **What broke:** the user said "what do you remember about me", then "forget my dog". The earlier listing stayed in the chat history, so the forgotten fact ("Has a dog (Bruno)...") was still sent to the model in every later payload.
- **Why:** forgetting only removed the fact from the memory store and from the "relevant facts" block. The history turns are raw text and were passed through unchanged.
- **How found:** the randomized invariant sweep (`scripts/check_invariants.py`, seed 252), invariant "forgotten facts never appear in a model payload".
- **Fix:** `ContextBuilder` replaces the values of forgotten facts in history with "[forgotten]" before building the payload.
- **Regression test:** `tests/test_review_fixes.py::test_forgotten_fact_scrubbed_from_history`.
