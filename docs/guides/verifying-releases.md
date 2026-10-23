# Verifying SysForge Releases

Releases are GPG-signed end to end with the maintainer key: the release commit, the git tag, and
the source tarball. The AUR `sysforge` package verifies the tarball signature automatically at
install time via `validpgpkeys`.

To verify by hand, import the maintainer key (fingerprint in `keys/sysforge.asc`) and check the
tag and tarball:

```bash
gpg --import keys/sysforge.asc
git tag -v vX.Y.Z
gpg --verify sysforge-X.Y.Z.tar.gz.asc sysforge-X.Y.Z.tar.gz
```

Signing applies from the first release cut after this feature landed; earlier releases were
unsigned. See [SECURITY.md](../../SECURITY.md) for the disclosure policy.
