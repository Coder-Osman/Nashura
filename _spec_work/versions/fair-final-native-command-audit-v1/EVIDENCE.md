# Native command return-path audit evidence

## Result

The fixed library was rehashed and matched SHA-256
`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`.
The command id 86 factory entry resolves to `0xd2e63c`; the constructor at
`0xd309a8` installs the vtable at `0x18776a0`, whose `+0x18` slot points to the
audited local routine at `0xd30a00`.

The executor range `0xd30a00..0xd30f2c` decoded completely: 331 instructions,
all 331 reached by the conservative local CFG, one local return, 63 direct
calls and 7 indirect calls. The return's reaching definitions include literal
values `0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 12, 13, 22, 23, 24, 25, 50, 100`, plus
the arithmetic-derived expression `helper_result + 14`. These are instruction
level values, not semantic error names or proof that every branch is feasible.

The position helper range `0xcfcf50..0xcfd094` decoded completely: 81
instructions, all locally reachable, one return, 6 direct calls and 1 indirect
call. Its observed literal returns are `0..5`; helper meanings and caller-side
conditions remain unresolved.

Final tests: 10 passed, 0 failed, 0 skipped (`tests-final.xml`). The report is
`audit-report.json`. `version-manifest.json` binds the source, frozen engine,
and generated evidence files by SHA-256. The independent running search episode
was separately checked at snapshot `1792572096.2062898`: 20 groups were
committed through tick 844, the second player's tick-844 root was recorded as
128 visits, and the first player's tick-904 root was running. The worker and
both owned engines remained alive; no terminal/cleanup report existed. The
launched 244-file source set matched the local source SHA values exactly.

## Limits

This evidence verifies command dispatch and enumerates a local CFG only. Calls
are not followed, branch predicates are not solved, and the live command
queue's use of this virtual method has not been proven. Therefore it does not
establish semantic rejection causes, mask equivalence, legal rejection
reachability, general pending correctness, or any G0/G1 gate closure. It is
independent of the active search attempt and does not modify production code or
the frozen engine.
