"""
makepkg_flags.py — makepkg flag-string manipulation

Pure transforms over makepkg's compiler/linker flag strings (CFLAGS / CXXFLAGS
/ LDFLAGS / RUSTFLAGS) and the ``makepkg_flags`` invocation string.  No I/O, no
subprocess — every function takes a string (or list) and returns a transformed
string (or list), optionally reporting what it changed under the ``[FLAG]`` tag.

Owns the ``[FLAG]`` log tag.  Consumed by the conf-emission layer
(``makepkg_conf.emit_makepkg_conf``) and re-exported from ``makepkg_wrapper``
for the CLI/update call sites that expand the raw flags string.
"""
from sysforge import log

_flag_log = log.get_logger("FLAG")

# makepkg's install flags. Used to detect when a build invocation will hand
# the artifact to pacman -U. Both _invoke_with_retry (for sudo-timeout
# recovery) and cli._cmd_build (for packages.toml auto-tracking) consult
# this set. Kept in sync with pacman.BATCH_STRIP_FLAGS, which strips the
# same flags during update/build batch runs.
INSTALL_FLAGS = frozenset({"-i", "--install"})

# makepkg's dependency-sync flags. Stripped whenever sysforge has already
# satisfied deps itself and must stop makepkg from invoking ``sudo pacman -S``:
# the update/build batch path (via pacman.BATCH_STRIP_FLAGS) pre-installs
# repo makedeps in one shot, and the toolchain stage's staged-deps passes
# satisfy ``llvm=<ver>`` from a stage prefix that isn't published anywhere.
SYNC_FLAGS = frozenset({"--syncdeps", "-s"})


def expand_makepkg_flags(flags_str) -> list:
    """
    Split a makepkg flags string into a list of individual flags,
    expanding combined short flags like '-sfci' into ['-s', '-f', '-c', '-i'].
    Long flags (--noconfirm) and flags with values are passed through as-is.
    """
    if not flags_str:
        return []
    result = []
    for token in flags_str.split():
        if token.startswith("--"):
            result.append(token)
        elif token.startswith("-") and len(token) > 2:
            # Combined short flags e.g. -sfci → -s -f -c -i
            result.extend(f"-{ch}" for ch in token[1:])
        else:
            result.append(token)
    return result


# Flags that are lld-specific and must be stripped when lld is not the active linker.
# These may appear as bare tokens in LDFLAGS or as sub-tokens inside -Wl,... sequences.
_LLD_ONLY_FLAGS = frozenset({
    "--icf=all",
    "--icf=safe",
    "--icf=none",
})

# Full LTO flags incompatible with clang IR PGO. ThinLTO (-flto=thin) is
# compatible and must NOT be stripped. Bare -flto defaults to full in clang.
_FULL_LTO_FLAGS = frozenset({"-flto", "-flto=full", "-flto=auto"})


def _detect_linker_from_ldflags(ldflags_val):
    """Return the linker name declared by -fuse-ld=X in LDFLAGS, or None."""
    for token in ldflags_val.split():
        if token.startswith("-fuse-ld="):
            return token[len("-fuse-ld="):]
    return None


def _detect_linker_from_rustflags(rustflags_val):
    """Return the linker name declared by -C link-arg=-fuse-ld=X in RUSTFLAGS, or None."""
    tokens = rustflags_val.split()
    for i, token in enumerate(tokens):
        # Handle both "-C link-arg=-fuse-ld=X" (two tokens) and
        # "-Clink-arg=-fuse-ld=X" (single token)
        if token == "-C" and i + 1 < len(tokens):
            arg = tokens[i + 1]
            if arg.startswith("link-arg=-fuse-ld="):
                return arg[len("link-arg=-fuse-ld="):]
        elif token.startswith("-Clink-arg=-fuse-ld="):
            return token[len("-Clink-arg=-fuse-ld="):]
    return None


