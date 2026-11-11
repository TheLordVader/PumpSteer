# Passive learning yield: bounded review

Analysis branch from dde33ff. No runtime changes or relaxed quality limits.
Detailed field counts/grid-loss partition remain in the private audit directory.

The current chain already accepts a weighted observation when at least one
contributor changes numerically. All contributors still need valid HA report
times within60minutes. The dominant observed field loss occurs at indoor endpoint
coverage, before fitting. Source-change acceptance, aggregate observation and usable
regression row counts have different denominators and must not be conflated.

The RTM grid already carries accepted temperature context for a bounded60minutes;
these grid values are not independent physical measurements. Its core default15min
is overridden in the runtime, so simply copying upstream defaults would tighten
coverage. Pilot IntervalCollector's95%held-value coverage likewise cannot certify
physical source freshness from repeated polling.

Several context-change and temperature-unavailable events occur at identical
instants: multiple reasons are not independent sensor outages. Removing exclusions
or filling unknown values would inflate usable rows without establishing evidence.
Missing HA reports and missing physical measurements cannot be equated, especially
when repeated transport messages need not generate entity writes.

Retain current rules until per-source report/change provenance and exact blocking
causes are available at the relevant interruption times. Gather those through the
existing chain in a separately authorized passive trial, not a second collector.
No fabricated values, interpolation, physical-time claims or model fitting were used.

Verification: existing RoomMind adapter, common-chain, runtime and fit-evidence
tests:60passed in6.32s. These establish behavior, not optimum universal age limits
or real-world model performance. No full-regression rerun is claimed for this
documentation-only review.
