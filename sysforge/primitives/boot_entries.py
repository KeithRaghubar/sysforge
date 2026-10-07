# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
boot_entries.py — sysforge-managed systemd-boot loader entries (3.3.0-F5)

The one home for reading and writing Boot Loader Specification Type #1 entries
(``/boot/loader/entries/*.conf``). sysforge writes one entry per kernel it builds,
cloned from the operator's proven entry, and **never modifies, renames, or deletes
an entry it did not write** — operator entries are read only as a *template* and
as *coverage*. Ownership is two-factor: a ``sysforge-*.conf`` / ``*-sysforge-*.conf``
name **and** a ``# sysforge-managed`` first line.

Pure helpers (parse/render/plan) are separate from the side-effecting apply layer
so Gate 3, doctor and ``state boot-entries`` share the parser.
"""
import contextlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from sysforge.primitives import os_release
from sysforge.primitives.privilege import privileged_argv

MANAGED_HEADER_PREFIX = "# sysforge-managed"
ROLES = ("plain", "profiling", "autofdo", "propeller")
_ROLE_TITLES = {
    "plain": "sysforge",
    "profiling": "sysforge, profiling",
    "autofdo": "sysforge, AutoFDO",
    "propeller": "sysforge, AutoFDO + Propeller",
}
_HEADER_RE = re.compile(
    r"^# sysforge-managed \(template: (?P<template>.*?), role: (?P<role>[a-z]+)\)"
)
_PREFIX_RE = re.compile(r"^[A-Za-z0-9_]+$")
_SYSFORGE_NAME_RE = re.compile(r"^(?:[A-Za-z0-9_]+-)?sysforge-.+\.conf$")
# Keys copied verbatim from the template (besides options/initrd handled below).
_COPIED_KEYS = ("machine-id", "architecture")


@dataclass(frozen=True)
class LoaderEntry:
    filename: str
    lines: tuple[tuple[str, str], ...]
    header: str | None

    def get(self, key: str) -> str | None:
        for k, v in self.lines:
            if k == key:
                return v
        return None

    def get_all(self, key: str) -> list[str]:
        return [v for k, v in self.lines if k == key]

    @property
    def linux_basename(self) -> str | None:
        linux = self.get("linux")
        return linux.rsplit("/", 1)[-1] if linux else None


@dataclass(frozen=True)
class ManagedMeta:
    template: str | None
    role: str


def parse_entry(filename: str, text: str) -> LoaderEntry:
    """Parse a Type #1 entry: ``key value`` lines, whitespace-separated, ``#`` comments."""
    text = text.lstrip("﻿")
    header = None
    lines: list[tuple[str, str]] = []
    for i, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if i == 0:
                header = line
            continue
        parts = line.split(None, 1)  # key, then the rest after the first whitespace run
        lines.append((parts[0], parts[1].strip() if len(parts) > 1 else ""))
    return LoaderEntry(filename=filename, lines=tuple(lines), header=header)


def parse_managed_header(header: str | None) -> ManagedMeta | None:
    if not header:
        return None
    m = _HEADER_RE.match(header)
    if not m:
        return None
    return ManagedMeta(template=m["template"] or None, role=m["role"])


def is_sysforge_name(filename: str) -> bool:
    return bool(_SYSFORGE_NAME_RE.match(filename))


def is_managed(entry: LoaderEntry) -> bool:
    return is_sysforge_name(entry.filename) and parse_managed_header(entry.header) is not None


def validate_prefix(prefix: str | None) -> None:
    if prefix is None:
        return
    if not _PREFIX_RE.fullmatch(prefix):
        raise ValueError(
            f"boot_entries_prefix {prefix!r} must match [A-Za-z0-9_]+ "
            "(no '-', '/', '.', or spaces)"
        )


def entry_filename(kernel: str, prefix: str | None) -> str:
    return f"{prefix + '-' if prefix else ''}sysforge-{kernel}.conf"


def title_for(pretty_name: str, role: str, kernel: str) -> str:
    return f"{pretty_name} ({_ROLE_TITLES.get(role, f'sysforge, {kernel}')})"


def boots_kernel(entry: LoaderEntry, kernel: str) -> bool:
    return entry.linux_basename == f"vmlinuz-{kernel}"


def _swap_basename(path: str, new_base: str) -> str:
    head, sep, _ = path.rpartition("/")
    return f"{head}{sep}{new_base}"


def render_entry(
    *,
    template: LoaderEntry,
    template_kernel: str,
    kernel: str,
    role: str,
    title: str,
    version: str | None,
) -> str:
    """Render a managed entry for ``kernel`` from the operator's ``template``.

    sort-key + machine-id are copied when present; ``version`` is written only when
    the template carries a sort-key (systemd-boot orders sort-key groups by version;
    without one, filenames order the menu and version is unused).
    """
    sort_key = template.get("sort-key")
    out = [
        f"{MANAGED_HEADER_PREFIX} (template: {template.filename}, role: {role}) — "
        "regenerated on every kernel build; edit the template instead",
        f"title {title}",
    ]
    if sort_key:
        out.append(f"sort-key {sort_key}")
        if version:
            out.append(f"version {version}")
    for key in _COPIED_KEYS:
        if (val := template.get(key)):
            out.append(f"{key} {val}")
    out.append(f"linux {_swap_basename(template.get('linux') or '/', f'vmlinuz-{kernel}')}")
    old_initramfs = {
        f"initramfs-{template_kernel}.img",
        f"initramfs-{template_kernel}-fallback.img",
    }
    for initrd in template.get_all("initrd"):
        if initrd.rsplit("/", 1)[-1] in old_initramfs:
            initrd = _swap_basename(initrd, f"initramfs-{kernel}.img")
        out.append(f"initrd {initrd}")
    for opt in template.get_all("options"):
        out.append(f"options {opt}")
    return "\n".join(out) + "\n"


EFI_SELECTED_VAR = Path(
    "/sys/firmware/efi/efivars/LoaderEntrySelected-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"
)


class BootEntryError(Exception):
    """Managed entries cannot be produced (no template, wrong partition, unreadable dir)."""


@dataclass(frozen=True)
class Template:
    entry: LoaderEntry
    kernel: str


@dataclass(frozen=True)
class WantedKernel:
    kernel: str
    role: str
    version: str | None


@dataclass(frozen=True)
class SyncPlan:
    writes: tuple[tuple[str, str], ...] = ()
    deletes: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    info: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


def read_selected_entry(path: Path | None = None) -> str | None:
    """The entry id systemd-boot booted (4-byte attribute header, UTF-16LE, NUL-terminated)."""
    path = path if path is not None else EFI_SELECTED_VAR
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    text = raw[4:].decode("utf-16-le", errors="ignore").split("\x00", 1)[0].strip()
    return text or None


def read_pretty_name(path: Path | None = None) -> str:
    # Use the centralized os_release module for all os-release reading.
    # Pass a single-entry paths tuple for testing, or None for production.
    paths = (path,) if path is not None else None
    ident = os_release.identify(paths)
    return ident.pretty_name or "Linux"


def load_entries(entries_dir: Path) -> list[LoaderEntry]:
    """Every ``*.conf`` in ``entries_dir`` (sorted). Raises OSError if unreadable."""
    return [
        parse_entry(p.name, p.read_text(encoding="utf-8", errors="replace"))
        for p in sorted(entries_dir.iterdir())  # iterdir raises on a missing dir; glob would not
        if p.name.endswith(".conf") and p.is_file()
    ]


def _usable(entry: LoaderEntry | None) -> TypeGuard[LoaderEntry]:
    return bool(entry and entry.get("linux") and not entry.get("efi"))


def _as_template(entry: LoaderEntry) -> Template:
    basename = entry.linux_basename or ""
    return Template(entry, basename.removeprefix("vmlinuz-"))


def resolve_template(
    entries: list[LoaderEntry], *, selected: str | None, pkgname: str
) -> Template:
    """Spec §B: selected entry (following a managed entry's template), then the
    operator entry booting ``vmlinuz-<pkgname>``, then ``vmlinuz-linux``."""
    by_name = {e.filename: e for e in entries}
    sel = by_name.get(selected) if selected else None
    if sel is not None and is_managed(sel):
        meta = parse_managed_header(sel.header)
        origin = by_name.get(meta.template) if meta and meta.template else None
        sel = origin if _usable(origin) and not is_managed(origin) else sel
    if _usable(sel):
        return _as_template(sel)
    operator = [e for e in entries if not is_managed(e) and _usable(e)]
    for kernel in (pkgname, "linux"):
        for e in operator:
            if boots_kernel(e, kernel):
                return _as_template(e)
    raise BootEntryError(
        "no usable loader entry to clone — write one loader entry for a working "
        'kernel in /boot/loader/entries, or set boot_entries = "off" in kernel.toml'
    )


def _managed_kernel(entry: LoaderEntry) -> str | None:
    b = entry.linux_basename
    return b.removeprefix("vmlinuz-") if b else None


def plan_prune(entries: list[LoaderEntry], existing_images: set[str]) -> SyncPlan:
    deletes = tuple(
        e.filename
        for e in entries
        if is_managed(e) and _managed_kernel(e) not in existing_images
    )
    return SyncPlan(deletes=deletes)


def missing_references(rendered: str, kernel: str, existing_images: set[str]) -> list[str]:
    """Paths in a rendered entry's ``linux``/``initrd`` lines that would not resolve.

    A path is valid when it exists under ``BOOT_DIR``, or it is the target kernel's own
    root-level image and the kernel is in ``existing_images`` (dry-run: not installed yet).
    """
    own = {f"/vmlinuz-{kernel}", f"/initramfs-{kernel}.img"} if kernel in existing_images else set()
    entry = parse_entry("", rendered)
    paths = [v for k, v in entry.lines if k in ("linux", "initrd")]
    return [p for p in paths if p not in own and not (BOOT_DIR / p.lstrip("/")).exists()]


def effective_prefix(template: Template, prefix: str | None) -> str | None:
    """The filename prefix actually used: a sort-key template ignores the prefix."""
    return None if template.entry.get("sort-key") else prefix


def plan_sync(
    entries: list[LoaderEntry],
    *,
    template: Template,
    wanted: list[WantedKernel],
    existing_images: set[str],
    prefix: str | None,
    pretty_name: str,
) -> SyncPlan:
    managed = [e for e in entries if is_managed(e)]
    operator = [e for e in entries if not is_managed(e)]
    info: list[str] = []
    if template.entry.get("sort-key") and prefix:
        info.append(
            "boot_entries_prefix ignored: your entries use sort-key "
            "(sysforge copies the sort-key instead)"
        )
    prefix = effective_prefix(template, prefix)

    # kernel -> (role, version): wanted kernels win over re-rendered ones.
    targets: dict[str, tuple[str, str | None]] = {}
    for e in managed:
        k = _managed_kernel(e)
        if k in existing_images:
            meta = parse_managed_header(e.header)
            if meta is not None:
                targets[k] = (meta.role, e.get("version"))
    for w in wanted:
        targets[w.kernel] = (
            w.role,
            w.version or targets.get(w.kernel, (None, None))[1],
        )

    writes: list[tuple[str, str]] = []
    deletes: list[str] = list(plan_prune(entries, existing_images).deletes)
    notes: list[str] = []
    skipped: list[str] = []
    for kernel, (role, version) in sorted(targets.items()):
        ours = [e.filename for e in managed if _managed_kernel(e) == kernel]
        if any(boots_kernel(e, kernel) for e in operator):
            for name in ours:
                deletes.append(name)
                notes.append(
                    f"removed sysforge entry {name}: your own entry already boots {kernel}"
                )
            continue
        name = entry_filename(kernel, prefix)
        if any(e.filename == name and not is_managed(e) for e in entries):
            notes.append(
                f"cannot write {name}: an entry you own already has that name "
                "— rename it, or set boot_entries_prefix"
            )
            skipped.append(kernel)
            continue
        rendered = render_entry(
            template=template.entry,
            template_kernel=template.kernel,
            kernel=kernel,
            role=role,
            title=title_for(pretty_name, role, kernel),
            version=version,
        )
        missing = missing_references(rendered, kernel, existing_images)
        if missing:
            notes.append(
                f"cannot write {name}: it would reference {', '.join(missing)}, "
                "which do not exist under /boot — set boot_entries = \"off\" and "
                "write this entry by hand"
            )
            skipped.append(kernel)
            continue
        writes.append((name, rendered))
        deletes.extend(n for n in ours if n != name)
    return SyncPlan(
        writes=tuple(writes),
        deletes=tuple(dict.fromkeys(deletes)),
        notes=tuple(notes),
        info=tuple(info),
        skipped=tuple(skipped),
    )


BOOT_DIR = Path("/boot")


def entries_dir() -> Path:
    return BOOT_DIR / "loader" / "entries"


def existing_images(boot_dir: Path | None = None) -> set[str]:
    d = boot_dir if boot_dir is not None else BOOT_DIR
    try:
        return {p.name.removeprefix("vmlinuz-") for p in d.glob("vmlinuz-*")}
    except OSError:
        return set()


def _run(*args, **kwargs):
    """Indirection so tests can intercept every subprocess boot_entries issues."""
    return subprocess.run(*args, **kwargs)  # noqa: TID251 — this module's single test-intercept seam; callers pass privileged argv


def check_boot_path(run=None) -> None:
    """Classic entries load kernels from their own partition: require $BOOT == /boot."""
    run = run or _run
    r = run(["bootctl", "--print-boot-path"], capture_output=True, text=True)
    path = (r.stdout or "").strip()
    if r.returncode != 0 or not path:
        raise BootEntryError(
            f"bootctl could not locate the boot partition ({(r.stderr or '').strip()}) — "
            'mount it, or set boot_entries = "off" in kernel.toml'
        )
    if Path(path) != BOOT_DIR:
        raise BootEntryError(
            f"systemd-boot reads entries from {path}, but kernels install to {BOOT_DIR}; "
            "managed entries cannot reference them — set boot_entries = \"off\" in "
            "kernel.toml (UKI / kernel-install setups manage their own entries)"
        )


def _priv(run, argv: list[str], name: str) -> None:
    r = run(privileged_argv(argv), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"[KERNEL] boot entry {name}: `{' '.join(argv)}` failed "
            f"({(r.stderr or '').strip() or f'exit {r.returncode}'})"
        )


def apply_plan(plan: SyncPlan, *, dry_run: bool, run=None) -> list[str]:
    """Perform ``plan`` atomically (cp -> .tmp -> mv). Deletes only managed files."""
    run = run or _run
    d = entries_dir()
    shown: list[str] = []
    # Pure-read ownership checks, before ANY mutation (also in dry-run).
    for name in plan.deletes:
        path = d / name
        if path.exists():
            current = parse_entry(name, path.read_text(encoding="utf-8", errors="replace"))
            if not is_managed(current):
                raise RuntimeError(f"[KERNEL] refusing to remove {name}: not sysforge-managed")
    for name, _content in plan.writes:
        path = d / name
        if path.exists():
            current = parse_entry(name, path.read_text(encoding="utf-8", errors="replace"))
            if not is_managed(current):
                raise RuntimeError(f"[KERNEL] refusing to overwrite {name}: not sysforge-managed")
    for name, content in plan.writes:
        if dry_run:
            shown.append(f"[dry-run] would write {name}:")
            shown.extend(f"    {line}" for line in content.splitlines())
            continue
        fd, tmp = tempfile.mkstemp(prefix="sysforge-entry-", suffix=".conf")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            Path(tmp).chmod(0o644)
            dest = d / name
            _priv(run, ["cp", tmp, f"{dest}.tmp"], name)
            _priv(run, ["mv", f"{dest}.tmp", str(dest)], name)
        finally:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
    for name in plan.deletes:
        if dry_run:
            shown.append(f"[dry-run] would remove {name}")
            continue
        _priv(run, ["rm", "-f", str(d / name)], name)
    return shown


def audit(entries: list[LoaderEntry], images: set[str]) -> list[tuple[str, str, str]]:
    """Read-only findings for doctor / state listing (systemd-boot only)."""
    out: list[tuple[str, str, str]] = []
    names = {e.filename for e in entries}
    for e in entries:
        if not is_managed(e):
            continue
        kernel = _managed_kernel(e)
        if kernel not in images:
            out.append(("boot_entry_dangling",
                        f"{e.filename} boots vmlinuz-{kernel}, which is not installed",
                        "run `sysforge run kernel` (prunes it) or remove the entry"))
        meta = parse_managed_header(e.header)
        if meta and meta.template and meta.template not in names:
            out.append(("boot_entry_template_gone",
                        f"{e.filename} was cloned from {meta.template}, which no longer exists",
                        "it still boots, but won't follow future `options` edits; "
                        "rebuild the kernel to re-clone from your current entry"))
    for kernel in sorted(images):
        if not any(boots_kernel(e, kernel) for e in entries):
            out.append(("boot_entry_missing",
                        f"no loader entry boots vmlinuz-{kernel}",
                        "rebuild it with `sysforge run kernel`, or write an entry"))
    return out


def rows_for(entries: list[LoaderEntry], images: set[str]) -> list[tuple[str, str, str, str]]:
    """kernel → entry → owner → template, for `sysforge state boot-entries`."""
    rows: list[tuple[str, str, str, str]] = []
    for kernel in sorted(images):
        match = next((e for e in entries if boots_kernel(e, kernel) and not is_managed(e)), None) \
            or next((e for e in entries if boots_kernel(e, kernel)), None)
        if match is None:
            rows.append((kernel, "-", "none", "-"))
            continue
        meta = parse_managed_header(match.header) if is_managed(match) else None
        rows.append((kernel, match.filename, "sysforge" if meta else "you",
                     (meta.template or "-") if meta else "-"))
    for e in entries:
        if is_managed(e) and _managed_kernel(e) not in images:
            meta = parse_managed_header(e.header)
            rows.append(("(missing)", e.filename, "sysforge",
                         (meta.template if meta else None) or "-"))
    return rows
