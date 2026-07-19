# Releasing DployDB

This runbook defines the public release and maturity contract. Release artifacts
must come from a clean, reviewed commit on `main`; local files in `dist/` are
never publishable evidence.

## Version policy

- `0.1.x` contains compatible Alpha bug and security fixes.
- `0.2.0` through `0.8.x` may add Alpha features. Breaking changes are allowed
  only in a minor release and require changelog and migration guidance.
- `0.9.0` is Beta. Public CLI, exit-code, JSON, configuration, backup, and
  durable-state contracts are then frozen except for critical safety fixes.
- `1.0.0rcN` is a release candidate. `1.0.0` is Stable.
- After `1.0.0`, compatible features use minor versions, fixes use patches, and
  breaking changes require a new major version.

Unknown durable state is never migrated by guesswork. A release must provide a
tested migration or refuse safely with an exact recovery action.

## Promotion evidence

Beta requires three independent supported Linux VPS installations, at least 20
real deploy/restore/recovery operations, all documented failure drills, and 30
consecutive days without an unresolved P0/P1 issue, data loss, secret leakage,
or unexplained `recovery_required` result.

`1.0.0rc1` requires frozen public contracts and a proven upgrade from the latest
`0.x`. Stable requires a 30-day RC soak across at least three production
installations, the complete safety gate, and no open P0/P1 or high-severity
security issue. P0 includes data loss, an unsafe production mutation, secret
exposure, or unrecoverable state. P1 includes broken backup verification,
rollback, restore, locking, recovery, or clean installation.

Evidence is collected manually, with user consent, and stored only in redacted
release checklists. DployDB adds no telemetry.

## Prepare a release

1. Update the version, Alpha/Beta/Stable classifier, changelog, compatibility
   guidance, and implementation-plan evidence in one release pull request.
2. Run the focused release-readiness tests and the complete validation commands
   from [CONTRIBUTING.md](CONTRIBUTING.md).
3. Build fresh artifacts with `uv build`, validate them with
   `scripts/verify_distribution.py`, run the isolated pipx audit, and run the
   clean-Linux gate. Review the wheel and source archive contents.
4. Merge only after required CI passes. Confirm the exact commit is present on
   `origin/main` and no critical issue is open.
5. Create an annotated SSH-signed tag such as `v0.1.0`, push only the tag, and
   confirm GitHub reports it as verified.

## Automated publication

The tag workflow builds the artifacts once, creates checksums and provenance,
and leaves a draft GitHub prerelease. A RecursiveWay maintainer approves the
`testpypi` environment first. After TestPyPI installation passes, a maintainer
approves the `pypi` environment. Both registries use OIDC Trusted Publishing;
long-lived upload tokens are prohibited.

After PyPI installation succeeds, the workflow publishes the GitHub prerelease
and attaches the same wheel, source archive, and `SHA256SUMS`. The release is
complete only when GitHub and both registry pages are independently verified.

If TestPyPI or PyPI publication fails, do not retag or reuse a version with
different bytes. Keep the GitHub release as a draft, diagnose the failure, and
rerun the immutable tag workflow when safe. PyPI files cannot be replaced or
rolled back; a bad public release must be yanked and superseded by a new patch.

If a tag-triggered run fails before artifacts are built because the runner did
not materialize the annotated tag object, preserve the verified tag. Fix the
workflow through a protected pull request, merge it to `main`, and manually
dispatch `Publish release` from `main` with the existing canonical tag. The
recovery path explicitly fetches that remote tag object and checks out the
immutable tagged source; it must not be used to move or recreate a tag. The
normal protected TestPyPI and PyPI approvals still apply.

## One-time repository setup

- Make the repository public, enable Issues, private vulnerability reporting,
  secret scanning, push protection, and Dependabot alerts.
- Protect `main`, require CI, prohibit force pushes/deletion, and restrict `v*`
  tag creation/deletion to maintainers.
- Create protected `testpypi` and `pypi` environments with a required reviewer.
- Register matching pending publishers on TestPyPI and PyPI for GitHub owner
  `recursiveway`, repository `dployDB`, workflow `release.yml`, and the exact
  environment name. A pending publisher does not reserve the package name.
- Register the dedicated Ed25519 public key as a GitHub signing key and configure
  the local repository to SSH-sign release tags.
