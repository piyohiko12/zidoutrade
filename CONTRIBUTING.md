# Contributing

The safest collaboration unit is a draft pull request.

1. Create a branch such as `claude/<topic>` or `agent/<topic>`.
2. State the affected invariant before editing.
3. Add or update a synthetic regression test.
4. Run the complete warning-as-error test suite.
5. Open a draft PR and request an independent review.

Do not upload screenshots or logs containing account, order, balance, position,
price, or PnL data. Do not use a real brokerage connection to reproduce a bug.
Model the condition with an injected fake broker.

The project intentionally has no automatic deployment or activation workflow.
Merging source code cannot arm the trading adapter.

From a clean checkout, run the suite without installing the package:

```bash
PYTHONPATH=src python3 -B -W error -m unittest discover -s tests -v
```
