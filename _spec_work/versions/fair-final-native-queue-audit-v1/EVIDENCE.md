# Native queue dispatch evidence

The frozen 27,554,368-byte library matches SHA-256
`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`.
The imported command auditor is unchanged and its SHA is checked before import.

The static call chain is manager step -> manager inner -> queue step -> command
vtable +0x18. The ct86 slot resolves to 0xd30a00. The manager step and inner
ranges decode to 116 and 218 instructions; the queue append and step ranges
decode to 49 and 703. All instructions in those ranges are conservatively
locally reachable. This is a CFG property, not proof of runtime feasibility.

At the queue's age-20 branch, the call arguments are w2=2 and w3=0. A nonzero
result sets command+8 before joining destruction; zero first writes the
execution tick. The alternative branch uses w2=3 and w3=0. Initial age >=21
passes through a virtual tick adjustment before age is recomputed. The
comparison therefore does not describe every command's original age directly.

The manager's virtual tail exit at 0x10623b8 is explicitly fingerprinted by all
nine instructions ending at that exit. Its target remains unresolved; the
auditor neither follows it nor assumes it returns. Unregistered indirect exits
continue to fail. The first joint run exposed this omission (25 passed,
3 failed); its XML was subsequently overwritten, so only the tool output
records that run. After the fingerprint and negative tests were added, the
final joint run passed 42 tests in 0.375 seconds, with no failures or skips.
Those 42 include the prior command auditor's 10 tests and are not additive.

`audit-report.json` binds exact decoded ranges, anchors, vtable slots and
limitations. `version-manifest.json` binds source, contracts and final evidence
by SHA-256. No running deployment, production source or binary was changed.

This evidence does not establish the runtime mode predicate, branch
satisfiability, mask equivalence, legal reservation rejection, general pending
reachability, a complete Full128 episode, or any G0/G1 gate closure.
`training_admissible=false`; the 34/43 limited item registry is unchanged.
