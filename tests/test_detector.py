"""Step 1: threshold detection. Invented service names: thresholds are generic."""
import detector as d


def table(**svcs):
    # history present; baseline traffic = current traffic unless a test sets it (no accidental "surge")
    return {name: {"rate_base": m.get("rate", 1.0), "history": 1.0, "served": 1.0, **m} for name, m in svcs.items()}


def test_error_and_latency_thresholds_against_own_baseline():
    t = table(**{"svc-a": {"err": 0.30, "err_base": 0.01, "rate": 0.5},        # errors jumped (60 requests)
                 "svc-b": {"err": 0.06, "err_base": 0.05, "rate": 5.0},        # high but normal for it
                 "svc-g": {"err": 0.04, "err_base": 0.0, "rate": 7.0},         # partial fault: 4% from ~0
                 "svc-c": {"p95": 4200.0, "p95_base": 900.0, "rate": 2.0},     # latency 4.7x
                 "svc-f": {"p95": 1900.0, "p95_base": 500.0, "rate": 0.03},    # 4x, but from ~5 requests
                 "svc-d": {"p95": 1500.0, "p95_base": 1000.0},                 # slow but its usual
                 "svc-e": {"err": 0.50, "err_base": 0.0, "rate": 0.01},        # 1 failed call: a blip
                 "svc-h": {"err": 0.667, "err_base": 0.167, "rate": 0.05}})    # 4 of 6 requests: too few
    found = {(b.service, b.text.split()[0]) for b in d.breaches(t)}
    assert found == {("svc-a", "error"), ("svc-c", "p95"), ("svc-g", "error")}


def test_needs_two_checks_then_cools_down_but_new_services_still_alert():
    now = [0.0]
    det = d.Detector(clock=lambda: now[0])
    a = [d.Breach("svc-a", "x")]
    assert det.check(a) == []                     # first sighting: could be a blip
    assert [b.service for b in det.check(a)] == ["svc-a"]
    now[0] = 60
    both = [d.Breach("svc-a", "x"), d.Breach("svc-b", "y")]
    det.check(both)                               # svc-b first sighting; svc-a in cooldown
    assert [b.service for b in det.check(both)] == ["svc-b"]  # a second fault overlapping the first still alerts
    now[0] = d.COOLDOWN_S + 1                    # svc-a cooled down; svc-b (alerted at 60 s) still quiet
    assert [b.service for b in det.check(both)] == ["svc-a"]  # still breaching after its cooldown: alert again


def test_alert_text_states_values_not_causes():
    text = d.alert_text([d.Breach("svc-a", "error rate of svc-a is 30.0% (its 30-min baseline 1.0%)")])
    assert text.startswith("Threshold alert: ") and "30.0%" in text


def test_no_alert_without_history():
    # right after a demo restart Prometheus has no baseline: normal background errors must not look like a jump
    assert d.breaches({"svc-a": {"err": 0.30, "rate": 5.0}}) == []


def test_fault_switched_on_again_is_seen_against_the_previous_window():
    # the 30-min baseline still holds an earlier spell of the same fault (3.7%), but the last 2 min were clean
    t = table(**{"svc-a": {"err": 0.035, "err_base": 0.037, "err_prev": 0.0, "rate": 7.0},
                 "svc-b": {"err": 0.035, "err_base": 0.037, "err_prev": 0.034, "rate": 7.0}})  # steady: no alert
    assert [(b.service, "jumped" in b.text) for b in d.breaches(t)] == [("svc-a", True)]


def test_no_alert_during_warm_up():
    # 15 min after a restart there is a "baseline" but no traffic 30 min ago: warm-up latency must not alert
    assert d.breaches({"svc-a": {"p95": 13800.0, "p95_base": 300.0, "rate": 5.0, "rate_base": 5.0}}) == []


