"""Validate simulator causality and scoring, never require a favorable TI result."""

from datetime import timedelta

import pytest


def _offline_policy_inputs(minute=0):
    import thermal_complete_validation as validation

    at = validation.START + timedelta(minutes=minute)
    context = dict(
        mode="normal",
        brake_factor=0.0,
        target_temperature=21.0,
        aggressiveness=3,
        preheat_enabled=True,
    )
    latest = {
        entity: dict(
            state=str(value),
            last_reported=at.isoformat(),
            attributes={"unit_of_measurement": "°C"},
        )
        for entity, value in zip(validation.IDS, (21.0, 5.0, 5.0))
    }
    rec = dict(
        eligible=True,
        reason="bounded_conditional_shadow_advice",
        action="bounded_preheat",
        operating_mode="normal",
        issued_at=validation.START.isoformat(),
        model_version=1,
        delta_fake_c=-1.0,
        valid_for_minutes=30,
    )
    result = dict(
        status="conditional",
        response={"confirmed": True},
        model_version=1,
        recommendation=rec,
    )
    return dict(
        cached=None,
        result=result,
        now=at,
        previous_context=context,
        next_context=dict(context),
        latest=latest,
        forecast=validation.published_forecast(validation.CASES[0], 0, 5.0),
    )


def test_offline_cache_reuses_original_advice_only_with_current_qualified_context():
    from thermal_complete_validation import select_offline_advice

    first = select_offline_advice(**_offline_policy_inputs())
    assert first is not None
    args = _offline_policy_inputs(29)
    args["cached"] = first
    args["result"]["recommendation"] = dict(
        eligible=False, reason="awaiting_current_forecast_issuance"
    )
    reused = select_offline_advice(**args)
    assert reused is first
    assert reused["recommendation"]["issued_at"] == first["recommendation"]["issued_at"]


@pytest.mark.parametrize(
    "change",
    [
        "expiry",
        "model",
        "unsafe",
        "unconfirmed",
        "status",
        "mode",
        "factor",
        "stale_sensor",
        "restored",
        "weather",
        "prices",
    ],
)
def test_offline_cache_clears_on_expiry_revision_or_lost_qualification(change):
    from dataclasses import replace
    from thermal_complete_validation import select_offline_advice

    first = select_offline_advice(**_offline_policy_inputs())
    args = _offline_policy_inputs(31 if change == "expiry" else 1)
    args["cached"] = first
    args["result"]["recommendation"] = dict(
        eligible=False, reason="awaiting_current_forecast_issuance"
    )
    if change == "model":
        args["result"]["model_version"] = 2
    if change == "unsafe":
        args["result"]["recommendation"]["reason"] = "unsafe_or_unknown_context"
    if change == "unconfirmed":
        args["result"]["response"]["confirmed"] = False
    if change == "status":
        args["result"]["status"] = "insufficient_data"
    if change == "mode":
        args["next_context"]["mode"] = "braking"
    if change == "factor":
        args["next_context"]["brake_factor"] = 0.1
    if change == "stale_sensor":
        next(iter(args["latest"].values()))["last_reported"] = (
            args["now"] - timedelta(minutes=16)
        ).isoformat()
    if change == "restored":
        next(iter(args["latest"].values()))["attributes"]["restored"] = True
    if change == "weather":
        args["forecast"] = replace(args["forecast"], weather=())
    if change == "prices":
        args["forecast"] = replace(args["forecast"], prices=())
    assert select_offline_advice(**args) is None


def test_pure_offline_application_rejects_changed_model_and_31_minute_advice():
    from thermal_complete_validation import START, simulate_assistance

    rec = _offline_policy_inputs()["result"]["recommendation"]
    args = dict(
        now=START,
        indoor=21.0,
        comfort_floor=19.5,
        ceiling=21.7,
        mode="normal",
        qualified=True,
        model_version=1,
    )
    assert simulate_assistance(5.0, rec, **args) == 4.0
    assert simulate_assistance(5.0, rec, **{**args, "model_version": 2}) == 5.0
    assert (
        simulate_assistance(5.0, rec, **{**args, "now": START + timedelta(minutes=31)})
        == 5.0
    )


@pytest.mark.parametrize(
    ("confirmed", "suspected", "truth", "expected"),
    [
        (False, False, False, "abstained_no_heat"),
        (False, False, True, "abstained_heat"),
        (False, True, False, "abstained_no_heat"),
        (False, True, True, "abstained_heat"),
        (True, False, False, "tn"),
        (True, False, True, "fn"),
        (True, True, False, "fp"),
        (True, True, True, "tp"),
    ],
)
def test_heat_scoring_keeps_no_reference_abstentions_out_of_confusion(
    confirmed, suspected, truth, expected
):
    from thermal_complete_validation import classify_heat_window

    assert classify_heat_window(confirmed, suspected, truth) == expected


def test_suite_cli_defaults_to_original_and_accepts_explicit_blind(tmp_path):
    from thermal_complete_validation import parse_args

    assert parse_args(["--output", str(tmp_path)]).suite == "original"
    assert parse_args(["--output", str(tmp_path), "--suite", "blind"]).suite == "blind"
    with pytest.raises(SystemExit):
        parse_args(["--output", str(tmp_path), "--suite", "unlocked"])


