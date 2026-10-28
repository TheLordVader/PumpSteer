"""Pure steps for PumpSteer's existing brake and preheat ramps.

The controller supplies the previous state and commits the returned state.  A
caller may instead retain the returned values in an isolated replay state.
"""

BRAKE_RAMP_MAX_DT_SECONDS = 60.0
PREHEAT_RAMP_MAX_DT_SECONDS = 300.0


def brake_ramp_step(
    factor,
    last,
    last_expensive,
    requested,
    now,
    ramp_in,
    ramp_out,
    hold_minutes,
    *,
    max_dt_seconds=BRAKE_RAMP_MAX_DT_SECONDS,
):
    if last is None:
        dt_seconds = max_dt_seconds
    else:
        dt_seconds = min(max((now - last).total_seconds(), 1.0), max_dt_seconds)

    if requested:
        last_expensive = now
        rate = 1.0 / (ramp_in * 60.0)
        factor += dt_seconds * rate
    else:
        hold_active = (
            last_expensive is not None
            and (now - last_expensive).total_seconds() < hold_minutes * 60.0
        )
        if not hold_active:
            rate = 1.0 / (ramp_out * 60.0)
            factor -= dt_seconds * rate
    return max(0.0, min(1.0, factor)), now, last_expensive


def preheat_ramp_step(
    factor,
    requested,
    ramp_in,
    ramp_out,
    dt_seconds,
    *,
    max_dt_seconds=PREHEAT_RAMP_MAX_DT_SECONDS,
):
    elapsed_minutes = max(0.0, min(dt_seconds, max_dt_seconds)) / 60.0
    step_up = elapsed_minutes / max(ramp_in, 1.0)
    step_down = elapsed_minutes / max(ramp_out, 1.0)
    if requested:
        factor += step_up
    else:
        factor -= step_down
    return max(0.0, min(1.0, factor))
