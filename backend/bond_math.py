"""Standardized par-bond scenarios, not prices of identified tradable securities.

100 principal, semiannual coupons with a prorated final stub, nominal YTM,
coupons/matured principal held as zero-yield cash until the horizon. Funding is
simple interest on initial capital, without leverage, fees or haircut effects.
"""
from __future__ import annotations

import math

TENOR_YEARS = {"3月": .25, "6月": .5, "1年": 1., "2年": 2., "3年": 3.,
               "5年": 5., "7年": 7., "10年": 10., "30年": 30.}


def finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def interp(curve: list[dict], years: float) -> float | None:
    points = sorted((TENOR_YEARS[p["tenor"]], p["value"]) for p in curve
                    if p.get("tenor") in TENOR_YEARS and finite(p.get("value")))
    if not points:
        return None
    if years <= points[0][0]:
        return points[0][1]
    for (t0, y0), (t1, y1) in zip(points, points[1:]):
        if t0 <= years <= t1:
            return y0 + (y1 - y0) * math.log(years / t0) / math.log(t1 / t0)
    return points[-1][1]


def par_flows(years: float, y: float) -> list[tuple[float, float]]:
    """y is decimal nominal annual YTM, compounded semiannually."""
    if years <= 0 or not math.isfinite(y) or y <= -2:
        raise ValueError("positive maturity and finite YTM > -200% required")
    dates = [i / 2 for i in range(1, math.ceil(years * 2))] + [years]
    discount = lambda t: (1 + y / 2) ** (-2 * t)
    intervals = [t - (dates[i - 1] if i else 0) for i, t in enumerate(dates)]
    coupon = (1 - discount(years)) / sum(dt * discount(t) for t, dt in zip(dates, intervals))
    return [(t, 100 * coupon * dt + (100 if i == len(dates) - 1 else 0))
            for i, (t, dt) in enumerate(zip(dates, intervals))]


def price(flows: list[tuple[float, float]], y: float, at: float = 0) -> float:
    if y <= -2:
        raise ValueError("YTM below discounting domain")
    return sum(cash * (1 + y / 2) ** (-2 * (t - at)) for t, cash in flows if t > at + 1e-10)


def scenario_rows(curve: list[dict], funding_pct: float | None, horizon: float = .25) -> list[dict]:
    if not finite(funding_pct):
        funding_pct = None
    rows = []
    for point in curve:
        tenor, y_pct = point.get("tenor"), point.get("value")
        n = TENOR_YEARS.get(tenor)
        if n is None or not finite(y_pct) or y_pct <= -200:
            continue
        y = y_pct / 100
        flows = par_flows(n, y)
        p0 = price(flows, y)
        paid = sum(cash for t, cash in flows if t <= horizon + 1e-10)
        remaining = max(n - horizon, 0)
        static_y = (interp(curve, remaining) or 0) / 100 if remaining else y
        # Same-YTM holding return includes accrued coupon and avoids treating
        # a full annual yield spread as a three-month coupon receipt.
        carry = (price(flows, y, horizon) + paid - p0) / p0 * 10000
        static = (price(flows, static_y, horizon) + paid - p0) / p0 * 10000
        funding = funding_pct / 100 * horizon * 10000 if funding_pct is not None else None
        net = static - funding if funding is not None else None
        eps = .0001
        up, down = price(flows, y + eps), price(flows, y - eps)
        duration = (down - up) / (2 * eps * p0)
        convexity = (down + up - 2 * p0) / (eps * eps * p0)

        def total_at(shift: float) -> float:
            return (price(flows, static_y + shift, horizon) + paid - p0) / p0 * 10000

        breakeven = None
        if remaining and funding is not None:
            lo, hi = max(-.5, -1.999 - static_y), .5
            if total_at(lo) >= funding >= total_at(hi):
                for _ in range(60):
                    mid = (lo + hi) / 2
                    if total_at(mid) > funding:
                        lo = mid
                    else:
                        hi = mid
                breakeven = (lo + hi) / 2 * 10000
        rows.append({
            "tenor": tenor, "years": n, "yield": y_pct,
            "gross_carry_bp_3m": round(carry, 3),
            "carry_bp_3m": round(carry - funding, 3) if funding is not None else None,
            "funding_bp_3m": round(funding, 3) if funding is not None else None,
            "roll_bp_3m": round(static - carry, 3),
            "gross_static_bp_3m": round(static, 3),
            "total_static_bp_3m": round(net, 3) if net is not None else None,
            "breakeven_bp_3m": round(breakeven, 3) if breakeven is not None else None,
            "modified_duration": round(duration, 4), "convexity": round(convexity, 4),
            "dv01_per_100": round(duration * p0 * .0001, 5),
            "scenarios": [{"shift_bp": b, "gross_bp": round(total_at(b / 10000), 3),
                           "net_bp": round(total_at(b / 10000) - funding, 3) if funding is not None else None}
                          for b in (-25, -10, 10, 25)],
            "remaining_years": remaining,
        })
    return sorted(rows, key=lambda r: r["years"])
