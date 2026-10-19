from decision import DayForecast
from survival import optimise_window

def day(i, lo=14, hi=26, rain=3.5, wind=3): return DayForecast(f"d{i}", lo, hi, rain, wind)

# 1) Calm weather, vigorous plant -> plant now (or very soon), high survival
calm = [day(i) for i in range(12)]
r = optimise_window(90, calm, soil_moisture=0.30)
print("calm/strong:", r.best_day, round(r.expected_survival,2), r.confidence, r.main_risk)
assert r.best_day <= 1 and r.expected_survival > 0.8

# 2) Cold snap on days 0-2 -> optimiser should push the window later
cold = [day(0, lo=-1), day(1, lo=0), day(2, lo=1)] + [day(i) for i in range(3, 12)]
r = optimise_window(90, cold, soil_moisture=0.30)
print("cold snap  :", r.best_day, round(r.expected_survival,2), r.main_risk)
assert r.best_day >= 2

# 3) Weak plant, good weather -> waiting to build vigour beats planting immediately
r = optimise_window(45, calm, soil_moisture=0.30)
now = r.options[0].mean_survival
print("weak plant :", r.best_day, round(r.expected_survival,2), "vs day0", round(now,2))
assert r.best_day > 0 and r.expected_survival > now

# 4) Dry soil + hot, no rain -> drought should be the flagged risk
dry = [day(i, hi=34, rain=0) for i in range(12)]
r = optimise_window(85, dry, soil_moisture=0.10)
print("dry & hot  :", r.best_day, round(r.expected_survival,2), r.main_risk)
assert r.main_risk in ("drought", "heat")
print("all survival-model tests passed")
