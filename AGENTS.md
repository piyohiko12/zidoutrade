# Agent instructions

This repository implements `RSI_AUTOPILOT_V1`, a supervised, SIMULATE-only
research system. These instructions apply to every directory.

## Development workflow

- Work on a branch and use a draft pull request.
- Keep changes small and independently reviewable.
- Use only synthetic data in tests and examples.
- Run `PYTHONPATH=src python3 -B -W error -m unittest discover -s tests -v`
  before handoff.
- Document any behavior change in `docs/RSI_AUTOPILOT_V1_SPEC_JA.md`.

## Prohibited actions

- Network/OpenD/account/order access during tests.
- `TrdEnv.REAL`, `unlock_trade`, short selling, options, crypto, extended hours.
- Automatic installation, registration, subscription, trial, billing, or
  entitlement changes.
- Committing mutable runtime artifacts or sensitive/market/performance data.
- Ranking candidates by predicted profit in V1. The user selects from an
  eligibility-filtered, deterministic list.

## Architecture rules

- Indicator, strategy, risk, and selection logic must be pure where possible.
- The moomoo SDK may be imported lazily only by the paper-broker adapter.
- Every side effect must be injectable so tests use a fake.
- Canonical JSON is UTF-8, sorted, compact, newline terminated, and hashed with
  SHA-256 where an immutable decision boundary is required.
- Existing durable intents are query-only after restart.
- Control mode and exposure state are separate axes.
- Public UI/status must be redacted and must not imply that a TCP connection or
  shallow hash check proves that trading is safe.
