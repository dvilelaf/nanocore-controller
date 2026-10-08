import copy
import json
import os
import stat
from pathlib import Path

import pytest

from nanocore_controller import backup_io
from nanocore_controller.backup_io import load_backup, write_backup
from nanocore_controller.errors import BackupError, ValidationError

FIXTURE = Path(__file__).parent / "fixtures" / "backup-v1.json"


@pytest.fixture
def document():
    return json.loads(FIXTURE.read_text())


def listing(directory):
    return sorted(entry.name for entry in directory.iterdir())


def test_round_trips_a_valid_backup(tmp_path, document):
    path = tmp_path / "backup.json"
    write_backup(path, document)
    assert load_backup(path) == document
    assert listing(tmp_path) == ["backup.json"]


def test_written_file_is_private_and_formatted(tmp_path, document):
    document["preset"]["name"] = "Café ♫"
    path = tmp_path / "backup.json"
    write_backup(path, document)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    assert "Café ♫" in text
    assert text.startswith('{\n  "format"')


def test_refuses_an_existing_path_and_keeps_its_content(tmp_path, document):
    path = tmp_path / "backup.json"
    path.write_text("precious")
    with pytest.raises(BackupError):
        write_backup(path, document)
    assert path.read_text() == "precious"
    assert listing(tmp_path) == ["backup.json"]


def test_refuses_symlinks_even_dangling(tmp_path, document):
    target = tmp_path / "target.json"
    target.write_text("precious")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "nowhere.json")
    for path in (link, dangling):
        with pytest.raises(BackupError):
            write_backup(path, document)
    assert target.read_text() == "precious"
    assert not (tmp_path / "nowhere.json").exists()
    assert listing(tmp_path) == ["dangling.json", "link.json", "target.json"]


def test_non_finite_and_unserialisable_documents_leave_nothing(tmp_path, document):
    for bad in (float("nan"), float("inf"), object(), {1, 2}):
        broken = copy.deepcopy(document)
        broken["preset"]["extra"] = bad
        with pytest.raises(BackupError):
            write_backup(tmp_path / "backup.json", broken)
    assert listing(tmp_path) == []


def test_missing_directory_is_a_backup_error(tmp_path, document):
    with pytest.raises(BackupError):
        write_backup(tmp_path / "missing" / "backup.json", document)


@pytest.mark.parametrize("failing", ["link", "fsync", "write"])
def test_failures_leave_no_truncated_or_temp_file(tmp_path, document, monkeypatch, failing):
    def boom(*args, **kwargs):
        raise OSError("injected")

    if failing == "link":
        monkeypatch.setattr(backup_io.os, "link", boom)
    elif failing == "fsync":
        monkeypatch.setattr(backup_io.os, "fsync", boom)
    else:
        monkeypatch.setattr(backup_io.os, "fdopen", boom)
    with pytest.raises(BackupError):
        write_backup(tmp_path / "backup.json", document)
    assert listing(tmp_path) == []


def test_directory_fsync_failure_leaves_no_file(tmp_path, document, monkeypatch):
    real_fsync = os.fsync
    calls = []

    def fsync(fd):
        calls.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        if calls[-1]:
            raise OSError("injected")
        real_fsync(fd)

    monkeypatch.setattr(backup_io.os, "fsync", fsync)
    with pytest.raises(BackupError):
        write_backup(tmp_path / "backup.json", document)
    assert calls == [False, True]
    assert listing(tmp_path) == []


def test_fsyncs_the_file_then_the_directory(tmp_path, document, monkeypatch):
    real_fsync = os.fsync
    calls = []

    def fsync(fd):
        calls.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(backup_io.os, "fsync", fsync)
    write_backup(tmp_path / "backup.json", document)
    assert calls == [False, True]


def test_load_rejects_oversized_files(tmp_path, document):
    path = tmp_path / "backup.json"
    write_backup(path, document)
    size = path.stat().st_size
    assert load_backup(path, max_bytes=size) == document
    with pytest.raises(BackupError, match="too large"):
        load_backup(path, max_bytes=size - 1)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_load_rejects_non_finite_literals(tmp_path, document, literal):
    text = FIXTURE.read_text().replace('"preset_volume": 84', f'"preset_volume": {literal}')
    assert literal in text
    path = tmp_path / "backup.json"
    path.write_text(text)
    with pytest.raises(BackupError):
        load_backup(path)


@pytest.mark.parametrize(
    "content",
    [b"", b"{", b"not json", b"\xff\xfe\x00", b"[" * 100000, b'{"a": 1} trailing'],
)
def test_load_rejects_non_json(tmp_path, content):
    path = tmp_path / "backup.json"
    path.write_bytes(content)
    with pytest.raises(BackupError):
        load_backup(path)


def test_load_rejects_unreadable_paths(tmp_path):
    with pytest.raises(BackupError):
        load_backup(tmp_path / "missing.json")
    with pytest.raises(BackupError):
        load_backup(tmp_path)


@pytest.mark.parametrize("content", ["[]", "5", '"x"', "null"])
def test_load_requires_an_object_root(tmp_path, content):
    path = tmp_path / "backup.json"
    path.write_text(content)
    with pytest.raises(ValidationError):
        load_backup(path)


def test_load_validates_the_schema(tmp_path, document):
    document["preset"]["chain_order"] = [0, 1, 2, 3, 4, 5, 6, "x"]
    path = tmp_path / "backup.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ValidationError):
        load_backup(path)


def test_load_does_not_block_on_a_fifo(tmp_path):
    path = tmp_path / "pipe"
    os.mkfifo(path)
    with pytest.raises(BackupError):
        load_backup(path)
