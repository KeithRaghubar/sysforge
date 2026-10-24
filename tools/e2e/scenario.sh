#!/usr/bin/env bash
# scenario.sh -- run one end-to-end scenario as the current (demo) user, in its
# own login session. Called by run.sh; not meant to be run by hand.
#
# Usage: scenario.sh SCENARIO_FILE
set -euo pipefail
E2E_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=tools/e2e/lib.sh
source "$E2E_DIR/lib.sh"
cd ~
# `podman exec` and runuser pass no terminal type; curses programs refuse to
# start without one.
export TERM=xterm-256color
# shellcheck disable=SC1090
source "$1"
