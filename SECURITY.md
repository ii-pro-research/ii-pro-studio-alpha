# Security policy

II PRO Studio is experimental research software. No production security guarantee or supported release schedule is asserted.

## Reporting a vulnerability

If GitHub private vulnerability reporting is enabled, use the repository's Security tab to report privately. Otherwise open a public issue requesting a private reporting channel without disclosing the vulnerability details. Do not post credentials, session URLs, live database content, private logs or exploit payloads publicly.

Include the affected commit, environment, impact, reproduction steps using synthetic data where possible, and a suggested mitigation. Do not test against another person's installation without permission.

## Operational boundaries

- Studio is designed for loopback access; do not expose it directly to the internet.
- Core remains the authority for scoped approvals, execution budgets and recovery.
- Unknown execution status is not proof of safe termination. Use the documented recovery procedure; never replay an unknown operation automatically.
- Live providers and Docker require explicit configuration. Use isolated project copies, pinned reviewed images and non-secret test fixtures.
- Keep tokens, keys, credentials, local Core state and private evidence out of Git and exported context. Protect operational backups separately.
- Verify third-party code and model terms before distribution. Public visibility does not grant authority to redistribute every embedded asset.

See [contributor instructions](AGENTS.md) and [roadmap](docs/ROADMAP.md).
