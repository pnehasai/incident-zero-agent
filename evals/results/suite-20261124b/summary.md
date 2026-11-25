# Live suite 20261124b

| Case | Fault | Expected | Said (confidence) | Detect s | Investigate s | Detected | Diagnosis | Fix | Pass |
|---|---|---|---|---|---|---|---|---|---|
| S2 | cart | cart | cart (high) | 64 | 112 | yes | yes | yes | PASS |
| S3 | product-catalog | product-catalog | product-catalog (high) | 51 | 226 | yes | yes | yes | PASS |
| S4 | payment | payment | payment (high) | 48 | 44 | yes | yes | yes | PASS |

Passed 3 / 3.
Faults detected 3 / 3, median 51 s, max 64 s.
Correct service named 3 / 3; correct fix decision 3 / 3.
No-fault runs without a false alarm 0 / 0.

## Confidence vs correctness (faults that were investigated)

| Confidence | Runs | Correct service | Share correct |
|---|---|---|---|
| high | 3 | 3 | 100% |

Confident (high) and wrong: none.
