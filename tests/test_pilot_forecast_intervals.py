"""Real price intervals keep their timestamps and explicit units."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from custom_components.pumpsteer.pilot_forecast import price_window

T = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)


def make_state(rows, *, unit="SEK/kWh", reported=T):
    return SimpleNamespace(
        last_reported=reported,
        attributes={"unit_of_measurement": unit, "raw_today": rows},
    )


def row(start, minutes, value):
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=minutes)).isoformat(),
        "value": value,
    }


def test_quarter_hour_windows_survive_dst_with_actual_offsets():
    from zoneinfo import ZoneInfo

    local = ZoneInfo("Europe/Stockholm")
    rows = [
        row((T + timedelta(minutes=15 * i)).astimezone(local), 15, i) for i in range(8)
    ]
    # Construct endpoints in UTC before rendering to avoid wall-time DST ambiguity.
    for i, item in enumerate(rows):
        item["end"] = (
            (T + timedelta(minutes=15 * (i + 1))).astimezone(local).isoformat()
        )
    result = price_window([make_state(rows)], T, timedelta(hours=2))
    assert result is not None and len(result) == 8
    assert [p[2] for p in result] == list(range(8))
    assert result[-1][1] == T + timedelta(hours=2)


def test_unknown_unit_gap_future_or_naive_timestamp_abstains():
    rows = [row(T + timedelta(minutes=15 * i), 15, i) for i in range(8)]
    assert (
        price_window([make_state(rows, unit="unknown")], T, timedelta(hours=2)) is None
    )
    assert (
        price_window([make_state(rows[:3] + rows[4:])], T, timedelta(hours=2)) is None
    )
    assert (
        price_window(
            [make_state(rows, reported=T + timedelta(seconds=1))], T, timedelta(hours=2)
        )
        is None
    )
    rows[0]["start"] = "2026-10-25T00:00:00"
    assert price_window([make_state(rows)], T, timedelta(hours=2)) is None


def test_malformed_source_metadata_abstains():
    bad = SimpleNamespace(last_reported=T, attributes="invalid")
    assert price_window([bad], T, timedelta(hours=2)) is None


def test_missing_optional_tomorrow_does_not_hide_complete_current_source():
    rows = [row(T + timedelta(minutes=15 * i), 15, i) for i in range(8)]
    missing = SimpleNamespace(state="unavailable", attributes={}, last_reported=None)
    assert len(price_window([make_state(rows), missing], T, timedelta(hours=2))) == 8


def test_unavailable_source_cannot_reuse_old_raw_attributes():
    rows = [row(T + timedelta(minutes=15 * i), 15, i) for i in range(8)]
    unavailable = make_state(rows)
    unavailable.state = "unavailable"
    assert price_window([unavailable], T, timedelta(hours=2)) is None


def test_stale_optional_source_does_not_hide_complete_live_source():
    rows = [row(T + timedelta(minutes=15 * i), 15, i) for i in range(8)]
    stale = make_state(rows, reported=T - timedelta(days=2))
    assert len(price_window([make_state(rows), stale], T, timedelta(hours=2))) == 8
    restored = make_state(rows)
    restored.attributes["restored"] = True
    assert len(price_window([make_state(rows), restored], T, timedelta(hours=2))) == 8


def test_parallel_undated_price_list_does_not_mask_timestamped_intervals():
    rows = [row(T + timedelta(minutes=15 * i), 15, i) for i in range(8)]
    state = make_state(rows)
    state.attributes["prices"] = [1.0] * 8
    assert len(price_window([state], T, timedelta(hours=2))) == 8


def test_incomplete_hourly_view_does_not_hide_complete_raw_quarters():
    rows = [row(T + timedelta(minutes=15 * i), 15, i) for i in range(8)]
    state = make_state(rows[:4])
    state.attributes["raw_tomorrow"] = rows[4:]
    state.attributes["prices"] = [{"start": T.isoformat(), "value": 2.0}]
    result = price_window([state], T, timedelta(hours=2))
    assert result is not None and [item[2] for item in result] == list(range(8))


def test_incomplete_raw_cannot_be_patched_by_parallel_hourly_view():
    rows = [row(T + timedelta(minutes=15 * i), 15, i) for i in range(8)]
    state = make_state(rows[:3] + rows[4:])
    state.attributes["prices"] = [row(T + timedelta(hours=i), 60, i) for i in range(2)]
    assert price_window([state], T, timedelta(hours=2)) is None
