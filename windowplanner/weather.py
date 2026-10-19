"""Weather via Open-Meteo: free, no API key, no card, 16-day forecast, plus modelled soil moisture.

fetch_forecast(lat, lon) -> (list[DayForecast], soil_moisture or None)
The soil value is Open-Meteo's *modelled* 3-9 cm moisture (m3/m3), a fallback for when the
user has no physical sensor. A real sensor reading should always take priority.
"""
import json
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

try:
    from .decision import DayForecast
except ImportError:
    from decision import DayForecast

API_URL = "https://api.open-meteo.com/v1/forecast"


class WeatherError(Exception):
    pass


def parse_open_meteo(payload: dict) -> Tuple[List[DayForecast], Optional[float]]:
    """Pure function (no network) so it can be unit-tested."""
    try:
        daily = payload["daily"]
        days = [
            DayForecast(
                date=daily["time"][i],
                temp_min=float(daily["temperature_2m_min"][i]),
                temp_max=float(daily["temperature_2m_max"][i]),
                rain_mm=float(daily["precipitation_sum"][i] or 0.0),
                wind_ms=float(daily["wind_speed_10m_max"][i] or 0.0),
            )
            for i in range(len(daily["time"]))
            if daily["temperature_2m_min"][i] is not None and daily["temperature_2m_max"][i] is not None
        ]
    except (KeyError, TypeError, ValueError) as e:
        raise WeatherError(f"Unexpected weather response: {e}")
    if not days:
        raise WeatherError("Weather response contained no forecast days")

    soil = None
    try:
        vals = [v for v in payload["hourly"]["soil_moisture_3_to_9cm"][:24] if v is not None]
        if vals:
            soil = sum(vals) / len(vals)
    except (KeyError, TypeError):
        pass
    return days, soil


def fetch_forecast(lat: float, lon: float, days: int = 16, timeout: int = 10):
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_min,temperature_2m_max,precipitation_sum,wind_speed_10m_max",
        "hourly": "soil_moisture_3_to_9cm",
        "wind_speed_unit": "ms",
        "forecast_days": days,
        "timezone": "auto",
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise WeatherError(f"Could not reach weather service: {e}")
    return parse_open_meteo(payload)
