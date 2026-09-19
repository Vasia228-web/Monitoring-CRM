"""Бекап: узгоджена копія, відновлення з архіву, вивантаження поза машину."""
from __future__ import annotations

import json
import sqlite3
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from realty import backup, notify, ops  # noqa: E402


def _make_db(path: Path, listings: int = 5) -> None:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")      # як у робочій базі під навантаженням
    con.execute("CREATE TABLE listings (id INTEGER PRIMARY KEY, price REAL)")
    con.execute("CREATE TABLE price_events (id INTEGER PRIMARY KEY, listing_id INT)")
    con.executemany("INSERT INTO listings(price) VALUES (?)",
                    [(1000.0 + i,) for i in range(listings)])
    con.execute("INSERT INTO price_events(listing_id) VALUES (1)")
    con.commit()
    # З'єднання лишаємо відкритим: частина даних ще лежить у -wal, і копія
    # через `cp` основного файлу їх би загубила.
    return con


class _FakeTelegram(BaseHTTPRequestHandler):
    received: list[dict] = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        type(self).received.append({"path": self.path, "size": len(body)})
        out = json.dumps({"ok": True, "result": {"message_id": len(self.received)}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", engine)
    monkeypatch.setattr(ops, "OPS_DB_URL", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=engine, expire_on_commit=False, future=True))
    ops.OpsBase.metadata.create_all(engine)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeTelegram)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _FakeTelegram.received = []
    monkeypatch.setattr(notify, "API_BASE", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:секрет")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(backup, "REMOTE", "")
    monkeypatch.setattr(backup, "TELEGRAM", True)
    yield tmp_path
    srv.shutdown()


def test_backup_is_verified_by_restoring_it(env):
    con = _make_db(env / "live.db", listings=7)
    try:
        res = backup.run(db_url=f"sqlite:///{env / 'live.db'}", dest=env / "bk")
    finally:
        con.close()
    assert res.status == "ok", res.problems
    assert res.rows == {"listings": 7, "price_events": 1}
    assert res.restored_ok
    assert res.offsite == ["telegram:1"]
    # До Telegram пішов саме файл архіву, і токен у шляху не світиться в результаті.
    assert _FakeTelegram.received[0]["path"].endswith("/sendDocument")
    assert "секрет" not in json.dumps(backup.as_dict(res))

    archive = env / "bk" / res.file
    check = backup.verify_archive(archive)
    assert check["match"] and check["rows"]["listings"] == 7
    with ops.ops_session() as s:
        rec = s.query(backup.BackupRecord).one()
        assert rec.status == "ok" and rec.restored_ok == 1


def test_restore_never_overwrites_an_existing_file(env):
    con = _make_db(env / "live.db")
    con.close()
    res = backup.run(db_url=f"sqlite:///{env / 'live.db'}", dest=env / "bk")
    archive = env / "bk" / res.file
    target = env / "restored.db"
    target.write_text("щось цінне")
    with pytest.raises(FileExistsError):
        backup.restore(archive, target)
    assert target.read_text() == "щось цінне"
    target.unlink()
    backup.restore(archive, target)
    assert backup.row_counts(target)["listings"] == 5


def test_corrupted_archive_is_detected(env):
    con = _make_db(env / "live.db")
    con.close()
    res = backup.run(db_url=f"sqlite:///{env / 'live.db'}", dest=env / "bk")
    archive = env / "bk" / res.file
    # Підміняємо базу в архіві на урізану — маніфест каже 5 рядків, а їх 1.
    work = env / "tamper"
    work.mkdir()
    with tarfile.open(archive, "r:xz") as tar:
        tar.extractall(work, filter="data")
    c = sqlite3.connect(work / "realty.db")
    c.execute("DELETE FROM listings WHERE id > 1")
    c.commit()
    c.close()
    bad = env / "bad.tar.xz"
    with tarfile.open(bad, "w:xz") as tar:
        for name in ("realty.db", "manifest.json"):
            tar.add(work / name, arcname=name)
    assert not backup.verify_archive(bad)["match"]


def test_backup_without_offsite_copy_is_not_a_success(env, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    con = _make_db(env / "live.db")
    con.close()
    res = backup.run(db_url=f"sqlite:///{env / 'live.db'}", dest=env / "bk")
    assert res.restored_ok
    assert res.status == "failed"
    assert any("поза машиною" in p for p in res.problems)
    assert backup.last_success_at() is None


def test_only_the_last_few_backups_are_kept(env, monkeypatch):
    folder = env / "bk"
    folder.mkdir()
    import os, time
    for i in range(10):
        f = folder / f"{backup.PREFIX}2026090{i}.tar.xz"
        f.write_bytes(b"x")
        os.utime(f, (time.time() - 1000 + i, time.time() - 1000 + i))
    (folder / "чуже.tar.xz").write_bytes(b"x")
    removed = backup.prune(folder, keep=3)
    assert len(removed) == 7
    left = sorted(p.name for p in folder.iterdir())
    assert "чуже.tar.xz" in left          # не наше — не чіпаємо
    assert len([n for n in left if n.startswith(backup.PREFIX)]) == 3


def test_telegram_errors_never_leak_the_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:дуже-секретний")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(notify, "API_BASE", "http://127.0.0.1:9")   # нікого немає
    with pytest.raises(notify.NotifyError) as e:
        notify.send_message("тест")
    assert "дуже-секретний" not in str(e.value)


def test_deliberately_local_copy_is_not_counted_as_backup(env):
    con = _make_db(env / "live.db")
    con.close()
    res = backup.run(db_url=f"sqlite:///{env / 'live.db'}", dest=env / "bk", upload=False)
    assert res.status == "local" and res.restored_ok
    assert backup.last_success_at() is None


def _fake_rclone(tmp_path, store: Path, log: Path) -> Path:
    """Підробка rclone: копіює у «хмару»-теку й пише виклики в журнал."""
    store.mkdir(exist_ok=True)
    binary = tmp_path / "rclone"
    binary.write_text(f"""#!/bin/bash
echo "$@" >> {log}
cmd="$1"; shift
args=("$@")
case "$cmd" in
  copy) cp "${{args[@]: -2:1}}" {store}/ ;;          # copy [прапорці] ФАЙЛ remote:
  lsjson) python3 -c "
import json, os
d = '{store}'
print(json.dumps([{{'Name': n, 'Size': os.path.getsize(os.path.join(d, n))}}
                  for n in os.listdir(d)]))" ;;
  deletefile) rm -f "{store}/$(basename "${{args[@]: -1:1}}")" ;;
esac
""")
    binary.chmod(0o755)
    return binary


def test_backup_goes_to_the_cloud_and_rotates_there(env, monkeypatch):
    store, log = env / "cloud", env / "rclone.log"
    monkeypatch.setenv("RCLONE_BIN", str(_fake_rclone(env, store, log)))
    monkeypatch.setattr(backup, "RCLONE_REMOTE", "gdrive:realty-backups")
    monkeypatch.setattr(backup, "TELEGRAM", False)
    for i in range(8):                      # старі архіви вже в «хмарі»
        (store / f"{backup.PREFIX}2026090{i}-000000.tar.xz").write_bytes(b"x")
    (store / "чуже.tar.xz").write_bytes(b"x")

    con = _make_db(env / "live.db", listings=3)
    con.close()
    res = backup.run(db_url=f"sqlite:///{env / 'live.db'}", dest=env / "bk")

    assert res.status == "ok" and res.offsite == ["rclone:gdrive:realty-backups"]
    assert (store / res.file).stat().st_size == res.size     # долетів цілим
    ours = sorted(p.name for p in store.iterdir() if p.name.startswith(backup.PREFIX))
    assert len(ours) == backup.KEEP and res.file in ours     # старі прибрано
    assert (store / "чуже.tar.xz").exists()                  # чуже не чіпаємо


def test_cloud_upload_failure_is_not_a_success(env, monkeypatch):
    broken = env / "rclone-broken"
    broken.write_text("#!/bin/bash\necho 'directory not found' >&2\nexit 3\n")
    broken.chmod(0o755)
    monkeypatch.setenv("RCLONE_BIN", str(broken))
    monkeypatch.setattr(backup, "RCLONE_REMOTE", "gdrive:realty-backups")
    monkeypatch.setattr(backup, "TELEGRAM", False)
    con = _make_db(env / "live.db")
    con.close()
    res = backup.run(db_url=f"sqlite:///{env / 'live.db'}", dest=env / "bk")
    assert res.status == "failed"
    assert any("rclone" in p for p in res.problems)
    assert backup.last_success_at() is None
