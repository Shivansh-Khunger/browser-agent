# Operator guide

## Startup

Install Python 3.11+, dependencies, and Chrome or Chromium. Browser Agent never
downloads browser binaries. Use `AGENT_BROWSER_EXECUTABLE` for explicit binary;
otherwise installed-browser discovery selects supported Chrome/Chromium variant.

Set `OPENROUTER_API_KEY`. For reusable authenticated state, protect
`AGENT_STATE_ROOT` and retain its generated `checkpoint.key`, or inject
`AGENT_STATE_ENCRYPTION_KEY` as base64-encoded 32 bytes.

## Modes and budgets

Headed mode is default; `HEADLESS=true` selects headless. Default launch,
navigation, action, settle, and shutdown budgets are 30, 30, 10, 8, and 10
seconds. Harnesses can construct `BrowserConfig` with different positive finite
budgets.

Interactive prompts share one browser session. One-shot invocation owns one
session. Actions serialize; cancellation invalidates session and runs bounded
cleanup. Process leak after shutdown is fatal.

## Outcomes

- `succeeded`: action and required evidence completed.
- `partial`: known subset completed or required capture degraded.
- `failed`: action did not dispatch or failed with known result.
- `uncertain`: mutation dispatched but effect could not be confirmed; do not retry blindly.

Policy rejection, stale target, disabled/readonly control, ambiguity, and invalid
coordinates are recoverable structured failures. Launch, disconnect, corrupted
connection, checkpoint seal, and teardown failures terminate session.

Verification challenge requires human intervention. Resume after user completes
challenge, but task automation outcome remains failed. Agent does not promise or
attempt verification bypass.

## Target handles

Displayed `[index]` maps to handle from current observation. Any newer observation
invalidates prior handles. Mutating stale target is never rematched automatically.
Take fresh observation and reconsider action. `fill_form` stops at first stale or
failed field and reports completed fields.
