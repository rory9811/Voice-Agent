"""Task-owned evidence, retained until the run ends; never reads live buffers."""

from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
from pathlib import Path

from .contracts import AgentRequest, WorkArtifact


class FrozenAgentContext:
    def __init__(
        self,
        request: AgentRequest,
        workspace: Path,
        *,
        revision: str = "",
        artifact_dir: Path | None = None,
    ):
        self.active = True
        self.identity = (request.session_id, request.work_id)
        self.context = copy.deepcopy(request.context)
        self.workspace = workspace.resolve()
        self.artifact_dir = artifact_dir
        self.inputs = {}
        input_dir = self.workspace / "inputs" / revision
        input_dir.mkdir(parents=True, exist_ok=True)
        for item in request.inputs:
            if item.name in self.inputs:
                raise ValueError("duplicate input filename")
            path = input_dir / item.name
            path.write_bytes(item.data)
            self.inputs[item.name] = {
                "name": item.name,
                "path": str(path),
                "mime_type": item.mime_type,
                "size_bytes": len(item.data),
                "sha256": hashlib.sha256(item.data).hexdigest(),
            }

    def fetch(self, identity: tuple[str, str], arguments: dict):
        if not self.active or identity != self.identity:
            raise ValueError("context lease does not belong to an active run")
        if set(arguments) - {"section", "offset", "max_chars"}:
            raise ValueError("unknown context arguments")
        section = arguments.get("section", "inventory")
        if section == "inventory":
            value = {
                "sections": ["snapshot", "inputs"],
                "inputs": list(self.inputs.values()),
            }
        elif section == "snapshot":
            value = self.context
        elif section == "inputs":
            value = list(self.inputs.values())
        else:
            raise ValueError("unknown frozen context section")
        offset, limit = arguments.get("offset", 0), arguments.get("max_chars", 8000)
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 16000
        ):
            raise ValueError("invalid context bounds")
        text = json.dumps(value, ensure_ascii=False)
        return {
            "text": text[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(text) else None,
        }

    def artifact(self, name: str) -> WorkArtifact:
        path = (self.workspace / name).resolve()
        if not path.is_relative_to(self.workspace) or not path.is_file():
            raise ValueError(
                "artifact must be an existing file inside this run workspace"
            )
        if path.is_relative_to(self.workspace / "inputs"):
            raise ValueError("input files are not generated artifacts")
        data = path.read_bytes()
        if self.artifact_dir is not None:
            # Keep old Work downloads stable when a continuation edits the same file.
            archived = self.artifact_dir / path.relative_to(self.workspace)
            archived.parent.mkdir(parents=True, exist_ok=True)
            archived.write_bytes(data)
            path = archived
        return WorkArtifact(
            str(path),
            mimetypes.guess_type(path)[0] or "application/octet-stream",
            len(data),
            hashlib.sha256(data).hexdigest(),
        )
