"""All HA-scanned directories count, including hidden rollback directories."""

import json
from scripts.check_pumpsteer_package import check_layout


def test_rejects_duplicate_pumpsteer_manifest(tmp_path):
    root = tmp_path / "custom_components"
    primary = root / "pumpsteer"
    backup = root / ".pumpsteer-backup"
    primary.mkdir(parents=True)
    backup.mkdir()
    for directory in (primary, backup):
        (directory / "manifest.json").write_text(json.dumps({"domain": "pumpsteer"}))
    assert check_layout(root) is False
    (backup / "manifest.json").unlink()
    assert check_layout(root) is True
