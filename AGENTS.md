# PumpSteer – project instructions

Apply the global Codex working principles; these are PumpSteer-specific contracts.

- Preserve PI as the primary regulation. Respect existing comfort limits and
  Ohmigo safety functions; diagnostics or experimental thermal models must not
  silently become active control or bypass established safeguards.
- Define startup, restored-state freshness, relevant integration/configuration
  reload and recovery behavior before changing control. Reconcile stale commands
  safely, avoid unintended reactivation, and recover automatically when valid
  inputs return according to existing documented contracts. Do not weaken manual
  or physical fallbacks. Service acknowledgement is not proof of hardware action.
- Test affected control sequences, input loss/return and relevant lifecycle events
  with existing offline tests and simulated hardware. Never use real heat-pump or
  Ohmigo commands as an implicit test. Keep diagnostics separate from active control.
- JohanAlvedal/PumpSteer is upstream. Work in the owner's fork and scoped branches;
  do not write to upstream or change remote/push policy without explicit mandate.
  Preserve upstream-compatible behavior unless the task explicitly approves a change.
- Keep private installation details, addresses, credentials and operational
  evidence out of public source, examples, tests and commits. Use synthetic data;
  publication and production changes each require their own authorized scope.
