#!/usr/bin/env bash
# ============================================================================
# release.sh — Bump patch version, commit, tag, push.
#
# Triggered by .git/hooks/pre-push on `git push` to master.
# Sets RELEASING=1 on its own push calls so the pre-push hook does not recurse.
# ============================================================================
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
PLUGIN_JSON="$REPO_ROOT/.claude-plugin/plugin.json"

if [ ! -f "$PLUGIN_JSON" ]; then
  echo "[release] $PLUGIN_JSON not found" >&2
  exit 1
fi

# Bump patch version in plugin.json (in-place, JSON-safe via python3).
NEW_VERSION="$(python3 - "$PLUGIN_JSON" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
parts = data["version"].split(".")
if len(parts) != 3 or not all(p.isdigit() for p in parts):
    sys.exit(f"unsupported version format: {data['version']}")
parts[2] = str(int(parts[2]) + 1)
data["version"] = ".".join(parts)
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
print(data["version"])
PY
)"

TAG="v$NEW_VERSION"
echo "[release] Bumped plugin version to $NEW_VERSION"

# Stage + commit the version bump.
git add "$PLUGIN_JSON"
git commit -m "chore: bump version to $TAG"

# Tag the new commit.
git tag -a "$TAG" -m "Release $TAG"

# Push commits and tag with RELEASING=1 so pre-push hook short-circuits.
echo "[release] Pushing master + $TAG to origin..."
RELEASING=1 git push origin master
RELEASING=1 git push origin "$TAG"

echo "[release] Done. $TAG published."
