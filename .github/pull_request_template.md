## What changed


## Why


## Safety impact

- [ ] SIMULATE-only remains enforced
- [ ] selected-symbol-only remains enforced
- [ ] no duplicate or blind retry path was added
- [ ] no sensitive/runtime/market/performance data is included
- [ ] no activation marker or account-bound lock is included

## Validation

```text
PYTHONPATH=src python3 -B -W error -m unittest discover -s tests -v
```

## External access

- [ ] No OpenD/account/order/network access was used

## Remaining limitations
<!-- List unresolved assumptions and why the order hard stop remains safe. -->
