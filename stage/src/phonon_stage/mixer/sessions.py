"""Mixer session store — save / list / load named snapshots.

A "session" is one of two things:

  * **full**  — a complete `MixerState.to_dict()` payload. Loading
    one wipes the live state and replaces it with the snapshot, then
    triggers a full reconcile so PipeWire follows. Use case: party
    preset, "ce que je faisais hier soir", studio template.

  * **fx-only** — just the `inserts` of one target (master or a
    specific output). Loading applies *only* to that target's
    chain; the rest of the live mix is untouched. Use case:
    "j'aime la chaîne que je viens de monter sur le master, je
    veux pouvoir y revenir sans rappeler tout le routage".

Identifiers are timestamps in a filesystem-safe ISO-compact form
(`2026-05-18T16-43-12`) — the operator never types them, they're
generated at save time. A free-text `comment` is the human label.

Layout on disk:

    /var/lib/phonon/sessions/
    ├── 2026-05-18T16-43-12.json   (scope=full or scope=fx-only)
    ├── 2026-05-19T09-12-04.json
    └── ...

Each file is self-describing — no manifest, no index. Listing the
directory and parsing each header is O(n) but n is small (~tens
to low hundreds at most). The store writes atomically (tmp + rename)
so a crash mid-save can't corrupt an existing snapshot.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path


SessionScope = Literal["full", "fx-only"]


@dataclass
class SessionMeta:
    """Header info for a session — what the UI's list view shows
    without needing to open the full payload. `target` is set only
    for fx-only sessions ("master" or "output:<id>")."""

    id: str
    timestamp: str
    comment: str
    scope: SessionScope
    target: str | None = None


@dataclass
class Session:
    """A full session record — header + payload. For scope=full,
    payload is a MixerState.to_dict() dict. For scope=fx-only,
    payload is a list of PluginInsert.to_dict() dicts."""

    meta: SessionMeta
    payload: Any = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.meta.id,
            "timestamp": self.meta.timestamp,
            "comment": self.meta.comment,
            "scope": self.meta.scope,
            "payload": self.payload,
        }
        if self.meta.target is not None:
            body["target"] = self.meta.target
        return body

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        meta = SessionMeta(
            id=str(data["id"]),
            timestamp=str(data["timestamp"]),
            comment=str(data.get("comment") or ""),
            scope=data["scope"],
            target=(str(data["target"]) if data.get("target") is not None else None),
        )
        return cls(meta=meta, payload=data.get("payload"))


class SessionStoreError(Exception):
    """Bad session id, missing file, parse error."""


def _safe_id(now: datetime) -> str:
    """ISO-8601 compact form — `:` replaced with `-` so the id is
    usable both as filename and URL path component. UTC second
    precision; collisions are vanishingly rare with single-operator
    use, but defended against below by an integer suffix on conflict."""
    return now.strftime("%Y-%m-%dT%H-%M-%S")


class SessionStore:
    """JSON-on-disk session store. Single directory, one file per
    session. All I/O methods are sync — the store is small enough
    that the FastAPI layer can call us directly from its async
    handlers without offloading."""

    def __init__(self, dir_path: Path) -> None:
        self._dir = dir_path

    # ── Save ────────────────────────────────────────────────────

    def save_full(self, state_dict: dict[str, Any], comment: str) -> Session:
        """Write a full-mixer snapshot. `state_dict` should be the
        output of MixerState.to_dict()."""
        return self._write(scope="full", target=None, payload=state_dict, comment=comment)

    def save_fx(
        self, target: str, inserts: list[dict[str, Any]], comment: str
    ) -> Session:
        """Write an fx-only snapshot for a single chain target.
        `target` is "master" or "output:<id>". `inserts` is the list
        of PluginInsert.to_dict() dicts in chain order."""
        if not target:
            msg = "save_fx requires a non-empty target"
            raise SessionStoreError(msg)
        return self._write(
            scope="fx-only", target=target, payload=inserts, comment=comment
        )

    def _write(
        self,
        scope: SessionScope,
        target: str | None,
        payload: Any,
        comment: str,
    ) -> Session:
        now = datetime.now(timezone.utc).astimezone()
        sid = _safe_id(now)
        # Collision guard — same-second saves get -1, -2 suffixes.
        candidate = sid
        suffix = 1
        while (self._dir / f"{candidate}.json").exists():
            candidate = f"{sid}-{suffix}"
            suffix += 1
        sid = candidate
        meta = SessionMeta(
            id=sid,
            timestamp=now.isoformat(timespec="seconds"),
            comment=comment.strip(),
            scope=scope,
            target=target,
        )
        session = Session(meta=meta, payload=payload)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self._dir / f"{sid}.json", session.to_dict())
        return session

    @staticmethod
    def _atomic_write(path: Path, body: dict[str, Any]) -> None:
        # tmp file in the same directory so os.replace stays atomic
        # on every POSIX filesystem we care about.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(path.parent), text=True
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(body, f, indent=2, sort_keys=False)
                f.write("\n")
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    # ── List / get / delete ─────────────────────────────────────

    def list(self) -> list[SessionMeta]:
        """Return every session's header info, newest first.
        Malformed files are skipped (logged silently — the operator
        can also delete them by hand)."""
        if not self._dir.exists():
            return []
        out: list[SessionMeta] = []
        for path in sorted(self._dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            try:
                session = Session.from_dict(data)
            except (KeyError, ValueError):
                continue
            out.append(session.meta)
        return out

    def get(self, session_id: str) -> Session:
        path = self._path_for(session_id)
        if not path.exists():
            msg = f"session {session_id!r} not found"
            raise SessionStoreError(msg)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            msg = f"session {session_id!r} unreadable: {exc}"
            raise SessionStoreError(msg) from exc
        try:
            return Session.from_dict(data)
        except (KeyError, ValueError) as exc:
            msg = f"session {session_id!r} malformed: {exc}"
            raise SessionStoreError(msg) from exc

    def delete(self, session_id: str) -> None:
        path = self._path_for(session_id)
        if not path.exists():
            msg = f"session {session_id!r} not found"
            raise SessionStoreError(msg)
        path.unlink()

    def _path_for(self, session_id: str) -> Path:
        # Reject ids that try to escape the sessions directory.
        if "/" in session_id or ".." in session_id or session_id.startswith("."):
            msg = f"invalid session id: {session_id!r}"
            raise SessionStoreError(msg)
        return self._dir / f"{session_id}.json"