def _replace_rustflags_linker(rustflags_val, new_linker):
    """Replace -C link-arg=-fuse-ld=X in RUSTFLAGS with a new linker."""
    tokens = rustflags_val.split()
    out = []
    i = 0
    while i < len(tokens):
        if tokens[i] == "-C" and i + 1 < len(tokens) and tokens[i + 1].startswith("link-arg=-fuse-ld="):
            out.append("-C")
            out.append(f"link-arg=-fuse-ld={new_linker}")
            i += 2
        elif tokens[i].startswith("-Clink-arg=-fuse-ld="):
            out.append(f"-Clink-arg=-fuse-ld={new_linker}")
            i += 1
        else:
            out.append(tokens[i])
            i += 1
    return " ".join(out)


def _inject_linker(ldflags_val, linker_name):
    """
    Replace an existing -fuse-ld=X token in LDFLAGS, or prepend one if absent.
    Returns the updated LDFLAGS string.
    """
    new_token = f"-fuse-ld={linker_name}"
    tokens = ldflags_val.split() if ldflags_val else []
    for i, t in enumerate(tokens):
        if t.startswith("-fuse-ld="):
            if tokens[i] != new_token:
                _flag_log.info(f"Replaced {tokens[i]} with {new_token} (--ld override)")
            tokens[i] = new_token
            return " ".join(tokens)
    _flag_log.info(f"Injected {new_token} into LDFLAGS (--ld override)")
    return " ".join([new_token] + tokens)


def _strip_full_lto(flags_val: str) -> tuple[str, list[str]]:
    """
    Remove full-LTO flags from a compiler flag string.
    ThinLTO (-flto=thin) is preserved — it is compatible with clang IR PGO.
    Returns (cleaned_str, list_of_stripped_tokens).
    """
    stripped = []
    kept = []
    for token in flags_val.split():
        if token in _FULL_LTO_FLAGS:
            stripped.append(token)
        else:
            kept.append(token)
    return " ".join(kept), stripped


def _strip_lld_flags(ldflags_val):
    """
    Remove lld-specific flags from an LDFLAGS string.
    Handles bare tokens (--icf=all) and flags embedded in -Wl,... sequences.
    Returns (cleaned_str, list_of_stripped_tokens).
    """
    stripped = []
    out_tokens = []
    for token in ldflags_val.split():
        if token.startswith("-Wl,"):
            subtokens = token[4:].split(",")
            kept = []
            for sub in subtokens:
                if sub in _LLD_ONLY_FLAGS:
                    stripped.append(sub)
                else:
                    kept.append(sub)
            if kept:
                out_tokens.append("-Wl," + ",".join(kept))
        elif token in _LLD_ONLY_FLAGS:
            stripped.append(token)
        else:
            out_tokens.append(token)
    return " ".join(out_tokens), stripped


# -march values that are host-CPU-specific or 64-bit-only ISA levels and
# therefore invalid for an i686 (lib32-*) build. ``native`` resolves to the
# host's amd64 microarch (e.g. znver3); ``x86-64`` and its v2/v3/v4 levels
# are defined only for 64-bit code generation.
_LIB32_INVALID_MARCH = frozenset({
    "-march=native",
    "-march=x86-64",
    "-march=x86-64-v2",
    "-march=x86-64-v3",
    "-march=x86-64-v4",
})


def _scrub_lib32_arch_flags(flags_val: str) -> tuple[str, list[str]]:
    """Strip 64-bit-only ``-march=`` tokens from a CFLAGS-style string.

    Returns ``(cleaned, stripped_tokens)``. Non-``-march`` tokens, and
    ``-march=`` values not in the invalid set (e.g. ``-march=i686``), are
    preserved verbatim. Used only for lib32-* builds.
    """
    stripped: list[str] = []
    out_tokens: list[str] = []
    for token in flags_val.split():
        if token in _LIB32_INVALID_MARCH:
            stripped.append(token)
        else:
            out_tokens.append(token)
    return " ".join(out_tokens), stripped