def test_traffic_source_never_alerts():
    # only client spans (a load generator): no "served" row, so even 50% errors do not alert
    t = {"loadgen": {"rate_base": 1.0, "history": 1.0, "rate": 1.0, "err": 0.5, "err_base": 0.0}}
    assert d.breaches(t) == []


def test_jump_rule_needs_enough_failed_calls():
    # 0.8 calls/s, 1.5% -> 5.7%: ~5 failed calls in 2 min, below JUMP_MIN_CALLS (live false alarm, 4 Nov)
    t = table(svc={"rate": 0.8, "err": 0.057, "err_prev": 0.015, "err_base": 0.04})
    assert d.breaches(t) == []
    t = table(svc={"rate": 5.0, "err": 0.057, "err_prev": 0.015, "err_base": 0.04})
    assert [b.service for b in d.breaches(t)] == ["svc"]


def test_shorter_baseline_for_demos():
    before = d.BASELINE_MIN
    try:
        d.configure(10)
        assert "[10m]" in d.QUERIES["rate_base"] and "offset 10m" in d.QUERIES["history"]
        t = table(svc={"rate": 5.0, "err": 0.4, "err_base": 0.0})
        assert "10-min baseline" in d.breaches(t)[0].text
        d.configure(1)
        assert d.BASELINE_MIN == 5  # never below 5 min
    finally:
        d.configure(before)


def test_warm_up_falls_back_to_10_min_baseline():
    # no 30-min history yet (just after a start or a sleep) but 10 min of it: alert against the 10-min baseline
    m = {"served": 1.0, "rate_base_warmup": 5.0, "history_warmup": 5.0, "err_base_warmup": 0.0, "rate": 5.0, "err": 0.4}
    got = d.breaches({"svc": m})
    assert [b.service for b in got] == ["svc"] and "10-min baseline" in got[0].text
    # with the full history the 30-min baseline is used, even if the warm-up rows are present too
    m.update({"rate_base": 5.0, "history": 5.0, "err_base": 0.0})
    assert "30-min baseline" in d.breaches({"svc": m})[0].text
    assert "rate_base_warmup" in d.QUERIES and "[10m]" in d.QUERIES["rate_base_warmup"]


# ---- rules added after the live suite of 22 Nov (faults requests alone do not show)
def test_rare_operation_failing_alerts_though_its_service_rate_barely_moves():
    # live: EmptyCart 2-4 of 8-17 calls failing while GetCart/AddItem (~340 calls) were fine
    ops = {"Empty": {"calls": 17.0, "failed": 4.0, "failed_recent": 1.0, "calls_base": 50.0, "failed_base": 0.0},
           "Get": {"calls": 540.0, "failed": 0.0, "calls_base": 1600.0, "failed_base": 0.0}}
    t = table(**{"svc-a": {"err": 0.007, "err_base": 0.0, "rate": 0.9, "ops": ops}})
    assert [(b.service, b.signal) for b in d.breaches(t)] == [("svc-a", "operation_errors")]


def test_operation_rule_ignores_few_failures_usual_failures_and_new_operations():
    ops = {"two": {"calls": 5.0, "failed": 2.0, "calls_base": 20.0, "failed_base": 0.0},       # 2 failures: noise
           "usual": {"calls": 20.0, "failed": 6.0, "calls_base": 60.0, "failed_base": 18.0},   # always 30%
           "new": {"calls": 10.0, "failed": 5.0}}                                              # no history
    assert d.breaches(table(**{"svc-a": {"rate": 1.0, "ops": ops}})) == []


def test_cpu_jump_against_own_average_alerts_but_steady_high_cpu_and_start_up_do_not():
    res = {"hot": {"cpu": 387.0, "cpu_base": 3.0, "uptime": 9000.0},
           "busy": {"cpu": 170.0, "cpu_base": 140.0, "uptime": 9000.0},     # a load generator: always high
           "young": {"cpu": 300.0, "cpu_base": 5.0, "uptime": 120.0}}       # JVM start-up after a restart
    t = {n: {"res": r} for n, r in res.items()}
    assert [(b.service, b.signal) for b in d.breaches(t)] == [("hot", "container_cpu")]


