# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
bench_state.py — ``<state_dir>/bench.toml``, the last bench result per target (3.4.0-F6).

The one reader and writer. A result is keyed by target (``[toolchain]``) and
carries the installed version, the build's ``built_at`` and the workload
fingerprint it was measured on, so ``is_stale`` can tell ``state profiles`` when
any of them has since moved.

The write deliberately does not go through ``atomic_write.replace_file``: that
seam escalates through the privilege seam when the direct write is refused, and
``bench`` is a read-only verb that must never prompt for sudo. A save it cannot
make raises ``OSError``, which the caller reports as a warning.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
import tomllib
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FILENAME = "bench.toml"


@dataclass(frozen=True)
class BenchResult:
    installed_version: str
    stock_version: str
    corpus: str
    fingerprint: str
    runs: int
    stock_median_s: float
    installed_median_s: float
    ratio: float | None  # None across a major-version gap
    measured_at: str
    # build_state's built_at for clang (3.4.0-B18): a rebuild with a fresh
    # profile keeps pkgver-pkgrel, so the version alone can't date the build.
    # "" for a result recorded before the field existed, which reads as stale.
    built_at: str = ""
    # bench_workload.stat_key of a configured corpus (3.4.0-B20): lets
    # ``state profiles`` skip re-hashing it while paths, sizes and mtimes match.
    corpus_key: str = ""


# What a hand-edited table must still hold for ``load`` to trust it (TOML may
# write a whole-number float as an int).
_FIELD_TYPES: dict[str, type | tuple[type, ...]] = {
    "installed_version": str, "stock_version": str, "corpus": str,
    "fingerprint": str, "runs": int, "stock_median_s": (int, float),
    "installed_median_s": (int, float), "ratio": (int, float), "measured_at": str,
    "built_at": str, "corpus_key": str,
}
# Fields an older bench.toml may lack; ``load`` keeps the dataclass default.
_OPTIONAL = frozenset({"ratio", "built_at", "corpus_key"})


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(state_dir: Path) -> dict:
    try:
        with (Path(state_dir) / FILENAME).open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _esc(value: str) -> str:
    return (value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r"))


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return f'"{_esc(str(value))}"'


def _serialize(data: dict) -> str:
    out = ["# Written by `sysforge bench`; one table per target. Safe to delete."]
    for target in sorted(data):
        out.append(f"\n[{target}]")
        for key, value in data[target].items():
            if value is not None:
                out.append(f"{key} = {_scalar(value)}")
    return "\n".join(out) + "\n"


def _write(path: Path, text: str) -> None:
    """Temp file in the destination dir, fsync, rename. Never escalates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp.chmod(0o644)
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def save(state_dir: Path, target: str, result: BenchResult) -> None:
    """Record *result* as *target*'s table, keeping every other target's.

    Raises ``OSError`` when the state dir is not writable by this user.
    """
    data = {k: v for k, v in _read(state_dir).items() if isinstance(v, dict)}
    data[target] = asdict(result)
    _write(Path(state_dir) / FILENAME, _serialize(data))


def load(state_dir: Path, target: str) -> BenchResult | None:
    """*target*'s last result; ``None`` when missing, malformed, incomplete or mistyped."""
    table = _read(state_dir).get(target)
    if not isinstance(table, dict):
        return None
    kwargs: dict[str, Any] = {f.name: table.get(f.name) for f in fields(BenchResult)}
    for key, value in list(kwargs.items()):
        if value is None and key in _OPTIONAL:
            if key != "ratio":
                del kwargs[key]
            continue
        if not isinstance(value, _FIELD_TYPES[key]) or isinstance(value, bool):
            return None
    return BenchResult(**kwargs)


def is_stale(result: BenchResult, installed_version: str | None, fingerprint: str,
             built_at: str | None) -> bool:
    """The installed build or the workload has moved since *result* was measured.

    *built_at* is build_state's timestamp for the installed clang; a result with
    none recorded is stale, since nothing ties it to the current build.
    """
    return (result.installed_version != installed_version
            or result.fingerprint != fingerprint
            or not result.built_at or result.built_at != built_at)
