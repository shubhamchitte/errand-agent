# Bug log

Template per entry: **What broke** / **Why** / **Fix** / **Regression test**.

(No bugs logged yet.)

## Tracer redacted its own token-count fields
**What broke**: model_call events logged input_tokens/output_tokens/context_tokens as "[REDACTED]", and summary() crashed summing strings.
**Why**: sanitize() secret-key regex matched the substring "token" (also "pin"/"otp"/"card" in unrelated keys).
**Fix**: secret names are matched strictly (token/otp/pin/card only as the whole key or after `_`/`-` at the end; `tokens` no longer matches).
**Regression test**: tests/test_trace.py::test_jsonl_events, test_summary, test_sanitize.
