"""Щоденні бекапи бази з перевіркою відновлення.

Історія цін не відтворюється: старих цін на сайтах уже немає, і ця база —
єдина копія. Тому бекап вважається зробленим, лише коли пройшов усі кроки:

  1. копія через SQLite backup API — те саме, що `.backup` у консолі sqlite3.
     Не `cp`: у режимі WAL поруч лежать `-wal` і `-shm` з даними, яких ще
     немає в основному файлі, а `cp` посеред запису дає биту копію;
  2. `PRAGMA integrity_check` на копії;
  3. архів tar.xz (база + телеметрія + маніфест із кількістю рядків);
  4. ВІДНОВЛЕННЯ з архіву в тимчасову теку: цілісність і кількість рядків
     мають збігтися з маніфестом. Бекап, з якого жодного разу не
     відновлювались, — не бекап;
  5. вивантаження ПОЗА машину (Telegram і/або scp). Копія на тому ж диску
     захищає від помилки в програмі, але не від смерті диска;
  6. ротація: локально тримаємо BACKUP_KEEP останніх, старші прибираємо.

Кожна спроба записується в телеметрію; невдалий бекап бачить сигнал тиші.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import DateTime, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from . import notify, ops
from .config import DATA_DIR

log = logging.getLogger(__name__)

BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(DATA_DIR / "backups")))
KEEP = int(os.getenv("BACKUP_KEEP", "7"))
# Куди ще класти копію поза машиною: `користувач@хост:тека` для scp.
REMOTE = os.getenv("BACKUP_REMOTE", "").strip()
# Telegram як сховище поза машиною — вмикається, якщо є токен і чат.
# Тимчасове рішення: бот не надішле файл більший за 50 МБ, а база росте.
TELEGRAM = os.getenv("BACKUP_TELEGRAM", "1") not in ("0", "false", "")
# Хмарне сховище через rclone, напр. `gdrive:realty-backups` (Google Drive).
# Доступ до акаунта дає людина одноразово (`rclone config`); ми лише копіюємо.
RCLONE_REMOTE = os.getenv("BACKUP_RCLONE_REMOTE", "").strip()
# Щоденний ритм: `--if-due` робить бекап, лише якщо останній успішний старший.
DUE_AFTER_HOURS = float(os.getenv("BACKUP_EVERY_HOURS", "20"))
PREFIX = "realty-backup-"
# Один бекап на машину (рецензія E9, D53): нічний бекап на старті вікна 04:10 і таймер
# 04:30 (`--if-due`) інакше могли б іти разом — два архіви xz на HDD, і нічний за
# стелею 15 хв «не вдався б», хоча інший удався. flock знімається сам, коли процес
# помирає.
LOCK_NAME = "backup.lock"
# Залишки бекапу, убитого посеред роботи (тимчасова тека з копіями баз, .part):
# прибираються, коли старші за стелю кроку «бекап» (runner.TASK_TIMEOUTS, 15 хв).
LEFTOVER_AGE_S = 15 * 60
# Таблиці, за кількістю рядків у яких звіряється відновлення.
# listing_events — журнал зняттів і повернень Блоку 1 (E8, D52): відновлення з архіву
# звіряє й його. Таблиці, якої в архіві немає (старі архіви), row_counts пропускає.
KEY_TABLES = ("listings", "price_events", "properties", "check_events", "data_reports",
              "listing_events")


class BackupRecord(ops.OpsBase):
    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=ops._now, index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")   # ok | failed
    file: Mapped[str | None] = mapped_column(String(200))
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    rows: Mapped[str | None] = mapped_column(Text)          # JSON {таблиця: рядків}
    restored_ok: Mapped[int] = mapped_column(Integer, default=0)
    offsite: Mapped[str | None] = mapped_column(Text)        # куди вивантажено
    message: Mapped[str | None] = mapped_column(Text)


@dataclass
class BackupResult:
    status: str
    file: str | None = None
    size: int = 0
    sha256: str | None = None
    rows: dict = field(default_factory=dict)
    restored_ok: bool = False
    offsite: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)


# --- Кроки --------------------------------------------------------------------------


def _db_path(url: str) -> Path:
    if not url.startswith("sqlite:///"):
        raise ValueError(f"бекап уміє лише SQLite, а не {url.split(':')[0]}")
    return Path(url[len("sqlite:///"):])


def snapshot_db(src: Path, dst: Path) -> None:
    """Узгоджена копія працюючої бази через backup API."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    target = sqlite3.connect(dst)
    try:
        with target:
            source.backup(target, pages=4096)
    finally:
        target.close()
        source.close()


