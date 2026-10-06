"""Daily-step simulation engine.

One call to ``Engine.run`` replays the whole world from ``(config, seed)`` and yields
canonical event dicts in global time order. Per day the order of operations is fixed:

1. arrivals (customer.created, price exposure, conversion, subscription start)
2. billing: payment retries due today, then invoices due today (may churn involuntarily)
3. demand, regional load, service quality, throttling, usage / request events
4. voluntary churn and tier changes (end of day)
5. hourly regional service metrics

Customers react to service quality with a one-day lag. All randomness comes from
``rng_for(seed, stream, day)`` so streams do not depend on iteration order.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from numpy.typing import NDArray

from praxis.domain.experiments import assignment_uniform
from praxis.simulator.config import FAILURE_REASONS, PAYMENT_METHODS, TIERS, SimulationConfig
from praxis.simulator.events import Draft, EventFactory
from praxis.simulator.infrastructure import HOURS, Infrastructure, RegionDay
from praxis.simulator.population import Population, rng_for

STREAM_DEMAND, STREAM_BILLING, STREAM_CHURN, STREAM_CONVERSION, STREAM_INFRA = 1, 2, 3, 4, 5

UNBORN, PROSPECT, CONVERTED, ACTIVE, CHURNED, LOST = range(6)

# second offsets within a day; strictly increasing along every causal chain
T_CREATED, T_PRICE, T_CONVERSION, T_SUBSCRIPTION = 60, 120, 180, 240
T_TIER_CHANGE = 12 * 3600
T_INVOICE, T_ATTEMPT, T_RESULT, T_INV_CHURN = 3600, 3660, 3720, 3780
T_USAGE, T_REQUEST, T_CHURN = 23 * 3600, 23 * 3600 + 1, 23 * 3600 + 50 * 60

F64 = NDArray[np.float64]
I64 = NDArray[np.int64]
Event = dict[str, Any]


@dataclass
class _DayCtx:
    """Mutable per-day handles shared with ``Engine._attempt``."""

    day: int
    ts0: int
    add: Callable[[Draft], None]
    u_pay: F64
    u_reason: F64
    state: NDArray[np.int8]
    burden: F64
    pending: dict[int, list[tuple[int, str, int, int, str]]]


def _uniform_from_hash(salt: str, ids: list[str]) -> F64:
    return np.array([assignment_uniform(salt, cid) for cid in ids], dtype=np.float64)


class Engine:
    def __init__(self, config: SimulationConfig, seed: int, population: Population) -> None:
        self.cfg = config
        self.seed = seed
        self.pop = population
        self.run_id = f"{seed}-{config.config_hash[:12]}"
        self.infra = Infrastructure(config, population)
        self.factory = EventFactory(self.run_id)

        self.prod_ids = [p.id for p in config.products]
        self.region_ids = [r.id for r in config.regions]
        self.ref_price = np.array([p.ref_price_micros for p in config.products], dtype=np.float64)
        self.weights = self.infra.weights
        self.compute_cols = np.array([p.compute_intensive for p in config.products])
        self.request_col = next(
            (i for i, p in enumerate(config.products) if p.emits_requests), None
        )
        self.tier_fee = [t.base_fee_minor for t in config.tiers]
        self.tier_hazard = np.array([t.churn_hazard_daily for t in config.tiers])
        self.tier_conv = np.array([t.conversion_base for t in config.tiers])
        b = config.billing
        weights = np.array([b.failure_reason_weights[r] for r in FAILURE_REASONS])
        self._reason_cdf = np.cumsum(weights / weights.sum())
        sd = config.run.start_date
        self.epoch0 = int(datetime(sd.year, sd.month, sd.day, tzinfo=UTC).timestamp())
        self.start_dow = config.run.start_date.weekday()

        self._global_mult = self._build_global_multipliers()
        self._treated = [
            _uniform_from_hash(iv.salt, population.ids) < iv.treated_fraction
            for iv in config.pricing.interventions
        ]
        # World-level failure injection (Phase 5): a fraction of CONTROL units is charged the
        # treatment price anyway. Their exposure is logged truthfully (arm = control, the price
        # actually charged), so the analysis can detect it. Independent of the arm hash.
        self._priced_as_treated = [
            treated
            | (
                _uniform_from_hash(f"{iv.salt}:contamination", population.ids)
                < iv.contamination_fraction
            )
            for iv, treated in zip(config.pricing.interventions, self._treated, strict=True)
        ]

    # ------------------------------------------------------------------ helpers
    def _build_global_multipliers(self) -> F64:
        days, n_p = self.cfg.run.days, len(self.prod_ids)
        mult = np.ones((days, n_p))
        for ch in self.cfg.pricing.price_changes:
            if ch.day < days:
                mult[ch.day :, self.prod_ids.index(ch.product)] *= ch.multiplier
        return mult

    def _date(self, day: int) -> str:
        return (self.cfg.run.start_date + timedelta(days=day)).isoformat()

    def _prices(self, day: int) -> I64:
        mult = np.tile(self._global_mult[day], (self.pop.n, 1))
        for iv, treated in zip(
            self.cfg.pricing.interventions, self._priced_as_treated, strict=True
        ):
            if iv.start_day <= day < iv.end_day:
                mult[treated, self.prod_ids.index(iv.product)] *= iv.price_multiplier
        prices: I64 = np.rint(self.ref_price[None, :] * mult).astype(np.int64)
        return prices

    def _attempt(
        self, ctx: _DayCtx, i: int, inv: str, number: int, amount: int, prev_uid: str
    ) -> None:
        cid = self.pop.ids[i]
        flow = f"inv:{inv}"
        method = PAYMENT_METHODS[self.pop.method[i]]
        att_uid = f"patt:{inv}:{number}"
        ctx.add(
            Draft(
                ctx.ts0 + T_ATTEMPT,
                cid,
                att_uid,
                "payment.attempted",
                {
                    "invoice_id": inv,
                    "attempt_number": number,
                    "amount_minor": amount,
                    "currency": "GBP",
                    "payment_method": method,
                },
                flow,
                prev_uid,
            )
        )
        p_ok = (
            self.pop.pay_reliability[i]
            if number == 1
            else min(
                1.0,
                self.cfg.billing.retry_success_base
                + self.cfg.billing.retry_success_slope * self.pop.pay_reliability[i],
            )
        )
        if ctx.u_pay[i] < p_ok:
            ctx.add(
                Draft(
                    ctx.ts0 + T_RESULT,
                    cid,
                    f"pok:{inv}:{number}",
                    "payment.succeeded",
                    {
                        "invoice_id": inv,
                        "attempt_number": number,
                        "amount_minor": amount,
                        "currency": "GBP",
                    },
                    flow,
                    att_uid,
                )
            )
            return
        reason = FAILURE_REASONS[
            min(
                int(np.searchsorted(self._reason_cdf, ctx.u_reason[i])),
                len(FAILURE_REASONS) - 1,
            )
        ]
        final = number >= self.cfg.billing.max_attempts
        fail_uid = f"pfail:{inv}:{number}"
        ctx.add(
            Draft(
                ctx.ts0 + T_RESULT,
                cid,
                fail_uid,
                "payment.failed",
                {
                    "invoice_id": inv,
                    "attempt_number": number,
                    "amount_minor": amount,
                    "currency": "GBP",
                    "reason": reason,
                    "final": final,
                },
                flow,
                att_uid,
            )
        )
        ctx.burden[i] += 1.0
        if final:
            if ctx.state[i] == ACTIVE:
                ctx.state[i] = CHURNED
                ctx.add(
                    Draft(
                        ctx.ts0 + T_INV_CHURN,
                        cid,
                        f"churn:{cid}",
                        "churn.observed",
                        {
                            "reason": "involuntary_payment",
                            "tenure_days": int(self.pop.tenure_days[i])
                            + max(0, ctx.day - int(self.pop.created_day[i])),
                        },
                        f"life:{cid}",
                        fail_uid,
                    )
                )
        else:
            ctx.pending[ctx.day + self.cfg.billing.retry_offsets_days[number - 1]].append(
                (i, inv, number + 1, amount, fail_uid)
            )

    # --------------------------------------------------------------------- run
    def run(self) -> Iterator[Event]:  # noqa: C901, PLR0912, PLR0915 - complexity-debt
        cfg, pop, beh = self.cfg, self.pop, self.cfg.behaviour
        n, n_p = pop.n, len(self.prod_ids)
        state = np.full(n, UNBORN, dtype=np.int8)
        cur_tier = pop.tier.astype(np.int64).copy()
        last_price = np.zeros((n, n_p), dtype=np.int64)
        accrued = np.zeros(n, dtype=np.int64)
        burden = np.zeros(n)
        degr_lag = np.zeros(len(self.region_ids))
        invoice_counter = 0
        pending: dict[int, list[tuple[int, str, int, int, str]]] = defaultdict(list)
        ivs = cfg.pricing.interventions
        base_demand = pop.base_load[:, None] * pop.mix / self.weights[None, :]
        base_demand = np.where(
            self.compute_cols[None, :], base_demand * pop.compute_intensity[:, None], base_demand
        )

        for day in range(cfg.run.days):
            ts0 = self.epoch0 + day * 86400
            drafts: list[Draft] = []
            add = drafts.append
            dow = (self.start_dow + day) % 7
            prices = self._prices(day)
            ratio = prices / self.ref_price[None, :]
            price_idx = (pop.mix * ratio).sum(axis=1)

            # ---- 1. arrivals -------------------------------------------------
            arrivals = np.flatnonzero((state == UNBORN) & (pop.created_day == day))
            state[arrivals] = PROSPECT
            for i in arrivals.tolist():
                cid = pop.ids[i]
                add(
                    Draft(
                        ts0 + T_CREATED,
                        cid,
                        f"cust:{cid}",
                        "customer.created",
                        {
                            "region_id": self.region_ids[pop.region[i]],
                            "industry": cfg.industries[pop.industry[i]].id,
                            "tier": TIERS[pop.tier[i]],
                            "preferred_payment_method": PAYMENT_METHODS[pop.method[i]],
                            "is_existing": not bool(pop.is_new[i]),
                            "tenure_days": int(pop.tenure_days[i]),
                        },
                        f"life:{cid}",
                    )
                )

            # ---- price exposure (arrivals + changes + experiment starts) ---------
            exposable = ((state == PROSPECT) | (state == ACTIVE))[:, None] & (pop.mix > 0)
            changed = exposable & (prices != last_price)
            list_price = np.rint(self.ref_price * self._global_mult[day]).astype(np.int64)
            for iv in ivs:
                if iv.start_day == day:
                    changed[:, self.prod_ids.index(iv.product)] |= exposable[
                        :, self.prod_ids.index(iv.product)
                    ]
            for i, p in np.argwhere(changed).tolist():
                cid = pop.ids[i]
                exp_id: str | None = None
                arm: str | None = None
                for k, iv in enumerate(ivs):
                    if iv.start_day <= day < iv.end_day and self.prod_ids[p] == iv.product:
                        exp_id = iv.id
                        arm = "treatment" if self._treated[k][i] else "control"
                add(
                    Draft(
                        ts0 + T_PRICE,
                        cid,
                        f"price:{cid}:{day}:{p}",
                        "price.exposed",
                        {
                            "product": self.prod_ids[p],
                            "unit_price_micros": int(prices[i, p]),
                            "list_price_micros": int(list_price[p]),
                            "experiment_id": exp_id,
                            "arm": arm,
                        },
                        f"life:{cid}",
                    )
                )
            last_price[changed] = prices[changed]

            # ---- conversion / subscription start -----------------------------------
            u_conv = rng_for(self.seed, STREAM_CONVERSION, day).random(n)
            conv_p = np.clip(
                self.tier_conv[pop.tier]
                * price_idx ** (beh.conversion_price_beta * pop.elasticity),
                *beh.conversion_clip,
            )
            for i in arrivals.tolist():
                cid = pop.ids[i]
                if pop.is_new[i]:
                    converted = bool(u_conv[i] < conv_p[i])
                    add(
                        Draft(
                            ts0 + T_CONVERSION,
                            cid,
                            f"conv:{cid}",
                            "conversion.observed",
                            {
                                "converted": converted,
                                "price_index_milli": max(1, round(float(price_idx[i]) * 1000)),
                            },
                            f"life:{cid}",
                            f"cust:{cid}",
                        )
                    )
                    if not converted:
                        state[i] = LOST
                        continue
                    state[i] = CONVERTED
                add(
                    Draft(
                        ts0 + T_SUBSCRIPTION,
                        cid,
                        f"sub:{cid}",
                        "subscription.started",
                        {
                            "tier": TIERS[pop.tier[i]],
                            "products": [
                                self.prod_ids[p] for p in np.flatnonzero(pop.mix[i] > 0).tolist()
                            ],
                            "origin": "new" if pop.is_new[i] else "existing",
                            "base_fee_minor": self.tier_fee[pop.tier[i]],
                            "billing_period_days": cfg.billing.period_days,
                        },
                        f"life:{cid}",
                        f"cust:{cid}",
                    )
                )
                state[i] = ACTIVE

            # ---- 2. billing: retries then invoices ---------------------------------
            rng_b = rng_for(self.seed, STREAM_BILLING, day)
            u_pay = rng_b.random(n)
            u_reason = rng_b.random(n)
            ctx = _DayCtx(day, ts0, add, u_pay, u_reason, state, burden, pending)

            for i, inv, number, amount, prev_uid in ctx.pending.pop(day, []):
                self._attempt(ctx, i, inv, number, amount, prev_uid)

            period = cfg.billing.period_days
            due = np.flatnonzero(
                (state == ACTIVE)
                & (day > pop.bill_anchor)
                & ((day - pop.bill_anchor) % period == 0)
            )
            for i in due.tolist():
                cid = pop.ids[i]
                invoice_counter += 1
                inv = f"inv_{invoice_counter:09d}"
                amount = int((accrued[i] + 5000) // 10000) + self.tier_fee[cur_tier[i]]
                accrued[i] = 0
                inv_uid = f"inv:{inv}"
                add(
                    Draft(
                        ts0 + T_INVOICE,
                        cid,
                        inv_uid,
                        "invoice.created",
                        {
                            "invoice_id": inv,
                            "amount_minor": amount,
                            "currency": "GBP",
                            "period_start": self._date(day - period),
                            "period_end": self._date(day - 1),
                            "tier": TIERS[cur_tier[i]],
                        },
                        f"inv:{inv}",
                    )
                )
                self._attempt(ctx, i, inv, 1, amount, inv_uid)

            # ---- 3. demand, load, service, usage ------------------------------------
            rng_d = rng_for(self.seed, STREAM_DEMAND, day)
            active = state == ACTIVE
            season = 1.0 + pop.season_amp * np.cos(2 * np.pi * (dow - pop.season_peak) / 7.0)
            growth_f = (1.0 + pop.growth) ** day
            svc = np.exp(-beh.demand_service_beta * pop.service_sens * degr_lag[pop.region])
            spike = self.infra.spike_on(day)[pop.region]
            elas = ratio ** pop.elasticity[:, None]
            lam = base_demand * (season * growth_f * svc)[:, None] * elas * spike
            lam = np.where(active[:, None], lam, 0.0)
            shape = np.broadcast_to(pop.dispersion[:, None], lam.shape)
            requested = rng_d.poisson(rng_d.gamma(shape, lam / shape)).astype(np.int64)

            avail = self.infra.availability_on(day)
            load = np.bincount(
                pop.region,
                weights=(requested * self.weights[None, :] * avail[pop.region]).sum(axis=1),
                minlength=len(self.region_ids),
            )
            svc_day: RegionDay = self.infra.evaluate(
                day, load, rng_for(self.seed, STREAM_INFRA, day)
            )
            ok = svc_day.served_ratio[pop.region][:, None] * avail[pop.region]
            served = rng_d.binomial(requested, np.clip(ok, 0.0, 1.0)).astype(np.int64)
            accrued += (served * prices).sum(axis=1)

            for i, p in np.argwhere(served > 0).tolist():
                cid = pop.ids[i]
                add(
                    Draft(
                        ts0 + T_USAGE,
                        cid,
                        f"use:{cid}:{day}:{p}",
                        "usage.observed",
                        {
                            "product": self.prod_ids[p],
                            "region_id": self.region_ids[pop.region[i]],
                            "units": int(served[i, p]),
                            "throttled_units": int(requested[i, p] - served[i, p]),
                            "unit_price_micros": int(prices[i, p]),
                        },
                        f"use:{cid}:{day}",
                    )
                )
            if self.request_col is not None:
                col = served[:, self.request_col]
                idx = np.flatnonzero(col > 0)
                req = col[idx] * 1000
                errs = rng_d.binomial(req, svc_day.mean_err[pop.region[idx]])
                jitter = np.exp(rng_d.normal(0.0, 0.1, idx.size))
                p50 = svc_day.mean_p50[pop.region[idx]] * jitter
                p95 = svc_day.mean_p95[pop.region[idx]] * jitter
                for j, i in enumerate(idx.tolist()):
                    cid = pop.ids[i]
                    add(
                        Draft(
                            ts0 + T_REQUEST,
                            cid,
                            f"req:{cid}:{day}",
                            "request.completed",
                            {
                                "region_id": self.region_ids[pop.region[i]],
                                "request_count": int(req[j]),
                                "error_count": int(errs[j]),
                                "latency_p50_ms": round(float(p50[j]), 2),
                                "latency_p95_ms": round(float(p95[j]), 2),
                            },
                            f"use:{cid}:{day}",
                        )
                    )

            # ---- 4. voluntary churn and tier changes -----------------------------------
            rng_c = rng_for(self.seed, STREAM_CHURN, day)
            u_churn, u_tier, u_dir = rng_c.random(n), rng_c.random(n), rng_c.random(n)
            lin = (
                beh.churn_price_beta * pop.churn_sens * np.log(price_idx)
                + pop.service_sens
                * (
                    beh.churn_latency_beta * svc_day.degradation[pop.region]
                    + beh.churn_error_beta * svc_day.mean_err[pop.region]
                )
                + beh.churn_failure_beta * burden
            )
            hazard = np.minimum(self.tier_hazard[cur_tier] * np.exp(lin), beh.churn_hazard_cap)
            churn_now = (state == ACTIVE) & (u_churn < 1.0 - np.exp(-hazard))
            for i in np.flatnonzero(churn_now).tolist():
                cid = pop.ids[i]
                state[i] = CHURNED
                add(
                    Draft(
                        ts0 + T_CHURN,
                        cid,
                        f"churn:{cid}",
                        "churn.observed",
                        {
                            "reason": "voluntary",
                            "tenure_days": int(pop.tenure_days[i])
                            + max(0, day - int(pop.created_day[i])),
                        },
                        f"life:{cid}",
                    )
                )
            change = (state == ACTIVE) & (u_tier < beh.tier_change_daily)
            up_p = np.clip(0.5 + 40.0 * pop.growth, 0.1, 0.9)
            for i in np.flatnonzero(change).tolist():
                cid = pop.ids[i]
                go_up = bool(u_dir[i] < up_p[i])
                if cur_tier[i] == len(TIERS) - 1:
                    go_up = False
                elif cur_tier[i] == 0:
                    go_up = True
                new_tier = int(cur_tier[i]) + (1 if go_up else -1)
                add(
                    Draft(
                        ts0 + T_TIER_CHANGE,
                        cid,
                        f"chg:{cid}:{day}",
                        "subscription.changed",
                        {
                            "from_tier": TIERS[cur_tier[i]],
                            "to_tier": TIERS[new_tier],
                            "base_fee_minor": self.tier_fee[new_tier],
                        },
                        f"life:{cid}",
                    )
                )
                cur_tier[i] = new_tier
            burden *= beh.burden_decay
            degr_lag = svc_day.degradation

            # ---- 5. hourly regional service metrics -------------------------------------
            for r, rid in enumerate(self.region_ids):
                avail_ids = [self.prod_ids[p] for p in np.flatnonzero(avail[r]).tolist()]
                for h in range(HOURS):
                    add(
                        Draft(
                            ts0 + h * 3600,
                            rid,
                            f"svc:{rid}:{day}:{h}",
                            "service.metric_observed",
                            {
                                "region_id": rid,
                                "capacity_units": max(1, round(float(svc_day.capacity[r]))),
                                "utilization": round(float(svc_day.util_h[r, h]), 4),
                                "latency_p50_ms": round(float(svc_day.p50_h[r, h]), 2),
                                "latency_p95_ms": round(float(svc_day.p95_h[r, h]), 2),
                                "error_rate": round(float(svc_day.err_h[r, h]), 6),
                                "available_products": avail_ids,
                                "marginal_cost_micros": {
                                    pid: int(svc_day.cost_h[r, h, p])
                                    for p, pid in enumerate(self.prod_ids)
                                },
                            },
                            f"svc:{rid}:{day}",
                        )
                    )

            drafts.sort(key=lambda d: (d.ts, d.entity_id, d.uid))
            for d in drafts:
                yield self.factory.build(d)
