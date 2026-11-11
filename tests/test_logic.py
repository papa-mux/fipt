"""Тесты чистой логики (без Textual и без mpv). Запуск: python3 tests/test_logic.py"""
import json
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fiptui import i18n, media, queue_store, settings, tags, themes  # noqa: E402


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name in ("track10.mp3", "track2.mp3", "Track1.flac", "cover.jpg", ".hidden.mp3", "list.m3u", "clip.MKV"):
            _write(os.path.join(self.tmp, name), "")
        os.makedirs(os.path.join(self.tmp, "Album B"))
        os.makedirs(os.path.join(self.tmp, "album a"))
        os.makedirs(os.path.join(self.tmp, ".git"))
        _write(os.path.join(self.tmp, "album a", "x.ogg"), "")
        _write(os.path.join(self.tmp, "Album B", "y.opus"), "")

    def test_list_dir_order_and_filter(self):
        items = media.list_dir(self.tmp)
        names = [n for n, _ in items]
        self.assertEqual(names[:2], ["album a", "Album B"])             # папки первыми, без учёта регистра
        self.assertNotIn("cover.jpg", names)                              # не медиа
        self.assertNotIn(".hidden.mp3", names)
        self.assertNotIn(".git", names)
        files = [n for n, d in items if not d]
        self.assertEqual(files[:3], ["clip.MKV", "list.m3u", "Track1.flac"])

    def test_natural_order(self):
        files = [os.path.basename(f) for f in media.folder_media(self.tmp)]
        self.assertEqual(files, ["clip.MKV", "Track1.flac", "track2.mp3", "track10.mp3"])  # 2 < 10, плейлист исключён

    def test_recursive(self):
        files = [os.path.relpath(f, self.tmp) for f in media.folder_media(self.tmp, recursive=True)]
        self.assertIn(os.path.join("album a", "x.ogg"), files)
        self.assertIn(os.path.join("Album B", "y.opus"), files)
        self.assertFalse(any(".git" in f or f.endswith(".m3u") for f in files))

    def test_missing_dir(self):
        self.assertEqual(media.list_dir("/no/such/dir"), [])
        self.assertEqual(media.folder_media("/no/such/dir"), [])

    def test_urls(self):
        self.assertTrue(media.is_url("https://youtu.be/x"))
        self.assertTrue(media.is_url("  rtmp://a/b "))
        self.assertFalse(media.is_url("/home/u/a.mp3"))
        self.assertFalse(media.is_url("C:\\a.mp3"))
        self.assertEqual(media.short_name("https://a.b/dir/song%20one.mp3?x=1"), "song one.mp3")
        self.assertEqual(media.short_name("https://a.b/"), "a.b")

    def test_display_name(self):
        self.assertEqual(media.display_name({"filename": "/m/a.mp3", "title": " Hi "}), "Hi")
        self.assertEqual(media.display_name({"filename": "/m/a.mp3"}), "a.mp3")

    def test_format_time(self):
        self.assertEqual(media.format_time(0), "0:00")
        self.assertEqual(media.format_time(59.9), "0:59")
        self.assertEqual(media.format_time(3600), "1:00:00")
        self.assertEqual(media.format_time(float("nan")), "--:--")
        self.assertEqual(media.format_time("x"), "--:--")

    def test_metadata(self):
        md = {"ARTIST": "Boards of Canada", "title": "Roygbiv", "Album": "MHTRTC"}
        self.assertEqual(media.now_playing_text("file.mp3", md), ("Boards of Canada — Roygbiv", "MHTRTC"))
        self.assertEqual(media.now_playing_text("file.mp3", None), ("file.mp3", ""))
        self.assertEqual(media.now_playing_text("", {"artist": "A"}), ("", ""))   # артист без названия не выводим

    def test_fix_mojibake(self):
        fix = media.fix_mojibake
        utf8_as_latin1 = "Чужой взгляд — Концерт".encode("utf-8").decode("latin-1")
        self.assertNotEqual(utf8_as_latin1, "Чужой взгляд — Концерт")
        self.assertEqual(fix(utf8_as_latin1), "Чужой взгляд — Концерт")       # случай со скриншота
        self.assertEqual(fix("Иноходец".encode("cp1251").decode("latin-1")), "Иноходец")   # старые русские теги
        for fine in ("Гражданская оборона", "Café del Mar", "Étienne Daho", "Beyoncé", "Mötley Crüe",
                     "plain ascii", "", "日本語", "Привет Ä"):
            self.assertEqual(fix(fine), fine, fine)                            # нормальный текст не трогаем
        self.assertEqual(fix("Beyonc\u00c3\u00a9"), "Beyoncé")                   # типичный «Ã©»
        self.assertEqual(fix(None), None)

    def test_mojibake_fixed_in_metadata_and_names(self):
        bad = lambda t: t.encode("utf-8").decode("latin-1")
        md = {"artist": bad("Кино"), "title": bad("Группа крови"), "album": bad("Группа крови")}
        self.assertEqual(media.now_playing_text("x.mp3", md), ("Кино — Группа крови", "Группа крови"))
        self.assertEqual(media.now_playing_text(bad("Название"), None), ("Название", ""))
        self.assertEqual(media.display_name({"filename": "a.mp3", "title": bad("Песня")}), "Песня")

    def test_bar(self):
        self.assertEqual(media.bar_cells(10, 30, 60), 5)
        self.assertEqual(media.bar_cells(10, 99, 60), 10)       # позиция за пределами — не вылезаем
        self.assertEqual(media.bar_cells(10, None, 60), 0)
        self.assertEqual(media.bar_cells(10, 5, 0), 0)          # поток без длительности
        self.assertEqual(media.bar_cells(0, 5, 10), 0)
        self.assertEqual(media.click_fraction(0, 80), 0.0)
        self.assertEqual(media.click_fraction(79, 80), 1.0)
        self.assertEqual(media.click_fraction(5, 1), 0.0)


