# Bounded temperature-blocker provenance

Base: f21f206. No quality limit, weighting, grid rule, model physics, output or
training eligibility is changed. This is passive diagnostic metadata only.

## Historical limit

The field Recorder schema does contain last_reported_ts. For the bounded indoor
source query, stored report times equal state-update times; the rows do not supply
all identical reports or the exact in-memory snapshot at every blocked cycle.
Reconstructing ages from the latest recorded rows therefore gives candidate causes,
not an exact replay of runtime provenance. Physical measurement time remains unknown.
Private historical counts and extracts are not committed.

## Existing-chain addition

A new `temperature_diagnostics` field on an existing
`required_temperature_missing_or_stale` interruption records:

- source entity/role/weight and numeric value;
- the supplied HA report timestamp and age at that cycle;
- the existing applicable age limit;
- numeric change against the last seen numeric value (not proof of physical sampling);
- existing physical-time-known status;
- primary blocker: missing/invalid value, missing/future/stale report time or
  out-of-order report; otherwise null;
- availability of the supplied aggregate indoor/outdoor values.

At most four source entries are retained (normal configuration: three indoor plus
outdoor); a truncation flag is explicit for larger synthetic/unsupported inputs.
No new event kind, entity, Store, database, timer or collection system is added.
Existing event retention/checkpoint policy remains in force.

The snapshot is frozen **at interruption start only**. Continuing invalid cycles
are not a new diagnostic stream. Later blocker changes within the same interruption
are not fully reconstructed by this snapshot. Waiting on an unchanged valid value
also does not produce a new diagnostic snapshot or training observation. Existing
identical-value counters remain separate. Other interruption causes are unchanged.

The extra diagnostic payload is about1.4KB in the synthetic three-source fixture;
actual size varies with entity IDs. No additional event/checkpoint is scheduled.
The configured global event cap still applies; bounded extra bytes per interruption
are not a claim that the complete Store size is unchanged.

## Verification

Tests were observed failing on absent metadata before implementation. They cover
stale/missing/future/reordered reports, an invalid numeric value, frozen snapshots,
Store export/restore, source-count bounds and100identical polls without extra events.
A180-cycle synthetic baseline comparison with the pre-change runtime produced
identical exported model/counters/commands/events after removing only the added
metadata. Neither model fitting nor real outputs are exercised in that comparison.

Next: inspect these snapshots from a separately authorized passive trial, alongside
source report policies. Decide whether source information is genuinely absent or
merely blocked before changing age rules or using unchanged republishes. This
change itself does not increase the number of eligible rows or qualify a model.

Results: targeted diagnostics/chain/storage/runtime suite30passed (0.72s before
adding the repeated-poll cap test); final full suite with pinned RoomMind enabled
**1653passed (50.26s)**, including all three new diagnostic tests. Test-file Ruff
and diff whitespace checks passed. No production operation or push performed.
