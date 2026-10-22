# Native command return-path audit v1

## Scope

This independent, read-only audit binds to the frozen AArch64 `libg.so` SHA-256
`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`. It verifies
the native command id 86 lookup through the factory table, the construction
vtable slot that points to the command-local validation/execution function,
and a complete local control-flow enumeration of that function.

The audited routine is `0xd30a00..0xd30f2c` (end-exclusive). The adjacent
function begins at `0xd30f2c`; it is excluded. Every AArch64 instruction in the
local range must decode, branch destinations must remain in range, and every
reachable return must have a classified reaching definition for `w0/x0`.
Direct and indirect calls are recorded as unresolved interprocedural edges;
their effects are not inferred. Dynamic return expressions remain dynamic.

## Evidence limits

The report is a static local-CFG inventory, not a proof of global runtime
reachability, the live queue's dispatch path, helper semantics, condition
satisfiability, or equivalence between the engine's checks and the legal-action
mask. It does not close the general pending/rejection or G0/G1 acceptance gate.
No production source or engine binary is changed.

## Reproduction

From the repository root:

```sh
firstlight_CR/.venv-live/bin/python \
  _spec_work/versions/fair-final-native-command-audit-v1/audit_ct86.py \
  _spec_work/versions/fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so \
  --output _spec_work/versions/fair-final-native-command-audit-v1/audit-report.json

firstlight_CR/.venv-live/bin/python -m pytest -q \
  _spec_work/versions/fair-final-native-command-audit-v1/test_audit_ct86.py
```

The auditor rejects an unexpected library SHA, missing executable coverage,
changed dispatch/vtable anchors, undecodable bytes, out-of-range branches,
reachable indirect jumps, and unclassified return values.
