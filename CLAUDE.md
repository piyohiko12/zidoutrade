# Claude collaboration guide

Claude and Codex may collaborate through GitHub branches and pull requests.
Before changing code, read `AGENTS.md`, `SECURITY.md`, and
`docs/RSI_AUTOPILOT_V1_SPEC_JA.md` completely.

Non-negotiable boundaries:

1. Do not add `REAL`, `unlock_trade`, short selling, extended-hours orders, or
   automatic account selection.
2. Do not install dependencies, create accounts, start trials, or enable paid
   services. The core remains Python-standard-library only.
3. Do not place an order while developing or testing. Use injected fake brokers.
4. Do not commit local runtime files, account identifiers, market data, prices,
   PnL, order identifiers, or activation artifacts.
5. One pull request should state the safety invariant it changes and include a
   regression test. Never weaken a fail-closed check merely to make a test pass.
6. Performance claims require a preregistered sealed OOS evaluation. A passing
   unit test is not evidence of profitability.
7. The current release intentionally rejects every SIMULATE order before SDK
   dispatch. Do not remove or bypass that hard stop in an ordinary refactor.
   Activation requires a dedicated security PR resolving every blocker listed
   in README, fault-injection tests, and independent review.

Preferred handoff format in a PR comment:

- files changed and why;
- safety invariants affected;
- exact test command and result;
- unresolved assumptions;
- explicit confirmation that OpenD, accounts, and orders were not accessed.
