from weather import parse_open_meteo, WeatherError

sample = {
    "daily": {
        "time": ["2026-10-20", "2026-10-21", "2026-10-22"],
        "temperature_2m_min": [12.1, 3.5, None],
        "temperature_2m_max": [24.0, 18.2, 20.0],
        "precipitation_sum": [0.0, 12.4, None],
        "wind_speed_10m_max": [3.2, 9.8, 4.0],
    },
    "hourly": {"soil_moisture_3_to_9cm": [0.30, 0.32, None, 0.28] + [0.30] * 20},
}
days, soil = parse_open_meteo(sample)
assert len(days) == 2, "day with missing temperatures should be skipped"
assert days[1].temp_min == 3.5 and days[1].rain_mm == 12.4
assert soil is not None and 0.28 <= soil <= 0.32

days, soil = parse_open_meteo({"daily": sample["daily"]})
assert soil is None  # no hourly block -> no soil estimate

try:
    parse_open_meteo({})
    raise SystemExit("expected WeatherError")
except WeatherError:
    pass
print("weather parser tests passed")
