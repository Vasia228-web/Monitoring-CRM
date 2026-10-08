"""Нічна смуга одного хоста — окремий процес `cli.py night lane` (E9, D53).

Смуга лише питає й класифікує: той самий `engine.check_one`, що й у кроці циклу
(підпис, повторний 404 з перевіркою існування, гачки доказів Блоків 3/4 на тілі вже
отриманої відповіді — жодного зайвого запиту). У базу перевірки не пише: кожен
результат — рядок JSON у файл, який диригент застосовує пакетами ПІСЛЯ запобіжника.

Правила смуги:
  * темп — `pace` з плану (policy.pace(mode="night") = max(liveness delay,
    SOURCES.full_delay)): пауза старт-до-старту між БУДЬ-ЯКИМИ запитами смуги, хоч би
    на яку адресу хоста (www., m., картка API) — один потік на сайт;
  * після `stop_at` (epoch) нових запитів не видає — решта ключів «не дійшла черга»;
  * ключ, чий сайт чи джерело рядка вже під запобіжником (файл held.json, який
    диригент переписує після кожного пакета), не питає зовсім;
  * run.max_consecutive_blocks 401/403/429 поспіль або частка блокувань понад
    lanes.block_share (після block_min_requests запитів) — смуга стоїть до кінця ночі;
  * далі — дозбір identity свого джерела (identity_backfill.work), якщо лишився час, —
    ТИМ САМИМ потоком (IdentityGate): та сама пауза старт-до-старту, той самий дедлайн і
    ті самі лічильники блокувань (рецензія E9, D53); прохід стрічки LUN/flombu — лише
    стрічка (`scrape --no-detail --min-delay <темп>`), без сторінок деталей на чужих
    хостах.
SIGTERM диригента — рядок «done» із причиною й вихід.

Процес смуги запускає лише диригент: `cli.py night lane` без змінної LANE_ENV або без
замка циклу в руках батьківського процесу відмовляє (рецензія E9, D53).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from ..fetcher import BLOCKING_CODES, Fetcher
from ..liveness import engine, existence, queue
from . import codec

log = logging.getLogger(__name__)

# Змінна оточення процесу смуги (її хост) — так `cli.py night lane` знає, що його
# запустив диригент, а не людина.
LANE_ENV = "REALTY_NIGHT_LANE"
BLOCK_STOPS = ("blocks", "block_share")


def refusal(spec: dict) -> str | None:
    """Чому цей процес смуги НЕ має права питати сайти (None — має).

    Смуга не бере замка циклу сама — її права від диригента, що тримає замок. Ручний
    чи залишений `cli.py night lane --plan … --out …` інакше питав би сайти з
    мережею й без замка — паралельно з циклом (рецензія E9, D53)."""
    from ..runner import LOCK_PATH, lock_busy

    if not os.environ.get(LANE_ENV):
        return (f"смугу запускає лише нічний диригент (`cli.py night`): немає змінної "
                f"{LANE_ENV}")
    lock = Path(spec.get("lock_path") or LOCK_PATH)
    info = lock_busy(lock)
    pid = (info or {}).get("pid")
    if not info or not str(pid).isdigit() or int(pid) != os.getppid():
        return (f"замок циклу ({lock}) тримає не батьківський процес (диригент): "
                f"{info or 'ніхто'}")
    return None


class Pacer:
    """Пауза смуги: старт-до-старту не частіше за `pace`, незалежно від адреси."""

    def __init__(self, pace: float, clock) -> None:
        self.pace = float(pace)
        self.clock = clock
        self._last: float | None = None

    def next_slot(self) -> float:
        """Коли (за `clock.monotonic`) можна видати наступний запит."""
        now = self.clock.monotonic()
        return now if self._last is None else max(now, self._last + self.pace)

    def wait(self) -> None:
        now = self.clock.monotonic()
        if self._last is not None and now < self._last + self.pace:
            self.clock.sleep(self._last + self.pace - now)
            now = self.clock.monotonic()
        self._last = now


class PacedNet:
    """Фетчер смуги з паузою `Pacer` перед кожним запитом (і перевіркою існування).

    Запит, чий слот припадає на `stop_at` чи пізніше, не видається зовсім: відповідь
    «немає» (code 0, error deadline) — для класифікатора це «не визначено», для
    перевірки існування — «не відповіла» (не знімаємо)."""

    def __init__(self, fetcher, pacer: Pacer, *, stop_at: float, clock) -> None:
        self._fetcher = fetcher
        self._pacer = pacer
        self._stop_at = stop_at
        self._clock = clock

    def check(self, url, method="HEAD", delay=None, max_bytes=0):
        from ..fetcher import ProbeResult

        if self._clock.time() + (self._pacer.next_slot() - self._clock.monotonic()) \
                >= self._stop_at:
            return ProbeResult(code=0, method=method, url=url, final_url=url, error="deadline")
        self._pacer.wait()
        return self._fetcher.check(url, method=method, delay=delay, max_bytes=max_bytes)


class Lane:
    """Смуга за планом `spec` (див. conductor.lane_spec); результати — у `out`."""

    def __init__(self, spec: dict, out, *, fetcher, cfg, hooks=(), clock=time,
                 now_fn=None, identity_fn=None, snapshots=None) -> None:
        self.host = spec["host"]
        self.pace = float(spec["pace"])
        self.stop_at = float(spec["stop_at"])
        self.items = [codec.item_from_json(d) for d in spec["items"]]
        self.identity = spec.get("identity")
        self.held_path = Path(spec["held_path"]) if spec.get("held_path") else None
        self.block_share = float(spec["block_share"])
        self.block_min_requests = int(spec["block_min_requests"])
        self.max_consecutive = int(spec["max_consecutive_blocks"])
        self.family = spec.get("family")
        self.out = out
        self.cfg = cfg
        self.hooks = list(hooks)
        self.clock = clock
        self.now_fn = now_fn or queue._now
        self.identity_fn = identity_fn
        self.stats = engine.LaneStats(host=self.host)
        self.pacer = Pacer(self.pace, clock)
        # Лічильник — під паузою й дедлайном: рахуються лише справді видані запити.
        self.net = PacedNet(engine.CountingFetcher(fetcher, self.stats), self.pacer,
                            stop_at=self.stop_at, clock=clock)
        if snapshots is None:
            snapshots = existence.load_snapshots(cfg, existence.snapshot_sources(cfg))
        self.ctx = existence.Context(cfg=cfg, now=self.now_fn(), snapshots=snapshots)
        self.i = 0
        self.consecutive = 0
        self.skipped_held = 0
        # Запити дозбору identity цієї смуги (IdentityGate) — окремо від перевірок, але
        # у тих самих блокуваннях (поспіль і частка).
        self.id_requests = 0
        self.id_blocked = 0
        self.check_seconds = 0.0                       # від старту до останньої перевірки
        self.stopped: str | None = None
        self.finished = False
        self.started = clock.monotonic()
        self._held: set[str] = set()
        self._held_stamp = None

    # --- запобіжник --------------------------------------------------------------------

    def held(self) -> set[str]:
        if self.held_path is None:
            return self._held
        try:
            st = self.held_path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            return self._held
        if stamp != self._held_stamp:
            try:
                self._held = set(json.loads(self.held_path.read_text()).get("held") or ())
                self._held_stamp = stamp
            except (OSError, ValueError):
                pass                                   # пів-записаний файл — наступного разу
        return self._held

    def is_held(self, item) -> bool:
        held = self.held()
        return bool(held) and (self.family in held or any(r.source in held for r in item.rows))

    # --- робота ------------------------------------------------------------------------

    def late(self) -> bool:
        return self.clock.time() >= self.stop_at

    def _slot_epoch(self) -> float:
        """Коли (epoch) смуга видала б наступний запит з урахуванням паузи."""
        return self.clock.time() + (self.pacer.next_slot() - self.clock.monotonic())

    def _write(self, rec: dict) -> None:
        self.out.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.out.flush()

    def step(self) -> bool:
        """Наступний ключ; False — більше нічого (усе пройдено, дедлайн чи зупинка)."""
        if self.stopped or self.i >= len(self.items):
            return False
        if self._slot_epoch() >= self.stop_at:
            # Наступний запит уже не вкладається до stop_requests — решта «не дійшла черга».
            self.stopped = "deadline"
            return False
        idx, item = self.i, self.items[self.i]
        self.i += 1
        if self.is_held(item):
            self.skipped_held += 1
            self._write({"t": "skip", "i": idx, "why": "held"})
            return True
        outcome, result = engine.check_one(item, self.net, cfg=self.cfg, ctx=self.ctx,
                                           hooks=self.hooks, delay=self.pace,
                                           now_fn=self.now_fn, late=self.late)
        v = outcome.verdict
        self.stats.signatures[v.signature] = self.stats.signatures.get(v.signature, 0) + 1
        self._write({"t": "item", "i": idx, "at": outcome.at.isoformat(),
                     "verdict": codec.verdict_to_json(v), "capture": outcome.capture})
        self.check_seconds = round(self.clock.monotonic() - self.started, 1)
        if result.code in BLOCKING_CODES:
            self.stats.blocked += 1
            self.consecutive += 1
        else:
            self.consecutive = 0
        self.check_blocks()
        return True

    def check_blocks(self) -> bool:
        """5 відмов поспіль чи частка понад block_share (після block_min_requests) —
        смуга стоїть до кінця ночі. Рахуються й запити дозбору identity."""
        if self.stopped in BLOCK_STOPS:
            return True
        total = self.stats.requests + self.id_requests
        blocked = self.stats.blocked + self.id_blocked
        if self.consecutive >= self.max_consecutive:
            self.stopped = "blocks"
        elif total >= self.block_min_requests and blocked / total > self.block_share:
            self.stopped = "block_share"
        else:
            return False
        log.warning("%s: смуга стоїть до кінця ночі (%s: %d поспіль, %d із %d запитів)",
                    self.host, self.stopped, self.consecutive, blocked, total)
        return True

    def finish(self, stopped: str | None = None) -> dict:
        """Дозбір identity (якщо смугу не зупинили блокування й лишився час) і «done»."""
        if self.finished:
            return {}
        self.finished = True
        if stopped and not self.stopped:
            self.stopped = stopped
        identity = None
        interrupted: BaseException | None = None
        if (self.identity and self.identity_fn is not None and not self.late()
                and self.stopped not in (*BLOCK_STOPS, "sigterm")):
            try:
                identity = self.identity_fn(self.identity, self.stop_at,
                                            gate=IdentityGate(self))
            except SystemExit as e:                    # SIGTERM диригента посеред дозбору
                identity = {"status": "interrupted"}
                self.stopped = self.stopped or "sigterm"
                interrupted = e
            except Exception as e:                     # noqa: BLE001 — дозбір не важливіший за звіт
                log.exception("%s: дозбір identity впав", self.host)
                identity = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
            self._write({"t": "identity", "source": self.identity, "report": identity})
        summary = {"t": "done", "host": self.host, "requests": self.stats.requests,
                   "blocked": self.stats.blocked, "stopped": self.stopped,
                   "items": len(self.items), "done": self.i - self.skipped_held,
                   "skipped_held": self.skipped_held,
                   "not_reached": len(self.items) - self.i,
                   "seconds": round(self.clock.monotonic() - self.started, 1),
                   "check_seconds": self.check_seconds,
                   "identity_requests": self.id_requests,
                   "identity_blocked": self.id_blocked,
                   "signatures": dict(self.stats.signatures)}
        self._write(summary)
        if interrupted is not None:
            raise interrupted
        return summary

    def run(self) -> dict:
        while self.step():
            pass
        return self.finish()


class _GateLimiter:
    """Обмежувач фетчера дозбору: своя пауза (напр., 1,2 с DIM.RIA) І пауза смуги —
    старт-до-старту з УСІМА запитами смуги (перевірки, повтори 429 теж)."""

    def __init__(self, gate: "IdentityGate", inner) -> None:
        self.gate = gate
        self.inner = inner

    def wait(self, url: str, delay: float | None = None) -> None:
        self.inner.wait(url, delay)
        self.gate.wait()


class IdentityGate:
    """Дозбір identity у смузі — той самий потік запитів, що й перевірки смуги
    (рецензія E9, D53): пауза Pacer смуги, її дедлайн stop_at і її блокування. 401/403/
    429 дозбору рахуються в ті самі «5 поспіль» і «частку» — після них смуга стоїть
    («blocks»), диригент бачить це, друге вікно хост пропускає, двічі поспіль — хост
    чекає рішення власника."""

    def __init__(self, lane: Lane) -> None:
        self.lane = lane
        self.pace = lane.pace

    def stopped(self) -> bool:
        lane = self.lane
        return lane.stopped in BLOCK_STOPS or lane._slot_epoch() >= lane.stop_at

    def wait(self) -> None:
        """Перед кожним запитом дозбору: зупинено — виняток ДО запиту (картку не
        питали); інакше пауза смуги."""
        from ..identity_backfill import Stop

        if self.stopped():
            raise Stop(f"смугу {self.lane.host} зупинено ({self.lane.stopped or 'дедлайн'})")
        self.lane.pacer.wait()

    def observe(self, code: int) -> None:
        lane = self.lane
        lane.id_requests += 1
        if code in BLOCKING_CODES:
            lane.id_blocked += 1
            lane.consecutive += 1
        else:
            lane.consecutive = 0
        lane.check_blocks()

    def fetcher(self, delay: float, label: str) -> Fetcher:
        f = Fetcher(delay=delay, label=label)
        f.limiter = _GateLimiter(self, f.limiter)
        f.on_status = self.observe
        return f


def identity_job(source: str, stop_at: float, gate: IdentityGate | None = None) -> dict:
    """Дозбір identity одного джерела до `stop_at` (epoch) — у процесі смуги."""
    from .. import identity_backfill

    deadline = time.monotonic() + max(0.0, stop_at - time.time())
    report = {"done": {}, "left": {}, "errors": 0}
    identity_backfill.work([source], deadline, report, gate=gate)
    return report


def main(plan_path: Path, out_path: Path) -> int:
    """`cli.py night lane`: смуга за файлом плану; мережа — справжня (Fetcher)."""
    from ..fetcher import Fetcher
    from ..liveness import capture, policy

    spec = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    why = refusal(spec)
    if why is not None:
        log.error("смуга не стартує: %s", why)
        print(f"смуга не стартує: {why}", file=sys.stderr)
        return 2
    cfg = policy.load()
    fetcher = Fetcher(delay=float(spec["pace"]), use_cache=False, label="night")
    with open(out_path, "a", encoding="utf-8") as out:
        lane = Lane(spec, out, fetcher=fetcher, cfg=cfg, hooks=capture.default_hooks(cfg),
                    identity_fn=identity_job)

        def on_term(*_):
            raise SystemExit(143)

        signal.signal(signal.SIGTERM, on_term)
        try:
            lane.run()
        except SystemExit:
            lane.finish(stopped="sigterm")
            raise
        finally:
            fetcher.close()
    return 0


if __name__ == "__main__":                              # pragma: no cover — запуск через cli.py
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
