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
echo "Pulled $(git rev-list --count "${BEFORE}..${AFTER}") commits"
echo "------------------------------------------------------------"

# Decide between full install.sh (heavy: ~30-60s on Pi 3 — apt update,
# systemd unit rewrites, sudoers, BT/PTP scaffolding) and a fast path
# (just pip-reinstall + restart, ~5-10s) based on what actually changed.
# Anything under deploy/ or a pyproject.toml change → full reinstall.
# Otherwise we trust install.sh's prior run and only refresh the venv.
CHANGED_FILES="$(git diff --name-only "${BEFORE}..${AFTER}")"
NEEDS_FULL=false
WHY=""
while IFS= read -r f; do
    case "$f" in
        deploy/*)             NEEDS_FULL=true; WHY="$f"; break ;;
        */pyproject.toml)     NEEDS_FULL=true; WHY="$f"; break ;;
        pyproject.toml)       NEEDS_FULL=true; WHY="$f"; break ;;
    esac
done <<< "${CHANGED_FILES}"

if [ "${NEEDS_FULL}" = "true" ]; then
    echo "Full reinstall required (changed: ${WHY})"
    echo "------------------------------------------------------------"
    bash "${REPO_ROOT}/deploy/install.sh"
else
    echo "Fast path: only Python/static changes — pip reinstall + restart"
    echo "------------------------------------------------------------"

    VENV_DIR=/opt/phonon/stage/.venv
    STAGE_SRC="${REPO_ROOT}/stage"

    if [ ! -d "${VENV_DIR}" ]; then
        echo "[error] venv missing at ${VENV_DIR} — falling back to full install.sh"
        bash "${REPO_ROOT}/deploy/install.sh"
    else
        # Force-reinstall the package source — pip otherwise short-circuits
        # on 'Requirement already satisfied' and our changes don't land.
        # --no-deps because we know nothing in pyproject changed (caught
        # above) and we want the fast path to stay fast.
        "${VENV_DIR}/bin/pip" install --force-reinstall --no-deps "${STAGE_SRC}" --quiet

        # Restart the user-instance phonon-stage. Same pattern as install.sh
        # step 11/11: needs systemd-run because we're root and the daemon
        # runs in the phonon user's systemd --user session.
        PHONON_UID="$(id -u phonon)"
        systemd-run --uid="${PHONON_UID}" --gid="${PHONON_UID}" \
            -p PAMName=login --pipe --wait -- \
            systemctl --user restart phonon-stage 2>&1 || true

        echo "  Restarted phonon-stage with new code"
    fi
fi

echo "============================================================"
echo "phonon update finished at $(date -Iseconds)"
echo "============================================================"
