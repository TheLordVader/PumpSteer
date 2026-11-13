# Offline installation candidate

Production reference supplied by owner: aff5419119e72fb99c49906953e75f63c94e99a1; HA Core 2026.10.4. Current production files and Core version were not reverified: no usable authenticated production connection was identified in this run.

Candidate code: 33ca1261caa38593bbd0ab090e9c19ea1feb676c, branch candidate/base-ui-20261113. Exactly two cherry-picked commits above aff5419: PR4 367314fa and PR5 ba31e013. No intervening commits from their newer development base were included. The sensor diff is solely the inertia-dependent brake hold; remaining integration changes are OptionsFlow and translations. Manifest unchanged.

Full candidate regression: 1652 passed, no skips, 46.75 seconds with pinned RoomMind. The count differs from PR5's 1666 because the candidate uses the older production base, not the newer experimental development base. Native HA probe passed unchanged saves, clearing, selector conversion, section validation and first-source alias rejection, with 94 fields/10 sections.

Native probe environment remains HA2025.12.5. Exact HA2026.10.4 testing is NOT complete: it requires Python>=3.14.2. Available downloadable Python was 3.14.0; the attempted 3.14.2 download was unavailable. Do not represent this as exact-production-version compatibility proof.

Downloaded failed GitHub Actions logs for PR4 run43382173241 and PR5 run43412665891. Their observed pytest failures reference missing git object b273afc in three historical parity cases, consistent with shallow checkout, rather than a failing control assertion. Full-history local regression passes. Existing Ruff/style warnings must not be confused with proof of functional failure; no mass formatting or CI configuration change was made.

Installation status: NO-GO pending read-only verification of actual installed file hashes/revision and Core version, and the native Configure saving suite on exact HA2026.10.4. Test collapsed existing values, Ohmigo/GOS, explicit clears and verification revocation before installation approval. No production installation, restart, settings change, merge or push was performed.
