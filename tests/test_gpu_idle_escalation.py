# Tests for the GPU-idle escalation helpers (mon_utils.update_gpu_idle_since /
# gpu_idle_hours). Pure functions — no AWS, NVML, or network. The emphasis is on
# the negative property: anything ambiguous (a failed GPU query, activity above
# threshold) must reset the streak, never extend it.

import sys
import types
from unittest.mock import MagicMock

# mon_utils imports boto3/psutil at module level; neither is needed by the
# functions under test, so stub them out rather than requiring the runtime deps.
sys.modules.setdefault("boto3", MagicMock())
sys.modules.setdefault("psutil", MagicMock())

from mon_utils import gpu_idle_hours, update_gpu_idle_since  # noqa: E402

T0 = 1_000_000.0
HOUR = 3600.0


class TestUpdateGpuIdleSince:
    def test_starts_streak_when_gpu_first_reads_idle(self):
        assert update_gpu_idle_since(None, 0.0, 10, False, T0) == T0

    def test_preserves_original_start_while_idle_continues(self):
        assert update_gpu_idle_since(T0, 0.0, 10, False, T0 + 5 * HOUR) == T0

    def test_resets_on_gpu_activity(self):
        assert update_gpu_idle_since(T0, 55.0, 10, False, T0 + HOUR) is None

    def test_resets_on_failed_gpu_query(self):
        # Unknown must never count toward an escalation — same fail-safe rule
        # as the pilot light.
        assert update_gpu_idle_since(T0, 0.0, 10, True, T0 + HOUR) is None

    def test_no_streak_starts_while_gpu_active(self):
        assert update_gpu_idle_since(None, 90.0, 10, False, T0) is None

    def test_threshold_is_inclusive_after_rounding(self):
        # Mirrors the pilot-light comparison: round(avg) <= threshold.
        assert update_gpu_idle_since(None, 10.4, 10, False, T0) == T0
        assert update_gpu_idle_since(None, 10.6, 10, False, T0) is None

    def test_streak_restarts_fresh_after_activity(self):
        after_activity = update_gpu_idle_since(T0, 55.0, 10, False, T0 + HOUR)
        assert after_activity is None
        restart = update_gpu_idle_since(after_activity, 0.0, 10, False, T0 + 2 * HOUR)
        assert restart == T0 + 2 * HOUR


class TestGpuIdleHours:
    def test_no_streak_is_zero(self):
        assert gpu_idle_hours(None, T0) == 0.0

    def test_hours_since_streak_start(self):
        assert gpu_idle_hours(T0, T0 + 72 * HOUR) == 72.0

    def test_clock_skew_never_goes_negative(self):
        assert gpu_idle_hours(T0, T0 - HOUR) == 0.0
