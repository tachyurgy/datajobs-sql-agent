#!/bin/sh
# Refuse to push if an API-key shape, or any identity pattern listed in a local (untracked) file,
# appears in tracked files. Identity patterns live outside the repo so this script cannot leak them.
set -e
PAT_FILE="${ASKDATA_SCAN_PATTERNS:-$HOME/.config/askdata/identity-patterns}"
KEYS='AQ\.Ab8|AIza[0-9A-Za-z_-]{20}|gsk_[0-9A-Za-z]{20}|xkeysib-|cfat_|cfut_|sk-[A-Za-z0-9]{20}'
if git grep -nIE "$KEYS" -- . ':!scripts/scan.sh'; then echo "scan: API key shape found" >&2; exit 1; fi
if [ -f "$PAT_FILE" ] && git grep -nIi -f "$PAT_FILE" -- . ; then echo "scan: identity pattern found" >&2; exit 1; fi
echo "scan: clean"
