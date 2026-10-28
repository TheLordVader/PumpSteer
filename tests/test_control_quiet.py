"""Passive PI copies can avoid logs without altering controller behaviour."""

import copy
import logging
from datetime import datetime, timedelta, timezone

from custom_components.pumpsteer.control import PIController


def test_pi_logging_enabled_by_default_and_suppressed_for_explicit_quiet_copy(caplog):
    now = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    original = PIController()
    with caplog.at_level(logging.DEBUG, logger="custom_components.pumpsteer.control"):
        original.reset(now)
        original.compute(21, 20.5, 5, 1, now)
    assert any("PIController reset" in record.message for record in caplog.records)
    assert any(record.message.startswith("PI: ") for record in caplog.records)

    quiet = copy.deepcopy(original)
    quiet.emit_log = False
    old_state = (original._integral, original._last_time, original._last_error)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="custom_components.pumpsteer.control"):
        quiet.reset(now + timedelta(minutes=1))
        quiet.compute(21, 20.5, 5, 1, now + timedelta(minutes=1))
    assert not caplog.records
    assert (original._integral, original._last_time, original._last_error) == old_state


def test_quiet_pi_and_live_pi_have_identical_numeric_state_across_modes():
    start = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    live = PIController()
    quiet = PIController(emit_log=False)
    for minute, indoor, braking, behaviour in (
        (0, 20.5, False, "freeze"),
        (1, 20.0, True, "freeze"),
        (4, 20.2, True, "decay"),
        (5, 21.2, False, "freeze"),
    ):
        args = (21, indoor, 5, 1, start + timedelta(minutes=minute))
        kwargs = {"braking_active": braking, "brake_behavior": behaviour, "kd": 0.1}
        assert live.compute(*args, **kwargs) == quiet.compute(*args, **kwargs)
        assert (live._integral, live._last_time, live._last_error) == (
            quiet._integral,
            quiet._last_time,
            quiet._last_error,
        )
    live.reset(start + timedelta(minutes=6))
    quiet.reset(start + timedelta(minutes=6))
    assert (live._integral, live._last_time, live._last_error) == (
        quiet._integral,
        quiet._last_time,
        quiet._last_error,
    )
