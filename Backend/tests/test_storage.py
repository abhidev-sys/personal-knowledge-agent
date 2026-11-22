import uuid

import pytest

from app.core.exceptions import InvalidInputError
from app.ingestion.storage import FileStorage


def test_save_read_delete(tmp_path):
    storage = FileStorage(tmp_path)
    owner, source = uuid.uuid4(), uuid.uuid4()
    relative = storage.save(owner, source, b"%PDF-data", ".pdf")
    assert relative == f"{owner}/{source}.pdf"
    assert storage.read(relative) == b"%PDF-data"
    storage.delete(relative)
    with pytest.raises(FileNotFoundError):
        storage.read(relative)
    storage.delete(relative)  # deleting twice is harmless


def test_save_overwrites_atomically(tmp_path):
    storage = FileStorage(tmp_path)
    owner, source = uuid.uuid4(), uuid.uuid4()
    storage.save(owner, source, b"one", ".pdf")
    relative = storage.save(owner, source, b"two", ".pdf")
    assert storage.read(relative) == b"two"
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("bad", ["../outside", "a/../../outside", "/etc/passwd", "C:\\Windows\\win.ini", ""])
def test_path_traversal_is_rejected(tmp_path, bad):
    storage = FileStorage(tmp_path)
    with pytest.raises(InvalidInputError):
        storage.read(bad)
    with pytest.raises(InvalidInputError):
        storage.delete(bad)


@pytest.mark.parametrize("suffix", ["pdf", ".PDF", ".pdf/../x", "", ".waytoolongsuffix"])
def test_bad_suffix_is_rejected(tmp_path, suffix):
    with pytest.raises(InvalidInputError):
        FileStorage(tmp_path).save(uuid.uuid4(), uuid.uuid4(), b"x", suffix)