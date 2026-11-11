"""Synthetic diagnostic snapshots must not change eligibility or flood the chain."""

from datetime import timedelta

from test_thermal_intelligence_runtime import START, _cycle

from custom_components.pumpsteer.thermal_intelligence.runtime import ThermalIntelligence


def blocked(runtime, at=START):
    runtime.begin_cycle(
        _cycle(at, [("a", 0.7, 20, 0), ("b", 0.2, 19, 0), ("c", 0.1, 18, 3601)]),
        use_ohmigo_input=True,
    )
    return [e for e in runtime.export_state()["events"] if e["kind"] == "interruption"][
        -1
    ]


def test_stale_contributor_has_a_frozen_source_snapshot():
    runtime = ThermalIntelligence()
    event = blocked(runtime)
    diag = event["temperature_diagnostics"]
    source = next(s for s in diag["sources"] if s["entity"] == "c")
    assert source["reported_at"] == (START - timedelta(seconds=3601)).isoformat()
    assert source["age_seconds"] == 3601
    assert source["age_limit_seconds"] == 3600
    assert source["value"] == 18
    assert source["numeric_value_changed"] is True
    assert source["blocker"] == "report_time_stale"
    assert diag["snapshot_scope"] == "interruption_start_only"
    assert not [
        e for e in runtime.export_state()["events"] if e["kind"] == "observation"
    ]
    before = runtime.export_state()["events"]
    blocked(runtime)
    assert runtime.export_state()["events"] == before
    restored = ThermalIntelligence()
    assert restored.restore_state(runtime.export_state())
    assert (
        next(
            e
            for e in restored.export_state()["events"]
            if e.get("temperature_diagnostics")
        )["temperature_diagnostics"]
        == diag
    )


def test_missing_future_and_reordered_reports_keep_distinct_causes():
    for expected, report, value in [
        ("report_time_missing", None, 21),
        ("report_time_future", (START + timedelta(minutes=1)).isoformat(), 21),
        ("missing_or_invalid_value", START.isoformat(), None),
    ]:
        runtime = ThermalIntelligence()
        cycle = _cycle(START, [("a", 1, 20, 0)])
        cycle["sources"][0].update(reported_at=report, value=value)
        runtime.begin_cycle(cycle, use_ohmigo_input=False)
        event = [
            e for e in runtime.export_state()["events"] if e["kind"] == "interruption"
        ][-1]
        assert event["temperature_diagnostics"]["sources"][0]["blocker"] == expected
    runtime = ThermalIntelligence()
    runtime.begin_cycle(_cycle(START, [("a", 1, 20, 0)]), use_ohmigo_input=False)
    cycle = _cycle(START + timedelta(seconds=30), [("a", 1, 21, 60)])
    runtime.begin_cycle(cycle, use_ohmigo_input=False)
    event = [
        e for e in runtime.export_state()["events"] if e["kind"] == "interruption"
    ][-1]
    assert (
        event["temperature_diagnostics"]["sources"][0]["blocker"]
        == "report_out_of_order"
    )


def test_snapshot_size_is_bounded_and_repeated_polling_adds_no_events():
    runtime = ThermalIntelligence()
    cycle = _cycle(START, [(str(i), 1, 20, 3601) for i in range(8)])
    runtime.begin_cycle(cycle, use_ohmigo_input=False)
    initial = runtime.export_state()
    diag = initial["events"][-1]["temperature_diagnostics"]
    assert len(diag["sources"]) == 4
    assert diag["sources_truncated"] is True
    for _ in range(100):
        runtime.begin_cycle(cycle, use_ohmigo_input=False)
    assert runtime.export_state()["events"] == initial["events"]
