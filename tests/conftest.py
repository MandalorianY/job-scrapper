from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

os.environ.setdefault("SCRAPPER_DISABLE_LOCAL_CONFIG", "1")

settings.register_profile("local", max_examples=50)
settings.register_profile("ci", max_examples=500)
settings.register_profile("fast", max_examples=10, suppress_health_check=[HealthCheck.too_slow])
settings.load_profile(os.getenv("PROFILE", "local"))
