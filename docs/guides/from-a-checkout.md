# Running SysForge from a Git Checkout

For a quick trial or a dev environment, without going through the AUR.

---

## Install from the checkout

```bash
make dev-install    # needs sudo
```

This installs an editable venv entry point and symlinks the repo's config, hooks, completions,
and manpage into their real system paths (mirroring where the packaged install would put them).
It's verbose about what it links, idempotent to re-run, and fully reversible:

```bash
make dev-uninstall
```

Because it links into the real system paths, remove any concurrently-installed `sysforge`
package first. If a real (non-symlink) file already exists at a target path, `dev-install` skips
it rather than overwriting it.

---

## Tab completion

The package and `make dev-install` both install the zsh and bash completions. To install them by
hand from the repo root:

```bash
# zsh, permanent
sudo cp completions/_sysforge /usr/share/zsh/site-functions/

# bash, permanent
sudo cp completions/sysforge.bash /usr/share/bash-completion/completions/sysforge
```

For the current session only, from the repo root: `fpath=($(pwd)/completions $fpath) && compinit`
(zsh) or `source completions/sysforge.bash` (bash). Both complete subcommands, flags, and package
names.
