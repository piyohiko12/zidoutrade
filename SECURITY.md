# Security policy

## Scope

This repository is designed for moomoo paper trading (`SIMULATE`) only. It has
no supported path to `REAL`, does not call `unlock_trade`, and must connect only
to the existing local OpenD endpoint at `127.0.0.1:11111`.

The current reviewed release cannot submit even a `SIMULATE` order. Both the
runner and broker transport contain an unconditional fail-closed boundary. Do
not remove it until every blocker listed in the README is implemented, tested,
and independently reviewed in a separate pull request.

## Never commit

- account identifiers or reversible hashes of low-entropy identifiers;
- passwords, tokens, cookies, private keys, or `.env` files;
- activation markers, final locks, order reservations, runtime state, logs, or
  journal entries;
- balances, positions, prices, PnL, market-data extracts, or order history.

Use synthetic values in tests. A clone is intentionally disarmed: it starts in
`SHADOW` mode and contains no activation artifact.

## Target safety invariants

The order-lifecycle items below are the reviewed target design. The current
hard stop makes them non-executable rather than claiming they are production
complete.

- `SIMULATE`, US long-only, RTH, one user-selected active symbol.
- No automatic software installation, registration, subscription, trial,
  entitlement expansion, credit purchase, or billing action.
- Durable intent is written before an order attempt. A recovered intent is
  query-only and is never blindly re-dispatched.
- The system promises at-most-once dispatch attempts, not broker-side exactly
  once execution.
- `PAUSE_ENTRIES` blocks new buys but cannot suppress reconciliation and an
  otherwise safe exit of a known position.
- Ambiguous broker state becomes `RECOVERY_REQUIRED`; it never triggers a
  guessed order.
- Unattended operation remains disabled until independent position protection
  has been demonstrated in paper trading.

## Reporting a vulnerability

Do not include credentials, account data, prices, or order records in a public
issue. Provide a minimal synthetic reproducer and identify the violated safety
invariant. Treat any route to `REAL`, an unselected symbol, a duplicate dispatch,
or an exit larger than the reconciled position as critical.
