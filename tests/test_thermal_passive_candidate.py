"""A single passive candidate, no optional heat-source configuration required."""

import asyncio
import math
from datetime import timedelta
from types import SimpleNamespace

from test_thermal_observer import NOW, observer, report
from test_brake_provenance import _sensor


def test_default_candidate_detects_heat_like_residual_and_recovers():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    value = 21
    for minute in range(71):
        at = NOW + timedelta(minutes=minute)
        if minute:
            supplied = 1.2 if 30 < minute <= 50 else 0
            decay = math.exp(-0.04 / 60)
            value = 5 + (value - 5) * decay + supplied / 0.04 * (1 - decay)
        obs.observe(at, report(value, at), report(5, at), braking=True)
        if minute == 50:
            evidence = obs.attributes()["heat_evidence"]
            assert evidence["status"] == "suspected_extra_heat"
            assert evidence["confidence"] == "moderate"
            assert evidence["verified"] is False
            assert obs.model.pending_samples == 3
    assert obs.attributes()["heat_evidence"]["status"] == "consistent_with_model"
    assert obs.model.pending_samples == 5


def test_unknown_ordinary_heating_in_normal_mode_is_not_external_heat():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    for minute in range(61):
        at = NOW + timedelta(minutes=minute)
        obs.observe(at, report(21 + minute * 0.03, at), report(5, at), braking=False)
    assert obs.attributes()["heat_evidence"]["confidence"] == "none"
    assert obs.attributes()["reason"] == "outside_braking"
    assert obs.model.pending_samples == 0
    assert not obs.diagnostics.pending_predictions


def test_changed_brake_depth_requires_a_new_comparable_baseline():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    for minute in range(51):
        at = NOW + timedelta(minutes=minute)
        value = 5 + 16 * math.exp(-0.04 * minute / 60) + max(0, minute - 30) * 1.2 / 60
        obs.observe(
            at,
            report(value, at),
            report(5, at),
            braking=True,
            control_context=("braking", 1 if minute <= 30 else 0.5),
        )
    assert obs.attributes()["heat_evidence"]["status"] == "unconfirmed_model"
    assert obs.attributes()["heat_evidence"]["confidence"] == "none"
    assert obs.attributes()["heat_evidence"]["baseline_confirmed"] is False


def test_legacy_source_preference_is_not_a_temperature_source_boundary():
    sensor, _ = _sensor()
    before = sensor._thermal_source_ids()
    sensor._config_entry.options.update(
        external_heat_sources=True, thermal_assist_enabled=True
    )
    assert sensor._thermal_source_ids() == before


def test_options_form_needs_no_heat_source_or_assistance_configuration(monkeypatch):
    from custom_components.pumpsteer.options_flow import PumpSteerOptionsFlowHandler
    from custom_components.pumpsteer import options_flow

    # The existing schema stub omits HA's description keyword; only selectors
    # are a test boundary here, the real options-form method is exercised.
    monkeypatch.setattr(options_flow.vol, "Optional", lambda key, **kwargs: key)
    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(data={}, options={})
    flow.hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: []))
    flow.async_show_form = lambda **kwargs: kwargs
    result = asyncio.run(flow.async_step_init())
    schema = result["data_schema"]
    fields = {getattr(k, "schema", k) for k in getattr(schema, "schema", schema)}
    assert "external_heat_sources" in fields
    assert "thermal_assist_enabled" not in fields
    assert "indoor_temp_entity" in fields
    assert "real_outdoor_entity" in fields


def test_clean_quantized_response_has_no_suspicion_after_confirmation():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    confirmed = False
    for minute in range(241):
        at = NOW + timedelta(minutes=minute)
        value = round(5 + 16 * math.exp(-0.04 * minute / 60), 1)
        obs.observe(at, report(value, at), report(5, at), braking=True)
        evidence = obs.attributes()["heat_evidence"]
        assert evidence["status"] != "suspected_extra_heat"
        confirmed = confirmed or evidence.get("baseline_confirmed", False)
    assert confirmed  # Later delta changes may correctly require reconfirmation.


def test_invalid_explicit_context_cannot_train_or_forecast():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    for minute in range(61):
        at = NOW + timedelta(minutes=minute)
        obs.observe(
            at,
            report(21 + minute * 0.02, at),
            report(5, at),
            braking=True,
            control_context=("unknown", 1),
        )
    assert obs.model.pending_samples == 0
    assert not obs.diagnostics.pending_predictions
    assert obs.attributes()["heat_evidence"]["confidence"] == "none"


def test_confirmed_heat_pauses_new_forecasts_but_keeps_existing_failures():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    for minute in range(61):
        at = NOW + timedelta(minutes=minute)
        value = 5 + 16 * math.exp(-0.04 * minute / 60) + max(0, minute - 30) * 1.2 / 60
        obs.observe(at, report(value, at), report(5, at), braking=True)
    attrs = obs.attributes()
    assert attrs["heat_evidence"]["status"] == "suspected_extra_heat"
    assert attrs["predicted_change_c"] == {}
    assert attrs["forecast_issued_at"] is None
    assert attrs["quality"]["30"]["count"] == 2
    assert attrs["quality"]["30"]["mae_c"] > 0.25
    assert obs.diagnostics.pending_count > 0  # old120min targets are not erased


def test_flat_windows_rejected_by_learner_are_counted_as_rejected():
    obs = observer()
    for minute in range(31):
        at = NOW + timedelta(minutes=minute)
        obs.observe(at, report(21, at), report(5, at), braking=True)
    assert obs.attributes()["learning_windows"] == {"accepted": 0, "rejected": 3}
