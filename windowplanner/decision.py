"""Core decision matrix: merges Plant Readiness Index (PRI) with a weather forecast.
Pure Python, no external dependencies, so it is easy to unit test."""
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class Action(str, Enum):
    GREEN_LIGHT = "green_light"        # PRI high, weather good -> plant now
    HOLD = "hold_position"             # PRI high, weather bad  -> wait X days
    KEEP_INDOORS = "keep_indoors"      # PRI low,  weather good -> plant not ready
    WARNING = "warning"                # PRI low,  weather turning bad


@dataclass
class DayForecast:
    date: str
    temp_min: float      # deg C (night low)
    temp_max: float      # deg C
    rain_mm: float = 0.0
    wind_ms: float = 0.0


@dataclass
class Thresholds:
    """Per-crop safety guardrails. Tune these (or load per species from the DB)."""
    pri_ready: int = 80
    min_temp: float = 5.0        # night low below this = cold risk
    max_temp: float = 35.0       # heat spike
    max_rain_mm: float = 25.0    # storm / heavy rain day
    max_wind_ms: float = 14.0
    window_days: int = 2         # consecutive safe days needed to call it a "window"
    hazard_horizon: int = 7      # days ahead we scan for deteriorating weather


@dataclass
class Recommendation:
    action: Action
    headline: str
    detail: str
    days_until_window: Optional[int] = None
    hazards: List[str] = field(default_factory=list)


def day_hazards(d: DayForecast, t: Thresholds) -> List[str]:
    out = []
    if d.temp_min < t.min_temp:
        out.append(f"cold ({d.temp_min:.0f}°C low on {d.date})")
    if d.temp_max > t.max_temp:
        out.append(f"heat ({d.temp_max:.0f}°C high on {d.date})")
    if d.rain_mm > t.max_rain_mm:
        out.append(f"heavy rain ({d.rain_mm:.0f} mm on {d.date})")
    if d.wind_ms > t.max_wind_ms:
        out.append(f"strong wind ({d.wind_ms:.0f} m/s on {d.date})")
    return out


def find_window(forecast: List[DayForecast], t: Thresholds) -> Optional[int]:
    """Index of the first day that starts `window_days` consecutive hazard-free days."""
    n = t.window_days
    for i in range(len(forecast) - n + 1):
        if all(not day_hazards(d, t) for d in forecast[i:i + n]):
            return i
    return None


def recommend(pri: float, forecast: List[DayForecast], t: Thresholds = Thresholds()) -> Recommendation:
    ready = pri >= t.pri_ready
    window = find_window(forecast, t)
    horizon = forecast[: t.hazard_horizon]
    upcoming_hazards = [h for d in horizon for h in day_hazards(d, t)]

    if ready and window == 0:
        return Recommendation(
            Action.GREEN_LIGHT, "Green light: plant now",
            "Your plant is ready and the next few days look safe.", 0)

    if ready:
        if window is None:
            return Recommendation(
                Action.HOLD, "Hold position",
                "Plant is ready, but no safe window in the forecast yet. Check back tomorrow.",
                None, upcoming_hazards[:3])
        return Recommendation(
            Action.HOLD, f"Hold position: window opens in {window} day{'s' if window != 1 else ''}",
            "Plant is ready, but the outdoor window is closed for now.",
            window, upcoming_hazards[:3])

    # Not ready
    if upcoming_hazards:
        return Recommendation(
            Action.WARNING, "Warning: plant underdeveloped, weather worsening",
            "Not ready to transplant, but conditions are deteriorating. Consider feeding, "
            "hardening off gradually, or sheltering it (cloche, cold frame).",
            window, upcoming_hazards[:3])

    return Recommendation(
        Action.KEEP_INDOORS, "Keep indoors",
        "Weather is great, but the plant needs more growth (leaf area / stem / roots) first.",
        window)