def test_memory_alert_needs_high_rising_and_above_own_average():
    res = {"leak": {"mem": 88.0, "mem_base": 76.0, "mem_5m_ago": 84.0, "uptime": 9000.0},
           "full": {"mem": 88.0, "mem_base": 87.0, "mem_5m_ago": 88.0, "uptime": 9000.0},   # always near its limit
           "flat": {"mem": 88.0, "mem_base": 70.0, "mem_5m_ago": 88.0, "uptime": 9000.0}}   # stopped rising
    t = {n: {"res": r} for n, r in res.items()}
    assert [(b.service, b.signal) for b in d.breaches(t)] == [("leak", "container_memory")]


def test_traffic_surge_alerts_on_a_served_service():
    t = table(**{"svc-a": {"rate": 29.0, "rate_base": 7.8},       # live flood: 7.8 -> 29 req/s
                 "svc-b": {"rate": 0.9, "rate_base": 0.2}})       # 4.5x but tiny: not a flood
    assert [(b.service, b.signal) for b in d.breaches(t)] == [("svc-a", "traffic_surge")]


def test_operation_that_stopped_failing_does_not_alert_from_its_old_window():
    # replay 22 Nov: a stopped container's failures stayed in the 10-min window 4 min after it was back
    ops = {"Get": {"calls": 770.0, "failed": 217.0, "failed_recent": 0.0, "calls_base": 2300.0, "failed_base": 40.0}}
    assert d.breaches(table(**{"svc-a": {"rate": 1.3, "ops": ops}})) == []


# ---- fixes after the live run of 24 Nov
def test_service_recovering_from_an_outage_is_not_a_flood():
    # product-catalog back after a stop: 7.1 req/s against a baseline the outage pulled to ~0
    t = table(**{"svc-a": {"rate": 7.1, "rate_base": 0.03}})
    assert d.breaches(t) == []


def test_latency_spike_within_a_services_usual_peaks_does_not_alert():
    # email, ~30 requests per 2 min: p95 jumps to 0.7-0.9 s every few minutes; 1.1 s is one of those
    jitter = table(svc={"p95": 1120.0, "p95_base": 200.0, "p95_peak": 880.0, "rate": 0.28})
    assert d.breaches(jitter) == []
    fault = table(svc={"p95": 4000.0, "p95_base": 200.0, "p95_peak": 880.0, "rate": 0.28})
    assert [b.signal for b in d.breaches(fault)] == ["p95_latency"]
    assert "p95_peak" in d.QUERIES and "p95_peak_warmup" in d.QUERIES


def test_recovery_back_to_the_old_level_is_not_a_flood_but_a_real_flood_is():
    # 24 Nov replay: product-catalog 8.2 req/s against a 1.6 req/s average its outage pulled down; before it, ~7 req/s
    back = table(**{"svc-a": {"rate": 8.2, "rate_base": 1.6, "rate_peak": 7.0}})
    assert d.breaches(back) == []
    flood = table(**{"svc-a": {"rate": 29.0, "rate_base": 7.8, "rate_peak": 9.0}})
    assert [b.signal for b in d.breaches(flood)] == ["traffic_surge"]


def test_peak_check_only_for_low_traffic_and_no_baseline_from_an_outage():
    busy = table(svc={"p95": 2600.0, "p95_base": 100.0, "p95_peak": 2000.0, "rate": 5.0})   # 600 requests: stable p95
    assert [b.signal for b in d.breaches(busy)] == ["p95_latency"]
    # down 10 min ago (traffic then 0) and no 30-min history: no baseline at all
    m = {"served": 1.0, "rate": 8.2, "rate_base_warmup": 1.6, "history_warmup": 0.0, "rate_peak_warmup": 0.7}
    assert d.breaches({"svc": m}) == []
