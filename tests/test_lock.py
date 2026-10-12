import json

from apphelper.core.lock import PasswordLock


def test_set_and_verify(tmp_path):
    lock = PasswordLock(tmp_path)
    assert not lock.is_set() and not lock.verify("x")
    lock.set("1234")
    assert lock.is_set()
    assert lock.verify("1234") and not lock.verify("12345") and not lock.verify("")
    raw = (tmp_path / "lock.json").read_text()
    assert "1234" not in raw and set(json.loads(raw)) == {"salt", "iterations", "hash"}


def test_corrupt_file_counts_as_unset(tmp_path):
    (tmp_path / "lock.json").write_text("{broken")
    assert not PasswordLock(tmp_path).is_set()
