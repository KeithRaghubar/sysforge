# SysForge

SysForge is an AUR helper that builds and tracks packages tuned to your CPU. Every package it builds, from the AUR or your own PKGBUILDs, gets `-march=native`, LTO, or whichever rule-based compiler-flag profile matches its PKGBUILD. It can go further with profile-guided optimization (PGO): per package, for the kernel (AutoFDO and Propeller), and for its own LLVM/Clang toolchain, which it can also rewrite with BOLT. It records what it built and with which flags, so `sysforge update` can tell when a package's source, flags or toolchain have moved on. Pacman owns the package database; SysForge owns the build configuration layer above it.

![On a fresh install, sysforge build cbonsai -si reviews the AUR package's PKGBUILD, builds it with -march=native and installs it; sysforge update then finds nothing to rebuild](docs/assets/quickstart.gif)

<sub><!--version-->v4.0.0<!--/version--></sub>

---

## Quick start

Install from the AUR, run the one-time setup, then build. Anything you build is maintained from
then on: `sysforge update` rebuilds it as upstream moves, repo packages included.

```bash
git clone https://aur.archlinux.org/sysforge.git && cd sysforge && makepkg -si
sudo sysforge run reconfigure && sudo sysforge run hardware   # one-time setup
sysforge build neovim-git -si                                 # build and install with your profile
sysforge update                                               # later: rebuild what has moved on
```

[Getting started](docs/guides/getting-started.md) walks through each step: requirements, choosing
gcc or LLVM, stopping maintenance of a package, reviewing changed sources and the `setup` presets.

---

## Everyday use

`build`, `update` and `resolve` cover most days; `doctor`, `log` and `state` explain what
happened, and most read-only commands take `--json` for scripts.
[Everyday commands](docs/guides/commands.md) lists them by task, along with how SysForge fits
alongside `makepkg` and `pacman`, freezing source downloads and turning the AUR off.

---

## Guides

Each guide covers one feature, step by step:

- [Drift and rebuilds](docs/guides/drift.md): what `update` rebuilds on its own after a profile or
  compiler change, and what it only reports.
- [Profile-guided optimization](docs/guides/pgo.md): package PGO, toolchain PGO, and kernel
  AutoFDO/Propeller, including how to collect a profile that actually helps.
- [Sandboxed builds](docs/guides/sandbox.md): running PKGBUILDs in a clean chroot instead of as you.
- [Freezing sources](docs/guides/freeze.md): `--frozen`, `--thaw`, reviewing changed sources and
  PGP key fetches.
- [Custom kernels](docs/guides/kernel.md) and [boot entries](docs/guides/boot-entries.md):
  `sysforge run kernel` from config review to boot-safety checks, and the loader entries it writes.

The [concept pages](docs/concepts/) explain the techniques behind them, independent of SysForge:
how an Arch package is built, compiler optimization, PGO and post-link optimization, compiler
bootstrapping, shared libraries and ABI, kernel and boot basics, and measuring performance. The
[glossary](docs/concepts/glossary.md) defines the terms they share.

---

## What SysForge touches

SysForge installs packages and edits a few system files, always through `sudo` and only for the
command that needs it. [What SysForge touches](docs/guides/what-sysforge-touches.md) lists every
file it writes, every root command it runs, and how to undo it, for one package or entirely.

---

## Fresh install from the Arch ISO

SysForge can also install a whole system from the Arch ISO, then build everything in your package
list with your profile. [Fresh install](docs/guides/fresh-install.md) covers it from connecting
to the network to the first boot.

---

## About

SysForge started as a tool to streamline its author's own Arch Linux build and maintenance workflow on a daily-driver machine, and grew into a general-purpose AUR helper. The codebase is developed essentially end-to-end with Claude Code, but it is deliberately structured to stay hand-editable: the modular design docs under `docs/design/` are the source of truth, every module has a documented owner and API, and the full test suite gates every change. Planned features live in [ROADMAP.md](ROADMAP.md).

Issues are the best channel for bug reports and feedback; pull requests are welcome too. See
[CONTRIBUTING.md](CONTRIBUTING.md), or [run SysForge from a git checkout](docs/guides/from-a-checkout.md)
to try changes without the AUR.

Releases are GPG-signed; [Verifying releases](docs/guides/verifying-releases.md) shows how to
check one. SysForge follows XDG, FHS, SemVer, Keep a Changelog and REUSE, among others; the full
list with how each is enforced is in [DESIGN.md](DESIGN.md) (§Standards & Specifications).

MIT licensed. See [LICENSE](LICENSE).
