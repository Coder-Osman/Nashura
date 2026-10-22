# Native queue dispatch audit v1

This independent read-only audit extends the frozen command-local CFG auditor
without changing it or the running experiment. The input library must match
SHA-256 `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`.
The imported auditor's SHA is checked before execution.

The manager step calls its inner routine, which reads the queue at manager+0x38
and invokes 0xd2f37c by a call or a tail branch. The queue's bit0-clear branch
compares command age with 20. At age 20 it calls the command vtable+0x18 with
w2=2 and w3=0. The frozen ct86 vtable resolves this slot to 0xd30a00. A nonzero
return sets command+8 and joins command destruction; zero writes its execution
tick before joining destruction. A different branch uses w2=3 and w3=0.

Each stated instruction sequence and all ct86 executor/type/tick-setter slots
are checked. The decoded ranges receive conservative local CFG traversal.
Unknown external branches fail except for the explicitly registered queue tail
call and a fingerprinted virtual tail exit at 0x10623b8. That exit's destination
remains unresolved; it is recorded as an exit rather than followed or assumed
to return. The full nine-instruction fingerprint is required. Changed
instructions, missing anchors, wrong vtable targets and wrong
library identities are rejected by tests.

The age comparison uses a recomputed age: an initial age of at least 21 first
passes through a virtual tick adjustment. The retain/remove comparison must
not be interpreted as directly describing every command's original age.

This establishes a static native manager→queue→ct86 validation link. It does
not solve branch feasibility, prove which mode predicate holds at runtime,
map all errors to rule names, equate the policy masks to the native checks,
or demonstrate that a legal reservation can reach rejection/pending. It does
not close G0/G1 or authorize training. No binary or production file is edited.

Reproduce from the repository root with the project Python:

```sh
firstlight_CR/.venv-live/bin/python \
  _spec_work/versions/fair-final-native-queue-audit-v1/audit_queue.py \
  _spec_work/versions/fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so \
  --output _spec_work/versions/fair-final-native-queue-audit-v1/audit-report.json
firstlight_CR/.venv-live/bin/python -m pytest -q \
  _spec_work/versions/fair-final-native-command-audit-v1/test_audit_ct86.py \
  _spec_work/versions/fair-final-native-queue-audit-v1/test_audit_queue.py
```
