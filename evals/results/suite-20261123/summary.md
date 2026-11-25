# Live suite 20261123

| Case | Fault | Expected | Said (confidence) | Detect s | Investigate s | Detected | Diagnosis | Fix | Pass |
|---|---|---|---|---|---|---|---|---|---|
| S1 | recommendation | recommendation | recommendation (high) | 93 | 71 | yes | yes | yes | PASS |
| S2 | cart | cart | cart (high) | 46 | 61 | yes | yes | yes | PASS |
| N1 | none | no alert | - (-) | - | 0 | yes | yes | yes | INVALID (clock gap 12:24-13:19 (laptop slept; watched 2 of 10 min)) |
| S3 | product-catalog | product-catalog | - (-) | 596 | 77 | yes | **no** | **no** | INVALID (no baseline (Prometheus gap 12:25-13:20 from the sleep)) |
| S4 | payment | payment | payment (high) | 77 | 50 | yes | yes | yes | PASS |
| S5 | currency | currency | currency (high) | 61 | 89 | yes | yes | yes | PASS |
| N2 | none | no alert | email (high) | 510 | 47 | **no** | **no** | **no** | FAIL |

Not counted (invalid, re-run by resuming the same --run): N1, S3.

Passed 4 / 5.
Faults detected 4 / 4, median 77 s, max 93 s.
Correct service named 4 / 4; correct fix decision 4 / 4.
No-fault runs without a false alarm 0 / 1.

## Confidence vs correctness (faults that were investigated)

| Confidence | Runs | Correct service | Share correct |
|---|---|---|---|
| high | 4 | 4 | 100% |

Confident (high) and wrong: none.
