"""Обмежувач темпу: пауза до одного хоста не має гальмувати інші."""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from realty.fetcher import RateLimiter


def test_same_host_requests_are_spaced():
    limiter = RateLimiter(default_delay=0.0)
    start = time.monotonic()
    for _ in range(3):
        limiter.wait("https://a.example/x", delay=0.15)
    assert time.monotonic() - start >= 0.28      # дві паузи між трьома запитами


def test_different_hosts_do_not_wait_for_each_other():
    """Головна властивість: сон по одному хосту не тримає замок для решти.

    Якщо спати із замком, десять хостів вишикуються в чергу й паралельність
    зникне — саме це й робило обхід у п'ять разів повільнішим, ніж міг бути.
    """
    limiter = RateLimiter(default_delay=0.0)
    hosts = [f"https://h{i}.example/x" for i in range(10)]
    for url in hosts:                             # займаємо слот кожному хосту
        limiter.wait(url, delay=0.3)

    done = threading.Barrier(len(hosts) + 1)

    def worker(url):
        limiter.wait(url, delay=0.3)
        done.wait()

    start = time.monotonic()
    for url in hosts:
        threading.Thread(target=worker, args=(url,), daemon=True).start()
    done.wait(timeout=5)
    elapsed = time.monotonic() - start
    # Послідовне очікування дало б ~3 с; паралельне вкладається в одну паузу.
    assert elapsed < 1.0, f"хости чекали один одного: {elapsed:.2f} с"


def test_two_threads_on_one_host_do_not_fire_together():
    """Захоплення слота відбувається під замком, інакше пауза нічого не варта."""
    limiter = RateLimiter(default_delay=0.0)
    limiter.wait("https://one.example/x", delay=0.25)
    stamps: list[float] = []
    lock = threading.Lock()

    def worker():
        limiter.wait("https://one.example/x", delay=0.25)
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    stamps.sort()
    for earlier, later in zip(stamps, stamps[1:]):
        assert later - earlier >= 0.2
