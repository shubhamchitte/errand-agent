# Recording script (about 5 minutes)

Launch once: `make record` (scripted, fresh DB, `--scenario demo`) or `make record-live` (live model, same forced outcomes).
Use a terminal at least 170 columns wide so the one-line context stat is not cut off. Type each message exactly as written.
Every command below was rehearsed with `--scripted --plain` (the same agent code as the UI, only the renderer differs).
Rehearsal date was Sunday 8 Nov 2026; "tomorrow" is Monday. If you record on a Friday, "tomorrow" is already a Saturday, so
type "on Monday" instead of "tomorrow" in beat 1, otherwise the correction changes nothing.

What `--scenario demo` forces (all on the first booking only): injection review on QuickFix Plumbers (default catalogue),
the first hold lasts 1 minute so approving triggers a re-hold, that re-hold quotes +150, the first `book` call times out but
actually books, the second and third `book` calls succeed. Everything is derived from the DB, so it survives the restart in beat 5.

Right pane: after every model turn there is a line `memory: 64 stored -> 15 sent (7 excluded: forgotten/unconfirmed) · context 808/4,000 tokens`.
Mention it once in beat 1; it is also in the JSONL (`context_stat` events) in `--plain`.

## Beat 1: memory fills the gaps, correction keeps the rest
1. Type `I need a plumber`
   - Left: "Could you tell me the date?" (only the date is asked).
   - Right: `slots_from_memory {"filled": {"area": "Indiranagar", "budget_inr": 1500, "earliest_time": "10:00"}}` and the `memory:` line.
   - Say: "Area, budget and not-before-10am came from memory, so it only asks what is missing."
2. Type `tomorrow`
   - Left: approval card, Sharma Plumbing Works, Mon 9 Nov 11:00, ₹1,200, "Slot held for 1 min", Why line citing [m005] [m008] [m001].
   - Right: yellow APPROVAL CARD panel.
3. Type `actually make it Saturday at 12`
   - Left: new card for Sat 14 Nov 11:00 (nearest slot to 12:00), still ₹1,200 inside the ₹1,500 budget, still Indiranagar, still after 10:00.
   - Right: `APPROVAL_INVALIDATED ... changed: date_start, date_end, preferred_time` then a new card.
   - Say: "A correction changes one field; budget and area stay, and the old approval is dead."

## Beat 2: compare, then the injection review
1. Type `compare my options`
   - Left: all 4 fitting providers (Sharma, AquaCare, PipePro, QuickFix with "1 review flagged"), one line each, and "Your pending approval is unchanged."
   - Right: `RANKING ... (review text used: False)`.
2. Type `what do the reviews say about QuickFix?` (id form also works: `reviews for p_plm_2`)
   - Left: QuickFix summary, "Flagged: one review (Admin Notice) contains instructions aimed at an AI assistant ... ignored. Nothing has changed: your pending approval is exactly as before."
   - Right: the black-on-yellow `!! INJECTION FLAG` under `TOOL get_provider`.
   - Say: "The review tries to make the agent book; it is data, it is flagged, and the pending card did not move."
   - The pending card is still Sharma. Avoid words like "book", "instead", "switch" in this message: they route to a booking change.

