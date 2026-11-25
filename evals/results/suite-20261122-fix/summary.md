# Live suite 20261122-fix

| Case | Fault | Expected | Said (confidence) | Detect s | Investigate s | Detected | Diagnosis | Fix | Pass |
|---|---|---|---|---|---|---|---|---|---|
| S1 | recommendation | recommendation | recommendation (high) | 46 | 38 | yes | yes | yes | PASS |
| F1 | paymentFailure 50% | payment | payment (high) | 154 | 52 | yes | yes | yes | PASS |
| S2 | cart | cart | cart (high) | 77 | 40 | yes | yes | yes | PASS |
| F2 | adHighCpu on | ad | ad (medium) | - | 65 | **no** | yes | yes | FAIL |
| N1 | none | no alert | - (-) | - | 0 | yes | yes | yes | PASS |
| S3 | product-catalog | product-catalog | product-catalog (high) | 93 | 43 | yes | yes | yes | PASS |
| F3 | cartFailure 50% | cart | cart (high) | - | 35 | **no** | yes | yes | FAIL |
| F4 | paymentUnreachable on | checkout / payment | cart (low) | - | 65 | **no** | **no** | yes | FAIL |
| S4 | payment | payment | payment (high) | 47 | 37 | yes | yes | yes | PASS |
| F5 | productCatalogFailure on | product-catalog | - (-) | - | 45 | **no** | **no** | yes | FAIL |
| F6 | recommendationCacheFailure on | recommendation | recommendation (high) | 224 | 62 | yes | yes | yes | PASS |
| F7 | adFailure on | ad | ad (high) | 81 | 83 | yes | yes | yes | PASS |
| S5 | currency | currency | currency (high) | 92 | 33 | yes | yes | yes | PASS |
| F8 | emailMemoryLeak 100x | email | - (-) | - | 54 | **no** | **no** | yes | FAIL |
| F9 | productCatalogLockContention on | product-catalog | product-catalog (low) | 94 | 47 | yes | yes | yes | PASS |
| F11 | adManualGc on | ad | ad (high) | 80 | 49 | yes | yes | yes | PASS |
| F12 | intlShippingSlowdown 5sec | shipping | email (high) | - | 54 | **no** | **no** | yes | FAIL |
| F13 | imageSlowLoad 5sec | frontend-proxy / image-provider | payment (low) | 93 | 48 | yes | **no** | yes | FAIL |
| F15 | loadGeneratorFloodHomepage on | frontend / frontend-proxy / load-generator | cart (low) | - | 42 | **no** | **no** | yes | FAIL |
| N2 | none | no alert | - (-) | - | 0 | yes | yes | yes | PASS |

Passed 12 / 20.
Faults detected 11 / 18, median 92 s, max 224 s.
Correct service named 12 / 18; correct fix decision 18 / 18.
No-fault runs without a false alarm 2 / 2.

## Confidence vs correctness (faults that were investigated)

| Confidence | Runs | Correct service | Share correct |
|---|---|---|---|
| high | 11 | 10 | 91% |
| medium | 1 | 1 | 100% |
| low | 4 | 1 | 25% |
| none | 2 | 0 | 0% |

Confident (high) and wrong: F12.
