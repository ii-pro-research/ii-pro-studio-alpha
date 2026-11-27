# Contributing to II PRO Studio

The project aims to build reliable, increasingly local-first engineering intelligence. Small, testable contributions are preferred over speculative architecture expansion.

1. Discuss substantial changes in an issue before implementation. Describe the concrete problem and how success will be measured.
2. Work on a separate branch, preserve existing user data and follow [AGENTS.md](AGENTS.md).
3. Use synthetic fixtures for tests. Run the relevant Python and UI checks described in the [README](README.md). Document any skip or missing dependency.
4. Submit a focused pull request explaining scope, test results, security implications and rollback. Do not include credentials, live databases, private evidence or personal data.
5. Distinguish mock results from real model or Worker experiments. Live experiments need separately authorized budgets and immutable evidence.

Bug reports should include the commit, platform, expected and observed behavior, and a minimal sanitized reproduction. See [SECURITY.md](SECURITY.md) for vulnerabilities.

Submit only material you have the right to contribute. II PRO-authored contributions are licensed under [Apache-2.0](LICENSE). Preserve the original notices of all third-party materials.
