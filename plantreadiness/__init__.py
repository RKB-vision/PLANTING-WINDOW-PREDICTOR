"""Species-aware plant-readiness countdown prototype.

This package replaces the former rule/Grok-labelled classification workflow
with:

* a SQLite database of plants, observations, readiness events and
  14-day post-transplant feedback,
* an explicitly separated synthetic demonstration mode,
* a real-data import + quality-check pipeline for the Super Orange CSV,
* supervised-learning countdown models trained only on recorded readiness
  events, with plant-grouped validation,
* species-aware model selection with an explicit fallback order.

Nothing in this package constitutes validated botanical science.  Any result
produced from synthetic data must be labelled ``SYNTHETIC DEMO``.
"""

__version__ = "0.1.0"