def integrity(path: Path) -> str:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()


def row_counts(path: Path, tables=KEY_TABLES) -> dict[str, int]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                for t in tables if t in have}
    finally:
        con.close()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_archive(archive: Path, expected_rows: dict | None = None) -> dict:
    """Відновлює архів у тимчасову теку й перевіряє, що дані на місці.

    Повертає {"integrity": ..., "rows": {...}, "match": bool}. Нічого не пише
    поза тимчасовою текою — робоча база не зачіпається.
    """
    with tempfile.TemporaryDirectory(prefix="realty-restore-") as tmp:
        with tarfile.open(archive, "r:xz") as tar:
            tar.extractall(tmp, filter="data")
        db = Path(tmp) / "realty.db"
        manifest = json.loads((Path(tmp) / "manifest.json").read_text())
        check = integrity(db)
        rows = row_counts(db)
        expected = expected_rows if expected_rows is not None else manifest["rows"]
        return {"integrity": check, "rows": rows, "manifest": manifest,
                "match": check == "ok" and rows == expected}


def _upload_scp(archive: Path) -> str:
    host, _, folder = REMOTE.partition(":")
    folder = folder or "."
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host]
    subprocess.run([*ssh, f"mkdir -p {folder}"], check=True, timeout=60,
                   capture_output=True)
    subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                    str(archive), f"{host}:{folder}/"], check=True, timeout=600,
                   capture_output=True)
    # Звіряємо розмір на тому боці: «scp повернув 0» ще не доказ.
    out = subprocess.run([*ssh, f"stat -c %s {folder}/{archive.name} 2>/dev/null || "
                                f"stat -f %z {folder}/{archive.name}"],
                         check=True, timeout=60, capture_output=True, text=True)
    remote_size = int(out.stdout.strip().splitlines()[-1])
    if remote_size != archive.stat().st_size:
        raise RuntimeError(f"розмір на {host} ({remote_size}) ≠ локальному")
    # Ротація на тому боці — лише наші файли.
    subprocess.run([*ssh, f"cd {folder} && ls -1t {PREFIX}*.tar.xz 2>/dev/null "
                          f"| tail -n +{KEEP + 1} | xargs -r rm -f --"],
                   timeout=60, capture_output=True)
    return f"scp:{REMOTE}"


def _rclone() -> str:
    binary = os.getenv("RCLONE_BIN", "").strip() or shutil.which("rclone")
    local = Path.home() / ".local" / "bin" / "rclone"
    if not binary and local.exists():
        binary = str(local)
    if not binary:
        raise RuntimeError("rclone не знайдено (~/.local/bin/rclone або PATH)")
    return binary


