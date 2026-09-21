# Cutover and release evidence

One nodriver/CDP runtime is supported. Cutover gate requires:

| Gate | Command/evidence |
| --- | --- |
| Lockfile | `uv lock --check` |
| Format/lint/type | `ruff format --check`, `ruff check`, `pyright` |
| Pure/contract/state | focused contract suites, then full `pytest` |
| Real Chrome | `tests/test_nodriver_lifecycle.py`, `test_nodriver_page_actions.py`, `test_nodriver_workflows.py` |
| Dependency + old-runtime audit | `python scripts/cutover_audit.py` |
| Secret canary | workflow test asserts secrets absent from results/artifacts |
| Performance bounds | timeout, observation-limit, capture-budget, and cleanup tests |

Release record must attach CI run plus local acceptance rows below. Record exact
browser version printed in checkpoint metadata and confirm no owned PID remains.

| Platform | Mode | Browser selection | Required result |
| --- | --- | --- | --- |
| Linux | headless | discovery | workflows pass; zero leaked processes |
| Linux | headless | explicit executable | workflows pass; metadata path matches |
| Linux | headed (virtual display allowed) | discovery | workflows pass; zero leaks |
| macOS | headless | discovery | workflows pass; zero leaked processes |
| macOS | headless | explicit executable | workflows pass; metadata path matches |
| macOS | headed | discovery | workflows pass; zero leaked processes |

Configured Google Chrome and Chromium variants each require one discovery or
explicit-path run when available. Missing optional variant is recorded; absence of
all supported variants fails release. CI executes Linux headless automatically;
release operator supplies headed Linux and macOS evidence before tagging.

## Current branch evidence

- 2026-09-21, macOS arm64, Google Chrome `153.0.8010.48`, headless discovery:
  full suite passed (`88 passed`); disconnect and forced-shutdown tests verified
  zero owned-process leaks and released profile locks.
- Lockfile, format, lint, strict type check, dependency/removed-runtime audit,
  secret-canary workflow, bounded observation/capture, and contract/state suites
  passed on same revision.
- Remaining release-only rows (Linux and macOS headed/explicit-path matrix) are
  CI/release evidence requirements; code merge does not waive them.
