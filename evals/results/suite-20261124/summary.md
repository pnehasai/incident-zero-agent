# Live suite 20261124

| Case | Fault | Expected | Said (confidence) | Detect s | Investigate s | Detected | Diagnosis | Fix | Pass |
|---|---|---|---|---|---|---|---|---|---|
| S1 | recommendation | recommendation | recommendation (high) | 95 | 85 | yes | yes | yes | PASS |
| S2 | cart | cart | - (-) | 48 | 101 | yes | **no** | **no** | FAIL |
| N1 | none | no alert | - (-) | - | 0 | yes | yes | yes | PASS |
| S3 | product-catalog | product-catalog | - (-) | 49 | 61 | yes | **no** | **no** | FAIL |
| S4 | payment | payment | - (-) | 80 | 98 | yes | **no** | **no** | FAIL |
| S5 | currency | currency | currency (high) | 95 | 179 | yes | yes | yes | PASS |
| N2 | none | no alert | - (-) | - | 0 | yes | yes | yes | PASS |

Passed 4 / 7.
Faults detected 5 / 5, median 80 s, max 95 s.
Correct service named 2 / 5; correct fix decision 2 / 5.
No-fault runs without a false alarm 2 / 2.

## Confidence vs correctness (faults that were investigated)

| Confidence | Runs | Correct service | Share correct |
|---|---|---|---|
| high | 2 | 2 | 100% |
| none | 3 | 0 | 0% |

Confident (high) and wrong: none.
