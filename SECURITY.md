# Security Policy

This toolkit writes into your Unreal project by design. Safety model:
localhost-only listener, token auth, a 101-command whitelist (no arbitrary code
execution), read-back verification on writes, scoped saves, transaction logs.

v6.4–v6.5 hardening adds: `/Game/`-only asset paths (`_safe_game_pkg`), log-file
and JUnit output allowlists, CSV/pattern/input caps, an 8MB HTTP body limit,
optional idempotency keys, advisory session locks, and a scenario command
allowlist that rejects destructive steps unless explicitly enabled.

## Reporting

Please use GitHub private security advisories instead of public issues.

## Notes

- Never expose port 8889 (5.1-5.5 bridge) beyond localhost; keep the token private.
- There is no default token. If `%APPDATA%\ue5_bridge\token` is missing, clients
  send no token and receive 403 rather than falling back to a fixed value.
- The UDP read channel (Remote Execution) is unauthenticated by engine design -
  use on trusted networks only; `UE5_DISABLE_REMOTE_EXEC_READ=1` disables it.
- Deletes are permanent; prefer dry-run modes when unsure.
- Locks are cooperative: direct `/build` or `/connect` calls bypass the lock
  layer. Route writes through `execute_command` if multiple agents share a project.
- No TLS: cross-machine use requires your own tunnel.
