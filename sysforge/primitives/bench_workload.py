# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
bench_workload.py — the compile workload ``sysforge bench toolchain`` times (3.4.0-F6).

The bench owns its TUs. It deliberately does not reuse BOLT's training TU
(``bolt._DEFAULT_WORKLOAD_TU``): the day BOLT works, Pass 5 profiles clang on
exactly that file, and a benchmark run on its own training input flatters the
result. A BOLT-tuning edit must also never silently change what the bench
measures, so the two inputs live apart. ``fingerprint`` pins a stored result to
the bytes it was measured on (``bench_state.is_stale``).
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx")
_DEFAULT_LABEL = "built-in synthetic TUs"

# Header-heavy C++: template instantiation, overload resolution and inlining
# dominate, which is where a PGO-built clang front/middle end should gain.
_CXX_TU = r"""
#include <algorithm>
#include <map>
#include <numeric>
#include <regex>
#include <string>
#include <unordered_map>
#include <vector>

template <typename K, typename V>
struct Table {
    std::map<K, std::vector<V>> rows;
    void add(const K& k, V v) { rows[k].push_back(std::move(v)); }
    template <typename F>
    auto fold(F f) const {
        std::unordered_map<K, decltype(f(std::declval<const std::vector<V>&>()))> out;
        for (const auto& [k, vs] : rows) out.emplace(k, f(vs));
        return out;
    }
};

int bench_cxx(const std::vector<std::string>& lines) {
    Table<std::string, int> t;
    std::regex word(R"(\w+)");
    for (const auto& l : lines)
        for (auto it = std::sregex_iterator(l.begin(), l.end(), word);
             it != std::sregex_iterator(); ++it)
            t.add(it->str(), static_cast<int>(it->length()));
    auto sums = t.fold([](const std::vector<int>& v) {
        return std::accumulate(v.begin(), v.end(), 0L);
    });
    std::vector<long> vals;
    for (const auto& [k, v] : sums) vals.push_back(v);
    std::sort(vals.begin(), vals.end());
    return vals.empty() ? 0 : static_cast<int>(vals.back());
}
"""

# Plain C: loop optimisation and codegen without the C++ front end.
_C_TU = r"""
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

static uint32_t mix(uint32_t h, uint32_t k) {
    k *= 0xcc9e2d51u; k = (k << 15) | (k >> 17); k *= 0x1b873593u;
    h ^= k; h = (h << 13) | (h >> 19); return h * 5u + 0xe6546b64u;
}

uint32_t bench_c(const uint8_t *data, size_t n, uint32_t seed) {
    uint32_t h = seed;
    size_t i = 0;
    for (; i + 4 <= n; i += 4) { uint32_t k; memcpy(&k, data + i, 4); h = mix(h, k); }
    uint32_t tail = 0;
    for (size_t j = 0; i + j < n; j++) tail |= (uint32_t)data[i + j] << (8 * j);
    h ^= tail; h ^= (uint32_t)n;
    h ^= h >> 16; h *= 0x85ebca6bu; h ^= h >> 13; h *= 0xc2b2ae35u; h ^= h >> 16;
    return h;
}

int cmp_u32(const void *a, const void *b) {
    uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;
    return (x > y) - (x < y);
}

void sort_hashes(uint32_t *v, size_t n) { qsort(v, n, sizeof *v, cmp_u32); }
"""

_DEFAULT_FILES = (("bench_workload.cpp", _CXX_TU), ("bench_workload.c", _C_TU))


@dataclass(frozen=True)
class Workload:
    """What the bench compiles: a display label, the sources, their fingerprint."""

    label: str
    files: tuple[Path, ...]
    fingerprint: str


def _digest(named: Sequence[tuple[str, bytes]]) -> str:
    h = hashlib.sha256()
    for name, data in sorted(named):
        h.update(name.encode("utf-8") + b"\0")
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()


def fingerprint(files: Sequence[Path]) -> str:
    """SHA256 over each file's name and bytes, order-independent."""
    return _digest([(p.name, p.read_bytes()) for p in files])


def default_fingerprint() -> str:
    """The built-in TUs' fingerprint, without writing them anywhere."""
    return _digest([(n, t.encode("utf-8")) for n, t in _DEFAULT_FILES])


def default_workload(dest_dir: Path) -> Workload:
    """Write the built-in TUs into *dest_dir* and return them as a workload."""
    files = []
    for name, text in _DEFAULT_FILES:
        path = Path(dest_dir) / name
        path.write_text(text, encoding="utf-8")
        files.append(path)
    return Workload(_DEFAULT_LABEL, tuple(sorted(files)), default_fingerprint())


def configured_workload(entries: Sequence[str]) -> Workload:
    """``toolchain.toml [bench] corpus`` → a workload. Raises ``ValueError``.

    Each entry is a source file or a directory (recursed, sorted), filtered to
    :data:`SOURCE_SUFFIXES`. A missing entry, or a corpus with no sources at
    all, is an error rather than an empty benchmark.
    """
    files: list[Path] = []
    for entry in entries:
        path = Path(entry).expanduser()
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*")
                                if p.is_file() and p.suffix in SOURCE_SUFFIXES))
        elif path.is_file():
            if path.suffix in SOURCE_SUFFIXES:
                files.append(path)
        else:
            raise ValueError(f"[bench] corpus entry {entry} does not exist")
    files = list(dict.fromkeys(files))
    if not files:
        raise ValueError(
            f"[bench] corpus {', '.join(entries)} holds no C/C++ sources "
            f"({' '.join(SOURCE_SUFFIXES)})"
        )
    return Workload(", ".join(entries), tuple(files), fingerprint(files))


def compile_argv(cc: str, cxx: str, src: Path, out: Path) -> list[str]:
    """One optimizing compile of *src* to an object (``-c``: no link)."""
    if src.suffix == ".c":
        return [cc, "-O2", "-c", str(src), "-o", str(out)]
    return [cxx, "-std=c++17", "-O2", "-c", str(src), "-o", str(out)]
