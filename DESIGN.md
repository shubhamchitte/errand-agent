# Design note

**State model.** One saved workflow per user (SQLite): IDLE, GATHERING, SEARCHING, COMPARING, AWAITING_APPROVAL, EXECUTING, VERIFYING, DONE, FAILED, ESCALATED, plus structured slots (service, area, date, budget, earliest time). A correction changes only the field mentioned. A table of allowed transitions rejects the rest; EXECUTING is reachable only from AWAITING_APPROVAL.

**Approval enforcement.** The model has read tools and `propose_*` tools only. A proposal becomes a card, and code stores its canonical form and SHA-256. Execution needs a user "yes" aimed at the card actually shown, not expired (30 min), with an unchanged hash. The hash only detects tampering with the stored card. It cannot know the world changed. That is the executor's job: it re-reads the booking, hold and price just before acting, and any change in provider, slot or price invalidates the card and asks again.

**Idempotency and recovery.** The key is derived from the action id and card hash and saved *before* the call. A repeated yes, a retry or a restart reuses it. On a timeout the executor asks `get_booking_status` and only then retries (same key) or reports. If the check also fails, the row is `UNKNOWN`, the user is told nothing is confirmed, and startup recovery re-checks it. A duplicate guard (same provider and slot) is independent of the key.

**Memory.** Facts are explicit or inferred; inferred ones are unusable until confirmed. Users can list, correct and forget. Forgotten facts are kept as soft-deleted rows and filtered everywhere.

**Context budget.** Each call is built to a 4,000-token cap. Relevant, current facts are picked by a deterministic score (intent, service, slots). Forgotten and unconfirmed facts are excluded. History and tool results are trimmed to fit. Fact ids and a context hash are logged per call, and tests check payloads.

**Observability.** One JSONL trace per run: state transitions, model calls (tokens, cost in USD and INR, latency, fact ids), tool calls with arguments and sanitized results, approvals, executions.

**Trade-offs.**
- *Single agent.* The task is one sequential workflow with one actor and one set of tools. A second agent would add handoffs and failure modes and fix nothing here.
- *Code for control, model for language.* Routing, state, approval and recovery are plain code, so evals can be exact and offline. The cost is a regex router that is narrower than a model.
- *SQLite, soft deletes, polling for events.* Simple and restart-safe, not built for scale.
- *Fail closed.* When unsure, it stops and says "nothing was changed" instead of guessing.

## Annotated trace excerpt

From `traces/e2e_timeout_after_success.jsonl` (trimmed). The scripted model runs the real loop; the mock's `book` times out but did book.

```
route        intent=rebook_same_provider  slots={service: electrician, earliest_time: 10:00}
slots_from_memory  area=Indiranagar, budget_inr=1500        # filled from saved prefs, not re-asked
tool_call    hold_slot(p_elc_1, 2026-11-09T10:00) -> held, quoted_price=700
pending_action  act_a519836042  hash=6bd00f0b...             # card saved; model cannot go further
user_message "yes"
approved     act_a519836042  hash=6bd00f0b...                # gate: right card, fresh, hash matches
idempotency_key_persisted  idem-book-act_a519836042-6bd00f0ba8c6   # saved BEFORE the call
tool_call    book(hold_0001, idem-...)  error=ToolTimeout (outcome unknown)
tool_call    get_booking_status(idem-...) -> booked, bk_0001      # source of truth asked, no blind retry
execution    done                                            # only now is success claimed
user_message "yes"                                           # repeated yes: no second book call
```
