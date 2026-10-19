# Source Freeze, Review and Network Policy with SysForge

How to stop sysforge from downloading new code, what the freeze covers and what it can't, how
changed sources are reviewed before they are built, and how PGP keys for signed sources are
fetched. Use the freeze when you want a run to build only
what is already on disk, for example while you review a batch of AUR changes, or on a schedule
where nothing should change behind your back.

For the internals see [DESIGN.md](../../DESIGN.md) (§Config Layer, `[security]`). This guide
only describes how to use it.

---

## Turning it on

For one run, pass `--frozen` before the command:

```bash
sysforge --frozen update
```

To make it the default, set it in `sysforge.toml`:

```toml
[security]
freeze_sources = true
```

and lift it for one run with `--no-frozen`:

```bash
sysforge --no-frozen update
```

`--frozen`, `--no-frozen` and `--thaw` are global options, so they go before the command and
apply to every verb: `update`, `build`, `run kernel`, `run toolchain` and the rest. If both are
given, `--no-frozen` wins. Run with `-v` to see the `source freeze ACTIVE` notice at startup.

---

## What the freeze refuses

While it is on, sysforge refuses every new download of code, or of the keys that decide which
code to trust:

- cloning a package from the AUR;
- checking out an official-repo package's PKGBUILD (`pkgctl repo clone`);
- fetching new commits into an existing checkout during source sync;
- probing a `-git` (or other VCS) package's upstream for a newer version (`--devel`);
- fetching a PGP key from a keyserver (see [PGP keys](#pgp-keys) below);
- downloading meson subprojects or git submodules to repair a failed build.

Checkouts already on disk still build. A package whose source would need a download is not
built. The run carries on with the rest, then lists the refused packages and exits non-zero:

```text
mesa: source freeze denied source_fetch (lift with --thaw mesa, or --no-frozen for the whole run)
...
2 package(s) refused by the source freeze: mesa, wine-git
```

The non-zero exit is deliberate. A frozen run that skipped packages did not do everything you
asked, and a script or timer should be able to tell.

---

## Letting one package through

`--thaw` lifts the freeze for the named packages and keeps it for everything else:

```bash
sysforge --frozen --thaw mesa update
sysforge --frozen --thaw mesa,wine-git update
sysforge --frozen --thaw mesa --thaw wine-git update
```

Name packages by their **pkgbase** (the name the refusal message prints). `--thaw` only narrows
a freeze that is already on. On its own it turns nothing on, and it lasts for that run only:
there is no saved list of trusted packages.

---

## What the freeze cannot cover

sysforge controls its own downloads. It does not control the ones makepkg makes while building:
the tarballs and git repos in a PKGBUILD's `source=()` array. Rather than imply a guarantee it
cannot give, sysforge lists, before each frozen build, the `source=()` entries makepkg is about
to download:

```text
mesa: 1 sources makepkg will fetch outside the gate:
    https://mesa.freedesktop.org/archive/mesa-25.2.3.tar.xz
```

Only remote entries that are not already downloaded are listed. A file shipped next to the
PKGBUILD, or one already in the PKGBUILD directory or your `SRCDEST`, needs no download and is
skipped. These lines are warnings, so run with `-v` to see them.

The list is what is left to review: check those URLs before the run if they matter to you.
The build sandbox (`[security] sandbox_builds`) does not change this, since a sandboxed build
still downloads its sources.

---

## `--frozen` versus `--offline`

They look alike but answer different questions.

| | `--offline` (`update`) | `--frozen` (global) |
| --- | --- | --- |
| Purpose | Speed: skip network work | Safety: refuse new code |
| Applies to | `update` only | every command |
| AUR version check (RPC) | skipped | still runs: it reads metadata, not code |
| A package needing a download | reported from local state | refused, and the run exits non-zero |
| PGP key fetch | not affected | refused |

Use `--offline` for a quick local report. Use `--frozen` when "nothing new gets in" has to hold.

---

## Reviewing changed sources

Before sysforge builds a package whose source changed since the last change you accepted, it
shows you the change (the whole source tree, not just the `PKGBUILD`) and asks: view the diff,
accept, skip this package, or abort the run. `update` and `build` follow the same rules.

Accepting is what counts: if the build then fails, the next run does not ask about the same
change again, only about anything newer. A run with `--no-review` accepts nothing, so the next
reviewed run shows everything that changed since your last accept.

A run that could not ask is never treated as a yes. With no terminal to ask on (a systemd
timer, cron, or output piped into `tee`), the changed package is **refused**: it is not built,
the rest of the run carries on, the summary lists it under "Refused at review", and the run
exits 1. Run `sysforge update` at a terminal to look at it.

Changes from Arch's own packaging repos are accepted without asking (logged at `-v`): the same
people publish the binary packages you already install. Change the trusted list in
`packages.toml`:

```toml
[build]
review_trusted_sources = ["repo"]   # valid: "repo", "aur", "git", "local"; [] reviews everything
```

`--review` reviews everything for one run, trusted sources included.

### Unattended runs

If you run `update` from a timer, a changed AUR package is now refused instead of being
built. To keep building unattended, choose one:

- `[build] review = "accept"`: accept every change without asking. Each one is still listed
  in the summary under "Accepted without review".
- `--no-review` on the timer's command line, or `[build] review = false`: no gate at all.

`sysforge setup` asks how to treat changed sources, whether to use the AUR and, once a
chroot exists, whether to build in the sandbox, and saves the answers you change. Its `secure`
preset turns review and plan confirmation on and, once a chroot exists, the sandbox; its
`low-friction` preset sets `review = false`, with the same risk as above.

Both options above mean code nobody looked at is built and installed. A build sandbox
(`[security] sandbox_builds`) limits what a build can read while it runs, but it does not
inspect what gets installed, and package install scripts run as root.

---

## Turning the AUR off

If you only build repo packages and your own local PKGBUILDs, you can tell sysforge never to
contact the AUR:

```toml
# sysforge.toml
[aur]
enabled = false
```

What stops:

- cloning or fetching from the AUR;
- AUR lookups: version checks, `fetch`/`build` resolving a name, dependency resolution;
- `sysforge search`'s AUR section (it prints `AUR results omitted ([aur] enabled = false)`);
- the AUR package-name list used by shell completion.

A package you track from the AUR shows in the `update` summary as
`<pkg>: AUR disabled ([aur] enabled = false), not updated`. It is left as it is and does not
count as a failure. `sysforge build <pkg>` refuses it up front. A dependency that only the AUR
has is named in a warning, and the build that needs it fails as it would for any missing
dependency. A checkout whose `origin` is somewhere other than the AUR (a GitHub repo, say)
still syncs, whatever its `source` label says.

Unlike the freeze, this is a standing choice: `--thaw` and `--no-frozen` do not lift it.
`--offline` is different again: it skips all network work for one `update`, including repo
checkouts and version checks.

Even with the AUR on, `update` only refreshes the package-name list when you track something
from the AUR.

---

## PGP keys

Some PKGBUILDs list `validpgpkeys`: the signers makepkg will accept for the downloaded sources.
That list exists so a person decides who the upstream signer is. If sysforge imported every
key a PKGBUILD names, a tampered PKGBUILD could bring its own key.

So when a listed key is missing from your keyring, sysforge fetches it into a throwaway keyring
first and shows you each key's fingerprint, its owner and the package that asked for it. It is
imported only if you confirm (the default is No). A run without a terminal, such as a timer,
imports nothing: sysforge prints the fingerprints and the `gpg --recv-keys` command to import
them by hand, and makepkg then stops at its signature check.

Keys shipped with the package (`keys/pgp/*.asc` next to the PKGBUILD) are imported without
asking.

For unattended batch runs where you accept that trade-off, restore automatic imports:

```toml
[security]
auto_fetch_pgp_keys = true
```

The keys are still shown. Either way, a keyserver fetch is a download, so the freeze refuses
it unless the package is thawed.

---

## Quick reference

| I want to... | Run or set |
| --- | --- |
| update without pulling any new code | `sysforge --frozen update` |
| freeze by default | `[security] freeze_sources = true` |
| lift a default freeze for one run | `sysforge --no-frozen update` |
| let one package through | `sysforge --frozen --thaw <pkgbase> update` |
| see what makepkg will still download | add `-v` to a frozen run |
| import PGP keys without a prompt | `[security] auto_fetch_pgp_keys = true` |
| review every changed source, trusted ones too | `sysforge update --review` |
| build changed sources unattended (listed in the summary) | `[build] review = "accept"` |
| skip review entirely | `--no-review` or `[build] review = false` |
| never contact the AUR | `[aur] enabled = false` in `sysforge.toml` |
