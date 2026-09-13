#!/usr/bin/env bash
# install-skill.sh — install/sync this repo's skill into a Hermes profile (or any destination).
#
# A profile install is a SNAPSHOT and will drift: its SKILL.md, scripts/ and templates/ are copies,
# while SKILL.md's How-to-Run points at <this-skill>/scripts/oprun.py. A stale install therefore runs
# stale semantics — the conductor would execute the old completion path against a fixed repo. Run
# this after any change to the product's completion path, and in CI with --check to catch drift.
#
#   scripts/install-skill.sh                    # sync into the coder profile (the conductor's)
#   scripts/install-skill.sh --profile default  # the default profile (~/.hermes/skills/...)
#   scripts/install-skill.sh --dest DIR         # any destination: a fixture, another host
#   scripts/install-skill.sh --check            # report drift, write nothing, exit 2 if stale
#
# Idempotent. Coreutils only. Never touches the repo.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_SUBDIR="skills/autonomous-ai-agents/oprun"
PROFILE="coder"
DEST=""
MODE="install"
DIRS="scripts references templates assets"

usage() {
  sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="${2:?--profile needs a value}"; shift 2 ;;
    --dest)    DEST="${2:?--dest needs a path}";    shift 2 ;;
    --check)   MODE="check"; shift ;;
    -h|--help) usage 0 ;;
    *) echo "install-skill.sh: unknown argument '$1'" >&2; usage 2 ;;
  esac
done

if [ -z "$DEST" ]; then
  if [ "$PROFILE" = "default" ]; then
    DEST="$HOME/.hermes/$SKILL_SUBDIR"
  else
    DEST="$HOME/.hermes/profiles/$PROFILE/$SKILL_SUBDIR"
  fi
fi

# Everything the skill ships: the prose plus every sibling artifact. Globbed, not hard-coded, so a
# newly added module is installed instead of silently missing from the install.
relative_files() {
  printf '%s\n' SKILL.md
  # CHANGELOG.md is a root companion the install ships, so it drifts too — and a glob over the
  # subdirs below would silently skip it, which is a false "in sync".
  if [ -f "$REPO_DIR/CHANGELOG.md" ]; then printf '%s\n' CHANGELOG.md; fi
  local d
  for d in $DIRS; do
    [ -d "$REPO_DIR/$d" ] || continue
    ( cd "$REPO_DIR" && find "$d" -maxdepth 1 -type f \
        ! -name '*.pyc' ! -name '*.log' ! -path '*__pycache__*' -print )
  done | sort
}

[ -f "$REPO_DIR/SKILL.md" ] || { echo "install-skill.sh: no SKILL.md in $REPO_DIR" >&2; exit 1; }

echo "install-skill.sh: $REPO_DIR  ->  $DEST   [$MODE]"
[ "$MODE" = "check" ] || mkdir -p "$DEST"

same=0 updated=0 added=0 stale=0
while IFS= read -r rel; do
  src="$REPO_DIR/$rel"
  dst="$DEST/$rel"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    same=$((same + 1))
    continue
  fi
  if [ ! -f "$dst" ]; then
    state="added"; added=$((added + 1))
  else
    state="updated"; updated=$((updated + 1))
  fi
  stale=$((stale + 1))
  printf '  %-8s %s\n' "$state" "$rel"
  [ "$MODE" = "check" ] || { mkdir -p "$(dirname "$dst")"; cp -p "$src" "$dst"; }
done < <(relative_files)

echo "install-skill.sh: ${same} in sync, ${added} added, ${updated} updated"
if [ "$stale" -gt 0 ]; then
  if [ "$MODE" = "check" ]; then
    echo "install-skill.sh: STALE — ${stale} file(s) differ from the repo" >&2
    exit 2
  fi
  echo "install-skill.sh: install refreshed; the running agent may need a new session to reload it"
fi
