#!/bin/zsh
# Install exact launchd definitions from the active immutable release.
set -euo pipefail

ROOT="${0:A:h:h}"
ACTIVE_LINK="$HOME/TradingAgentRuntime"
TARGET_DIR="$HOME/Library/LaunchAgents"
[[ -L "$ACTIVE_LINK" ]] || { print -u2 -- "active runtime pointer is not a symlink"; exit 2; }
[[ "$ROOT" == "${ACTIVE_LINK:A}" ]] || {
  print -u2 -- "launchd definitions must be installed from the active immutable release"
  exit 2
}
[[ "$ROOT" == "$HOME/TradingAgentReleases/"* ]] || {
  print -u2 -- "active runtime is not an immutable release"
  exit 2
}

cd "$ROOT"
MODE=$("$ROOT/.venv/bin/python" -c \
  'import json; print(json.load(open("release-manifest.json"))["release_authority"]["mode"])')
[[ "$MODE" == "forward" || "$MODE" == "rollback" ]] || {
  print -u2 -- "release deployment authority mode is invalid"
  exit 2
}
shasum -a 256 -c release-file-inventory.sha256 >/dev/null
"$ROOT/.venv/bin/python" scripts/verify_source_tree.py \
  --root "$ROOT" --inventory "$ROOT/tracked-source-inventory.json" \
  --repository Elijah-Ang/Plutus >/dev/null
"$ROOT/.venv/bin/python" scripts/verify_release_artifact.py "$ROOT" >/dev/null
"$ROOT/.venv/bin/python" scripts/verify_deployment_authority.py \
  --manifest "$ROOT/release-manifest.json" --mode "$MODE" >/dev/null

mkdir -p "$TARGET_DIR"
chmod 700 "$TARGET_DIR"
for name in com.elijah.tradingagent.plist com.elijah.tradingagent.telegram.plist; do
  target="$TARGET_DIR/$name"
  [[ ! -L "$target" ]] || { print -u2 -- "$target is a symlink"; exit 2; }
  /usr/bin/install -m 600 "$ROOT/launchd/$name" "$target"
  plutil -lint "$target"
  cmp -s "$ROOT/launchd/$name" "$target" || {
    print -u2 -- "$target differs from the active release"
    exit 2
  }
done

print -- "Exact plists installed but not loaded."
print -- "Use the controlled deployment/start procedure; do not start from a source checkout."
