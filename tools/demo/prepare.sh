#!/usr/bin/env bash
# prepare.sh -- as the demo user: install sysforge from the checkout mounted at
# /src/sysforge and run the Quick start's one-time setup, leaving the system
# where a new user is after steps 1-3.
#
# Usage: prepare.sh COMMIT
#
# The setup runs with EDITOR=true, which accepts every shipped config file
# unchanged.
set -euo pipefail

"$(dirname "$0")/install.sh" "$1"
cd ~
sudo sysforge run hardware </dev/null
sudo EDITOR=true sysforge run reconfigure </dev/null
