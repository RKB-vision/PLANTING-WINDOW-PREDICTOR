from decision import *

def day(i, lo=12, hi=24, rain=0): return DayForecast(f"d{i}", lo, hi, rain)
good = [day(i) for i in range(8)]
cold_first = [day(0, lo=1), day(1, lo=2), day(2), day(3), day(4), day(5), day(6), day(7)]
cold_later = good[:4] + [day(4, lo=0)] + good[5:]

assert recommend(90, good).action == Action.GREEN_LIGHT
r = recommend(90, cold_first); assert r.action == Action.HOLD and r.days_until_window == 2, r
assert recommend(60, good).action == Action.KEEP_INDOORS
assert recommend(60, cold_later).action == Action.WARNING
print("all decision-matrix tests passed")
