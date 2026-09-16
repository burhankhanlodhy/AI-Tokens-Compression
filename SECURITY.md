# Security Policy

## Reporting a vulnerability

Do **not** open a public issue for a security vulnerability. Report it
privately so we can fix and disclose it on a coordinated schedule.

- Preferred: [GitHub private security advisory](https://github.com/burhankhanlodhy/AI-Tokens-Compression/security/advisories/new)
- Fallback: email <burhanlodhi1997@gmail.com> with a subject prefixed `[SECURITY]`

Please include a minimal reproduction, the affected version or commit, and
your assessment of impact. You can expect an acknowledgment within 3 business
days and, for confirmed issues, a fix with coordinated disclosure.

## Operational notes for users and maintainers

- **BYOK by design:** `token-saver` never stores client API keys — the
  incoming `Authorization` header is forwarded per-request and not persisted.
  Keep `Authorization` values out of configuration, logs, screenshots, and
  test fixtures.
- **Secrets are env-var only:** configuration lives in `token-saver/.env`,
  which is gitignored. Treat any credential that has ever appeared in
  repository history, CI logs, or a public issue as compromised and rotate it.
  Postgres credentials are documented in `.env.example`.