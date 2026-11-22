"""Local file storage for original uploads. Paths come from UUIDs, never from user input."""
import os
import re
import uuid
from pathlib import Path

from app.core.exceptions import InvalidInputError

_SUFFIX = re.compile(r"^\.[a-z0-9]{1,8}$")


class FileStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, owner_id: uuid.UUID, source_id: uuid.UUID, data: bytes, suffix: str) -> str:
        if not _SUFFIX.match(suffix):
            raise InvalidInputError("Invalid file extension.")
        relative = f"{owner_id}/{source_id}{suffix}"
        target = self._resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)  # atomic: never leaves a half-written file
        return relative

    def read(self, relative_path: str) -> bytes:
        return self._resolve(relative_path).read_bytes()

    def delete(self, relative_path: str) -> None:
        self._resolve(relative_path).unlink(missing_ok=True)

    def _resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if self.root not in candidate.parents:
            raise InvalidInputError("Invalid storage path.")
        return candidate