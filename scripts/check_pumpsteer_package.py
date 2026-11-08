"""Check HA's integration scan for duplicate PumpSteer manifests."""

import json
import sys
from pathlib import Path


def check_layout(root: Path) -> bool:
    """Fail closed on unreadable manifests or extra PumpSteer copies."""
    try:
        matches = []
        for directory in root.iterdir():
            manifest = directory / "manifest.json"
            if directory.is_dir() and manifest.is_file():
                if json.loads(manifest.read_text()).get("domain") == "pumpsteer":
                    matches.append(directory.name)
        return matches == ["pumpsteer"]
    except (OSError, ValueError, AttributeError):
        return False


if __name__ == "__main__":
    if len(sys.argv) != 2 or not check_layout(Path(sys.argv[1])):
        raise SystemExit("PumpSteer layout is unsafe or unreadable")
    print("Exactly one PumpSteer manifest in the correct integration directory")
