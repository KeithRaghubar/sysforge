## Man Pages

**Current (v1.0):** `argparse-manpage` generates `man/sysforge.1` from the argparse parser exposed via `_build_parser()` in `cli.py`. Generated during `make man` and during the PKGBUILD `build()` step (requires `python-argparse-manpage` makedepend). The generated page is checked into git so AUR-built tarballs ship with it without requiring AUR users to have `argparse-manpage` installed at unpack time; the release flow regenerates it via `make man` and commits the result alongside the version bump. Makefile target: `make man`.

**v1.0 planned migration — scdoc hybrid:**

Replace the auto-generated page with a hand-written scdoc template (`man/sysforge.1.scd.in`) covering SYNOPSIS, DESCRIPTION, FILES, EXAMPLES, and SEE ALSO, with OPTIONS sections auto-generated from the argparse parser by a small script (`tools/gen_options.py`) that walks `parser._subparsers` and emits scdoc-formatted option blocks. The Makefile combines them:

```
tools/gen_options.py → man/sysforge.1.scd.gen
sed -f .gen .scd.in  → man/sysforge.1.scd
scdoc               → man/sysforge.1
```

This gives hand-crafted prose with OPTIONS that stay automatically in sync with the CLI. `scdoc` becomes a makedepend; `python-argparse-manpage` is dropped.

---