def _rclone_run(*args: str, timeout: int = 900) -> str:
    r = subprocess.run([_rclone(), *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        # У помилках rclone шляхи й назви віддалених тек, але не токени:
        # вони лежать у ~/.config/rclone/rclone.conf і в вивід не потрапляють.
        raise RuntimeError(f"rclone {args[0]}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def _upload_rclone(archive: Path) -> str:
    """Копія в хмару (Google Drive тощо). Розмір звіряємо на тому боці."""
    _rclone_run("copy", "--no-traverse", str(archive), RCLONE_REMOTE)
    listing = json.loads(_rclone_run("lsjson", "--files-only", RCLONE_REMOTE, timeout=120))
    remote = {item["Name"]: item["Size"] for item in listing}
    size = remote.get(archive.name)
    if size is None:
        raise RuntimeError(f"після копіювання {archive.name} немає в {RCLONE_REMOTE}")
    if size != archive.stat().st_size:
        raise RuntimeError(f"розмір у {RCLONE_REMOTE} ({size}) ≠ локальному")
    # Ротація в хмарі — тільки наші архіви, найновіші лишаються.
    ours = sorted((n for n in remote if n.startswith(PREFIX) and n.endswith(".tar.xz")),
                  reverse=True)
    for old in ours[KEEP:]:
        _rclone_run("deletefile", f"{RCLONE_REMOTE.rstrip('/')}/{old}", timeout=120)
    return f"rclone:{RCLONE_REMOTE}"


def _upload_telegram(archive: Path, rows: dict, sha: str) -> str:
    caption = (f"Бекап {archive.name}\n"
               f"{socket.gethostname()} · {archive.stat().st_size / 1e6:.1f} МБ\n"
               f"оголошень {rows.get('listings', '?')}, "
               f"подій ціни {rows.get('price_events', '?')}\n"
               f"sha256 {sha[:16]}…")
    msg_id = notify.send_document(archive, caption)
    return f"telegram:{msg_id}"


def prune(folder: Path = None, keep: int = KEEP) -> list[str]:
    folder = folder or BACKUP_DIR
    files = sorted(folder.glob(f"{PREFIX}*.tar.xz"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    removed = []
    for old in files[keep:]:
        old.unlink()
        removed.append(old.name)
    # Залишки бекапу, який убили (SIGKILL, вимкнення): тимчасова тека з повною копією
    # баз (~120 МБ) і недописаний архів. Лише старші за стелю бекапу — свіжі можуть
    # належати бекапу, що йде (рецензія E9, D53).
    cutoff = time.time() - LEFTOVER_AGE_S
    for p in [*folder.glob(f"{PREFIX}*"), *folder.glob("*.part")]:
        try:
            if p.name.endswith((".tar.xz", ".sha256")) or p.stat().st_mtime > cutoff:
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
            removed.append(p.name)
        except OSError:
            continue
    return removed


def _acquire(dest: Path, wait_s: float):
    """(файл замка або None, чи довелося чекати чужий бекап)."""
    dest.mkdir(parents=True, exist_ok=True)
    fh = open(dest / LOCK_NAME, "a+")
    deadline = time.monotonic() + max(0.0, wait_s)
    waited = False
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh, waited
        except BlockingIOError:
            left = deadline - time.monotonic()
            if left <= 0:
                fh.close()
                return None, waited
            waited = True
            time.sleep(min(5.0, left))


def busy(dest: Path | None = None) -> bool:
    """Чи йде зараз інший бекап (замок не береться надовго — лише спроба)."""
    fh, _ = _acquire(dest or BACKUP_DIR, 0)
    if fh is None:
        return True
    fcntl.flock(fh, fcntl.LOCK_UN)
    fh.close()
    return False


def _result_of(rec: "BackupRecord") -> BackupResult:
    return BackupResult(rec.status, file=rec.file, size=rec.size or 0, sha256=rec.sha256,
                        rows=json.loads(rec.rows or "{}"), restored_ok=bool(rec.restored_ok),
                        offsite=[x for x in (rec.offsite or "").split(", ") if x],
                        problems=[rec.message] if rec.message else [])


# --- Прогін --------------------------------------------------------------------------


def run(db_url: str | None = None, dest: Path | None = None,
        upload: bool = True, wait_s: float = 0.0) -> BackupResult:
    """Бекап. Інший бекап уже йде — чекаємо його до `wait_s` і віддаємо ЙОГО результат
    (другий архів поспіль лише навантажив би диск); не дочекались — статус «busy»."""
    dest = dest or BACKUP_DIR
    ops.init_ops()
    fh, waited = _acquire(dest, wait_s)
    if fh is None:
        return BackupResult("busy", problems=["інший бекап ще йде (backup.lock)"])
    try:
        if waited:
            last = last_attempt()
            if last is not None and last.status != "running":
                log.info("бекап: дочекались іншого бекапу (№%s, %s) — його результат", last.id,
                         last.status)
                return _result_of(last)
        return _run(db_url, dest, upload)
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _run(db_url: str | None, dest: Path, upload: bool) -> BackupResult:
    from .config import DB_URL

    rec_id = _start_record()
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = dest / f"{PREFIX}{stamp}.tar.xz"
    res = BackupResult("failed", file=archive.name)
    try:
        src = _db_path(db_url or DB_URL)
        ops_src = _db_path(ops.OPS_DB_URL)
        with tempfile.TemporaryDirectory(prefix="realty-backup-", dir=dest) as tmp:
            tmp = Path(tmp)
            snapshot_db(src, tmp / "realty.db")
            check = integrity(tmp / "realty.db")
            if check != "ok":
                raise RuntimeError(f"копія не пройшла integrity_check: {check[:200]}")
            res.rows = row_counts(tmp / "realty.db")
            if ops_src.exists():
                snapshot_db(ops_src, tmp / "ops.db")
            manifest = {"created": datetime.now().isoformat(timespec="seconds"),
                        "host": socket.gethostname(), "source": str(src),
                        "rows": res.rows, "integrity": check}
            (tmp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False,
                                                          indent=1))
            partial = archive.with_suffix(".part")
            with tarfile.open(partial, "w:xz", preset=6) as tar:
                for name in ("realty.db", "ops.db", "manifest.json"):
                    if (tmp / name).exists():
                        tar.add(tmp / name, arcname=name)
            partial.rename(archive)
        res.size = archive.stat().st_size
        res.sha256 = _sha256(archive)
        (archive.parent / (archive.name + ".sha256")).write_text(
            f"{res.sha256}  {archive.name}\n")

        restored = verify_archive(archive, res.rows)
        res.restored_ok = restored["match"]
        if not res.restored_ok:
            raise RuntimeError(f"відновлення не збіглось: {restored['integrity']}, "
                               f"{restored['rows']} ≠ {res.rows}")

        if upload:
            targets = []
            if RCLONE_REMOTE:
                targets.append(("rclone", lambda: _upload_rclone(archive)))
            if TELEGRAM and notify.configured():
                targets.append(("telegram", lambda: _upload_telegram(archive, res.rows,
                                                                     res.sha256)))
            if REMOTE:
                targets.append(("scp", lambda: _upload_scp(archive)))
            if not targets:
                res.problems.append("немає жодного місця поза машиною: задайте "
                                    "TELEGRAM_* або BACKUP_REMOTE")
            for name, go in targets:
                try:
                    res.offsite.append(go())
                except Exception as e:
                    res.problems.append(f"{name}: {str(e)[:300]}")
        res.pruned = prune(dest)
        # Успіх — лише коли копія цілісна, відновлюється І лежить поза машиною.
        # Свідомо локальна копія (--no-upload, напр. для перенесення бази) —
        # окремий статус «local»: сигнал тиші її успішним бекапом не вважає.
        if not res.restored_ok:
            res.status = "failed"
        elif res.offsite:
            res.status = "ok"
        else:
            res.status = "local" if not upload else "failed"
    except Exception as e:
        res.problems.append(f"{type(e).__name__}: {str(e)[:400]}")
        log.exception("бекап не вдався")
    finally:
        _finish_record(rec_id, res)
    return res


def _start_record() -> int:
    with ops.ops_session() as s:
        r = BackupRecord(status="running")
        s.add(r)
        s.flush()
        return r.id


def _finish_record(rec_id: int, res: BackupResult) -> None:
    with ops.ops_session() as s:
        r = s.get(BackupRecord, rec_id)
        r.status = res.status
        r.file = res.file
        r.size = res.size
        r.sha256 = res.sha256
        r.rows = json.dumps(res.rows)
        r.restored_ok = int(res.restored_ok)
        r.offsite = ", ".join(res.offsite) or None
        r.message = "; ".join(res.problems) or None


def last_success_at() -> datetime | None:
    ops.init_ops()
    with ops.ops_session() as s:
        return s.scalar(select(func.max(BackupRecord.created_at))
                        .where(BackupRecord.status == "ok"))


def last_attempt() -> BackupRecord | None:
    ops.init_ops()
    with ops.ops_session() as s:
        return s.scalars(select(BackupRecord).order_by(BackupRecord.id.desc())
                         .limit(1)).first()


def attempt(rec_id: int) -> BackupRecord | None:
    ops.init_ops()
    with ops.ops_session() as s:
        return s.get(BackupRecord, rec_id)


def first_attempt_after(rec_id: int) -> BackupRecord | None:
    """Найперша спроба, почата після спроби `rec_id` (0 — будь-яка)."""
    ops.init_ops()
    with ops.ops_session() as s:
        return s.scalars(select(BackupRecord).where(BackupRecord.id > rec_id)
                         .order_by(BackupRecord.id).limit(1)).first()


def is_due(hours: float = DUE_AFTER_HOURS) -> bool:
    last = last_success_at()
    return last is None or ops._now() - last > timedelta(hours=hours)


def restore(archive: Path, target: Path) -> dict:
    """Розгортає архів у НОВИЙ файл. Наявний файл не перезаписується ніколи:
    підміну робочої бази людина робить сама, зупинивши збір."""
    if target.exists():
        raise FileExistsError(f"{target} уже існує — обираю не перезаписувати")
    result = verify_archive(archive)
    if not result["match"]:
        raise RuntimeError(f"архів пошкоджений: {result['integrity']}")
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(archive, "r:xz") as tar:
            tar.extract("realty.db", tmp, filter="data")
        shutil.move(Path(tmp) / "realty.db", target)
    return result


def as_dict(res: BackupResult) -> dict:
    return asdict(res)
