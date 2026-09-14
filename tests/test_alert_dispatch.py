# Tests for the cooldown-gated DM dispatch (mon_utils.send_alert_dm) and the
# tolerant env parsing (mon_utils.float_env). The key property under test: a
# failed DM must neither crash the caller nor silently burn the full cooldown
# window — it retries after ALERT_RETRY_FLOOR_HOURS.

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import mon_utils
from mon_utils import ALERT_RETRY_FLOOR_HOURS, float_env, send_alert_dm

HOUR = 3600.0


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(tmp_path, monkeypatch):
    """Isolated alert state files plus a controllable clock for mon_utils."""
    fake = FakeClock()
    monkeypatch.setattr(mon_utils, "time", SimpleNamespace(time=lambda: fake.now))
    monkeypatch.setattr(mon_utils, "_ALERT_STATE_FILE", str(tmp_path / "alert_state.json"))
    monkeypatch.setattr(mon_utils, "_ALERT_LOCK_FILE", str(tmp_path / "alert_state.lock"))
    return fake


class TestSendAlertDm:
    def test_success_sends_and_burns_full_cooldown(self, clock):
        dm = MagicMock()
        dm.send_dm.return_value = True
        assert send_alert_dm(dm, "k", 4.0, "paul", "hi") is True
        assert send_alert_dm(dm, "k", 4.0, "paul", "hi") is False
        assert dm.send_dm.call_count == 1
        clock.advance(4.1 * HOUR)
        assert send_alert_dm(dm, "k", 4.0, "paul", "hi") is True
        assert dm.send_dm.call_count == 2

    def test_failed_send_retries_after_floor_not_full_window(self, clock):
        dm = MagicMock()
        dm.send_dm.return_value = False
        assert send_alert_dm(dm, "k", 24.0, "paul", "hi") is False
        # Still inside the retry floor: gated, no second attempt.
        clock.advance((ALERT_RETRY_FLOOR_HOURS - 0.1) * HOUR)
        assert send_alert_dm(dm, "k", 24.0, "paul", "hi") is False
        assert dm.send_dm.call_count == 1
        # Past the floor (but far inside the 24h window): retried.
        clock.advance(0.2 * HOUR)
        dm.send_dm.return_value = True
        assert send_alert_dm(dm, "k", 24.0, "paul", "hi") is True
        assert dm.send_dm.call_count == 2

    def test_send_exception_is_contained_and_rescheduled(self, clock):
        dm = MagicMock()
        dm.send_dm.side_effect = RuntimeError("transport down")
        # Must not raise — a DM failure must never kill the monitoring loop.
        assert send_alert_dm(dm, "k", 24.0, "paul", "hi") is False
        clock.advance((ALERT_RETRY_FLOOR_HOURS + 0.1) * HOUR)
        dm.send_dm.side_effect = None
        dm.send_dm.return_value = True
        assert send_alert_dm(dm, "k", 24.0, "paul", "hi") is True

    def test_none_client_neither_sends_nor_burns_cooldown(self, clock):
        assert send_alert_dm(None, "k", 4.0, "paul", "hi") is False
        dm = MagicMock()
        dm.send_dm.return_value = True
        assert send_alert_dm(dm, "k", 4.0, "paul", "hi") is True

    def test_keys_are_independent(self, clock):
        dm = MagicMock()
        dm.send_dm.return_value = True
        assert send_alert_dm(dm, "shutdown_alert", 4.0, "paul", "a") is True
        assert send_alert_dm(dm, "gpu_idle_nag", 24.0, "paul", "b") is True

    def test_cooldown_shorter_than_retry_floor_still_gates_to_floor(self, clock):
        # Pathological config (e.g. ALERT_COOLDOWN_HOURS=0.1 for testing): the
        # backdated timestamp lands in the future, but the algebra cancels out —
        # the retry is gated to exactly the floor, not locked out indefinitely.
        dm = MagicMock()
        dm.send_dm.return_value = False
        assert send_alert_dm(dm, "k", 0.1, "paul", "hi") is False
        clock.advance((ALERT_RETRY_FLOOR_HOURS - 0.1) * HOUR)
        assert send_alert_dm(dm, "k", 0.1, "paul", "hi") is False
        assert dm.send_dm.call_count == 1
        clock.advance(0.2 * HOUR)
        dm.send_dm.return_value = True
        assert send_alert_dm(dm, "k", 0.1, "paul", "hi") is True


class TestFloatEnv:
    def test_unset_uses_default(self, monkeypatch):
        monkeypatch.delenv("X_GPUMON_TEST", raising=False)
        assert float_env("X_GPUMON_TEST", 72.0) == 72.0

    def test_empty_string_uses_default(self, monkeypatch):
        # os.getenv's default does NOT apply here — this is the crash case.
        monkeypatch.setenv("X_GPUMON_TEST", "")
        assert float_env("X_GPUMON_TEST", 72.0) == 72.0

    def test_malformed_uses_default(self, monkeypatch):
        monkeypatch.setenv("X_GPUMON_TEST", "72h")
        assert float_env("X_GPUMON_TEST", 72.0) == 72.0

    def test_valid_value_parses(self, monkeypatch):
        monkeypatch.setenv("X_GPUMON_TEST", "12.5")
        assert float_env("X_GPUMON_TEST", 72.0) == 12.5
