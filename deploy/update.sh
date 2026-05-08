#!/usr/bin/env bash
# Phonon — self-update script triggered by /system/update/apply.
# Runs under root via NOPASSWD sudoers grant on /usr/local/sbin/phonon-update.
#
# - flock prevents concurrent runs.
# - Pulls fast-forward only (refuses to merge over local changes).
# - Reinstalls (install.sh is idempotent and re-runs the systemd unit).
# - All output captured to /var/log/phonon/update.log so the UI can show
#   the trail later.

set -euo pipefail

# When invoked via the symlink at /usr/local/sbin/phonon-update,
# `$0` is the symlink path itself — `dirname $0` yields /usr/local/sbin,
# and ../.. resolves to /usr/local (no .git). Prefer the canonical repo
# path written by install.sh; fall back to readlink -f resolution so a
# manual invocation of deploy/update.sh still works on a host that
# hasn't run install.sh yet.
if [ -f /etc/phonon/repo-path ] && [ -d "$(cat /etc/phonon/repo-path)/.git" ]; then
    REPO_ROOT="$(cat /etc/phonon/repo-path)"
else
    REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
fi
LOG=/var/log/phonon/update.log
LOCK=/run/phonon-update.lock

mkdir -p "$(dirname "$LOG")"

exec 200>"$LOCK"
if ! flock -n 200; then
    echo "[$(date -Iseconds)] update already in progress — bailing out" >&2
    exit 1
fi

# Tee everything to the log + stderr (which sudo captures)
exec > >(tee -a "$LOG") 2>&1

echo "============================================================"
echo "phonon update started at $(date -Iseconds)"
echo "repo: ${REPO_ROOT}"
echo "============================================================"

cd "${REPO_ROOT}"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
BEFORE="$(git rev-parse HEAD)"

echo "branch: ${BRANCH}"
echo "before: ${BEFORE}"

# Hard fail if the working tree is dirty — we won't ff-merge over local edits.
if [ -n "$(git status --porcelain)" ]; then
    echo "[error] working tree has uncommitted changes — aborting"
    git status --short
    exit 2
fi

git fetch --quiet || { echo "[error] git fetch failed"; exit 3; }
git pull --ff-only

AFTER="$(git rev-parse HEAD)"
echo "after:  ${AFTER}"

if [ "${BEFORE}" = "${AFTER}" ]; then
    echo "Already up to date. Skipping reinstall."
    exit 0
fi

echo "------------------------------------------------------------"
echo "Pulled $(git rev-list --count "${BEFORE}..${AFTER}") commits — running install.sh"
echo "------------------------------------------------------------"

bash "${REPO_ROOT}/deploy/install.sh"

echo "============================================================"
echo "phonon update finished at $(date -Iseconds)"
echo "============================================================"
