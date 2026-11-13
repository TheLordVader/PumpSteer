"""Zone options explain passive operation and profile-specific alarm choices."""

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "filename",
    [
        "strings.json",
        "translations/en.json",
        "translations/sv.json",
        "translations/se.json",
    ],
)
def test_zone_pages_explain_passive_monitoring_and_alarm_fields(filename):
    root = Path("custom_components/pumpsteer")
    options = json.loads((root / filename).read_text())["options"]["step"]["init"][
        "sections"
    ]
    for number in range(1, 5):
        page = options[f"zone_{number}"]
        descriptions = page["data_description"]
        for field in (
            "enabled",
            "profile",
            "threshold",
            "comfort_threshold",
            "climate",
            "alarm_delay_minutes",
            "notify_service",
        ):
            assert descriptions[f"zone_{number}_{field}"]
        assert page["description"]


def test_english_options_and_swedish_alias_are_synchronized():
    root = Path("custom_components/pumpsteer")
    texts = [
        json.loads((root / filename).read_text())["options"]["step"]["init"]["sections"]
        for filename in (
            "strings.json",
            "translations/en.json",
            "translations/sv.json",
            "translations/se.json",
        )
    ]
    for page in ("zone_1", "zone_2", "zone_3", "zone_4", "notifications"):
        assert texts[0][page] == texts[1][page]
        assert texts[2][page] == texts[3][page]
