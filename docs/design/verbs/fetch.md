### `fetch.py`

Implements `sysforge fetch` — download one or more PKGBUILDs into `pkgbuild_src_dir` without building. Uses `find_pkgbuild` (auto-clones via `pkgctl_checkout` or `aur_clone` if not already present), then routes each already-cloned dir through `get_scheduler().request(SyncRequest(...))` for the shallow-fetch path (skipped with `--no-update`). The request is keyed on the PKGBUILD's own pkgbase (`build_prep.pkgbase_from_globals`), not the checkout dir name, so a renamed checkout keeps its `--thaw` match and VCS check (`3.5.0-B82`). Prints the resulting PKGBUILD directory path for each package. Exits 1 if any package failed, including a sync result that blocks (`FAILED`, `RATE_LIMITED`, `PURGE_REFUSED`, `FROZEN`).

Public API: `cmd_fetch(args)`.