class SettingsTests(unittest.TestCase):
    def test_defaults_complete(self):
        d = settings.default_settings()
        for key in ("language", "theme", "mpv_path", "video", "volume", "music_dir", "play_folder", "remember_queue"):
            self.assertIn(key, d)

    def test_sanitize_clamps_and_types(self):
        base = settings.default_settings()
        out = settings.sanitize({"volume": 999, "seek_step": -5, "play_folder": "yes", "video": "bogus",
                                 "mpv_args": "", "unknown": 1, "ytdl": False}, base)
        self.assertEqual(out["volume"], 130)
        self.assertEqual(out["seek_step"], 1)
        self.assertIs(out["play_folder"], base["play_folder"])           # не bool — игнорируем
        self.assertEqual(out["video"], base["video"])                    # не из списка допустимых
        self.assertEqual(out["mpv_args"], "")                            # пустые аргументы допустимы
        self.assertIs(out["ytdl"], False)
        self.assertNotIn("unknown", out)

    def test_sanitize_rejects_empty_required_string(self):
        base = settings.default_settings()
        self.assertEqual(settings.sanitize({"mpv_path": "  "}, base)["mpv_path"], base["mpv_path"])

    def test_sanitize_bool_not_int(self):
        base = settings.default_settings()
        self.assertEqual(settings.sanitize({"volume": True}, base)["volume"], base["volume"])

    def test_garbage_input(self):
        base = settings.default_settings()
        self.assertEqual(settings.sanitize("not a dict", base), base)


class QueueStoreTests(unittest.TestCase):
    def test_roundtrip_and_missing_files(self):
        tmp = tempfile.mkdtemp()
        a, b = os.path.join(tmp, "a.mp3"), os.path.join(tmp, "b.mp3")
        _write(a, ""); _write(b, "")
        path = os.path.join(tmp, "q.json")
        _write(path, json.dumps({"files": [a, "/gone/x.mp3", "https://x.y/z", b], "index": 3}))
        files, idx = queue_store.load_queue(path)
        self.assertEqual(files, [a, "https://x.y/z", b])
        self.assertEqual(idx, 2)                      # индекс сдвинулся: исчезнувший файл был до него

    def test_bad_files(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "q.json")
        self.assertEqual(queue_store.load_queue(path), ([], 0))        # файла нет
        _write(path, "{broken")
        self.assertEqual(queue_store.load_queue(path), ([], 0))        # битый JSON
        _write(path, json.dumps({"files": "nope"}))
        self.assertEqual(queue_store.load_queue(path), ([], 0))
        _write(path, json.dumps({"files": ["https://a.b/c"], "index": 99}))
        self.assertEqual(queue_store.load_queue(path), (["https://a.b/c"], 0))   # индекс вне диапазона


class ThemeTests(unittest.TestCase):
    def test_builtin_themes_valid(self):
        loaded = themes.load_all()
        self.assertGreaterEqual(len(loaded), 4)
        for name, t in loaded.items():
            self.assertTrue(t["primary"].startswith("#"), name)
            self.assertNotIn("editor_theme", t)

    def test_normalize_hex(self):
        self.assertEqual(themes.normalize_hex("#ABC"), "#aabbcc")
        self.assertEqual(themes.normalize_hex("A1B2C3"), "#a1b2c3")
        self.assertIsNone(themes.normalize_hex("#12"))
        self.assertIsNone(themes.normalize_hex(None))

    def test_validate_requires_name_and_primary(self):
        self.assertIsNone(themes.validate_theme({"name": "x"}))
        self.assertIsNone(themes.validate_theme({"primary": "#fff"}))
        self.assertEqual(themes.validate_theme({"name": "x", "primary": "#fff"})["primary"], "#ffffff")


