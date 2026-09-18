# Security Policy

This toolkit writes into your Unreal project by design. Safety model:
localhost-only listener, token auth, a 72-command whitelist (no arbitrary code
execution), read-back verification on writes, scoped saves, transaction logs.

## Reporting

Please use GitHub private security advisories instead of public issues.

## Notes

- Never expose port 8889 (5.1-5.5 bridge) beyond localhost; keep the token private.
- The UDP read channel (Remote Execution) is unauthenticated by engine design -
  use on trusted networks only.
- Deletes are permanent; prefer dry-run modes when unsure.
