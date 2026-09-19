"""Штучні джерела для перевірки лімітів часу — запускаються окремим процесом.

  python tests/fake_slow_source.py freeze  URL   # завис у виклику, що ігнорує все
  python tests/fake_slow_source.py trickle URL   # сайт віддає відповідь по краплі
  python tests/fake_slow_source.py ok      URL   # нормальне джерело, 2 оголошення

Кожне йде через справжній `Pipeline.run` і справжній `Fetcher`, тож перевіряється
та сама механіка, що працює в розкладі, а не її імітація.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from realty.fetcher import FetchError, Fetcher  # noqa: E402
from realty.pipeline import Pipeline  # noqa: E402
from realty.sources import REGISTRY  # noqa: E402
from realty.sources.base import BaseSource  # noqa: E402


def make(mode: str, url: str):
    class Fake(BaseSource):
        name = f"fake_{mode}"

        def iter_listings(self):
            self.begin_page(1)
            if mode == "freeze":
                # Так поводився OLX 10.09: виклик, у якого немає ліміту й
                # який не реагує ні на що, крім смерті процесу.
                time.sleep(3600)
            try:
                data = Fetcher(use_cache=False, label=self.name).get_json(url)
            except FetchError as e:
                self.give_up(f"{url} не відповів", e)
                return
            for i in range(int(data["n"])):
                yield {"external_id": f"{mode}-{i}", "original_url": f"{url}#{i}",
                       "price": 50000 + i, "currency": "USD", "rooms": 2,
                       "area_total": 55.0, "location": "вул. Тестова"}
            self.end_page()

    return Fake


if __name__ == "__main__":
    mode, url = sys.argv[1], sys.argv[2]
    cls = make(mode, url)
    REGISTRY[cls.name] = cls
    report = Pipeline(sources=[cls.name], use_llm=False, trigger="schedule").run()
    print(report.render())
