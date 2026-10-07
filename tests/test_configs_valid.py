"""Конфіги в config/: кожен файл чинний, і завантажувач справді суворий.

Конвенція (realty/configfiles.py, config/README.md): у коді немає значень за
замовчуванням; бракує ключа, є зайвий або тип не той — ConfigError. Тут —
і перевірка робочих файлів репозиторію, і перевірка самої суворості на
зіпсованих копіях: м'який завантажувач (як analytics_settings.json, що мовчки
ігнорує одруківки) непомітно повертав би старе значення.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import typing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import configfiles  # noqa: E402
from realty.configfiles import ConfigError  # noqa: E402

CONFIG = ROOT / "config"


def _topics_on_disk() -> list[str]:
    return sorted(p.relative_to(CONFIG).with_suffix("").as_posix()
                  for p in CONFIG.rglob("*.toml"))


# --- Робочі файли репозиторію --------------------------------------------------------------


def test_every_config_file_has_a_schema_and_every_schema_has_a_file():
    assert _topics_on_disk() == sorted(configfiles.SCHEMAS)


@pytest.mark.parametrize("name", _topics_on_disk())
def test_every_config_file_loads(name, monkeypatch):
    monkeypatch.delenv(configfiles.ENV_DIR, raising=False)
    value = configfiles.load(name)
    assert isinstance(value, configfiles.SCHEMAS[name])
    assert len(configfiles.config_hash(name)) == 64


def _cli_check(*extra: str, config_dir: Path | None = None):
    """`cli.py config check` окремим процесом; без config_dir — шлях за замовчуванням,
    той самий, яким іде розгортання (змінну прибрано з оточення повністю)."""
    env = {k: v for k, v in os.environ.items() if k != configfiles.ENV_DIR}
    if config_dir is not None:
        env[configfiles.ENV_DIR] = str(config_dir)
    return subprocess.run([sys.executable, "cli.py", "config", "check", *extra], cwd=ROOT,
                          capture_output=True, text=True, timeout=120, env=env)


def test_cli_config_check_passes_on_the_repository():
    r = _cli_check()
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"тека конфігів: {CONFIG}\n" in r.stdout
    assert "усі конфіги чинні" in r.stdout and "УВАГА" not in r.stdout


def _schema_fields():
    """(клас, поле, тип) усіх полів усіх схем, із вкладеними секціями."""
    out = []

    def walk(cls):
        hints = typing.get_type_hints(cls)
        for f in dataclasses.fields(cls):
            out.append((cls, f, hints[f.name]))
            if dataclasses.is_dataclass(hints[f.name]):
                walk(hints[f.name])

    for schema in configfiles.SCHEMAS.values():
        walk(schema)
    return out


def test_every_limit_fits_its_field_type():
    """Межа, що не пасує до типу поля («min» на рядку, «choices» на числі), тихо не
    діяла б — так само, як одруківка в її назві."""
    numeric = {int, float}
    for cls, f, tp in _schema_fields():
        item = typing.get_args(tp)[0] if typing.get_origin(tp) is tuple else tp
        for key in f.metadata:
            where = f"{cls.__name__}.{f.name}: межа {key}"
            assert key in configfiles.LIMIT_KEYS, where
            if key in ("min", "max"):
                assert item in numeric, f"{where} — лише для чисел, а тип {tp}"
            elif key in ("choices", "prefix"):
                assert item is str, f"{where} — лише для рядків, а тип {tp}"
            elif key == "min_len":
                assert typing.get_origin(tp) is tuple, f"{where} — лише для списків"


def test_misspelled_limit_is_refused_at_import():
    with pytest.raises(TypeError, match="невідомі межі"):
        configfiles._limits(mn=1)


def test_schemas_have_no_defaults_in_code():
    """«У коді немає значень за замовчуванням» — перевірено для кожного поля."""
    def walk(cls):
        assert cls.__dataclass_params__.frozen, f"{cls.__name__} не заморожений"
        for f in dataclasses.fields(cls):
            assert f.default is dataclasses.MISSING, f"{cls.__name__}.{f.name} має default"
            assert f.default_factory is dataclasses.MISSING, \
                f"{cls.__name__}.{f.name} має default_factory"
        for hint in typing.get_type_hints(cls).values():
            if dataclasses.is_dataclass(hint):
                walk(hint)

    for schema in configfiles.SCHEMAS.values():
        walk(schema)


def test_speed_values_match_the_owner_targets():
    """Цілі власника (промт 11, D46 п. 6) — саме ці числа, а не «приблизно»."""
    s = configfiles.load("speed")
    assert (s.targets.server_p95_ms, s.targets.tab_switch_ms, s.targets.button_ms) == \
        (300, 1500, 1500)


# --- Суворість на зіпсованих копіях --------------------------------------------------------


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    d = tmp_path / "config"
    d.mkdir()
    (d / "speed.toml").write_text((CONFIG / "speed.toml").read_text(encoding="utf-8"),
                                  encoding="utf-8")
    monkeypatch.setenv(configfiles.ENV_DIR, str(d))
    return d


def _edit(d: Path, old: str, new: str) -> None:
    p = d / "speed.toml"
    text = p.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    p.write_text(text.replace(old, new), encoding="utf-8")


def test_override_dir_is_used(cfg_dir):
    _edit(cfg_dir, "server_p95_ms = 300", "server_p95_ms = 250")
    assert configfiles.load("speed").targets.server_p95_ms == 250


@pytest.mark.parametrize("old,new,needle", [
    ("server_p95_ms = 300\n", "", "server_p95_ms: ключа немає"),                  # бракує
    ("server_p95_ms = 300", "server_p95_ms = 300\nserver_p59_ms = 300",
     "server_p59_ms: невідомий ключ"),                                           # одруківка
    ("server_p95_ms = 300", 'server_p95_ms = "300"', "очікувалось ціле число"),  # рядок
    ("server_p95_ms = 300", "server_p95_ms = 300.0", "очікувалось ціле число"),  # дробове
    ("server_p95_ms = 300", "server_p95_ms = true", "очікувалось ціле число"),   # bool ≠ 1
    ("pause_hidden = true", "pause_hidden = 1", "очікувалось true/false"),
    ("level = 5", "level = 12", "більше за допустимий максимум 9"),
    ('roles = ["owner", "friend"]', 'roles = ["owner", "guest"]', "не з переліку"),
    ('roles = ["owner", "friend"]', 'roles = "owner"', "очікувався список"),
    ('"/status", "/api/status",', '"/status", "api/status",', "має починатися з '/'"),
    ("[txn_watch]\n", "[txn_watch_typo]\n", "txn_watch: ключа немає"),          # секція
    # nan і inf — чинний TOML, але межі min/max їх не зупиняють (порівняння з nan
    # хибне, а в більшості полів максимуму немає).
    ("wait_max_s = 30", "wait_max_s = nan", "очікувалось скінченне число"),
    ("poll_s = 5", "poll_s = inf", "очікувалось скінченне число"),
    ("views_flush_s = 60", "views_flush_s = -inf", "очікувалось скінченне число"),
    # Ціле, яке не влазить у float: OverflowError не має проскочити повз ConfigError.
    ("pause_s = 2.5", "pause_s = " + "9" * 400, "число завелике"),
])
def test_broken_config_is_refused(cfg_dir, old, new, needle):
    _edit(cfg_dir, old, new)
    with pytest.raises(ConfigError) as e:
        configfiles.load("speed")
    assert needle in str(e.value)
    with pytest.raises(ConfigError):
        configfiles.config_hash("speed")
    with pytest.raises(ConfigError):
        configfiles.load_with_hash("speed")


def test_float_field_accepts_an_integer_literal(cfg_dir):
    """TOML пише 30 як ціле; для дробового поля це те саме число, не помилка."""
    s = configfiles.load("speed")
    assert s.probe.timeout_s == 30.0 and isinstance(s.probe.timeout_s, float)


def test_missing_file_and_bad_toml_are_errors(cfg_dir):
    (cfg_dir / "speed.toml").write_text("[targets\nx = 1", encoding="utf-8")
    with pytest.raises(ConfigError, match="помилка TOML"):
        configfiles.load("speed")
    (cfg_dir / "speed.toml").unlink()
    with pytest.raises(ConfigError, match="файлу немає"):
        configfiles.load("speed")
    with pytest.raises(ConfigError, match="невідома тема"):
        configfiles.load("no_such_topic")


def test_loaded_config_is_frozen(cfg_dir):
    s = configfiles.load("speed")
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.targets.server_p95_ms = 1                                     # type: ignore[misc]
    assert isinstance(s.rum.roles, tuple)


def test_hash_follows_values_not_comments(cfg_dir):
    before = configfiles.config_hash("speed")
    _edit(cfg_dir, "[targets]\n", "[targets]\n# новий коментар\n")
    assert configfiles.config_hash("speed") == before
    # Те саме число іншим написанням у дробовому полі — та сама версія.
    _edit(cfg_dir, "timeout_s = 30", "timeout_s = 30.0")
    assert configfiles.config_hash("speed") == before
    _edit(cfg_dir, "button_ms = 1500", "button_ms = 1400")
    assert configfiles.config_hash("speed") != before


def test_load_with_hash_takes_value_and_hash_from_one_read(cfg_dir):
    value, digest = configfiles.load_with_hash("speed")
    assert value == configfiles.load("speed")
    assert digest == configfiles.config_hash("speed") == configfiles.digest_of(value)


def test_cli_check_fails_on_a_broken_dir(cfg_dir):
    _edit(cfg_dir, "level = 5", "level = 0")
    (cfg_dir / "orphan.toml").write_text("x = 1\n", encoding="utf-8")
    r = _cli_check("--allow-override", config_dir=cfg_dir)
    assert r.returncode == 1
    assert "ПОМИЛКА speed" in r.stdout and "менше за допустимий мінімум 1" in r.stdout
    assert "ПОМИЛКА orphan" in r.stdout and "немає схеми" in r.stdout


def test_cli_check_refuses_an_override_unless_asked(cfg_dir):
    """REALTY_CONFIG_DIR читається й з .env — розгортання з ним не має пройти тихо."""
    r = _cli_check(config_dir=cfg_dir)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "усі конфіги чинні" in r.stdout                  # сама тека чинна,
    assert f"УВАГА: перекриття {configfiles.ENV_DIR}={cfg_dir}" in r.stdout   # але не з git
    ok = _cli_check("--allow-override", config_dir=cfg_dir)
    assert ok.returncode == 0, ok.stdout + ok.stderr


# --- Перечитування для сайту ----------------------------------------------------------------


def test_watched_rereads_by_mtime_at_most_once_per_interval(cfg_dir):
    now = [1000.0]
    w = configfiles.Watched("speed", clock=lambda: now[0])
    assert w.interval_s == configfiles.RELOAD_INTERVAL_S == 60
    _edit(cfg_dir, "server_p95_ms = 300", "server_p95_ms = 280")
    now[0] += 30                                   # раніше за хвилину — файл не читаємо
    assert w.get().targets.server_p95_ms == 300
    now[0] += 31
    assert w.get().targets.server_p95_ms == 280 and w.reloads == 1
    now[0] += 61                                   # файл не змінювався — без перечитування
    assert w.get().targets.server_p95_ms == 280 and w.reloads == 1


def test_watched_keeps_last_good_config_when_the_edit_is_broken(cfg_dir, caplog):
    now = [0.0]
    w = configfiles.Watched("speed", clock=lambda: now[0])
    good = w.digest
    _edit(cfg_dir, "server_p95_ms = 300", 'server_p95_ms = "швидко"')
    now[0] += 61
    value, digest = w.get_with_hash()                      # нічого не падає
    assert value.targets.server_p95_ms == 300               # сайт не падає
    assert digest == good                                   # і хеш — чинної версії
    assert any("не проходить перевірку" in r.getMessage() for r in caplog.records)


def test_watched_hash_describes_the_version_in_effect_not_the_file(cfg_dir):
    """Правка в межах 60 с: сайт ще на старому значенні — і хеш у записі той самий."""
    now = [0.0]
    w = configfiles.Watched("speed", clock=lambda: now[0])
    old_value, old_digest = w.get_with_hash()
    _edit(cfg_dir, "server_p95_ms = 300", "server_p95_ms = 250")
    now[0] += 30
    assert w.get_with_hash() == (old_value, old_digest)
    assert configfiles.config_hash("speed") != old_digest   # файл уже інший
    now[0] += 31
    value, digest = w.get_with_hash()
    assert value.targets.server_p95_ms == 250 and digest == configfiles.config_hash("speed")


def test_watched_does_not_lose_an_edit_made_during_the_first_read(cfg_dir, monkeypatch):
    """Позначку файлу беремо ДО читання: інакше правка між читанням і stat
    записала б нову позначку поруч зі старим значенням і ніколи б не підхопилась."""
    real = configfiles.load_with_hash
    calls = []

    def load_then_edit(name):
        result = real(name)
        if not calls:
            _edit(cfg_dir, "server_p95_ms = 300", "server_p95_ms = 2800")
        calls.append(name)
        return result

    monkeypatch.setattr(configfiles, "load_with_hash", load_then_edit)
    now = [0.0]
    w = configfiles.Watched("speed", clock=lambda: now[0])
    assert w.get().targets.server_p95_ms == 300
    now[0] += 61
    assert w.get().targets.server_p95_ms == 2800


def test_first_read_is_strict(cfg_dir):
    _edit(cfg_dir, "level = 5\n", "")
    with pytest.raises(ConfigError):
        configfiles.Watched("speed")


def test_site_reader_with_a_broken_file_rereads_at_most_once_per_interval(cfg_dir, monkeypatch):
    """Поки файл зламаний, сайт не розбирає його на кожному запиті."""
    monkeypatch.setattr(configfiles, "_watched", {})
    monkeypatch.setattr(configfiles, "_failed", {})
    now = [500.0]
    monkeypatch.setattr(configfiles, "_clock", lambda: now[0])
    reads = []
    real_read = configfiles._read
    monkeypatch.setattr(configfiles, "_read", lambda name: (reads.append(name), real_read(name))[1])

    _edit(cfg_dir, "level = 5\n", "")
    with pytest.raises(ConfigError):
        configfiles.get("speed")
    _edit(cfg_dir, "[gzip]\n", "[gzip]\nlevel = 5\n")       # полагодили
    now[0] += 30
    with pytest.raises(ConfigError):                      # у межах хвилини — та сама помилка,
        configfiles.get("speed")
    assert len(reads) == 1                                # файл не читали вдруге
    now[0] += 31
    assert configfiles.get("speed").gzip.level == 5
    assert configfiles.get_hash("speed") == configfiles.config_hash("speed")
