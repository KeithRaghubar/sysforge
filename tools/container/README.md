# SysForge Container Test Tier

A throwaway container that installs a locally-built sysforge package and asserts
its post-install packaging and portability invariants. Answers in seconds what
the [VM tier](../vm/README.md) needs a boot and a snapshot for.

Parameterized by distro. The `cachyos` arm is the point of the tier: it is the
only place that exercises what a same-distro test cannot.

## What it covers

The checks live in `tools/smoke.sh` — one copy, shared with `make vm-smoke`. That
script gathers every fact in a single remote invocation and evaluates them on the
host, so the transport (`ssh` → `podman exec`) is substitutable without touching
a check.

**Packaging integrity** (identical on both arms): `sysforge --version` liveness,
`pacman -Qi sysforge` registration, 3 pacman hooks, both completions, the
`tmpfiles.d` sentinel dir.

**Portability** — each one a *differential*: the ground truth read out of `/etc`
next to what sysforge itself resolved. That is why the same check is valid on
every arm without hardcoding an expected value.

| Check | Risk it covers |
|-------|----------------|
| os-release ID matches the expected distro | a mis-tagged base image silently re-testing Arch, making the arm vacuous |
| `doctor --distro` runs clean | the identity primitive is reachable and readable on this host |
| registered sync repos == every `[section]` in `pacman.conf` | repo/AUR shadowing — a derivative carries extra sync DBs ahead of `core`/`extra`, and a hardcoded repo list breaks the repo-vs-AUR makedep split |
| system `makepkg.conf` CFLAGS parsed verbatim | the merge baseline — a derivative's `-march=x86-64-v3`/LTO defaults must survive, never be replaced by a vendored default |
| version compare on a live `pkgver` | already-built fingerprints against bumped `pkgrel`s on core packages |

## What it does NOT cover

Left to the VM tier, because a container has no kernel of its own: bootstrap /
install, kernel staging, graphics and DKMS probes, restart detection. A green
container run is not a release gate on its own.

## Dependencies

```bash
make dev-deps-container    # podman
```

Rootless podman is fine — nothing here needs a privileged container. The full
dev-dependency set for every tier is `make dev-deps`; `make dev-deps-list` shows
what each tier needs and what is already installed.

## Use

```bash
# 1. Build a package from the working tree (same clean-chroot path as a release).
make vm-pkg-stable

# 2. Run the checks on the primary base.
make container-smoke

# 3. Run them on the derivative — the arm that actually tests portability.
make container-smoke-cachyos
```

Each run builds the image if needed, starts a *fresh* container, installs the
package, runs the checks, and removes the container. Fresh every time on purpose:
a re-used container carries the previous install, which would let a packaging
regression pass on leftovers.

Other targets:

```bash
make container-build                    # build the image only
make container-shell                    # install, then drop into a shell in it
make container-clean                    # remove container + image
make container-smoke DISTRO=cachyos     # any target takes DISTRO=
```

The script takes the same options directly:

```bash
./tools/container/harness.sh smoke --distro=cachyos --flavor=git --keep
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | every check passed |
| 1 | a check failed — a real packaging or portability break |
| 3 | the harness is unavailable: no podman, no network, or the base image cannot be pulled |

`3` is distinct on purpose. The tier is optional infrastructure, so a caller
(notably the release preflight) can warn on absence while still failing on a
real break.

## Flavors

| `--distro` | Base image | Expected `os-release` ID |
|------------|-----------|--------------------------|
| `arch` | `docker.io/library/archlinux:base-devel` | `arch` |
| `cachyos` | `docker.io/cachyos/cachyos:latest` | `cachyos` |

The base image *is* the parameterization. Nothing distro-specific is synthesized
in the `Containerfile` — no hand-written `os-release`, no pasted repo stanzas —
because a fixture we author would make the tier assert our own assumptions
instead of the distro's. Each arm's repos, `makepkg.conf` defaults and identity
come from that distro's own image.

Adding an arm is a row in the flavor table in `harness.sh` plus its base image.
Adding a *transport* (a remote host, another runtime) is a `reachable_<name>` /
`remote_<name>` pair in `tools/smoke.sh` and touches no check.
