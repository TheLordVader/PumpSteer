# v2.2.8 Base adaptation

Base:36f4ca15c933215cf02e4b6fef6592e6a139cf46 (the last explicitly installed
3.0candidate). Branch fix/base-v228-brake-hold-20261113. No production access,
installation, version change, merge or push is part of this work. Existing TI
code is retained, not reintroduced from another experimental branch; the separate
SAFE fix e3527d6 is not included.

## Sources and selected scope

- [Johan PR289](https://github.com/JohanAlvedal/PumpSteer/pull/289), reviewed actual
  source at2a67b56d0ee57c7f55ae7475810cb8e1393f1fd1: preheat temperature-drop gate.
- [Johan PR293](https://github.com/JohanAlvedal/PumpSteer/pull/293), reviewed actual
  source at1f11c08b9ad3fc642a08a4470ca0cd9d56eca340: inertia-scaled bridge. This is
  a merge head containing other changes; it was not cherry-picked wholesale.
- Upstream and this project have AGPL-3.0 license text. Reuse is limited to the
  bridge-window calculation and is attributed here; no second implementation,
  external integration or dependency was added.

## Implemented: inertia-scaled short-dip bridge

Reuse exactly the core rule fromPR293:
`max(configured_brake_hold_minutes, ramp_in + ramp_out)` in the existing normal-price
bridge block. Existing price-gap detection, prior-braking requirement, positive
brake ramp and comfort-floor escape remain. Existing bridge_limit_minutes/logging
automatically reflects the effective window. No settings or entities were added.
The expensive-price branch's separate ramp hold semantics are not changed.

| Inertia | Ramp in / out | New bridge window with30min configured | Old window |
| --- | --- | --- | --- |
|1–2.5|15/15min|30min|30min|
|4|24/15min|39min|30min|
|6|36/18min|54min|30min|
|10|60/30min|90min|30min|

Thus an already braking heavy house with inertia6 holds through a45minute normal
price gap, while a60minute gap releases it. Without prior braking the same45minute
window can still allow price preheat. A configured hold greater than the ramp
sum stays effective. A configured value below the ramp sum becomes a floor, not
an override that disables bridging. Comfort-floor release remains authoritative.

This reduces ramp cycling; it is not proof of lower energy cost. Longer bridging
also forgoes some potentially useful cheap-hour heating. No model of COP, tariffs
or real building losses was added to claim optimality of this heuristic.

## Not imported: unconditional temperature-drop requirement

PR289 requires at least2C forecast drop within its12point lookahead and scales
strength from the drop instead of absolute forecast cold. In3.0 that weather flag
gates actual boost, so a direct import suppresses price shifting in steady cold.

An isolated comparison loaded the exact upstream forecast.py and substituted only
its analyzer in our existing controller, with normalized hourly weather points,
15minute prices0.8→2.5SEK/kWh and an expensive period60minutes ahead:

| Synthetic weather | Existing3.0 | PR289 analyzer in same controller |
| --- | --- | --- |
|Steady−5C|preheating, strength1|normal, strength0|
|Cold front5C downwards|preheating|preheating|
|Warming−5C upwards to warm day|normal|normal|

No field-temperature replay, interpolated physical measurements or savings claim
was used. This comparison evaluates the forecast gate, not the complete upstream
integration. The loaded forecast code was not vendored or committed.

Retain existing economic opportunity: actual boost still requires an upcoming
expensive period, forecast suitability (or the existing cold fallback), enabled
preheat and temperature headroom. Weather alone with no upcoming price peak does
not activate boost. PR289's weather-only restriction is therefore rejected rather
than adding another hybrid rule or a new price threshold.

Remaining difference: ThermalOutlook may continue reporting preheat potential in
stable winter weather, and its strength can saturate at1. This is not a standalone
cost-benefit calculation; the controller's additional price/comfort gates matter.
The fallback without a usable ThermalOutlook also remains unchanged.

## Verification scope

New tests exercise real classify_price_list and the controller's actual
analyze_thermal_outlook path, using mocked async weather acquisition and physical
outputs. Merely assigning _last_outlook is insufficient because _do_update resets
and rebuilds it; the tests explicitly supply the normal forecast-builder path.

Cases: low/default/heavy inertia, custom configured floor,54minute inclusive
boundary versus55minutes,45/60minute gaps, retained brake versus preheat takeover,
comfort-floor escape, stable cold with/without price peak, cold front and warming,
and a heavy house without prior braking. All are synthetic functional scenarios,
not proof of financial benefit or real-world thermal response.

Runtime diff is confined to one bridge expression in sensor.py; forecast.py,
PI, comfort-floor rules, SAFE, physical transports, Relay Guard, TI and version
metadata are unchanged. Existing preheat headroom/elapsed-ramp/transition and
comfort tests run alongside the new scenarios. Internal read-only review found
no blocking issue; it is not independent external qualification.

Results:18new scenario cases;106targeted tests passed (0.31s). Final full regression
with existing pinned RoomMind adapter enabled: **1671passed (49.43s)**. New test-file
Ruff and git diff whitespace checks passed. No new dependencies or HA connections.
