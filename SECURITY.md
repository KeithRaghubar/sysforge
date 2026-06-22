# Security Policy

## Reporting a vulnerability

Please report security issues privately rather than in public issues or pull
requests. Use GitHub's private vulnerability reporting for this repository
(**Security → Report a vulnerability**), or the maintainer contact listed in the
`PKGBUILD` `# Maintainer:` line.

Please include enough detail to reproduce — affected version, environment, and a
proof of concept where possible. You can expect an acknowledgement and an initial
assessment of the report.

## Supported versions

Fixes target the latest released version and `main`. There is no long-term
support branch; upgrade to the newest release to receive security fixes.

## Release integrity

Releases are GPG-signed end to end with the maintainer key:

- the `release: vX.Y.Z` commit and the annotated git tag are signed;
- the source tarball ships a detached signature
  (`sysforge-X.Y.Z.tar.gz.asc`) plus `SHA256SUMS` / `SHA256SUMS.asc` on the
  GitHub release.

The AUR `sysforge` package declares the maintainer key in `validpgpkeys` and
lists the detached signature as a source, so `makepkg` verifies the maintainer
signature at install time and aborts on mismatch. To verify manually, see the
*Verifying releases* section of [README.md](README.md). The maintainer public key
(and its fingerprint) lives at [`keys/sysforge.asc`](keys/sysforge.asc).

Signing applies from the first release cut after this policy landed; earlier
releases were published unsigned.
