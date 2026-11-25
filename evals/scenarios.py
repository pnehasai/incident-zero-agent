"""End-to-end evaluation scenarios for the simulated telemetry world (evals/sim_world.py).

Each scenario is one incident shape seen (or expected) in the demo, described only through telemetry: metrics,
container resources, log messages and traces. `expected` is the ground truth used by the scorer; it never reaches
the agent. Values are realistic for the demo's traffic (checkout/payment ~0.3 req/s, frontend ~5 req/s).

expected keys:
  alert      - must the detector raise an alert?
  component  - service the diagnosis must name (None = no real incident)
  fix        - "none" (no action may be proposed), "restart_service" (must be proposed) or "any"
  accept     - for no-incident cases: acceptable outcomes ("INCONCLUSIVE" or a diagnosis with low confidence)
"""
from __future__ import annotations

from sim_world import Scenario

FP, FE = "frontend-proxy", "frontend"


def _flat(**vals):
    """Same values in every window (an old, steady problem)."""
    return {"now": dict(vals), "before": dict(vals), "baseline": dict(vals)}


SCENARIOS: list[Scenario] = [
    Scenario(
        id="payment-errors",
        description="payment rejects half of all charges; checkout and frontend fail as callers",
        expected={"alert": True, "component": "payment", "fix": "none"},
        metrics={"payment": {"now": {"err": 0.5}}, "checkout": {"now": {"err": 0.45, "p95": 420}},
                 FE: {"now": {"err": 0.03}}, FP: {"now": {"err": 0.025}}},
        traces=[{"path": [FP, FE, "checkout", "payment"], "count": 8, "error_at": "payment",
                 "message": "Payment request failed. Invalid token. app.loyalty.level=gold"}],
        logs=[{"service": "payment", "severity": "ERROR", "body": "Payment request failed. Invalid token.",
               "count_now": 20, "first_min_ago": 2, "last_min_ago": 0},
              {"service": "checkout", "severity": "WARN", "body": "failed to charge card: Invalid token",
               "count_now": 18, "first_min_ago": 2, "last_min_ago": 0}],
    ),
    Scenario(
        id="catalog-partial-errors",
        description="one product-catalog operation fails for some products; the error text names the injected fault "
                    "(must be withheld from the model)",
        expected={"alert": True, "component": "product-catalog", "fix": "none"},
        metrics={"product-catalog": {"now": {"err": 0.04}}, FE: {"now": {"err": 0.05}}, "checkout": {"now": {"err": 0.2}}},
        operations=[{"service": "product-catalog", "op": "oteldemo.ProductCatalogService/GetProduct",
                     "now": {"rate": 4.0, "err": 0.06}, "before": {"rate": 4.0}, "baseline": {"rate": 4.0}},
                    {"service": "product-catalog", "op": "oteldemo.ProductCatalogService/ListProducts",
                     "now": {"rate": 2.0, "err": 0.0}, "before": {"rate": 2.0}, "baseline": {"rate": 2.0}}],
        traces=[{"path": [FP, FE, "product-catalog"], "count": 6, "error_at": "product-catalog",
                 "message": "Error: Product Catalog Fail Feature Flag Enabled"},
                {"path": [FP, FE, "checkout", "product-catalog"], "count": 3, "error_at": "product-catalog",
                 "message": "Error: Product Catalog Fail Feature Flag Enabled"}],
        logs=[{"service": FE, "severity": "ERROR", "count_now": 15, "first_min_ago": 2, "last_min_ago": 0,
               "body": "failed to get product: rpc error: code = Internal desc = Error: Product Catalog Fail Feature Flag Enabled"}],
    ),
    Scenario(
        id="cpu-hog",
        description="ad burns ~4 cores; its latency rises, nothing errors",
        expected={"alert": True, "component": "ad", "fix": "any"},
        metrics={"ad": {"now": {"p95": 1500}}, FE: {"now": {"p95": 420}}},
        containers={"ad": {"cpu": 390.0, "cpu_prior": 6.0}},
        traces=[{"path": [FP, FE, "ad"], "count": 6, "slow_at": "ad", "slow_ms": 1500}],
    ),
    Scenario(
        id="crash-loop",
        description="recommendation leaks memory and restarts every few minutes; frontend calls go unanswered",
        expected={"alert": True, "component": "recommendation", "fix": "restart_service"},
        metrics={"recommendation": {"now": {"rate": 0.6, "err": 0.3, "p95": 1600}},
                 FE: {"now": {"err": 0.06, "p95": 1300}}, FP: {"now": {"err": 0.05, "p95": 1400}}},
        containers={"recommendation": {"restarts": 4.0, "uptime": 95.0, "mem": 88.0, "mem_prior": 50.0,
                                       "mem_prior_max": 70.0, "mem_growth": 0.4, "mem_slope_mb_min": 20.0, "mem_mb": 400.0}},
        traces=[{"path": [FP, FE, "recommendation"], "count": 5, "unanswered": True,
                 "message": "rpc error: code = Unavailable desc = connection refused"},
                {"path": [FP, FE, "recommendation", "product-catalog"], "count": 3, "slow_at": "recommendation",
                 "slow_ms": 1500}],
        logs=[{"service": FE, "severity": "ERROR", "count_now": 12, "first_min_ago": 6, "last_min_ago": 0,
               "body": "failed to get recommendations: rpc error: code = Unavailable"}],
    ),
    Scenario(
        id="cart-app-errors",
        description="cart cannot reach its store and throws on EmptyCart; the process itself is healthy "
                    "(a restart must not be proposed)",
        expected={"alert": True, "component": "cart", "fix": "none"},
        metrics={"cart": {"now": {"err": 0.12}}, FE: {"now": {"err": 0.04}}, "checkout": {"now": {"err": 0.3}}},
        operations=[{"service": "cart", "op": "oteldemo.CartService/EmptyCart",
                     "now": {"rate": 0.3, "err": 1.0}, "before": {"rate": 0.3}, "baseline": {"rate": 0.3}},
                    {"service": "cart", "op": "oteldemo.CartService/GetCart",
                     "now": {"rate": 1.7, "err": 0.0}, "before": {"rate": 1.7}, "baseline": {"rate": 1.7}}],
        traces=[{"path": [FP, FE, "checkout", "cart"], "count": 6, "error_at": "cart",
                 "message": "Can't access cart storage. System.ApplicationException: Wasn't able to connect to redis"}],
        logs=[{"service": "cart", "severity": "ERROR", "body": "Can't access cart storage", "count_now": 20,
               "first_min_ago": 3, "last_min_ago": 0}],
    ),
    Scenario(
        id="payment-unreachable",
        description="payment stops answering; checkout's calls to it fail with no span from payment",
        expected={"alert": True, "component": "payment", "fix": "any"},
        metrics={"payment": {"now": {"rate": 0.0}}, "checkout": {"now": {"err": 0.5, "p95": 2100}},
                 FE: {"now": {"err": 0.03}}},
        traces=[{"path": [FP, FE, "checkout", "payment"], "count": 8, "unanswered": True,
                 "message": "rpc error: code = Unavailable desc = connection error: dial tcp: lookup payment: no such host"}],
        logs=[{"service": "checkout", "severity": "ERROR", "count_now": 15, "first_min_ago": 2, "last_min_ago": 0,
               "body": "failed to charge card: could not charge the card: rpc error: code = Unavailable"}],
    ),
    Scenario(
        id="slow-dependency",
        description="shipping takes ~4 s per call; frontend-proxy gives up with 504s (the caller is not the cause)",
        expected={"alert": True, "component": "shipping", "fix": "any"},
        metrics={"shipping": {"now": {"p95": 4200}}, "checkout": {"now": {"p95": 4500}},
                 FE: {"now": {"p95": 1800}}, FP: {"now": {"err": 0.03, "p95": 2500}}},
        traces=[{"path": [FP, FE, "checkout", "shipping", "quote"], "count": 6, "slow_at": "shipping",
                 "slow_ms": 4000, "timeout": True},
                {"path": [FP, FE, "checkout", "shipping", "quote"], "count": 4, "slow_at": "shipping", "slow_ms": 4000}],
    ),
    Scenario(
        id="leftover-plus-new",
        description="an old cart problem keeps failing a few calls (began 45 min ago); payment errors start now",
        expected={"alert": True, "component": "payment", "fix": "none"},
        metrics={"cart": _flat(err=0.03), "payment": {"now": {"err": 0.3}}, "checkout": {"now": {"err": 0.3}},
                 FE: {"now": {"err": 0.025}}},
        operations=[{"service": "cart", "op": "oteldemo.CartService/EmptyCart", **_flat(rate=0.1, err=0.6)}],
        traces=[{"path": [FP, FE, "checkout", "cart"], "count": 2, "error_at": "cart", "message": "Can't access cart storage"},
                {"path": [FP, FE, "checkout", "payment"], "count": 6, "error_at": "payment",
                 "message": "Payment request failed. Invalid token."}],
        logs=[{"service": "cart", "severity": "ERROR", "body": "Can't access cart storage", "count_now": 3,
               "first_min_ago": 45, "last_min_ago": 0, "count_hist": 60},
              {"service": "payment", "severity": "ERROR", "body": "Payment request failed. Invalid token.",
               "count_now": 14, "first_min_ago": 2, "last_min_ago": 0}],
    ),
    Scenario(
        id="no-incident",
        description="normal operation with the demo's usual noise: a latency drift and steady background errors",
        expected={"alert": False, "component": None, "fix": "none", "accept": ["INCONCLUSIVE", "low"]},
        metrics={FE: {"now": {"p95": 210}}},
        manual_symptom="A user reports that the shop feels a bit slow today.",
    ),
    Scenario(
        id="memory-leak",
        description="email's memory climbs toward its limit; its latency and checkout's rise",
        expected={"alert": True, "component": "email", "fix": "any"},
        metrics={"email": {"now": {"p95": 1400}}, "checkout": {"now": {"p95": 1700}}},
        containers={"email": {"mem": 93.0, "mem_prior": 62.0, "mem_prior_max": 66.0, "mem_growth": 0.25,
                              "mem_slope_mb_min": 3.0, "mem_mb": 280.0}},
        traces=[{"path": [FP, FE, "checkout", "email"], "count": 5, "slow_at": "email", "slow_ms": 1300}],
    ),
]

BY_ID = {s.id: s for s in SCENARIOS}


def renaming(scenario: Scenario) -> dict[str, str]:
    """Consistent invented names for every service/container (shows the agent does not rely on known names)."""
    from sim_world import BASE_CONTAINERS, BASE_SERVICES

    names = sorted(set(BASE_SERVICES) | set(BASE_CONTAINERS) | set(scenario.metrics) | set(scenario.containers))
    return {n: f"svc-{chr(97 + i // 26)}{chr(97 + i % 26)}" for i, n in enumerate(names)}
