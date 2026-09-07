"""Розбір RSC-payload LUN, зокрема посилань на окремі текстові записи."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from realty.sources.lun import deref, iter_json_objects, resolve_text_rows

PAYLOAD = Path(__file__).resolve().parent.parent / "probes" / "_lun_payload2.txt"
pytestmark = pytest.mark.skipif(not PAYLOAD.exists(), reason="немає збереженого payload LUN")


@pytest.fixture(scope="module")
def payload() -> str:
    return PAYLOAD.read_text(encoding="utf-8")


def test_resolves_every_text_reference(payload):
    """Регресія: описи виду `"$31"` — це посилання на окремий запис потоку.

    Записи йдуть впритул, без переносу рядка, а їх довжина вказана в байтах
    UTF-8. Через якір на початок рядка й різання по символах опис губився
    приблизно в чверті оголошень LUN.
    """
    rows = resolve_text_rows(payload)
    objs = [o for o in iter_json_objects(payload) if "price" in o and "urlRaw" in o]
    refs = [o["text"] for o in objs
            if isinstance(o.get("text"), str) and o["text"].startswith("$")]
    assert refs, "у цьому payload немає посилань — тест втратив сенс"
    for ref in refs:
        assert len(deref(ref, rows)) > 100, f"{ref} не розв'язано"


def test_resolved_text_is_not_truncated(payload):
    """Різання по символах обрізало б текст приблизно на середині."""
    rows = resolve_text_rows(payload)
    longest = max(rows.values(), key=len)
    assert longest.rstrip()[-1] in ".!?»\")" or longest.rstrip()[-1].isalnum()


def test_deref_passes_through_plain_values():
    assert deref("звичайний опис", {"31": "x"}) == "звичайний опис"
    assert deref(None, {}) is None
    assert deref("$31", {"31": "текст"}) == "текст"
    assert deref("$99", {}) == ""      # посилання без запису — порожньо, не падіння
