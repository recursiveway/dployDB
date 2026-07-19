# Security policy

## Supported versions

DployDB is currently Alpha software. Only the latest published `0.x` release
receives security fixes; older Alpha releases are unsupported. This policy will
be revised before `1.0.0`.

## Reporting a vulnerability

Do not open a public issue or attach production logs. Use GitHub's private
[Report a vulnerability](https://github.com/recursiveway/dployDB/security/advisories/new)
form. Include the affected version, supported environment, impact, minimal
reproduction, and whether production data or credentials may have been exposed.
Redact secrets and personal or customer data.

RecursiveWay will acknowledge the report through the private advisory, assess
severity, coordinate a fix, and publish an advisory when users can update
safely. No response-time guarantee is offered during Alpha.

The product's security boundaries and operator responsibilities are documented
separately in [docs/security.md](docs/security.md).
