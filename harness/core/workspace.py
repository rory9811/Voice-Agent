"""Safe, bounded filesystem delivery for long delegate documents."""

from __future__ import annotations

import os
import tempfile

from .config import WorkspaceConfig
from .errors import HarnessError
from .models import DelegateRequest, WorkspaceFile


class WorkspaceNotConfiguredError(HarnessError):
    pass


class WorkspaceWriteError(HarnessError):
    pass


class WorkspaceWriter:
    def __init__(self, config: WorkspaceConfig) -> None:
        self._config = config

    @property
    def configured(self) -> bool:
        return self._config.root_path is not None

    def write_document(self, request: DelegateRequest, text: str) -> WorkspaceFile:
        root = self._config.root_path
        if root is None:
            raise WorkspaceNotConfiguredError("workspace is not configured")
        if len(text) > self._config.max_document_chars:
            raise WorkspaceWriteError("document exceeds configured workspace limit")
        directory = root / self._config.documents_subdir
        try:
            directory.mkdir(parents=True, exist_ok=True)
            filename = "delegate-{}.md".format(request.work_id[:12])
            target = (directory / filename).resolve()
            if directory.resolve() not in target.parents:
                raise WorkspaceWriteError("workspace path escaped configured root")
            fd, temporary = tempfile.mkstemp(
                prefix="delegate-", suffix=".tmp", dir=str(directory)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except WorkspaceWriteError:
            raise
        except OSError as exc:
            raise WorkspaceWriteError("workspace write failed") from exc
        return WorkspaceFile(
            path=str(target),
            relative_path=str(target.relative_to(root)),
            char_count=len(text),
        )
