# Getting Started with SysForge

From installing SysForge to your first optimized build, and what happens to that package
afterwards. The [README](../../README.md#quick-start) has the short version.

---

## Requirements

- Arch Linux, the primary supported base. CachyOS is validated each minor release for
  packaging, dependency resolution, `makepkg.conf` handling and version comparison; other Arch
  derivatives are expected to work but are unvalidated. The bootstrap, kernel-staging and
  graphics/DKMS features are validated on Arch only. `sysforge doctor system --distro` reports
  which tier the machine you are on falls in.
- Python 3.11+, plus `makepkg`, `pacman` and `sudo`.
- No other AUR helper: SysForge handles AUR builds directly.

SysForge's writable runtime directories (`/var/lib/sysforge`, `/var/cache/sysforge`) are owned by
the `sysforge` group. The package install creates the group; the first privileged run also adds
your user to it. Group membership only takes effect after you log out and back in. Until then
SysForge gives your user access to those directories directly (an ACL), so nothing breaks; you
may just be prompted for `sudo` once more than usual.

---

## The five steps

```bash
# 1. Install from the AUR
git clone https://aur.archlinux.org/sysforge.git
cd sysforge && makepkg -si

# 2. One-time bootstrap (sysforge reminds you until both complete)
sudo sysforge run reconfigure   # adopt system/toolchain defaults
sudo sysforge run hardware      # detect CPU/GPU, write the hardware profile

# 3. Edit the shipped config (self-documented)
sudo vim /etc/sysforge/profiles.toml

# 4. Build and install a package with your active profile
#    (bare makepkg short flags pass straight through)
sysforge build neovim-git -si

# 5. Rebuild outdated installed AUR packages
sysforge update
sysforge update --devel       # VCS packages too
sysforge update --dry-run     # preview only
sysforge update --sysupgrade  # ...then one `pacman -Syu` for the repo packages
```

Before step 1, [What SysForge touches](what-sysforge-touches.md) lists every file it writes,
when it uses `sudo`, and how to undo it.

---

## Choosing a compiler

The default build profile uses the system gcc. LLVM (clang/lld) is fully supported but opt-in:
install the LLVM `optdepends` (`clang`, which pulls in `llvm` and `compiler-rt`, and `lld`) and
set `toolchain = "llvm"` in `[defaults]` or a profile. To build sysforge's own optimized LLVM,
set `compiler = "llvm"` in `toolchain.toml` and run `sysforge run toolchain`, which installs the
compilers it needs to start. The [PGO guide](pgo.md) covers that path.

---

## What happens after a build

Anything you `build` is then *maintained*: `sysforge update` rebuilds it from source as upstream
advances. This works for repo packages too: `sysforge build mesa` keeps your optimized mesa
current. The first time, `build` asks before it builds a repo package from source, and saves
your answer in `packages.toml`:

![sysforge build tree -si asks whether to build the repo package tree from source, compiles it with -march=native, and pacman -Qi then shows it in the sf-build group](../assets/repo-build.gif)

To stop maintaining one:

- `sysforge state forget <pkg>` drops it from build state, leaves the installed build alone.
- `sysforge revert-to-stock <pkg>` reinstalls the repo build over it and demotes it in one step.
- `sudo pacman -S <pkg>` works too; the next `update` reconciles it back to stock.

`forget` and `revert-to-stock` are remembered: `update` will not pick the package back up
through a `packages.toml` entry or `repo_mode = "build_from_source"` (`sysforge state list`
shows these). `sysforge build <pkg>`, `sysforge update <pkg>` or `sysforge packages add <pkg>`
takes it back.

When a profile or the compiler changes, `update` also notices packages built the old way. The
[drift guide](drift.md) explains what it rebuilds on its own and what it only reports.

---

## Reviewing and confirming

Before a changed source is built, `update` and `build` show you what changed since the last
change you accepted and ask. With no terminal (a timer, cron, a pipe) that package is refused
instead: the rest of the run still builds, the summary lists it and the run exits 1. Changes
from Arch's own packaging repos are accepted without asking. For unattended runs, set
`[build] review = "accept"` in `packages.toml` (accepted changes are listed in the summary)
or pass `--no-review`; the [freeze guide](freeze.md) explains the trade-off.

At a terminal, `update` first prints its plan (what it will rebuild and why, what it will
upgrade, and the protections in force) and asks `Proceed? [Y/n]`. `--yes`, or
`confirm_plan = false` in `packages.toml`, skips the question. `sysforge setup` asks about
these choices (confirmation, review, AUR, build sandbox) and saves the ones you change.

If you have one clear goal, `sysforge setup` first offers a preset that sets several of these at
once, showing each change before it writes anything: `performance` (the LLVM toolchain and the
`optimized` profile), `low-friction` (no plan confirmation and no source review) or `secure`
(the `hardened` flag profile, review and confirmation on, clean-chroot builds once a chroot
exists, and kernel hardening). The values it writes are ordinary settings you can change later.
`sysforge setup --preset NAME` applies one without asking.

---

## How much it prints

By default sysforge prints errors plus the primary output only; `-v`/`-vv`/`-vvv` add
warnings/info/debug. To raise that baseline permanently set `sysforge.toml [log] verbosity`
(0–3), for example `verbosity = 2` to always see progress narration. A CLI flag always wins, and
`--quiet` forces silence (verbosity 0) for a single run.

Next: [Everyday commands](commands.md).
