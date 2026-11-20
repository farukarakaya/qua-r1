# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""Per-session access to imported PDFs, kept beside (never in) the syncable log."""

from pathlib import Path

from trading_r1.state.documents import DOCUMENT_SESSION_PREFIX
from trading_r1.state.fileio import MISSING, PrivateFileError, atomic_write_text, read_private_json


def _path(directory: Path, session_id: str) -> Path:
    return directory / f"{session_id}.pdfs.json"


def pdf_sync_excluded(directory: Path, session_id: str) -> bool:
    """An override is also a permanent exclusion, even if unreadable or set to off."""
    if session_id.startswith(DOCUMENT_SESSION_PREFIX):
        return True
    try:
        _path(directory, session_id).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        pass  # never upload if the marker cannot be checked
    return True


def pdfs_enabled(directory: Path, session_id: str | None) -> bool:
    if not session_id:
        return False
    try:
        value = read_private_json(_path(directory, session_id))
    except PrivateFileError:
        return False
    if value is MISSING:
        return session_id.startswith(DOCUMENT_SESSION_PREFIX)
    return value is True


def set_pdfs_enabled(directory: Path, session_id: str, enabled: bool) -> None:
    # Turning off an ordinary session that's never used PDFs must not exclude it from sync.
    if enabled or pdf_sync_excluded(directory, session_id):
        atomic_write_text(
            _path(directory, session_id), "true\n" if enabled else "false\n", durable=True
        )


def reset_pdfs_on_delete(log: Path) -> None:
    """Revoke access before discarding the transcript, but keep its upload exclusion."""
    if log.exists() or _path(log.parent, log.stem).exists():
        set_pdfs_enabled(log.parent, log.stem, False)
