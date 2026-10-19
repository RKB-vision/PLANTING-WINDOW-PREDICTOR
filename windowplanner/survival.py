"""Survival-optimal planting window (the 'novel' core).

Instead of hard weather thresholds, we model transplant survival as a hazard process
over the establishment period (the first ~E days after planting, when roots are
re-establishing) and pick the planting day that maximises *risk-adjusted* survival.

Four ideas combined:
  1. Hazard model:  P(survive) = exp(-sum_k w_k * v * s_k)
       s_k = daily stress (cold / heat / drought / waterlog / wind), smooth 0..1
       w_k = exp(-k/tau)  -> early days after planting matter most (transplant shock)
       v   = 1 + gamma*(1 - PRI/100) -> weaker plants amplify every stress
  2. Joint optimisation of *when to plant* and *how much the plant grows meanwhile*:
       PRI(d) = min(100, PRI0 + growth_per_day * d). Waiting costs weather exposure risk
       later but buys vigour; the optimiser finds the trade-off.
  3. Forecast-uncertainty-aware: Monte Carlo over forecast error that grows with lead time,
       so the answer carries a confidence band (p10) and we plan against the downside.
  4. Soil-moisture bucket model: a sensor reading (or a prior if absent) is rolled forward
       with forecast rain and evapotranspiration, so irrigation/drought risk is predicted,
       not just observed.
"""
from dataclasses import dataclass, field
from typing import List, Optional
import numpy as np
try:
    from .decision import DayForecast      # when imported as windowplanner.survival
except ImportError:
    from decision import DayForecast       # when run from inside this folder


@dataclass
class SpeciesProfile:
    name: str = "generic"
    cold_crit: float = 5.0        # night low (C) where cold stress reaches 50%
    heat_crit: float = 33.0       # day high (C) where heat stress reaches 50%
    moist_wilt: float = 0.12      # volumetric soil moisture: wilting point
    moist_sat: float = 0.45       # above this -> waterlogging stress
    wind_crit: float = 12.0       # m/s
    growth_per_day: float = 3.0   # PRI points gained per day while waiting; UNCALIBRATED prior, fit per species


@dataclass
class Config:
    establish_days: int = 5
    tau: float = 3.0              # decay of "transplant shock" weighting (days)
    gamma: float = 1.2            # vulnerability amplification for low PRI
    n_sims: int = 400
    risk_lambda: float = 0.5      # penalise uncertainty: score = mean - lambda*std
    shock: float = 0.6            # intrinsic transplant-shock hazard for an unvigorous plant
    seed: int = 7


@dataclass
class DayOption:
    day: int
    date: str
    mean_survival: float
    p10_survival: float
    score: float
    pri_at_planting: float


@dataclass
class WindowResult:
    best_day: int
    best_date: str
    expected_survival: float
    p10_survival: float
    confidence: str
    main_risk: str
    options: List[DayOption] = field(default_factory=list)


def _sig(x, k):
    return 1.0 / (1.0 + np.exp(-x / k))


def optimise_window(pri: float, forecast: List[DayForecast], soil_moisture: Optional[float] = None,
                    sp: SpeciesProfile = SpeciesProfile(), cfg: Config = Config()) -> WindowResult:
    rng = np.random.default_rng(cfg.seed)
    T = len(forecast)
    E = min(cfg.establish_days, T)
    tmin0 = np.array([d.temp_min for d in forecast]); tmax0 = np.array([d.temp_max for d in forecast])
    rain0 = np.array([d.rain_mm for d in forecast]); wind0 = np.array([d.wind_ms for d in forecast])
    lead = np.arange(T)
    # forecast error grows with lead time; unknown soil moisture adds uncertainty
    sig_t = np.minimum(0.8 + 0.25 * lead, 3.5)   # ~NWP skill: ~1C at day 0, ~3C by day 8-10
    S = cfg.n_sims
    tmin = tmin0 + rng.normal(0, 1, (S, T)) * sig_t
    tmax = tmax0 + rng.normal(0, 1, (S, T)) * sig_t
    rain = rain0 * np.exp(rng.normal(0, 0.5 + 0.05 * lead, (S, T)))
    wind = np.maximum(0, wind0 + rng.normal(0, 1, (S, T)) * (1 + 0.1 * lead))
    m0 = soil_moisture if soil_moisture is not None else 0.30
    m0_s = np.clip(m0 + rng.normal(0, 0.02 if soil_moisture is not None else 0.08, S), 0.03, 0.55)

    weights = np.exp(-np.arange(E) / cfg.tau)
    options: List[DayOption] = []
    drivers = {}
    for d in range(0, T - E + 1):
        sl = slice(d, d + E)
        # roll bucket per-sim from day 0 using per-sim m0
        m = m0_s.copy()
        dr = np.zeros((S, E)); wl = np.zeros((S, E))
        for t in range(d + E):
            et = 0.006 + 0.0006 * np.maximum(tmax[:, t] - 15, 0)
            m = np.clip(m + 0.0033 * rain[:, t] - et, 0.02, 0.6)
            if t >= d:
                dr[:, t - d] = _sig(sp.moist_wilt + 0.06 - m, 0.03)
                wl[:, t - d] = _sig(m - sp.moist_sat, 0.03)
        cold = _sig(sp.cold_crit - tmin[:, sl], 1.5)
        heat = _sig(tmax[:, sl] - sp.heat_crit, 1.5)
        windy = _sig(wind[:, sl] - sp.wind_crit, 2.0)
        comp = {"cold": cold, "heat": heat, "drought": dr, "waterlog": wl, "wind": windy}
        total = sum(comp.values())                    # (S, E)
        pri_d = min(100.0, pri + sp.growth_per_day * d)
        v = 1.0 + cfg.gamma * (1 - pri_d / 100.0)
        # baseline hazard + intrinsic transplant shock (quadratic in lack of vigour)
        shock = cfg.shock * (1 - pri_d / 100.0) ** 2
        hazard = 0.03 + shock + (total * weights).sum(axis=1) * v * 0.5
        surv = np.exp(-hazard)
        mean, std, p10 = surv.mean(), surv.std(), np.percentile(surv, 10)
        options.append(DayOption(d, forecast[d].date, float(mean), float(p10),
                                 float(mean - cfg.risk_lambda * std), float(pri_d)))
        drivers[d] = {k: float((a * weights).sum(axis=1).mean()) for k, a in comp.items()}

    best = max(options, key=lambda o: o.score)
    spread = best.mean_survival - best.p10_survival
    confidence = "high" if spread < 0.06 else "medium" if spread < 0.15 else "low"
    top = max(drivers[best.day], key=drivers[best.day].get)
    main_risk = top if drivers[best.day][top] > 0.08 else "none significant"
    return WindowResult(best.day, best.date, best.mean_survival, best.p10_survival,
                        confidence, main_risk, options)