## Beat 3: price drift invalidates approval, double yes
1. Type `yes`
   - Left: "The hold ran out and the price changed from ₹1,200 to ₹1,350. Nothing is booked yet." plus a new card (#f482).
   - Right: `TOOL hold_slot ... quoted_price 1350`, `APPROVAL_INVALIDATED ... quote changed 1200 -> 1350`, `execution {"status": "reask"`, new card. No `TOOL book` yet.
   - Say: "The old yes covered ₹1,200, so it does not carry over to ₹1,350."
2. Type `yes` (this approves the new card, go straight to beat 4)

## Beat 4: timeout but booked, checked, booked once
   - Left (from the second yes): "Booked (verified): ... ₹1,350. Booking id bk_0001. The request timed out on the way, but I checked: you're booked once, not twice."
   - Right: red `TOOL book(...) ERR ToolTimeout`, then green `TOOL get_booking_status` returning `"status": "booked"`, `STATE EXECUTING -> DONE`.
   - Say: "The timeout did not make it retry blindly; it asked the provider and found one booking."
3. Type `yes` again (the duplicate yes)
   - Left: "That's already done, nothing more to do." Right: no new `book` call.
   - Say: "A second yes cannot book twice."

## Beat 5: kill mid-booking, restart, recovery with the same key
1. Type `I need an electrician tomorrow` (card: Ravi Kumar Electricals, Mon 9 Nov 10:00, ₹700, no price change because a booking now exists)
2. Type `/crash-mid-booking` (debug aid; left shows "armed: ...")
3. Type `yes`
   - The process dies immediately with exit code 137 and prints `[debug] booking reached the provider; killing the process ...`.
   - Right (before death): `idempotency_key_persisted key=idem-book-act_92e7...` then `approval_check`. The book call reaches the provider (booking exists) but the agent never sees the reply.
   - Say: "I just killed it after the provider booked but before the agent heard back."
4. Run `make record-restart` (scripted) or `make record-restart-live` (live). No `--reset`, same DB.
   - Left on startup: "(recovery) Booked (verified): Ravi Kumar Electricals on Mon 9 Nov, 10:00 for ₹700. Booking id bk_0002."
   - Right: yellow `recovery key=idem-book-act_92e7... status=EXECUTING` (same key as before the kill), `TOOL get_booking_status`, no second `book`.
   - Say: "Same idempotency key, found by status check; one booking, no duplicate."
   - Plain Ctrl-C does NOT work for this: at the prompt it is a clean quit, and during the call it only interrupts the Python thread unpredictably. Use `/crash-mid-booking`.
   - The restart starts a new run: the trace pane and the `calls=` counters begin from zero.

## Beat 6: provider cancels, proactive replacement
1. Type `/cancel-provider bk_0001`
   - Left: "simulated: provider cancelled bk_0001. Run /events or send any message."
   - Right: yellow `external_event` line.
2. Type `/events`
   - Left: "Heads up: Sharma Plumbing Works cancelled your plumber visit on Sat 14 Nov, 11:00 (booking bk_0001). I found a replacement ... Book AquaCare Services on Sat 14 Nov, 11:00 for ₹1,450 (#8bd1)" with a Warning that AquaCare is in HSR Layout, not Indiranagar.
   - Right: `external_event`, `RANKING`, new approval card.
   - Say: "I did not ask for this; the agent noticed, told me, and still needs my yes."
3. Type `yes` -> "Booked (verified): AquaCare Services ... bk_0003." (third `book` call is forced to succeed).
   - A `yes` right after `/events` works (fixed: `/events` now binds the yes to the card it displayed).

## Beat 7: summary line
Type `/quit`. Left/terminal shows e.g. `1 AI calls, 4 tool calls, 1587 tokens, ₹0.00, 0.0 s`. Point at the same line in the trace pane (`run_summary`).
Say: "One line: AI calls, tool calls, tokens, rupees, seconds."
Caveat: the summary covers only the current process, i.e. the run after the restart. For a bigger number, send a message or two
after the restart before `/quit`, or quit before beat 5 in a separate rehearsal.

## Does not work / caveats
- Live mode was NOT rehearsed (no `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` in this environment). Routing, forced outcomes,
  approval gate, crash and recovery are model-independent. Wording will differ, and in beat 1 the live model may ask extra questions.
  Beat 2 depends on the live model calling `get_provider` and mentioning the flagged review (system prompt now instructs it to).
  Do one dry run with `make record-live` first.
- The first request on a fresh DB can trigger a one-off "You seem to ... Should I remember that?" guess question for unconfirmed
  inferred memories. In the rehearsal it did not appear with the beat 1 wording, but if it does, answer `no`.
- The "price drifts" beat is a re-hold at approval time (the 1-minute hold is already expired when you type yes), not a drift shown on the first card.
- The summary line (beat 7) is per process, see above.
- Date-relative: see the Friday note at the top. `make record` / `make record-live` always use `--reset`; never use them for the restart.


## UI display and cut-off text
- Normal screen (no alternate screen): each redraw clears and prints both panels sized to their content; long lines wrap, the newest stay visible. Scroll with the terminal itself (mouse/trackpad); `/bottom` returns to the normal view.
- `/trace [N]` shows the last N trace events as full JSON; `/chat` shows the whole chat history.
- On `/quit` the JSONL trace path and the chat transcript are printed (also saved as `runs/<id>.chat.txt`).
- To see anything cut off: /trace 5, or open runs/<id>.jsonl.