def test_heating_energy_is_physical_and_respects_delivery_lag():
    from thermal_complete_validation import CASES, Plant

    cold, warm = Plant(CASES[0]), Plant(CASES[0])
    initial = cold.air
    for _ in range(60):
        cold.step(5, 15, 0.3, 0, 0)
        warm.step(5, -5, 0.3, 0, 0)
    assert warm.air > cold.air
    assert cold.air < initial
    assert 0 < warm.electric_kwh < warm.heat_kwh
    assert 0 < warm.delivered_kw < 9


def test_asof_forecast_is_independent_of_hidden_future_truth(monkeypatch):
    import thermal_complete_validation as validation

    def forbidden_future(*args):
        raise AssertionError("Forecast read future plant truth")

    monkeypatch.setattr(validation, "truth_weather", forbidden_future)
    snapshot = validation.published_forecast(validation.CASES[0], 720, 5.2)
    assert snapshot.received_at == validation.START + timedelta(minutes=720)
    assert snapshot.weather_issued_at <= snapshot.received_at
    assert snapshot.weather_path(
        snapshot.received_at, snapshot.received_at + timedelta(hours=2), 5.2
    )
    assert snapshot.price_unit == "SEK/kWh"
    assert all(
        slot.start.date() == snapshot.received_at.date()
        or snapshot.received_at.hour >= 13
        for slot in snapshot.prices
    )


def test_outcome_audit_keeps_future_control_change_errors():
    from thermal_complete_validation import START, OriginalOutcomes

    audit = OriginalOutcomes(START + timedelta(days=4))
    audit.issue(
        {
            "forecast_id": "one",
            "received_at": START.isoformat(),
            "context": {"mode": "normal"},
            "prediction": {
                "issued_at": START.isoformat(),
                "due_at": (START + timedelta(minutes=30)).isoformat(),
                "horizon_minutes": 30,
                "model_version": 1,
                "temperature_c": 20.5,
                "indoor_temperature_c": 21,
            },
        }
    )
    # Runtime cancellation is deliberately irrelevant: future controller action
    # can make a conditional original forecast wrong, and that error is retained.
    audit.report(START + timedelta(minutes=31), 21.3)
    group = next(iter(audit.summary("train").values()))
    assert group["evaluated"] == 1
    assert group["model_mae_c"] == pytest.approx(0.8)
    assert group["persistence_mae_c"] == pytest.approx(0.3)


def test_missing_physical_report_is_an_outcome_abstention():
    from thermal_complete_validation import START, OriginalOutcomes

    audit = OriginalOutcomes(START + timedelta(days=4))
    audit.issue(
        {
            "forecast_id": "one",
            "received_at": START.isoformat(),
            "context": {"mode": "normal"},
            "prediction": {
                "issued_at": START.isoformat(),
                "due_at": (START + timedelta(minutes=30)).isoformat(),
                "horizon_minutes": 30,
                "model_version": 1,
                "temperature_c": 20.5,
                "indoor_temperature_c": 21,
            },
        }
    )
    audit.report(START + timedelta(minutes=35), 20.5)
    group = next(iter(audit.summary("train").values()))
    assert group["evaluated"] == 0
    assert group["outcomes"] == {"no_report_within_tolerance": 1}


def test_short_real_pi_run_preserves_actual_previous_command_provenance(
    monkeypatch, tmp_path
):
    import asyncio
    import csv
    import gzip

    import thermal_complete_validation as validation

    monkeypatch.setattr(validation, "TRAIN_MINUTES", 15)
    monkeypatch.setattr(validation, "TOTAL_MINUTES", 35)
    result = asyncio.run(validation.run_case(validation.CASES[0], tmp_path))
    with gzip.open(tmp_path / "actual_commands.csv.gz", "rt") as stream:
        commands = list(csv.DictReader(stream))
    for name in ("baseline", "ti"):
        arm = result["arms"][name]
        assert arm["fit_total"] == 0  # No artificial bootstrap on a short run.
        assert arm["retained_fit"] is None
        previous = {
            int(row["minute"]) + 1: float(row["actual_applied_fake_c"])
            for row in commands
            if row["arm"] == name
        }
        previous_pi = {
            int(row["minute"]) + 1: row for row in commands if row["arm"] == name
        }
        records = [
            event
            for path in (tmp_path / (name + "-events")).glob("*.jsonl.gz")
            for event in validation.recover_records(path)
            if event["kind"] == "decision"
        ]
        assert len(records) == 7  # Genuine five-minute indoor reports only.
        for event in records[1:]:
            minute = int(
                (
                    validation.stamp(event["received_at"]) - validation.START
                ).total_seconds()
                / 60
            )
            assert float(event["inputs"]["control"]["value"]) == previous[minute]
            assert event["inputs"]["control"]["attributes"]["pi_i_term"] == float(
                previous_pi[minute]["reported_pi_i_term"]
            )
            assert event["inputs"]["control"]["attributes"]["pi_error_c"] == float(
                previous_pi[minute]["reported_pi_error_c"]
            )
        assert (
            arm["pi_state_time_basis"]
            == "latest_reported_applied_control_not_physical_calculation_timestamp"
        )
        for period in arm["periods"].values():
            assert not period["runtime_original_events"]["integrity_issues"]
            assert not period["plant_and_controller"]["applications"]
            assert not period["plant_and_controller"]["confusion"]
    assert result["ti_minus_baseline"]["eval"]["electric_kwh"] == 0
