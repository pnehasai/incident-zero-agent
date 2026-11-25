# Live suite 20261122

| Case | Fault | Expected | Said (confidence) | Detect s | Investigate s | Detected | Diagnosis | Fix | Pass |
|---|---|---|---|---|---|---|---|---|---|
| S1 | recommendation | recommendation | recommendation (high) | 92 | 71 | yes | yes | yes | PASS |
| S2 | cart | cart | - (-) | 92 | 37 | yes | **no** | **no** | FAIL |
| N1 | none | no alert | - (-) | - | 0 | yes | yes | yes | PASS |
| S3 | product-catalog | product-catalog | product-catalog (high) | 46 | 35 | yes | yes | yes | PASS |
| S4 | payment | payment | - (-) | 107 | 38 | yes | **no** | **no** | FAIL |
| S5 | currency | currency | currency (high) | 61 | 30 | yes | yes | **no** | FAIL |
| N2 | none | no alert | - (-) | - | 0 | yes | yes | yes | PASS |

Passed 4 / 7.
Faults detected 5 / 5, median 92 s, max 107 s.
Correct service named 3 / 5; correct fix decision 2 / 5.
No-fault runs without a false alarm 2 / 2.

## Confidence vs correctness (faults that were investigated)

| Confidence | Runs | Correct service | Share correct |
|---|---|---|---|
| high | 3 | 3 | 100% |
| none | 2 | 0 | 0% |

Confident (high) and wrong: none.
