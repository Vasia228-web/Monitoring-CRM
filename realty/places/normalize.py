"""Ключ назви для порівняння: «мкр. «Пасiчна»» і «Пасічна» — той самий ключ (E10, D57).

Автоматичний збіг — лише за ТОЧНИМ ключем після нормалізації; решта — явні псевдоніми в
довіднику (нечіткий збіг лише підказує кандидата на /status — `suggest`). Етап 0
показав колізії «скелетного» ключа (Senat/Sonata, Липська вежа/Липські вежі), тож
вгадувати не беремося.

Кроки (усе — з config/places/rules.toml [normalize]):
  1. NFKC (повноширинні літери, лігатури);
  2. латинські двійники кирилиці в ЗМІШАНОМУ слові зводяться до письма більшості
     його літер: «Пасiчна» (латинська i) → «Пасічна», «Comfогt» → «Comfort»,
     «Сity» (кирилична С) → «City»; чисто латинське «Manhattan» лишається латиницею;
  3. casefold; суфікси на кшталт «(Івано-Франківськ)»;
  4. лапки й апострофи прибираються, тире й дефіси, «№» і розділові знаки → пробіл;
     відкривна лапка, приклеєна до слова («ЖК«Липки»»), спершу відокремлюється
     пробілом — інакше префікс злипся б із назвою;
  5. префікси типу («жк», «мкр», «р-н» …) на початку — цілими словами, повторно.

Крок 2 діє й на коротку латинську «літеру при числі»: «A5», «B2» з латинськими
двійниками → кирилиця («А5»), бо так їх пишуть і так, і так (рецензія E10: «ЖК A5» не
знаходився). Обидві сторони порівняння проходять ту саму нормалізацію, тож довідник
лишається узгодженим (колізії перевіряє Directory.build).
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from functools import lru_cache

_WORD = re.compile(r"[^\W\d_]+", re.U)
_LATIN = re.compile(r"[A-Za-z]")
_CYR = re.compile(r"[Ѐ-ӿ]")
_DASHES = "-‐‑‒–—―−"
_PUNCT = re.compile(r"[.,;:!?()\[\]{}/\\|+*#№%&_=<>~^\"]")
_SPACE = re.compile(r"\s+")
# Латинська «літера при числі» (1–2 літери впритул до цифр, без інших літер поруч).
_LETTER_NUM = re.compile(r"(?<![^\W\d_])[A-Za-z]{1,2}(?=\d)|(?<=\d)[A-Za-z]{1,2}(?![^\W\d_])")
# Відкривна лапка впритул після літери чи цифри («ЖК«Липки»», «ЖК"Senat"»).
_GLUED_QUOTE = re.compile(r"(?<=\w)([«“„\"])")


class Normalizer:
    """Нормалізатор, зібраний з rules.normalize (кешується за значенням правил)."""

    def __init__(self, cfg) -> None:
        lat_to_cyr, cyr_to_lat = {}, {}
        for pair in cfg.homoglyphs:
            lat, cyr = pair[0], pair[1]
            for a, b in ((lat, cyr), (lat.upper(), cyr.upper())):
                lat_to_cyr[a] = b
                cyr_to_lat[b] = a
        self._to_cyr = str.maketrans(lat_to_cyr)
        self._to_lat = str.maketrans(cyr_to_lat)
        self._quotes = str.maketrans({q: "" for q in cfg.quotes})
        self._suffixes = tuple(sorted({self._basic(s) for s in cfg.strip_suffixes if s},
                                      key=len, reverse=True))
        self._suffix_raw = tuple(sorted({s.casefold() for s in cfg.strip_suffixes if s},
                                        key=len, reverse=True))
        self._prefixes = {
            "district": self._prefix_list(cfg.district_prefixes),
            "complex": self._prefix_list(cfg.complex_prefixes),
        }

    # --- кроки ---------------------------------------------------------------------------

    def fold_mixed(self, text: str) -> str:
        """Латинські двійники в змішаних словах → письмо більшості літер слова."""
        def fix(m: re.Match) -> str:
            word = m.group(0)
            lat = len(_LATIN.findall(word))
            cyr = len(_CYR.findall(word))
            if not lat or not cyr:
                return word
            return word.translate(self._to_cyr if cyr >= lat else self._to_lat)
        text = _WORD.sub(fix, text)

        def fix_num(m: re.Match) -> str:
            word = m.group(0)
            out = word.translate(self._to_cyr)
            return out if not _LATIN.search(out) else word
        return _LETTER_NUM.sub(fix_num, text)

    def _basic(self, text: str) -> str:
        # «№» — до NFKC: та розкладає його на «No» («Квартал №5» → «квартал no5»).
        s = unicodedata.normalize("NFKC", (text or "").replace("№", " "))
        s = self.fold_mixed(s).casefold()
        s = s.translate(self._quotes)
        for ch in _DASHES:
            s = s.replace(ch, " ")
        s = _PUNCT.sub(" ", s)
        return _SPACE.sub(" ", s).strip()

    def _prefix_list(self, prefixes) -> tuple[str, ...]:
        return tuple(sorted({self._basic(p) for p in prefixes if self._basic(p)},
                            key=len, reverse=True))

    def key(self, text: str | None, kind: str = "district") -> str:
        """Ключ порівняння назви; "" — порожньо."""
        if not text:
            return ""
        s = unicodedata.normalize("NFKC", text.replace("№", " "))
        s = _GLUED_QUOTE.sub(r" \1", s)
        s = self.fold_mixed(s).casefold()
        # Суфікс «(Івано-Франківськ)» — до того, як дужки й дефіс стануть пробілами.
        stripped = s.strip()
        for suf in self._suffix_raw:
            if stripped.endswith(suf):
                stripped = stripped[: -len(suf)].strip()
        s = self._basic(stripped)
        for suf in self._suffixes:
            if s.endswith(" " + suf):
                s = s[: -len(suf) - 1].strip()
        prefixes = self._prefixes[kind]
        changed = True
        while changed and s:
            changed = False
            for p in prefixes:
                if s == p:
                    return ""
                if s.startswith(p + " "):
                    s = s[len(p) + 1:].strip()
                    changed = True
                    break
        return s


@lru_cache(maxsize=8)
def _cached(cfg) -> Normalizer:
    return Normalizer(cfg)


def normalizer(rules) -> Normalizer:
    """Нормалізатор для правил `rules` (PlacesRulesConfig) — один на значення правил."""
    return _cached(rules.normalize)


def suggest(name_key: str, candidates: dict[str, str], *, cutoff: float = 0.82) -> str | None:
    """Підказка для /status: найсхожіша відома назва (лише ПІДКАЗКА — автоматично не
    застосовується; Етап 0: Senat/Sonata). `candidates` — {ключ: показна назва}."""
    if not name_key or not candidates:
        return None
    best = difflib.get_close_matches(name_key, list(candidates), n=1, cutoff=cutoff)
    return candidates[best[0]] if best else None
