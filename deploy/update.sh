#!/usr/bin/env bash
# Phonon — self-update script triggered by /system/update/apply.
# Runs under root via NOPASSWD sudoers grant on /usr/local/sbin/phonon-update.
#
# Strategy:
#   * Auto-stash any local changes (chmod, mode flips, leftovers) before
#     pulling so a perm-difference doesn't refuse fast-forward.
#   * Diff BEFORE..AFTER to pick the cheapest deploy strategy:
#       - structural change (deploy/, sudoers, systemd, pyproject.toml)
#         → full install.sh (~30-60 s)
#       - Python source only → pip --force-reinstall + daemon restart
#         (~5-10 s)
#       - static/HTML only → daemon restart only (~2-3 s)
#       - docs/tests only → no-op (just record the SHA)
#   * Concurrent-safe via flock; full output streamed to update.log.

set -euo pipefail

# Resolve REPO_ROOT — symlink-aware. /usr/local/sbin/phonon-update is a
# symlink to deploy/update.sh; dirname $0 alone would land in /usr/local.
if [ -f /etc/phonon/repo-path ] && [ -d "$(cat /etc/phonon/repo-path)/.git" ]; then
    REPO_ROOT="$(cat /etc/phonon/repo-path)"
else
    REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
fi
LOG=/var/log/phonon/update.log
LOCK=/run/phonon-update.lock
VENV_DIR=/opt/phonon/stage/.venv

mkdir -p "$(dirname "$LOG")"
exec 200>"$LOCK"
if ! flock -n 200; then
    echo "[$(date -Iseconds)] update already in progress — bailing out" >&2
    exit 1
fi
exec > >(tee -a "$LOG") 2>&1

echo "============================================================"
echo "phonon update started at $(date -Iseconds)"
echo "repo: ${REPO_ROOT}"
echo "============================================================"

cd "${REPO_ROOT}"

# core.fileMode=false makes git stop tracking exec-bit changes, which
# install.sh's chmod loops would otherwise present as a 'dirty tree'
# and block fast-forward. Set on the repo (idempotent).
git config --local core.fileMode false 2>/dev/null || true

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
BEFORE="$(git rev-parse HEAD)"
echo "branch: ${BRANCH}"
echo "before: ${BEFORE}"

# Auto-stash local changes (chmod, mode flips, anything we couldn't
# prevent from leaking through). Keeping --include-untracked so even
# stray files don't block the pull. Empty stash is a no-op.
STASH_REF=""
if [ -n "$(git status --porcelain)" ]; then
    echo "auto-stash before pull"
    if git stash push --include-untracked -m "phonon-update auto-stash $(date -Iseconds)" >/dev/null; then
        STASH_REF="stash@{0}"
    fi
fi

# Pull with retries — git fetch can transiently fail on flaky networks.
FETCH_OK=false
for attempt in 1 2 3; do
    if git fetch --quiet 2>/dev/null; then
        FETCH_OK=true
        break
    fi
    sleep 2
done
if [ "${FETCH_OK}" != true ]; then
    echo "[error] git fetch failed after 3 attempts"
    [ -n "${STASH_REF}" ] && git stash pop --quiet 2>/dev/null || true
    exit 3
fi

if ! git pull --ff-only --quiet; then
    echo "[error] fast-forward refused — repo may have diverged commits"
    [ -n "${STASH_REF}" ] && git stash pop --quiet 2>/dev/null || true
    exit 4
fi

# Restore stash silently — conflicts are non-fatal, the user's local
# changes are preserved in stash list for them to inspect.
if [ -n "${STASH_REF}" ]; then
    git stash pop --quiet 2>/dev/null || echo "(stashed changes kept in 'git stash list')"
fi

AFTER="$(git rev-parse HEAD)"
echo "after:  ${AFTER}"

if [ "${BEFORE}" = "${AFTER}" ]; then
    echo "Already up to date. Skipping reinstall."
    exit 0
fi

COMMITS_PULLED=$(git rev-list --count "${BEFORE}..${AFTER}")
CHANGED_FILES="$(git diff --name-only "${BEFORE}..${AFTER}")"
echo "Pulled ${COMMITS_PULLED} commits, ${#CHANGED_FILES} files changed"
echo "------------------------------------------------------------"

# Strategy selection
NEEDS_FULL=false
NEEDS_PY=false
NEEDS_RESTART=false
WHY=""
while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
        deploy/*|*/sudoers*|*/systemd/*|*/dbus/*|*/udev/*)
            NEEDS_FULL=true; WHY="${WHY}${WHY:+, }${f}" ;;
        */pyproject.toml|pyproject.toml)
            NEEDS_FULL=true; WHY="${WHY}${WHY:+, }${f}" ;;
        stage/src/*.py|stage/src/*/*.py|stage/src/*/*/*.py|stage/src/*/*/*/*.py)
            NEEDS_PY=true; NEEDS_RESTART=true ;;
        stage/src/*static*|stage/src/*/static/*|stage/src/*/*/static/*)
            # Static assets are picked up at the next HTTP fetch — daemon
            # restart not strictly required, but reload of the running
            # FastAPI app is so cheap we do it anyway for predictability.
            NEEDS_RESTART=true ;;
        # docs/, tests/, .github/, README, etc. → no-op (just SHA bump)
    esac
done <<< "${CHANGED_FILES}"

if [ "${NEEDS_FULL}" = true ]; then
    echo "Full reinstall (changed: ${WHY})"
    echo "------------------------------------------------------------"
    bash "${REPO_ROOT}/deploy/install.sh"
elif [ "${NEEDS_PY}" = true ] || [ "${NEEDS_RESTART}" = true ]; then
    if [ ! -d "${VENV_DIR}" ]; then
        echo "[warn] venv missing at ${VENV_DIR} — falling back to install.sh"
        bash "${REPO_ROOT}/deploy/install.sh"
    else
        if [ "${NEEDS_PY}" = true ]; then
            echo "Python reinstall (--force-reinstall --no-deps)"
            "${VENV_DIR}/bin/pip" install --force-reinstall --no-deps "${REPO_ROOT}/stage" --quiet
        else
            # Static assets live inside the installed package
            # (stage/src/phonon_stage/static/...) — `pip install`
            # COPIES them into site-packages, it doesn't symlink.
            # So a daemon restart alone leaves the old copy on disk
            # being served forever. Reinstall the package whenever
            # static files moved.
            echo "Static/HTML only — pip reinstall to refresh static assets"
            "${VENV_DIR}/bin/pip" install --force-reinstall --no-deps "${REPO_ROOT}/stage" --quiet
        fi
        echo "Restarting phonon-stage user service"
        PHONON_UID="$(id -u phonon)"
        systemd-run --uid="${PHONON_UID}" --gid="${PHONON_UID}" \
            -p PAMName=login --pipe --wait -- \
            systemctl --user restart phonon-stage 2>&1 || true
    fi
else
    echo "Docs/tests only — daemon stays as-is."
fi

echo "============================================================"
echo "phonon update finished at $(date -Iseconds)"
echo "============================================================"
