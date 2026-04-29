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
_header() { echo; echo "── $*"; }

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

# ── 1. Check internet ─────────────────────────────────────────────────────────

_header "Checking internet connectivity"
if ! ping -c 1 -W 3 archlinux.org &>/dev/null; then
    echo "  No connectivity. Connect first:"
    echo "    Wired:    ip link  (usually automatic)"
    echo "    Wireless: iwctl station wlan0 connect \"SSID\""
    _die "Cannot reach archlinux.org"
fi
echo "  OK"

# ── 2. Grow cowspace if on live ISO ───────────────────────────────────────────

_header "Checking live-ISO cowspace"
_remount_cowspace

# ── 3. Install SysForge from AUR ──────────────────────────────────────────────

_header "Installing $PKG from AUR"
pacman -Sy --needed --noconfirm git base-devel

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
SYSFORGE_SRC="$BUILD_DIR/build/$PKG/src/$PKG"
if [[ -f "$SYSFORGE_SRC/pyproject.toml" ]]; then
    install -d -m 0755 /var/cache/sysforge
    rm -rf /var/cache/sysforge/source
    cp -a "$SYSFORGE_SRC" /var/cache/sysforge/source
    echo "  Source cached at /var/cache/sysforge/source for the configure stage"
else
    echo "  WARN: expected source tree at $SYSFORGE_SRC not found; configure stage will try the chroot-clone fallback" >&2
fi

echo "  Installed: $(sysforge --help 2>&1 | head -1 || echo "$PKG")"

# ── 4. Configure bootstrap.toml ───────────────────────────────────────────────

_header "Configure bootstrap.toml"
echo
echo "  Available block devices:"
lsblk -d -o NAME,SIZE,MODEL --noheadings | grep -v '^loop' | sed 's/^/    /'
echo

DEVICE=$(_prompt_required "Block device to install on (e.g. /dev/sda or /dev/nvme0n1)")
ROOT_FS=$(_prompt_choice   "Root filesystem [ext4/btrfs]" "ext4" "ext4" "btrfs")
HOSTNAME=$(_prompt_required "Hostname")
LOCALE=$(_prompt_default    "Locale" "en_US.UTF-8")
TIMEZONE=$(_prompt_timezone)
KEYMAP=$(_prompt_default    "Keymap" "us")
COUNTRY=$(_prompt_default    "Mirror country for reflector (leave blank for all)" "")
USERNAME=$(_prompt_default   "Primary username" "builder")
USER_PASSWORD=$(_prompt_password "User password")
ROOT_PASSWORD=$(_prompt_password "Root password")

# Build optional countries line
if [[ -n "$COUNTRY" ]]; then
    COUNTRIES_LINE="countries = [\"$COUNTRY\"]"
else
    COUNTRIES_LINE="# countries = []  # set to filter mirrors by country"
fi

cat > /etc/sysforge/bootstrap.toml << EOF
# bootstrap.toml — generated by iso-install.sh
target = "/mnt"

[partition]
device       = "$DEVICE"
esp_size_mib = 512
root_fs      = "$ROOT_FS"

[system]
hostname      = "$HOSTNAME"
locale        = "$LOCALE"
timezone      = "$TIMEZONE"
keymap        = "$KEYMAP"
username      = "$USERNAME"
user_password = "$USER_PASSWORD"
root_password = "$ROOT_PASSWORD"

[mirror]
$COUNTRIES_LINE
protocol = "https"
age      = 12
EOF

_header "bootstrap.toml written"
echo
sed 's/^\(\(root\|user\)_password\s*=\s*\).*/\1"[hidden]"/' /etc/sysforge/bootstrap.toml
echo
echo "────────────────────────────────────────────────"
echo "  To edit further:  vim /etc/sysforge/bootstrap.toml"
echo "  To run pipeline:  sysforge run pipeline --state-dir /mnt/var/lib/sysforge"
echo "────────────────────────────────────────────────"
