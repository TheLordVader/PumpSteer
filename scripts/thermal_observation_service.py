"""Only this finite trial's systemd units and credential copy; raw data stays."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from thermal_observation import atomic_json

NAME = "pumpsteer-thermal-reference-trial"
COLLECTOR = NAME + ".service"
TIMER = NAME + ".timer"
FINISH = "pumpsteer-thermal-reference-trial-finish.service"
BUNDLE = Path("/opt/pumpsteer-thermal-reference-trial")
DATA = Path("/var/lib") / NAME


def units(bundle, data, end):
    if end.tzinfo is None:
        raise ValueError("The trial deadline must be timezone-aware")
    end = (
        (end.astimezone(timezone.utc) + timedelta(microseconds=999999))
        .replace(microsecond=0)
        .strftime("%Y-%m-%d %H:%M:%S UTC")
    )
    config = bundle / "config.json"
    collector = f"""[Unit]
Description=Finite passive PumpSteer thermal observation
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
DynamicUser=yes
StateDirectory={data.name}
UMask=0077
Environment=PYTHONPATH={bundle}/vendor
LoadCredential=ha_environment:{bundle}/ha-environment
ExecStart=/usr/bin/python3 -B {bundle}/thermal_observation.py --config {config} --credential-env %d/ha_environment --credential-variable PUMPSTEER_HA_TOKEN
Restart=on-failure
RestartPreventExitStatus=78
RestartSec=15
RuntimeMaxSec=7d
TimeoutStopSec=60
MemoryMax=128M
CPUQuota=10%
TasksMax=16
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
StandardOutput=null
LogRateLimitIntervalSec=60
LogRateLimitBurst=2

[Install]
WantedBy=multi-user.target
"""
    timer = f"""[Unit]
Description=Stop and evaluate only the finite PumpSteer observation

[Timer]
OnCalendar={end}
Persistent=yes
AccuracySec=1s
Unit={FINISH}

[Install]
WantedBy=timers.target
"""
    finish = f"""[Unit]
Description=Offline final evaluation and removal of this trial's services
StartLimitIntervalSec=0

[Service]
Type=oneshot
UMask=0077
ExecCondition=/usr/bin/python3 -B {bundle}/thermal_observation_service.py --due {config}
ExecStartPre=/usr/bin/systemctl stop {COLLECTOR}
ExecStart=/usr/bin/python3 -B {bundle}/thermal_observation_evaluate.py --config {config}
ExecStopPost=/usr/bin/python3 -B {bundle}/thermal_observation_service.py --cleanup-result {config}
Restart=on-failure
RestartSec=15min
TimeoutStartSec=20min
TimeoutStopSec=60
MemoryMax=768M
CPUQuota=25%
TasksMax=16
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths={data} {bundle} /etc/systemd/system
ProtectHome=yes
PrivateTmp=yes
PrivateNetwork=yes
StandardOutput=null
LogRateLimitIntervalSec=60
LogRateLimitBurst=2

[Install]
WantedBy=multi-user.target
"""
    return {COLLECTOR: collector, TIMER: timer, FINISH: finish}


def cleanup(
    unit_dir=Path("/etc/systemd/system"),
    credential=BUNDLE / "ha-environment",
    run=subprocess.run,
    completed=True,
):
    """Exact allowlist; never remove raw files or touch unrelated/HA units."""
    commands = [["stop", COLLECTOR], ["disable", COLLECTOR]]
    names = []
    if completed:
        commands += [["disable", "--now", TIMER], ["disable", FINISH]]
        names += [COLLECTOR, TIMER, FINISH]
    for command in commands:
        run(["systemctl", *command], check=False)
    for name in names:
        (unit_dir / name).unlink(missing_ok=True)
    credential.unlink(missing_ok=True)
    run(["systemctl", "daemon-reload"], check=False)


def cleanup_result(config, result):
    """At most six automatic attempts; preserve a clear failure and all raw data."""
    # ExecStopPost also runs for a skipped ExecCondition at an early boot.
    if datetime.now(timezone.utc) < datetime.fromisoformat(config["end"]):
        return
    directory = Path(config["data_dir"])
    failure = directory / "evaluation-failure.json"
    attempts = (
        json.loads(failure.read_text()).get("failed_attempts", 0)
        if failure.exists()
        else 0
    )
    if result != "success":
        attempts += 1
        atomic_json(
            failure,
            {
                "failed_attempts": attempts,
                "result": result,
                "at": datetime.now(timezone.utc).isoformat(),
                "retry_exhausted": attempts >= 6,
            },
        )
    cleanup(completed=result == "success" or attempts >= 6)
    if result != "success" and attempts >= 6:
        subprocess.run(["systemctl", "--no-block", "stop", FINISH], check=False)


if __name__ == "__main__":
    command, path = sys.argv[1:]
    config = json.loads(Path(path).read_text())
    if command == "--due":
        end = datetime.fromisoformat(config["end"])
        raise SystemExit(0 if datetime.now(timezone.utc) >= end else 1)
    if command != "--cleanup-result":
        raise SystemExit("Unsupported trial helper command")
    cleanup_result(config, os.environ.get("SERVICE_RESULT", "unknown"))
