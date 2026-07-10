#!/bin/zsh
set -euo pipefail

RELEASE="${1:?usage: rollback_release.sh /absolute/previous-release-path}"
print -u2 -- "Stop both jobs first. Code-only rollback is permitted only after compatibility evidence; otherwise restore a verified backup before switching."
exec "${0:A:h}/deploy_release.sh" "$RELEASE"
