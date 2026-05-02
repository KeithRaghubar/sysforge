#!/usr/bin/env bash
# iso-install.sh — install SysForge on the Arch live ISO and configure bootstrap.toml
#
# Run as root from the live Arch install environment:
#   bash tools/iso-install.sh           # installs latest stable AUR 'sysforge'
#   bash tools/iso-install.sh --git     # installs AUR 'sysforge-git' (tip of main)
#
# After this script completes, run the bootstrap pipeline:
#   sysforge run pipeline --state-dir /mnt/var/lib/sysforge

set -euo pipefail

PKG="sysforge"
while [[ $# -gt 0 ]]; do
    case "$1" in
        -g|--git)
            PKG="sysforge-git"
            shift
            ;;
        -h|--help)
            sed -n '2,9p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            echo "Usage: $0 [--git]" >&2
            exit 2
            ;;
    esac
done

AUR_URL="https://aur.archlinux.org/${PKG}.git"
BUILD_USER="aurbuild"
SUDOERS_DROPIN="/etc/sudoers.d/99-${BUILD_USER}-iso-install"

# ── helpers ───────────────────────────────────────────────────────────────────

_die()    { echo "ERROR: $*" >&2; exit 1; }

# Visual primitives — kept in lockstep with sysforge/ui/headers.py so the
# shell script and the Python pipeline runner share one visual vocabulary.

_use_color() { [[ -t 1 && -z "${NO_COLOR:-}" ]]; }

_term_cols() {
    local cols=${COLUMNS:-}
    [[ -z "$cols" ]] && cols=$(tput cols 2>/dev/null || echo 80)
    (( cols < 40 ))  && cols=40
    (( cols > 100 )) && cols=100
    echo "$cols"
}

_double_rule() {
    local cols n=0 out=""
    cols=$(_term_cols)
    while (( n < cols )); do out+="═"; n=$((n + 1)); done
    if _use_color; then
        printf '\033[1m\033[36m%s\033[0m\n' "$out"
    else
        printf '%s\n' "$out"
    fi
}

_bold() {
    if _use_color; then printf '\033[1m%s\033[0m' "$1"; else printf '%s' "$1"; fi
}

_step() {
    local idx=$1 total=$2 name=$3 desc=$4
    echo
    _double_rule
    printf '  %s\n' "$(_bold "[$idx/$total] $name")"
    [[ -n "$desc" ]] && printf '  %s\n' "$desc"
    _double_rule
    echo
}

