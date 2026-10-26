"""Offline reproduction of report loss; uses real Core, no HA startup/network.

Run with an existing HA test interpreter, outside pytest/conftest's HA stubs:
    python -B tests/indoor_freshness_probe.py

Only synthetic MQTT payloads and entity IDs are used. MQTT decoding and its
change gate, StateMachine timestamps and template tracking are native Core.
The broker and queued entity-state writer are replaced at their boundaries.
No runtime fix is made: a received/cached value has no proven physical age.
"""

import asyncio
from datetime import timedelta
import importlib
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from homeassistant import loader
from homeassistant.components.mqtt.entity import MqttEntity
from homeassistant.components.mqtt.models import DATA_MQTT
from homeassistant.components.mqtt.sensor import MqttSensor
from homeassistant.const import __version__
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import TrackTemplate, async_track_template_result
from homeassistant.helpers.template import Template

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "sensor.room_a"
WEIGHTED = "sensor.weighted_test"


class Source:
    """Native MQTT methods with only the broker/write queue replaced."""

    _attrs_have_changed = MqttEntity._attrs_have_changed
    _message_callback = MqttEntity._message_callback
    _state_message_received = MqttSensor._state_message_received
    _update_state = MqttSensor._update_state
    _attr_force_update = False
    _attr_native_value = None
    _expire_after = None
    _numeric_state_expected = True
    _config = {}
    entity_id = SOURCE

    def __init__(self, hass):
        self.hass = hass
        self._template = lambda payload, default: str(
            json.loads(payload)["temperature"]
        )

    def receive(self, payload):
        message = SimpleNamespace(
            payload=json.dumps(payload), subscribed_topic="test/room"
        )
        self._message_callback(
            self._state_message_received,
            {"_attr_native_value", "_attr_last_reset", "_expired"},
            message,
        )

    def write(self):
        self.hass.states.async_set(
            self.entity_id,
            self._attr_native_value,
            {"unit_of_measurement": "°C"},
            force_update=self._attr_force_update,
        )


def observer_class():
    # Load pure thermal modules without importing integration setup/control.
    name = "indoor_freshness_probe_model"
    package = ModuleType(name)
    package.__path__ = [str(ROOT / "custom_components/pumpsteer")]
    sys.modules[name] = package
    return importlib.import_module(f"{name}.thermal_observer").ThermalObserver


async def probe():
    with tempfile.TemporaryDirectory(prefix="indoor-freshness-") as directory:
        hass = HomeAssistant(directory)
        hass.config.skip_pip = True
        loader.async_setup(hass)
        source = Source(hass)
        hass.data[DATA_MQTT] = SimpleNamespace(
            debug_info_entities={
                SOURCE: {"subscriptions": {"test/room": {"messages": []}}}
            },
            state_write_requests=SimpleNamespace(
                write_state_request=lambda entity: entity.write()
            ),
        )
        source.receive({"temperature": 20.0, "humidity": 50})
        hass.states.async_set("sensor.room_b", "20", {"unit_of_measurement": "°C"})
        hass.states.async_set("sensor.room_c", "20", {"unit_of_measurement": "°C"})
        expression = Template(
            "{{ (states('sensor.room_a')|float * 0.7 + "
            "states('sensor.room_b')|float * 0.2 + "
            "states('sensor.room_c')|float * 0.1)|round(2) }}",
            hass,
        )

        def changed(event, updates):
            hass.states.async_set(
                WEIGHTED, str(updates[-1].result), {"unit_of_measurement": "°C"}
            )

        tracker = async_track_template_result(
            hass, [TrackTemplate(expression, None)], changed
        )
        tracker.async_refresh()
        await hass.async_block_till_done()
        initial_source = hass.states.get(SOURCE)
        initial_weighted = hass.states.get(WEIGHTED)
        assert float(initial_weighted.state) == 20.0

        # A new MQTT packet with unchanged extracted temperature is discarded
        # by MQTT's native tracked-attribute gate, before StateMachine is called.
        source.receive({"temperature": 20.0, "humidity": 51})
        await hass.async_block_till_done()
        assert hass.states.get(SOURCE).last_reported == initial_source.last_reported
        assert hass.states.get(WEIGHTED).last_reported == initial_weighted.last_reported

        # Even forcing an identical HA report does not refresh a state-based
        # weighted template. It also says nothing about a cached temperature's age.
        source._attr_force_update = True
        source.receive(
            {"temperature": 20.0, "humidity": 52, "last_seen": "new_radio_packet"}
        )
        await hass.async_block_till_done()
        forced = hass.states.get(SOURCE)
        assert forced.last_reported > initial_source.last_reported
        assert forced.state == initial_source.state
        # force_update produces state_changed; equal template results are filtered.
        assert hass.states.get(WEIGHTED).last_reported == initial_weighted.last_reported

        # A genuine changed input produces a changed weighted result.
        source._attr_force_update = False
        source.receive({"temperature": 21.0, "humidity": 52})
        await hass.async_block_till_done()
        updated = hass.states.get(WEIGHTED)
        assert float(updated.state) == 20.7
        assert updated.last_reported > initial_weighted.last_reported

        # A changed secondary source can refresh the weighted timestamp while
        # another contribution remains old: template time is not all-source age.
        held_source_report = hass.states.get(SOURCE).last_reported
        hass.states.async_set("sensor.room_b", "21", {"unit_of_measurement": "°C"})
        await hass.async_block_till_done()
        assert float(hass.states.get(WEIGHTED).state) == 20.9
        assert hass.states.get(SOURCE).last_reported == held_source_report
        assert hass.states.get(WEIGHTED).last_reported > held_source_report

        # Keep the existing 15-minute observer gate; never refresh old readings
        # just because the collector sees another snapshot.
        observer = observer_class()(SimpleNamespace())
        sample = hass.states.get(WEIGHTED)
        assert (
            observer._reading(sample, sample.last_reported + timedelta(seconds=900))
            is not None
        )
        assert (
            observer._reading(sample, sample.last_reported + timedelta(seconds=901))
            is None
        )
        assert (
            observer._reading(sample, sample.last_reported + timedelta(seconds=901))
            is None
        )
        tracker.async_remove()
        return {
            "core_version": __version__,
            "mqtt_equal_temperature_does_not_refresh_ha_report": True,
            "force_update_alone_does_not_refresh_equal_weighted_result": True,
            "changed_temperature_updates_weighted_result": True,
            "weighted_timestamp_is_not_all_source_measurement_time": True,
            "observer_900_second_gate_preserved": True,
            "physical_measurement_time_verified": False,
            "core_started": False,
            "production_access": False,
        }


if __name__ == "__main__":
    # Any accidental network attempt is a test failure, not an API fallback.
    with patch.object(
        socket.socket, "connect", side_effect=AssertionError("Network forbidden")
    ):
        print(json.dumps(asyncio.run(probe()), indent=2))
