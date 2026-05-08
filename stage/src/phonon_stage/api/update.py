"""Self-update endpoints — let the user pull + reinstall from the UI.

Three operations:
  * status:  current commit / branch / how many commits behind upstream
  * preview: list of commits that `git pull` would apply (dry-run)
  * apply:   spawn the on-disk update script in the background

The actual update is delegated to /usr/local/sbin/phonon-update (a symlink
to deploy/update.sh, set up by install.sh) so we keep the sudo NOPASSWD
grant tightly scoped to one script path.

Repo location is read from /etc/phonon/repo-path (written by install.sh).
On a dev box without an install, every endpoint reports "not configured"
instead of crashing.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import structlog
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/system/update", tags=["system"])
logger = structlog.get_logger()

REPO_PATH_FILE = Path("/etc/phonon/repo-path")
UPDATE_SCRIPT = Path("/usr/local/sbin/phonon-update")
UPDATE_LOG = Path("/var/log/phonon/update.log")


def _repo_root() -> Path | None:
    """Read the repo path written by install.sh, or fall back to dev resolution."""
    if REPO_PATH_FILE.is_file():
        path = Path(REPO_PATH_FILE.read_text().strip())
        if path.is_dir() and (path / ".git").exists():
            return path
    # Dev fallback: resolve from this module's location (../../../..)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists() and (parent / "deploy").is_dir():
            return parent
    return None


async def _git(repo: Path, *args: str, timeout: float = 10.0) -> tuple[int, str]:
    """Run a git command in `repo`. Returns (rc, stdout). stderr is captured
    and discarded — caller infers from rc."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, ""
    return proc.returncode or 0, out.decode(errors="ignore").strip()


class CommitInfo(BaseModel):
    sha: str
    short: str
    subject: str
    author: str
    date: str  # ISO 8601


class UpdateStatus(BaseModel):
    available: bool
    repo_path: str
    branch: str
    current: CommitInfo | None
    behind: int
    dirty: bool
    last_update_at: str | None  # ISO of update.log mtime if it exists
    note: str


@router.get("/status", response_model=UpdateStatus)
async def update_status() -> UpdateStatus:
    repo = _repo_root()
    if repo is None:
        return UpdateStatus(
            available=False,
            repo_path="",
            branch="",
            current=None,
            behind=0,
            dirty=False,
            last_update_at=None,
            note="Update mechanism not configured (install.sh hasn't run)",
        )

    rc, branch = await _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        return UpdateStatus(
            available=False,
            repo_path=str(repo),
            branch="",
            current=None,
            behind=0,
            dirty=False,
            last_update_at=None,
            note="git rev-parse failed — repo broken?",
        )

    rc, sha = await _git(repo, "rev-parse", "HEAD")
    rc2, fmt = await _git(repo, "log", "-1", "--format=%h%x09%an%x09%aI%x09%s", "HEAD")
    current: CommitInfo | None = None
    if rc == 0 and rc2 == 0 and fmt:
        parts = fmt.split("\t", 3)
        if len(parts) == 4:
            current = CommitInfo(
                sha=sha, short=parts[0], author=parts[1], date=parts[2], subject=parts[3]
            )

    rc, _ = await _git(repo, "fetch", "--quiet")
    fetch_ok = rc == 0

    behind = 0
    if fetch_ok:
        rc, count = await _git(repo, "rev-list", "--count", f"HEAD..origin/{branch}")
        if rc == 0 and count.isdigit():
            behind = int(count)

    rc, status = await _git(repo, "status", "--porcelain")
    dirty = rc == 0 and bool(status.strip())

    last_update_at: str | None = None
    if UPDATE_LOG.is_file():
        with contextlib.suppress(OSError):
            from datetime import UTC, datetime

            ts = UPDATE_LOG.stat().st_mtime
            last_update_at = datetime.fromtimestamp(ts, tz=UTC).isoformat()

    note = ""
    if not fetch_ok:
        note = "git fetch failed (no network?)"
    elif dirty:
        note = "Local changes present — `git pull` will refuse fast-forward"
    elif behind > 0:
        note = f"{behind} commits behind {branch}"
    else:
        note = "Up to date"

    return UpdateStatus(
        available=UPDATE_SCRIPT.is_file() or UPDATE_SCRIPT.is_symlink(),
        repo_path=str(repo),
        branch=branch,
        current=current,
        behind=behind,
        dirty=dirty,
        last_update_at=last_update_at,
        note=note,
    )


class UpdatePreview(BaseModel):
    behind: int
    branch: str
    commits: list[CommitInfo]
    dirty: bool


@router.get("/preview", response_model=UpdatePreview)
async def update_preview() -> UpdatePreview:
    """Show the commits that `git pull` would bring in. Read-only."""
    repo = _repo_root()
    if repo is None:
        return UpdatePreview(behind=0, branch="", commits=[], dirty=False)

    rc, branch = await _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        return UpdatePreview(behind=0, branch="", commits=[], dirty=False)

    await _git(repo, "fetch", "--quiet")

    rc, status = await _git(repo, "status", "--porcelain")
    dirty = rc == 0 and bool(status.strip())

    rc, raw = await _git(
        repo,
        "log",
        f"HEAD..origin/{branch}",
        "--format=%H%x09%h%x09%an%x09%aI%x09%s",
        "--no-merges",
    )
    commits: list[CommitInfo] = []
    if rc == 0 and raw:
        for line in raw.splitlines():
            parts = line.split("\t", 4)
            if len(parts) == 5:
                commits.append(
                    CommitInfo(
                        sha=parts[0],
                        short=parts[1],
                        author=parts[2],
                        date=parts[3],
                        subject=parts[4],
                    )
                )

    return UpdatePreview(behind=len(commits), branch=branch, commits=commits, dirty=dirty)


@router.post("/apply", status_code=202)
async def update_apply() -> dict[str, str]:
    """Trigger the update script. Fire-and-forget — it ends with a
    systemctl restart of phonon-stage so this very request gets cut off
    when the script reaches that step. UI must poll /health to detect
    the daemon coming back up."""
    if not (UPDATE_SCRIPT.is_file() or UPDATE_SCRIPT.is_symlink()):
        return {
            "status": "not_configured",
            "detail": f"{UPDATE_SCRIPT} missing — install.sh hasn't run",
        }

    # Spawn under sudo (NOPASSWD entry set up by install.sh) and detach
    proc = await asyncio.create_subprocess_exec(
        "sudo",
        "-n",
        str(UPDATE_SCRIPT),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    logger.info("update.apply_spawned", pid=proc.pid)
    return {"status": "started", "pid": str(proc.pid)}