class TagsTests(unittest.TestCase):
    def test_format_name(self):
        self.assertEqual(tags.format_name("Boards of Canada", "Roygbiv"), "Boards of Canada — Roygbiv")
        self.assertEqual(tags.format_name("", " Solo "), "Solo")
        self.assertEqual(tags.format_name("Artist", ""), "")            # без названия — пусть будет имя файла

    def test_learn_fixes_mojibake(self):
        bad = "Кино".encode("utf-8").decode("latin-1")
        self.assertEqual(tags.learn_from_metadata({"ARTIST": bad, "title": bad}), "Кино — Кино")

    def test_learn_from_metadata(self):
        self.assertEqual(tags.learn_from_metadata({"ARTIST": "A", "Title": "T"}), "A — T")
        self.assertEqual(tags.learn_from_metadata({"artist": "A"}), "")
        self.assertEqual(tags.learn_from_metadata(None), "")

    def test_read_name_without_mutagen_or_file(self):
        self.assertFalse(tags.available())
        self.assertEqual(tags.read_name("/no/such.mp3"), "")
        self.assertEqual(tags.read_name("https://a.b/c.mp3"), "")
        self.assertEqual(tags.read_many(["/x.mp3"]), {"/x.mp3": ""})


class RichColorTests(unittest.TestCase):
    def test_alpha_channel_dropped(self):
        # именно это значение уронило SeekBar в Textual: Rich не разбирает #RRGGBBAA
        self.assertEqual(themes.rich_color("#C0CAF599", "#000000"), "#c0caf5")
        self.assertEqual(themes.rich_color("#5EA1FF", "#000000"), "#5ea1ff")
        self.assertEqual(themes.rich_color("#abc", "#000000"), "#aabbcc")

    def test_garbage_uses_fallback(self):
        for bad in (None, 5, "", "red", "rgb(1,2,3)", "#12"):
            self.assertEqual(themes.rich_color(bad, "#123456"), "#123456")


class LocaleTests(unittest.TestCase):
    CODE_DIR = os.path.join(ROOT, "fiptui")

    def _keys_in_code(self) -> set:
        keys: set = set()
        for dirpath, _, names in os.walk(self.CODE_DIR):
            for n in names:
                if not n.endswith(".py"):
                    continue
                with open(os.path.join(dirpath, n), encoding="utf-8") as f:
                    src = f.read()
                # прямые вызовы _t("key") / tr(lang, "key"); f-строки (динамические) проверяются отдельно
                for m in re.finditer(r'(?:\b_t|\btr)\((?:[a-z_.]+,\s*)?"([^"]+)"', src):
                    keys.add(m.group(1))
                keys |= set(re.findall(r'"((?:tip|footer|log|dlg|now|btn|cmd)_\w+)"', src))
        return keys

    def setUp(self):
        self.tables = {}
        for c in ("en", "ru", "ua"):
            with open(os.path.join(ROOT, "locales", f"{c}.json"), encoding="utf-8") as f:
                self.tables[c] = json.load(f)

    def test_same_keys_everywhere(self):
        en = set(self.tables["en"])
        for code, table in self.tables.items():
            self.assertEqual(set(table) ^ en, set(), f"{code} расходится с en")

    def test_placeholders_match(self):
        fmt = re.compile(r"\{(\w+)\}")
        for key, text in self.tables["en"].items():
            for code in ("ru", "ua"):
                self.assertEqual(set(fmt.findall(text)), set(fmt.findall(self.tables[code][key])),
                                 f"{code}:{key} — разные плейсхолдеры")

    def test_all_used_keys_exist(self):
        static = self._keys_in_code()
        self.assertGreater(len(static), 80)          # регулярка реально что-то нашла
        # id виджетов, совпавшие с шаблоном ключей, и служебные слова — не ключи локализации
        ignore = {"log_box", "now_box", "now_status", "now_sub", "now_title", "btn_container"}
        missing = sorted(k for k in static - ignore if k not in self.tables["en"])
        self.assertEqual(missing, [])

    def test_dynamic_keys_cover_choices(self):
        en = self.tables["en"]
        for key, values in settings.CHOICES.items():
            self.assertIn(f"set_{key}", en)
            for v in values:
                self.assertIn(f"opt_{key}_{v}", en)
        for mode in ("off", "track", "playlist"):
            self.assertIn(f"repeat_{mode}", en)
        for k in themes.COLOR_KEYS:
            self.assertIn(f"theme_color_{k}", en)
        for k in ("titles_from_tags", "play_folder", "ytdl", "remember_queue", "remember_volume", "show_log", "volume", "seek_step",
                  "volume_step", "mpv_path", "mpv_args", "music_dir"):
            self.assertIn(f"set_{k}", en)

    def test_no_markup_hazard_in_label_texts(self):
        # тексты, которые попадают в Label с разметкой, не должны содержать «[/]» и «[tag]»
        for key in ("hint_hotkeys", "hint_restart", "theme_hint"):
            for code, table in self.tables.items():
                self.assertNotRegex(table[key].replace("[/]", "").replace("[", "").replace("]", ""), r"\[|\]")

    def test_tr_fallback_and_format(self):
        self.assertEqual(i18n.tr("ru", "log_added", n=3), "[Добавлено в очередь: 3]\n")
        self.assertEqual(i18n.tr("xx", "log_added", n=3), "[Added to queue: 3]\n")   # неизвестный язык -> en
        self.assertEqual(i18n.tr("en", "no_such_key"), "no_such_key")
        self.assertEqual(i18n.tr("en", "log_added"), "[Added to queue: {n}]\n")      # нет аргумента — без падения


if __name__ == "__main__":
    unittest.main()