_field() {
    # _field LABEL VALUE → "  LABEL ........... VALUE"
    local label=$1 value=$2 prefix pad
    prefix=$(printf '  %s ' "$label")
    pad=$((30 - ${#prefix}))
    (( pad < 3 )) && pad=3
    printf '%s%s %s\n' "$prefix" "$(printf '%*s' "$pad" '' | tr ' ' '.')" "$value"
}

_prompt_required() {
    local label=$1 value
    while true; do
        read -r -p "  $label: " value
        [[ -n "$value" ]] && { echo "$value"; return; }
        echo "  (required — cannot be empty)" >&2
    done
}

_prompt_default() {
    local label=$1 default=$2 value
    read -r -p "  $label [$default]: " value
    echo "${value:-$default}"
}

_prompt_choice() {
    local label=$1 default=$2 a=$3 b=$4 value
    while true; do
        read -r -p "  $label [$default]: " value
        value="${value:-$default}"
        [[ "$value" == "$a" || "$value" == "$b" ]] && { echo "$value"; return; }
        echo "  Must be $a or $b" >&2
    done
}

_prompt_timezone() {
    local value
    while true; do
        read -r -p "  Timezone (e.g. America/Toronto, Europe/London, UTC): " value
        [[ -z "$value" ]] && { echo "  (required — cannot be empty)" >&2; continue; }
        [[ -e "/usr/share/zoneinfo/$value" ]] && { echo "$value"; return; }
        echo "  Invalid timezone. Check /usr/share/zoneinfo/ for valid values." >&2
    done
}

_prompt_password() {
    local label="${1:-Password}" p1 p2
    while true; do
        read -r -s -p "  ${label}: " p1; echo >&2
        [[ -z "$p1" ]] && { echo "  (required — cannot be empty)" >&2; continue; }
        read -r -s -p "  Confirm password: " p2; echo >&2
        [[ "$p1" == "$p2" ]] && { echo "$p1"; return; }
        echo "  Passwords do not match — try again." >&2
    done
}

_prompt_country() {
    local label=$1 value
    local -A valid=()

    if command -v reflector &>/dev/null; then
        while IFS= read -r entry; do
            [[ -n "$entry" ]] && valid["${entry,,}"]=1
        done < <(
            reflector --list-countries 2>/dev/null \
                | awk 'NR>2 { code=$(NF-1); $(NF-1)=""; $NF=""; sub(/[ \t]+$/,""); print; print code }'
        )
    fi

    while true; do
        read -r -p "  $label: " value
        [[ -z "$value" ]] && { echo ""; return; }
        if (( ${#valid[@]} == 0 )); then
            echo "$value"; return
        fi
        [[ -n "${valid[${value,,}]:-}" ]] && { echo "$value"; return; }
        echo "  Invalid country. Run 'reflector --list-countries' for valid names/codes." >&2
    done
}

# RFC 1123 hostname label: 1-63 chars, [a-zA-Z0-9-], no leading/trailing hyphen.
_prompt_hostname() {
    local value
    while true; do
        read -r -p "  Hostname: " value
        [[ -z "$value" ]] && { echo "  (required — cannot be empty)" >&2; continue; }
        if [[ "$value" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$ ]]; then
            echo "$value"; return
        fi
        echo "  Invalid hostname. RFC 1123: 1-63 chars, letters/digits/hyphens, no leading/trailing hyphen." >&2
    done
}

# Validate against `locale -a` if available; otherwise warn and accept.
_prompt_locale() {
    local default=$1 value
    local -A valid=()

    if command -v locale &>/dev/null; then
        while IFS= read -r entry; do
            [[ -n "$entry" ]] && valid["${entry,,}"]=1
        done < <(locale -a 2>/dev/null)
    fi

    while true; do
        read -r -p "  Locale [$default]: " value
        # Accept the default unconditionally on empty input — locale -a on the
        # live ISO often lacks en_US.UTF-8, so validating the default here would
        # reject it and trap the user.
        [[ -z "$value" ]] && { echo "$default"; return; }
        if (( ${#valid[@]} == 0 )); then
            echo "  WARN: 'locale' not found — skipping locale validation" >&2
            echo "$value"; return
        fi
        [[ -n "${valid[${value,,}]:-}" ]] && { echo "$value"; return; }
        echo "  Invalid locale. Run 'locale -a' for valid values." >&2
    done
}

# Validate against `localectl list-keymaps` if available; otherwise warn and accept.
_prompt_keymap() {
    local default=$1 value
    local -A valid=()

    if command -v localectl &>/dev/null; then
        while IFS= read -r entry; do
            [[ -n "$entry" ]] && valid["${entry,,}"]=1
        done < <(localectl list-keymaps 2>/dev/null)
    fi

    while true; do
        read -r -p "  Keymap [$default]: " value
        [[ -z "$value" ]] && { echo "$default"; return; }
        if (( ${#valid[@]} == 0 )); then
            echo "  WARN: 'localectl' not found — skipping keymap validation" >&2
            echo "$value"; return
        fi
        [[ -n "${valid[${value,,}]:-}" ]] && { echo "$value"; return; }
        echo "  Invalid keymap. Run 'localectl list-keymaps' for valid values." >&2
    done
}

# Validate that the input is a whole-disk block device (not a partition,
# loop, or rom). Refuse the device backing the live ISO so the user can't
# wipe the medium they booted from.
_prompt_block_device() {
    local label=$1 value type live_src live_dev
    if [[ -e /run/archiso/bootmnt ]]; then
        live_src=$(findmnt -no SOURCE /run/archiso/bootmnt 2>/dev/null || true)
        [[ -n "$live_src" ]] && live_dev=$(lsblk -no PKNAME "$live_src" 2>/dev/null || true)
    fi
    while true; do
        read -r -p "  $label: " value
        [[ -z "$value" ]] && { echo "  (required — cannot be empty)" >&2; continue; }
        if [[ ! -b "$value" ]]; then
            echo "  '$value' is not a block device. See list above." >&2
            continue
        fi
        type=$(lsblk -dn -o TYPE "$value" 2>/dev/null || true)
        if [[ "$type" != "disk" ]]; then
            echo "  '$value' is a '$type', not a whole disk. Pick a top-level device." >&2
            continue
        fi
        if [[ -n "$live_dev" && "$value" == "/dev/$live_dev" ]]; then
            echo "  '$value' is the live ISO medium — refusing to erase it." >&2
            continue
        fi
        echo "$value"; return
    done
}

# Escape an arbitrary string as a TOML basic string. JSON string syntax is a
# subset compatible with TOML basic strings for printable input.
_toml_escape() {
    printf '%s' "$1" | python3 -c 'import sys, json; print(json.dumps(sys.stdin.read()))'
}

# On the live ISO, / is an overlay backed by /run/archiso/cowspace (default
# 256 MiB tmpfs). base-devel + git won't fit. Grow it before pacman runs.
# No-op outside archiso.
_remount_cowspace() {
    local mp=/run/archiso/cowspace
    mountpoint -q "$mp" 2>/dev/null || return 0

    local mem_kb target_kb cur_bytes target_bytes cur_human target_human
    mem_kb=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
    [[ -n "$mem_kb" ]] || _die "Cannot read MemAvailable from /proc/meminfo"

    target_kb=$(( mem_kb / 2 ))
    (( target_kb > 4 * 1024 * 1024 )) && target_kb=$(( 4 * 1024 * 1024 ))
    (( target_kb < 1 * 1024 * 1024 )) && target_kb=$(( 1 * 1024 * 1024 ))

    cur_bytes=$(findmnt -bno SIZE "$mp")
    target_bytes=$(( target_kb * 1024 ))

    cur_human=$(numfmt --to=iec "$cur_bytes" 2>/dev/null || echo "${cur_bytes}B")
    target_human=$(numfmt --to=iec "$target_bytes" 2>/dev/null || echo "${target_bytes}B")

    if (( cur_bytes >= target_bytes )); then
        echo "  cowspace already $cur_human (≥ target $target_human) — leaving as is"
        return 0
    fi

    echo "  Remounting $mp: $cur_human → $target_human"
    if ! mount -o "remount,size=${target_kb}K" "$mp"; then
        echo >&2
        echo "  Failed to remount cowspace. Run manually before retrying:" >&2
        echo "    mount -o remount,size=${target_human} $mp" >&2
        echo "  Or boot the ISO with kernel param: cow_spacesize=${target_human}" >&2
        _die "cowspace remount failed"
    fi
}

# ── Welcome banner ────────────────────────────────────────────────────────────

echo
_double_rule
printf '  %s\n' "$(_bold "SysForge — live-ISO bootstrap")"
printf '  %s\n' "Installs $PKG and prepares /etc/sysforge/bootstrap.toml."
printf '  %s\n' "After this script you'll run: sysforge run pipeline --state-dir /mnt/var/lib/sysforge"
_double_rule
echo

# ── 1. Check internet ─────────────────────────────────────────────────────────

_step 1 5 "Check internet" "Verify reachability of archlinux.org"
if ! ping -c 1 -W 3 archlinux.org &>/dev/null; then
    echo "  No connectivity. Connect first:"
    echo "    Wired:    ip link  (usually automatic)"
    echo "    Wireless: iwctl station wlan0 connect \"SSID\""
    _die "Cannot reach archlinux.org"
fi
echo "  OK"

# ── 2. Grow cowspace if on live ISO ───────────────────────────────────────────

_step 2 5 "Grow cowspace" "Expand /run/archiso/cowspace so the AUR build fits"
_remount_cowspace

# ── 3. Install SysForge from AUR ──────────────────────────────────────────────

_step 3 5 "Install SysForge" "Build and install $PKG from the AUR"
# Refresh the pacman db separately so a sync failure surfaces with a clear
# pointer to the likely cause, instead of getting buried in the install output.
pacman -Sy 2>&1 || _die "pacman db sync failed — check /etc/pacman.d/mirrorlist and connectivity"
pacman -S --needed --noconfirm git base-devel

# makepkg refuses to run as root; create an unprivileged build user with
# passwordless sudo (needed by makepkg's pacman -U install step). The drop-in
# and any user we create are removed on exit.
_created_user=0
if ! id "$BUILD_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$BUILD_USER"
    _created_user=1
fi
install -m 0440 /dev/stdin "$SUDOERS_DROPIN" <<< "$BUILD_USER ALL=(ALL) NOPASSWD: ALL"

cleanup_build_user() {
    rm -f "$SUDOERS_DROPIN"
    [[ -n "${BUILD_DIR:-}" && -d "$BUILD_DIR" ]] && rm -rf "$BUILD_DIR"
    if [[ "$_created_user" == 1 ]]; then
        userdel -r "$BUILD_USER" 2>/dev/null || true
    fi
}
trap cleanup_build_user EXIT

BUILD_DIR=$(sudo -u "$BUILD_USER" mktemp -d -t "iso-install-$PKG-XXXX")
sudo -u "$BUILD_USER" mkdir -p "$BUILD_DIR/build" "$BUILD_DIR/pkg"
sudo -u "$BUILD_USER" git clone --quiet "$AUR_URL" "$BUILD_DIR/$PKG"
# Pin BUILDDIR/PKGDEST to the /tmp-backed work dir so makepkg's src/ and pkg/
# never touch cowspace, regardless of /etc/makepkg.conf. `env` is needed
# because sudo doesn't preserve env across the user switch.
( cd "$BUILD_DIR/$PKG" && \
    sudo -u "$BUILD_USER" \
        env BUILDDIR="$BUILD_DIR/build" PKGDEST="$BUILD_DIR/pkg" \
        makepkg -si --noconfirm --needed )

# Stash the cloned sysforge source where the configure stage can find it.
# pacman doesn't preserve the source tree from a wheel install, so without
# this the bootstrap pipeline can't copy sysforge into the target.
#
# The extracted dir name varies by PKGBUILD: stable uses a versioned tarball
# (sysforge-1.0.0), -git uses $pkgname (sysforge-git). Glob for whichever
# directory under src/ holds a pyproject.toml.
SYSFORGE_SRC=$(find "$BUILD_DIR/build/$PKG/src" -mindepth 2 -maxdepth 2 -name pyproject.toml -printf '%h\n' 2>/dev/null | head -1)
if [[ -n "$SYSFORGE_SRC" && -f "$SYSFORGE_SRC/pyproject.toml" ]]; then
    install -d -m 0755 /var/cache/sysforge
    rm -rf /var/cache/sysforge/source
    cp -a "$SYSFORGE_SRC" /var/cache/sysforge/source
    echo "  Source cached at /var/cache/sysforge/source for the configure stage"

    # Older sysforge releases (≤ v1.0.0) only check pip's direct_url.json for
    # a directory URL. Patch the installed dist-info to point at the cache so
    # the configure stage can locate the source without a release bump.
    DIST_INFO=$(find /usr/lib/python*/site-packages -maxdepth 1 -type d -name 'sysforge-*.dist-info' 2>/dev/null | head -1)
    if [[ -n "$DIST_INFO" ]]; then
        printf '%s\n' '{"url":"file:///var/cache/sysforge/source","dir_info":{"editable":false}}' \
            > "$DIST_INFO/direct_url.json"
        echo "  Patched $DIST_INFO/direct_url.json → /var/cache/sysforge/source"
    fi
else
    echo "  WARN: no pyproject.toml found under $BUILD_DIR/build/$PKG/src; configure stage will try the chroot-clone fallback" >&2
fi

echo "  Installed: $(pacman -Q "$PKG" 2>/dev/null || sysforge --version 2>/dev/null || echo "$PKG")"

# ── 4. Collect bootstrap configuration ────────────────────────────────────────

_step 4 5 "Collect bootstrap configuration" "Answer the prompts to populate /etc/sysforge/bootstrap.toml"
echo "  Available block devices:"
lsblk -d -o NAME,SIZE,MODEL --noheadings | grep -v '^loop' | sed 's/^/    /'
echo

DEVICE=$(_prompt_block_device "Block device to install on (e.g. /dev/sda or /dev/nvme0n1)")
echo
echo "  WARNING: $DEVICE will be ERASED. All data on it will be lost."
CONFIRM=$(_prompt_choice "Continue" "no" "yes" "no")
[[ "$CONFIRM" == "yes" ]] || _die "Aborted by user."
ROOT_FS=$(_prompt_choice   "Root filesystem [ext4/btrfs]" "ext4" "ext4" "btrfs")
HOSTNAME=$(_prompt_hostname)
LOCALE=$(_prompt_locale     "en_US.UTF-8")
TIMEZONE=$(_prompt_timezone)
KEYMAP=$(_prompt_keymap     "us")
COUNTRY=$(_prompt_country    "Mirror country for reflector — name or 2-letter code (leave blank for all)")
USERNAME=$(_prompt_default   "Primary username" "builder")
USER_PASSWORD=$(_prompt_password "User password")
ROOT_PASSWORD=$(_prompt_password "Root password")

# ── 5. Review and write ───────────────────────────────────────────────────────

_step 5 5 "Review and write" "Confirm the collected configuration before writing /etc/sysforge/bootstrap.toml"

_field "Block device"     "$DEVICE  (will be ERASED)"
_field "Root filesystem"  "$ROOT_FS"
_field "Hostname"         "$HOSTNAME"
_field "Locale"           "$LOCALE"
_field "Timezone"         "$TIMEZONE"
_field "Keymap"           "$KEYMAP"
_field "Mirror country"   "${COUNTRY:-<all>}"
_field "Username"         "$USERNAME"
_field "User password"    "[hidden]"
_field "Root password"    "[hidden]"
echo
WRITE_OK=$(_prompt_choice "Write this configuration" "no" "yes" "no")
[[ "$WRITE_OK" == "yes" ]] || _die "Aborted by user."

# Refuse to overwrite an existing bootstrap.toml without explicit confirmation,
# so a partial re-run doesn't blow away hand-edited fields.
if [[ -f /etc/sysforge/bootstrap.toml ]]; then
    echo
    echo "  /etc/sysforge/bootstrap.toml already exists."
    OVERWRITE=$(_prompt_choice "Overwrite" "no" "yes" "no")
    if [[ "$OVERWRITE" != "yes" ]]; then
        echo "  Keeping existing bootstrap.toml. Edit it directly with:"
        echo "    vim /etc/sysforge/bootstrap.toml"
        echo "  Then run:"
        echo "    sysforge run pipeline --state-dir /mnt/var/lib/sysforge"
        exit 0
    fi
fi

# Escape all user-supplied values as TOML basic strings. Without this, a
# password (or hostname etc.) containing ", \, or a control char produces
# malformed TOML. The escape is also a defense-in-depth against shell
# expansion inside the unquoted heredoc below.
DEVICE_TOML=$(_toml_escape "$DEVICE")
ROOT_FS_TOML=$(_toml_escape "$ROOT_FS")
HOSTNAME_TOML=$(_toml_escape "$HOSTNAME")
LOCALE_TOML=$(_toml_escape "$LOCALE")
TIMEZONE_TOML=$(_toml_escape "$TIMEZONE")
KEYMAP_TOML=$(_toml_escape "$KEYMAP")
USERNAME_TOML=$(_toml_escape "$USERNAME")
USER_PASSWORD_TOML=$(_toml_escape "$USER_PASSWORD")
ROOT_PASSWORD_TOML=$(_toml_escape "$ROOT_PASSWORD")

# Build optional countries line
if [[ -n "$COUNTRY" ]]; then
    COUNTRY_TOML=$(_toml_escape "$COUNTRY")
    COUNTRIES_LINE="countries = [$COUNTRY_TOML]"
else
    COUNTRIES_LINE="# countries = []  # set to filter mirrors by country"
fi

cat > /etc/sysforge/bootstrap.toml << EOF
# bootstrap.toml — generated by iso-install.sh
target = "/mnt"

[partition]
device       = $DEVICE_TOML
esp_size_mib = 512
root_fs      = $ROOT_FS_TOML

[system]
hostname      = $HOSTNAME_TOML
locale        = $LOCALE_TOML
timezone      = $TIMEZONE_TOML
keymap        = $KEYMAP_TOML
username      = $USERNAME_TOML
user_password = $USER_PASSWORD_TOML
root_password = $ROOT_PASSWORD_TOML

[mirror]
$COUNTRIES_LINE
protocol = "https"
age      = 12
EOF
chmod 0600 /etc/sysforge/bootstrap.toml

echo
_double_rule
printf '  %s\n' "$(_bold "bootstrap.toml written")"
_double_rule
echo
sed 's/^\(\(root\|user\)_password\s*=\s*\).*/\1"[hidden]"/' /etc/sysforge/bootstrap.toml
echo
_double_rule
echo "  To edit further:  vim /etc/sysforge/bootstrap.toml"
echo "  To run pipeline:  sysforge run pipeline --state-dir /mnt/var/lib/sysforge"
_double_rule
