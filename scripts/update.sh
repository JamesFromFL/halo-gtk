#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]; then
  printf '%s\n' "Refusing to update because the working tree has local changes." >&2
  printf '%s\n' "Commit, stash, or discard them before running scripts/update.sh." >&2
  exit 1
fi

git -C "$REPO_ROOT" pull --ff-only
"$REPO_ROOT/scripts/install.sh"
