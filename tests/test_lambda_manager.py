"""State-machine tests for lambda_manager.

Regression context: on 2026-07-06 (disk full) and 2026-07-31 (OOM livelock)
an instance whose SSM agent returned rc=1 with empty output was misdiagnosed
as "gpumon down", auto-fix mis-fired, and the box rotted in the then-terminal
NOT_FIXED state while the container was actually healthy.  These tests pin
the tri-state check semantics, the SUSPECT confirmation step, the
UNREACHABLE parking state, and NOT_FIXED self-recovery.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lambda_manager as lm


INSTANCE = "i-0123456789abcdef0"


class FakeEC2:
    """Records update_tag writes."""

    def __init__(self):
        self.tag_writes = []

    def create_tags(self, Resources, Tags):
        self.tag_writes.append(Tags[0]["Value"])

    @property
    def last_tag(self):
        return self.tag_writes[-1] if self.tag_writes else None


class QueuedSSM:
    """Stub for run_ssm_command returning queued invocation results in order."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, ssm, instance_id, commands, **kwargs):
        self.calls += 1
        return self.results.pop(0)


def ok(output):
    return {"Status": "Success", "StandardOutputContent": output + "\n"}


# rc=1 + empty output: the exact signature of the two production incidents —
# the shell could not run at all (disk full / OOM), so nothing was echoed.
BROKEN_INVOCATION = {"Status": "Failed", "StandardOutputContent": "", "ResponseCode": 1}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(lm.time, "sleep", lambda *_: None)


def queue(monkeypatch, results):
    stub = QueuedSSM(results)
    monkeypatch.setattr(lm, "run_ssm_command", stub)
    return stub


# ---------------------------------------------------------------------------
# Tri-state probe parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("inv,expected", [
    (None, lm.CHECK_UNKNOWN),                 # send/poll failed entirely
    (BROKEN_INVOCATION, lm.CHECK_UNKNOWN),    # ran but produced nothing (rc=1)
    (ok("active"), lm.CHECK_ACTIVE),
    (ok("inactive"), lm.CHECK_INACTIVE),
    (ok("garbage"), lm.CHECK_UNKNOWN),
])
def test_check_gpumon_running_tristate(monkeypatch, inv, expected):
    queue(monkeypatch, [inv])
    assert lm.check_gpumon_running(object(), INSTANCE) == expected


@pytest.mark.parametrize("inv,expected", [
    (None, lm.CHECK_UNKNOWN),
    (BROKEN_INVOCATION, lm.CHECK_UNKNOWN),
    (ok("yes"), "yes"),
    (ok("no"), "no"),
])
def test_docker_deployment_state_tristate(monkeypatch, inv, expected):
    queue(monkeypatch, [inv])
    assert lm._docker_deployment_state(object(), INSTANCE) == expected


# ---------------------------------------------------------------------------
# handle_check transitions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("current_tag,probe,expected_tag", [
    ("ACTIVE",      ok("active"),      "ACTIVE"),
    ("ACTIVE",      ok("inactive"),    "SUSPECT"),      # first strike, not FAILED
    ("SUSPECT",     ok("inactive"),    "FAILED"),       # confirmed on 2nd sweep
    ("SUSPECT",     ok("active"),      "ACTIVE"),       # blip cleared
    ("ACTIVE",      BROKEN_INVOCATION, "UNREACHABLE"),  # incident signature
    ("ACTIVE",      None,              "UNREACHABLE"),
    ("UNREACHABLE", ok("active"),      "ACTIVE"),       # box came back healthy
    ("UNREACHABLE", ok("inactive"),    "SUSPECT"),      # back, gpumon really down
])
def test_handle_check_transitions(monkeypatch, current_tag, probe, expected_tag):
    ec2 = FakeEC2()
    queue(monkeypatch, [probe])
    lm.handle_check(ec2, object(), INSTANCE, current_tag)
    assert ec2.last_tag == expected_tag


def test_handle_check_unreachable_stays_without_rewrite(monkeypatch):
    """Still-unreachable boxes don't churn tag writes every sweep."""
    ec2 = FakeEC2()
    queue(monkeypatch, [BROKEN_INVOCATION])
    lm.handle_check(ec2, object(), INSTANCE, "UNREACHABLE")
    assert ec2.tag_writes == []


# ---------------------------------------------------------------------------
# NOT_FIXED self-recovery
# ---------------------------------------------------------------------------

def test_not_fixed_self_recovers_when_active(monkeypatch):
    ec2 = FakeEC2()
    queue(monkeypatch, [ok("active")])
    lm.handle_not_fixed(ec2, object(), INSTANCE)
    assert ec2.last_tag == "ACTIVE"


@pytest.mark.parametrize("probe", [ok("inactive"), BROKEN_INVOCATION, None])
def test_not_fixed_stays_put_otherwise(monkeypatch, probe):
    ec2 = FakeEC2()
    queue(monkeypatch, [probe])
    lm.handle_not_fixed(ec2, object(), INSTANCE)
    assert ec2.tag_writes == []


# ---------------------------------------------------------------------------
# handle_fix: unreachable is not NOT_FIXED
# ---------------------------------------------------------------------------

def test_fix_guard_unknown_parks_unreachable_without_fixing(monkeypatch):
    ec2 = FakeEC2()
    stub = queue(monkeypatch, [BROKEN_INVOCATION])
    lm.handle_fix(ec2, object(), INSTANCE)
    assert ec2.last_tag == "UNREACHABLE"
    assert stub.calls == 1  # guard only — no fix commands were attempted


def test_fix_guard_no_deployment_marks_not_fixed(monkeypatch):
    ec2 = FakeEC2()
    queue(monkeypatch, [ok("no")])
    lm.handle_fix(ec2, object(), INSTANCE)
    assert ec2.last_tag == "NOT_FIXED"


def test_fix_step1_ssm_dropout_parks_unreachable(monkeypatch):
    ec2 = FakeEC2()
    queue(monkeypatch, [ok("yes"), None])  # deployment yes, step 1 unanswerable
    lm.handle_fix(ec2, object(), INSTANCE)
    assert ec2.last_tag == "UNREACHABLE"


def test_fix_step1_success_marks_active(monkeypatch):
    ec2 = FakeEC2()
    # deployment yes → step 1 ran ok → dockerized probe sees the container
    queue(monkeypatch, [ok("yes"), ok(""), ok("active")])
    lm.handle_fix(ec2, object(), INSTANCE)
    assert ec2.last_tag == "ACTIVE"


def test_fix_both_steps_genuinely_failed_marks_not_fixed(monkeypatch):
    ec2 = FakeEC2()
    # deployment yes → step 1 ran but container still down → step 2 ran and failed
    queue(monkeypatch, [ok("yes"), ok(""), ok("inactive"), BROKEN_INVOCATION])
    lm.handle_fix(ec2, object(), INSTANCE)
    assert ec2.last_tag == "NOT_FIXED"
