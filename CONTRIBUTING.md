# Contributing to token-saver

Thanks for looking at token-saver — a BYOK, multi-provider LLM compression
proxy. This document covers the public surface of the project: how to get a
development environment running, how the test suite is meant to be run, and
the conventions every commit is held to.

## Development setup

```bash
git clone https://github.com/burhankhanlodhy/AI-Tokens-Compression.git
cd AI-Tokens-Compression/token-saver

python3 -m venv .venv && source .venv/bin/activate
pip install -r proxy/requirements.txt

cp .env.example .env   # then fill in real values; .env is gitignored
```

`TOKEN_SAVER_PG_DSN` (full ledger DSN) and `TOKEN_SAVER_PG_BASE` (base DSN
for test databases) are required for the Postgres-backed suite; without them
those tests skip with an explicit reason naming the env var. The dev default
is a local Postgres on port 5433 (`docker compose up` provides one).

## Running the tests

```bash
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)

pytest test/                              # standard run
pytest $(ls -r test/test_*.py)            # reverse-order run
```

Both orderings must pass. The suite is held to strict test isolation: tests
that override environment variables or module globals must restore them
(monkeypatch) — a suite that is green only in one order is a bug, and CI
enforces the reverse-order run for exactly that reason.

## Commit conventions

- One logical change per commit; the message explains *why*, not just *what*.
- Every bug fix lands together with the test that would have caught it.
- Credentials never enter tracked files. The ledger DSN is read from the
  environment only (`proxy/db.py` raises if `TOKEN_SAVER_PG_DSN` is unset).

### Commit identity

The project is developed by a set of role-based agents. Each role commits
under its own identity so the public history attributes work to the acting
role rather than a shared placeholder:

```bash
git -c user.name="<role>" -c user.email="<role>@token-saver.local" commit ...
```

Roles currently committing: `product-manager`, `project-manager`,
`application-developer`, `database-administrator`, `qa-lead`,
`ui-ux-engineer`. Copyright in [LICENSE](LICENSE) belongs to the repository
owner, not to the acting role identities.

## Reporting a vulnerability

Do **not** open a public issue. See [SECURITY.md](SECURITY.md) for the
private advisory process.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
