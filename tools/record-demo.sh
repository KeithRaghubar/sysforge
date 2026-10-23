#!/usr/bin/env bash
# record-demo.sh -- regenerate docs/assets/demo.gif from docs/assets/demo.tape.
#
# The recording runs real commands against a throwaway copy of the live config
# and state, so it shows packages this machine actually built. The copy resets
# [log] verbosity to the shipped default and points HOME at a scratch dir, so no
# personal path or narration reaches the GIF. The tape names packages that must
# be in the state's build record (mesa, gamescope-nvidia); edit both together.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
config=${SYSFORGE_CONFIG_DIR:-/etc/sysforge}
state=${SYSFORGE_STATE_DIR:-/var/lib/sysforge}
src=${PKGBUILD_SRC_DIR:-$HOME/src}
demo=/tmp/sysforge-demo

if ! command -v vhs >/dev/null; then
    echo "record-demo: vhs is not installed (sudo pacman -S vhs)" >&2
    exit 1
fi

rm -rf "$demo"
mkdir -p "$demo/config" "$demo/state" "$demo/src/mesa"
for f in profiles toolchain kernel; do
    cp "$config/$f.toml" "$demo/config/"
done
cp "$repo/etc/sysforge/packages.toml" "$demo/config/"
sed -E 's/^verbosity[[:space:]]*=.*/#verbosity = 0/' "$config/sysforge.toml" > "$demo/config/sysforge.toml"
for f in build_state source_meta hardware_profile pipeline_state; do
    cp "$state/$f.toml" "$demo/state/"
done
cp "$src/mesa/PKGBUILD" "$demo/src/mesa/"

cd "$repo"
SF_DEMO_HOME=$demo SYSFORGE_CONFIG_DIR=$demo/config SYSFORGE_STATE_DIR=$demo/state \
    vhs docs/assets/demo.tape
rm -rf "$demo"
