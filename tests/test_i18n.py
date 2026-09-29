import multiprocessing
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from string import Template
from typing import Any
from unittest import mock

import orjson

from akari_bot_i18n import i18n


def _load_in_worker(
    languages: list[str],
    locales_path: str,
    cache_path: str,
    result_queue: multiprocessing.Queue,
) -> None:
    os.environ["AKARI_BOT_I18N_CACHE_DIR"] = cache_path
    from akari_bot_i18n.i18n import Locale, load_locale_file

    errors = load_locale_file(languages, [locales_path])
    result_queue.put((errors, Locale(languages[0]).t("greeting")))


def _observe_reload_in_worker(
    cache_path: str,
    namespace: str,
    command_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
) -> None:
    os.environ["AKARI_BOT_I18N_CACHE_DIR"] = cache_path
    from akari_bot_i18n import i18n as worker_i18n

    worker_i18n.MANIFEST_CHECK_INTERVAL = 0.05
    worker_i18n.connect_locale_snapshot(namespace)
    locale = worker_i18n.Locale("en_us")
    result_queue.put(locale["version"])
    command_queue.get(timeout=20)
    time.sleep(0.1)
    result_queue.put(locale["version"])


def _connect_in_worker(cache_path: str, namespace: str, result_queue: multiprocessing.Queue) -> None:
    os.environ["AKARI_BOT_I18N_CACHE_DIR"] = cache_path
    from akari_bot_i18n.i18n import Locale, connect_locale_snapshot

    generation = connect_locale_snapshot(namespace)
    result_queue.put((generation, Locale("en_us").t("greeting")))


class LocaleSQLiteTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._reset_state_for_testing()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.locales = self.root / "locales"
        self.locales.mkdir()
        self.cache = self.root / "cache"
        self.previous_cache = os.environ.get("AKARI_BOT_I18N_CACHE_DIR")
        os.environ["AKARI_BOT_I18N_CACHE_DIR"] = str(self.cache)

    def tearDown(self) -> None:
        i18n._reset_state_for_testing()
        if self.previous_cache is None:
            os.environ.pop("AKARI_BOT_I18N_CACHE_DIR", None)
        else:
            os.environ["AKARI_BOT_I18N_CACHE_DIR"] = self.previous_cache
        self.temporary_directory.cleanup()

    def write_locale(self, directory: Path, locale: str, data: dict) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{locale}.json").write_bytes(orjson.dumps(data))

    def test_load_translate_fallback_and_mapping_view(self) -> None:
        self.write_locale(
            self.locales,
            "en_us",
            {
                "greeting": "Hello, $name!",
                "fallback_only": "English fallback",
                "error": {"i18n": {"fallback": " missing"}},
            },
        )
        self.write_locale(self.locales, "zh_cn", {"greeting": "你好，$name！", "nested": {"key": "值"}})
        self.write_locale(self.locales, "ignored", {"greeting": "ignored"})

        errors = i18n.load_locale_file(["zh_cn", "en_us"], [str(self.locales)])
        locale = i18n.Locale("zh_cn", fallback_lng=["en_us"])

        self.assertEqual(errors, [])
        self.assertEqual(i18n.get_available_locales(), ["en_us", "ignored", "zh_cn"])
        self.assertEqual(locale.t("greeting", name="Akari"), "你好，Akari！")
        self.assertEqual(locale.t("fallback_only"), "English fallback")
        self.assertEqual(locale["nested.key"], "值")
        self.assertIsNone(locale["missing"])
        self.assertIn("nested.key", locale)
        self.assertEqual(dict(locale.data)["nested.key"], "值")
        self.assertEqual(locale.t_str("Say {I18N:greeting,name=Bot}"), "Say 你好，Bot！")

    def test_duplicate_keys_are_removed_and_reported(self) -> None:
        other_locales = self.root / "other-locales"
        self.write_locale(self.locales, "en_us", {"duplicate": "first", "first_only": "one"})
        self.write_locale(other_locales, "en_us", {"duplicate": "second", "second_only": "two"})

        errors = i18n.load_locale_file(["en_us"], [str(self.locales), str(other_locales)])
        locale = i18n.Locale("en_us")

        self.assertTrue(any('Conflict detected for key "duplicate"' in error for error in errors))
        self.assertIsNone(locale["duplicate"])
        self.assertEqual(locale["first_only"], "one")
        self.assertEqual(locale["second_only"], "two")

    def test_reload_replaces_the_snapshot(self) -> None:
        self.write_locale(self.locales, "en_us", {"changed": "old", "removed": "old value"})
        i18n.load_locale_file(["en_us"], [str(self.locales)])
        locale = i18n.Locale("en_us")
        self.assertEqual(locale["changed"], "old")

        self.write_locale(self.locales, "en_us", {"changed": "new value", "added": "new"})
        errors = i18n.Locale.reload()

        self.assertEqual(errors, [])
        self.assertEqual(locale["changed"], "new value")
        self.assertEqual(locale["added"], "new")
        self.assertIsNone(locale["removed"])

    def test_failed_reload_keeps_the_last_healthy_snapshot(self) -> None:
        self.write_locale(self.locales, "en_us", {"stable": "healthy"})
        i18n.load_locale_file(["en_us"], [str(self.locales)])
        locale = i18n.Locale("en_us")
        (self.locales / "en_us.json").write_bytes(b"{")

        with redirect_stderr(StringIO()):
            errors = i18n.Locale.reload()

        self.assertTrue(any("Failed to load" in error for error in errors))
        self.assertEqual(locale["stable"], "healthy")

    def test_reload_hashes_content_even_when_size_and_mtime_match(self) -> None:
        locale_path = self.locales / "en_us.json"
        self.write_locale(self.locales, "en_us", {"value": "old"})
        original_stat = locale_path.stat()
        i18n.load_locale_file(["en_us"], [str(self.locales)])

        self.write_locale(self.locales, "en_us", {"value": "new"})
        os.utime(locale_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        i18n.Locale.reload()

        self.assertEqual(i18n.Locale("en_us")["value"], "new")

    def test_flattening_preserves_explicit_dot_key_precedence(self) -> None:
        self.write_locale(self.locales, "en_us", {"a": {"b": "nested"}, "a.b": "explicit"})

        errors = i18n.load_locale_file(["en_us"], [str(self.locales)])

        self.assertEqual(errors, [])
        self.assertEqual(i18n.Locale("en_us")["a.b"], "explicit")

    def test_normal_load_uses_current_paths_and_reload_uses_registered_paths(self) -> None:
        other_locales = self.root / "other-locales"
        self.write_locale(self.locales, "en_us", {"first": "one"})
        self.write_locale(other_locales, "en_us", {"second": "two"})
        i18n.load_locale_file(["en_us"], [str(self.locales)])
        i18n.load_locale_file(["en_us"], [str(other_locales)])
        locale = i18n.Locale("en_us")
        root_before_reload = i18n._store.root

        self.assertIsNone(locale["first"])
        self.assertEqual(locale["second"], "two")

        i18n.Locale.reload()

        self.assertEqual(i18n._store.root, root_before_reload)
        self.assertEqual(locale["first"], "one")
        self.assertEqual(locale["second"], "two")

    def test_concurrent_processes_reuse_the_published_snapshot(self) -> None:
        self.write_locale(self.locales, "en_us", {"greeting": "Hello"})
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_load_in_worker,
                args=(["en_us"], str(self.locales), str(self.cache), result_queue),
            )
            for _ in range(4)
        ]

        for process in processes:
            process.start()
        results = [result_queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)

        self.assertTrue(all(process.exitcode == 0 for process in processes))
        self.assertEqual(results, [([], "Hello")] * len(processes))
        manifests = list(self.cache.rglob("current.json"))
        databases = list(self.cache.rglob("locales.*.db"))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(len(databases), 1)

    def test_build_and_connect_are_separate(self) -> None:
        namespace = "separate-reader-test"
        self.write_locale(self.locales, "en_us", {"greeting": "Hello"})

        errors = i18n.build_locale_snapshot(["en_us"], [str(self.locales)], namespace)

        self.assertEqual(errors, [])
        self.assertEqual(i18n.get_available_locales(), [])
        self.assertEqual(i18n.supported_locales, [])
        (self.locales / "en_us.json").unlink()

        generation = i18n.connect_locale_snapshot(namespace)

        self.assertTrue(generation)
        self.assertEqual(i18n.Locale("en_us")["greeting"], "Hello")
        self.assertEqual(i18n.supported_locales, ["en_us"])

        self.write_locale(self.locales, "en_us", {"greeting": "Updated"})
        i18n.build_locale_snapshot(["en_us"], [str(self.locales)], namespace)
        i18n.Locale.reload()

        self.assertEqual(i18n.Locale("en_us")["greeting"], "Updated")

    def test_reader_processes_only_connect_to_named_snapshot(self) -> None:
        namespace = "reader-process-test"
        self.write_locale(self.locales, "en_us", {"greeting": "Hello"})
        i18n.build_locale_snapshot(["en_us"], [str(self.locales)], namespace)
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_connect_in_worker,
                args=(str(self.cache), namespace, result_queue),
            )
            for _ in range(4)
        ]

        for process in processes:
            process.start()
        results = [result_queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)

        self.assertTrue(all(process.exitcode == 0 for process in processes))
        self.assertTrue(all(generation for generation, _ in results))
        self.assertEqual([translation for _, translation in results], ["Hello"] * len(processes))

    def test_build_keeps_only_current_and_previous_snapshots(self) -> None:
        namespace = "snapshot-retention-test"
        for version in range(6):
            self.write_locale(self.locales, "en_us", {"version": f"value-{version}"})
            i18n.build_locale_snapshot(["en_us"], [str(self.locales)], namespace)

        databases = list(self.cache.rglob("locales.*.db"))
        self.assertEqual(len(databases), 2)

    def test_reader_process_refreshes_after_another_process_reloads(self) -> None:
        namespace = "reload-reader-test"
        self.write_locale(self.locales, "en_us", {"version": "old"})
        i18n.build_locale_snapshot(["en_us"], [str(self.locales)], namespace)
        context = multiprocessing.get_context("spawn")
        command_queue = context.Queue()
        result_queue = context.Queue()
        process = context.Process(
            target=_observe_reload_in_worker,
            args=(str(self.cache), namespace, command_queue, result_queue),
        )
        process.start()
        self.assertEqual(result_queue.get(timeout=20), "old")

        self.write_locale(self.locales, "en_us", {"version": "new value"})
        i18n.build_locale_snapshot(["en_us"], [str(self.locales)], namespace)
        command_queue.put("read")

        self.assertEqual(result_queue.get(timeout=20), "new value")
        process.join(timeout=20)
        self.assertEqual(process.exitcode, 0)


class SafeStrftimeTests(unittest.TestCase):
    # 固定时间，避免断言依赖当前时间
    fixed = datetime(2026, 9, 17, 20, 30, 45)

    def setUp(self) -> None:
        i18n._reset_state_for_testing()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.locales = self.root / "locales"
        self.locales.mkdir()
        self.cache = self.root / "cache"
        self.previous_cache = os.environ.get("AKARI_BOT_I18N_CACHE_DIR")
        os.environ["AKARI_BOT_I18N_CACHE_DIR"] = str(self.cache)

    def tearDown(self) -> None:
        i18n._reset_state_for_testing()
        if self.previous_cache is None:
            os.environ.pop("AKARI_BOT_I18N_CACHE_DIR", None)
        else:
            os.environ["AKARI_BOT_I18N_CACHE_DIR"] = self.previous_cache
        self.temporary_directory.cleanup()

    def write_locale(self, directory: Path, locale: str, data: dict) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{locale}.json").write_bytes(orjson.dumps(data))

    def test_ascii_format_matches_native_strftime(self) -> None:
        fmt = "%Y-%m-%d %H:%M:%S"

        self.assertEqual(i18n.safe_strftime(self.fixed, fmt), self.fixed.strftime(fmt))

    def test_non_ascii_format_is_formatted_without_encoding_error(self) -> None:
        # Windows 上原生 strftime 会因 ANSI 代码页无法编码韩文而抛出 UnicodeEncodeError
        self.assertEqual(i18n.safe_strftime(self.fixed, "%Y년 %m월 %d일 %H:%M:%S"), "2026년 09월 17일 20:30:45")

    def test_literal_percent_survives_non_ascii_format(self) -> None:
        self.assertEqual(i18n.safe_strftime(self.fixed, "100%% 완료"), "100% 완료")
        # 末尾孤立的 '%' 按字面文本处理，比部分平台的原生行为更宽松
        self.assertEqual(i18n.safe_strftime(self.fixed, "%Y년 %m월 %d일 100%"), "2026년 09월 17일 100%")

    def test_format_without_directives_is_kept_verbatim(self) -> None:
        self.assertEqual(i18n.safe_strftime(self.fixed, "시간 없음"), "시간 없음")
        self.assertEqual(i18n.safe_strftime(self.fixed, ""), "")

    def test_date_object_is_supported(self) -> None:
        self.assertEqual(i18n.safe_strftime(date(2026, 9, 17), "%Y년 %m월 %d일"), "2026년 09월 17일")

    def test_format_with_adjacent_directives(self) -> None:
        self.assertEqual(i18n.safe_strftime(self.fixed, "%Y년%m"), "2026년09")

    def test_directive_modifiers_are_left_to_the_platform_strftime(self) -> None:
        self.assertEqual(
            i18n._split_strftime_format("%5Y년%-d"),
            [(True, "%5Y"), (False, "년"), (True, "%-d")],
        )
        try:
            expected = self.fixed.strftime("%5Y")
        except ValueError:
            # Windows 不支持字段宽度等修饰符，safe_strftime 同样会把 ValueError 抛出
            with self.assertRaises(ValueError):
                i18n.safe_strftime(self.fixed, "%5Y년")
        else:
            self.assertEqual(i18n.safe_strftime(self.fixed, "%5Y년"), f"{expected}년")

    def test_locale_strftime_uses_localized_format(self) -> None:
        self.write_locale(self.locales, "en_us", {"time.date.format": "%B %d, %Y"})
        self.write_locale(self.locales, "ko_kr", {"time.date.format": "%Y년 %m월 %d일"})

        errors = i18n.load_locale_file(["en_us", "ko_kr"], [str(self.locales)])

        self.assertEqual(errors, [])
        self.assertEqual(i18n.Locale("ko_kr").strftime(self.fixed, "time.date.format"), "2026년 09월 17일")
        self.assertEqual(
            i18n.Locale("en_us").strftime(self.fixed, "time.date.format"),
            self.fixed.strftime("%B %d, %Y"),
        )

    def test_locale_strftime_falls_back_to_another_locale_format(self) -> None:
        self.write_locale(self.locales, "en_us", {"time.date.format": "%B %d, %Y"})
        self.write_locale(self.locales, "ko_kr", {"greeting": "안녕"})
        i18n.load_locale_file(["en_us", "ko_kr"], [str(self.locales)])

        self.assertEqual(
            i18n.Locale("ko_kr").strftime(self.fixed, "time.date.format"),
            self.fixed.strftime("%B %d, %Y"),
        )

    def test_locale_strftime_substitutes_parameters(self) -> None:
        self.write_locale(self.locales, "en_us", {"time.date.format": "%Y년 $suffix"})
        i18n.load_locale_file(["en_us"], [str(self.locales)])

        self.assertEqual(
            i18n.Locale("en_us").strftime(self.fixed, "time.date.format", suffix="완료"),
            "2026년 완료",
        )


class _LocaleFixture(unittest.TestCase):
    """共享的临时目录/缓存隔离夹具，术语相关测试都基于真实快照运行。"""

    def setUp(self) -> None:
        i18n._reset_state_for_testing()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.locales = self.root / "locales"
        self.locales.mkdir()
        self.cache = self.root / "cache"
        self.previous_cache = os.environ.get("AKARI_BOT_I18N_CACHE_DIR")
        os.environ["AKARI_BOT_I18N_CACHE_DIR"] = str(self.cache)

    def tearDown(self) -> None:
        i18n._reset_state_for_testing()
        if self.previous_cache is None:
            os.environ.pop("AKARI_BOT_I18N_CACHE_DIR", None)
        else:
            os.environ["AKARI_BOT_I18N_CACHE_DIR"] = self.previous_cache
        self.temporary_directory.cleanup()

    def write_locale(self, directory: Path, locale: str, data: dict) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{locale}.json").write_bytes(orjson.dumps(data))

    def write_terms_file(self, directory: Path, locale: str, data: dict, subdirectory: str = "") -> None:
        """写入 <locales>/terms/[<subdirectory>/]<locale>.json。"""

        terms = directory / i18n.TERMS_DIRECTORY_NAME
        if subdirectory:
            terms = terms / subdirectory
        terms.mkdir(parents=True, exist_ok=True)
        (terms / f"{locale}.json").write_bytes(orjson.dumps(data))

    def load(self, languages: list[str], directory: Path | None = None, **kwargs: Any) -> list[str]:
        return i18n.load_locale_file(languages, [str(directory or self.locales)], **kwargs)

    def assert_term_issues(self, errors: list[str], *needles: str) -> None:
        for needle in needles:
            self.assertTrue(any(needle in error for error in errors), f"{needle!r} not reported in {errors}")
        self.assertTrue(all(error.startswith(i18n.TERM_AUDIT_PREFIX) for error in errors), errors)


class TermPlaceholderTests(_LocaleFixture):
    TERMS = {
        "term.target": "场景",
        "term.target.private": "私聊",
        "term.target.discord": "频道",
        "term.target.qq.group": "群聊",
    }

    def write_terms(self, extra: dict | None = None) -> None:
        self.write_locale(self.locales, "zh_cn", {**self.TERMS, **(extra or {})})

    def test_single_and_multiple_placeholders_are_resolved(self) -> None:
        self.write_terms(
            {
                "one": "当前{TERM:target}",
                "two": "{TERM:target}的{TERM:target}组",
                "none": "没有占位符",
            }
        )
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn", term_candidates=("discord",))

        self.assertEqual(locale.t("one"), "当前频道")
        self.assertEqual(locale.t("two"), "频道的频道组")
        self.assertEqual(locale.t("none"), "没有占位符")

    def test_invalid_placeholder_forms_are_kept_verbatim(self) -> None:
        value = "{term:target}|{TERM: target}|{TERM:Target}|{TERM:target|{{TERM:target}}"
        self.write_terms({"value": value})

        errors = self.load(["zh_cn"])
        locale = i18n.Locale("zh_cn", term_candidates=("discord",))

        # 小写标签、名称大写、含空白、未闭合与双写形式都不是占位符。
        self.assertEqual(locale.t("value"), value)
        # 双写形式是合法的字面量用法，不应被报告；其余可疑片段必须报出。
        self.assert_term_issues(errors, "{term:target}", "{TERM: target}", "{TERM:Target}", "{TERM:target|")
        self.assertFalse(any("{{TERM:target}}" in error for error in errors))

    def test_parameter_forms_are_resolved(self) -> None:
        self.write_terms(
            {
                "term.people": "${name} 有 ${count} 人",
                "literal": "{TERM:people,name=群聊,count=3}",
                "url": "{TERM:people,name=https://x/y?z=1&w=2,count=1}",
                "spaces": "{TERM:people,name=Alice Smith,count=2}",
            }
        )
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn")

        self.assertEqual(locale.t("literal"), "群聊 有 3 人")
        self.assertEqual(locale.t("url"), "https://x/y?z=1&w=2 有 1 人")
        self.assertEqual(locale.t("spaces"), "Alice Smith 有 2 人")

    def test_parameters_only_apply_to_the_term_text(self) -> None:
        self.write_terms(
            {
                "term.people": "${name} 有 ${count} 人",
                "value": "{TERM:people,name=群聊,count=3}，外层 ${count}",
            }
        )
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn")

        # 占位符自带的参数只作用于术语文本，外层 ${count} 仍由常规 kwargs 处理。
        self.assertEqual(locale.t("value", count=99), "群聊 有 3 人，外层 99")

    def test_missing_parameters_are_kept_in_the_term_text(self) -> None:
        self.write_terms({"term.people": "${name} 有 ${count} 人", "value": "{TERM:people,name=群聊}"})
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn")

        self.assertEqual(locale.t("value"), "群聊 有 ${count} 人")

    def test_malformed_parameter_forms_are_not_resolved_and_are_reported(self) -> None:
        value = "{TERM:target,}|{TERM:target,count}|{TERM:target,1st=x}|{TERM:target,count=3"
        self.write_terms({"value": value})
        errors = self.load(["zh_cn"])

        self.assertEqual(i18n.Locale("zh_cn").t("value"), value)
        for fragment in ("{TERM:target,}", "{TERM:target,count}", "{TERM:target,1st=x}", "{TERM:target,count=3"):
            self.assertTrue(any(fragment in error for error in errors), fragment)

    def test_term_and_i18n_placeholders_coexist(self) -> None:
        self.write_terms({"greeting": "你好", "value": "{TERM:target} {I18N:greeting}"})
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn", term_candidates=("discord",))

        # 大写标签不会吞掉 {I18N:...}，两者各自命中。
        self.assertEqual(locale.t_str("{TERM:target} {I18N:greeting}"), "频道 你好")
        self.assertEqual(locale.t_str("{I18N:value}"), "频道 你好")

    def test_candidate_chain_prefers_the_most_specific_candidate(self) -> None:
        self.write_terms({"value": "当前{TERM:target}"})
        self.assertEqual(self.load(["zh_cn"]), [])

        cases = {
            ("discord.dm.channel", "discord.dm", "private", "discord"): "私聊",
            ("qq.group", "private", "qq"): "群聊",
            ("discord.channel", "discord"): "频道",
            ("unknown.platform",): "场景",
        }
        for candidates, expected in cases.items():
            with self.subTest(candidates=candidates):
                locale = i18n.Locale("zh_cn", term_candidates=candidates)
                self.assertEqual(locale.t("value"), f"当前{expected}")

    def test_more_specific_candidate_wins_over_less_specific(self) -> None:
        self.write_terms({"value": "当前{TERM:target}", "term.target.discord.dm": "私信频道"})
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn", term_candidates=("discord.dm", "discord"))

        self.assertEqual(locale.t("value"), "当前私信频道")
        self.assertEqual(locale.term_key("target"), "term.target.discord.dm")

    def test_empty_candidate_chain_falls_back_to_base_term(self) -> None:
        self.write_terms({"value": "当前{TERM:target}"})
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn", term_candidates=())

        self.assertTrue(locale.term_scoped)
        self.assertEqual(locale.term_candidates, ())
        self.assertEqual(locale.t("value"), "当前场景")
        self.assertEqual(locale.term_key("target"), "term.target")

    def test_unbound_locale_still_resolves_base_term(self) -> None:
        self.write_terms({"value": "当前{TERM:target}"})
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn")

        self.assertFalse(locale.term_scoped)
        self.assertIsNone(locale.term_candidates)
        # 无会话出口（SMTP、配置注释等）不应泄漏占位符。
        self.assertEqual(locale.t("value"), "当前场景")

    def test_variants_are_only_looked_up_in_the_current_locale(self) -> None:
        self.write_locale(self.locales, "zh_cn", {**self.TERMS, "value": "当前{TERM:target}"})
        self.write_locale(self.locales, "en_us", {"term.target": "context", "value": "Current {TERM:target}"})
        self.assertEqual(self.load(["zh_cn", "en_us"]), [])

        # en_us 没有变体，即使 zh_cn 有 term.target.discord 也不应把中文名词嵌进英文句子。
        english = i18n.Locale("en_us", fallback_lng=["zh_cn"], term_candidates=("discord",))

        self.assertEqual(english.t("value"), "Current context")
        self.assertEqual(english.term_key("target"), "term.target")

    def test_missing_term_keeps_the_placeholder(self) -> None:
        self.write_terms(
            {
                "value": "当前{TERM:missing}与{TERM:missing2,count=3}",
                "duplicate_params": "{TERM:missing,a=1,a=2}",
            }
        )

        errors = self.load(["zh_cn"])
        locale = i18n.Locale("zh_cn", term_candidates=("discord",))

        # 构建期必须拦住缺失的基础键（缺失占位符的唯一来源）。
        self.assert_term_issues(errors, "term.missing", "term.missing2")
        self.assertEqual(locale.t("value"), "当前{TERM:missing}与{TERM:missing2,count=3}")
        # 原样保留意味着逐字节保留，包括重名参数。
        self.assertEqual(locale.t("duplicate_params"), "{TERM:missing,a=1,a=2}")
        self.assertEqual(locale.t("value", fallback=False), "当前{TERM:missing}与{TERM:missing2,count=3}")
        # 缺失时不附加语言回退提示，排障信息就是占位符本身。
        self.assertNotIn("{I18N:", locale.t("value"))
        self.assertIsNone(locale.term_key("missing"))
        self.assertEqual(locale.term("missing"), "{TERM:missing}")
        self.assertEqual(locale.term("missing", count=3), "{TERM:missing,count=3}")

    def test_term_values_are_plain_text_with_optional_dollar_params(self) -> None:
        self.write_terms({"term.people": "${name} 的${count}"})
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn")

        self.assertEqual(locale.term("people", name="群聊", count=3), "群聊 的3")
        # ${...} 是正当用法：缺参数时原样保留。
        self.assertEqual(locale.term("people", name="群聊"), "群聊 的${count}")

    def test_terms_resolve_in_t_str_input_i18n_values_and_kwargs(self) -> None:
        self.write_terms(
            {
                "greeting": "你好",
                "with_term": "当前{TERM:target}",
                "template": "值 ${value}",
            }
        )
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn", term_candidates=("discord",))

        self.assertEqual(locale.t_str("{TERM:target}"), "频道")
        self.assertEqual(locale.t_str("{I18N:with_term}"), "当前频道")
        self.assertEqual(locale.t("with_term"), "当前频道")
        self.assertEqual(locale.t("template", value="{TERM:target}"), "值 频道")
        self.assertEqual(locale.t({"zh_cn": "当前{TERM:target}"}), "当前频道")
        self.assertEqual(
            locale.t({"en_us": "ignored", "_fallback_locale": "回退{TERM:target}"}),
            "回退频道",
        )

    def test_term_resolution_does_not_consume_i18n_depth(self) -> None:
        chain = {f"chain.{index}": f"{{I18N:chain.{index + 1}}}" for index in range(9)}
        chain["chain.9"] = "结尾{TERM:target}"
        self.write_terms(chain)
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn", term_candidates=("discord",))

        self.assertEqual(locale.t_str("{I18N:chain.0}"), "结尾频道")

    def test_term_values_are_inserted_verbatim(self) -> None:
        self.write_terms({"term.weird": "{TERM:target}", "value": "前{TERM:weird}后"})

        errors = self.load(["zh_cn"])
        locale = i18n.Locale("zh_cn", term_candidates=("discord",))

        # 术语值含占位符是构建期错误，运行时仍按纯文本插入。
        self.assert_term_issues(errors, "term.weird", "plain text")
        # 单趟替换：替换进去的文本不会被重新扫描，因此不会递归解析。
        self.assertEqual(locale.t("value"), "前{TERM:target}后")
        self.assertEqual(locale.t_str("{I18N:value}"), "前{TERM:target}后")
        self.assertEqual(locale.t_str("{TERM:weird}"), "{TERM:target}")

    def test_unresolved_placeholders_are_idempotent(self) -> None:
        self.write_terms({"value": "当前{TERM:missing,count=3}"})

        errors = self.load(["zh_cn"])
        locale = i18n.Locale("zh_cn", term_candidates=("discord",))
        once = locale.t("value")

        self.assert_term_issues(errors, "term.missing")
        self.assertEqual(locale._resolve_terms(once), once)
        self.assertEqual(locale.t_str(once), once)

    def test_template_substitute_accepts_term_placeholders(self) -> None:
        for value in ("{TERM:target}", "{TERM:target,count=3}", "x{TERM:target}y"):
            with self.subTest(value=value):
                self.assertEqual(Template(value).substitute(), value)

    def test_strftime_format_is_not_term_resolved(self) -> None:
        self.write_terms({"time.format": "%Y {TERM:target}"})
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn", term_candidates=("discord",))

        self.assertEqual(locale.strftime(datetime(2026, 9, 17), "time.format"), "2026 {TERM:target}")

    def test_raw_lookup_helpers_do_not_resolve_terms(self) -> None:
        self.write_terms({"value": "当前{TERM:target}"})
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn", term_candidates=("discord",))

        # 查表与诊断入口保持原始语义。
        self.assertEqual(locale["value"], "当前{TERM:target}")
        self.assertIn("value", locale)
        self.assertEqual(locale.data["value"], "当前{TERM:target}")

    def test_term_and_term_key_helpers(self) -> None:
        self.write_terms()
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn", term_candidates=("qq.group", "qq"))

        self.assertEqual(locale.term("target"), "群聊")
        self.assertEqual(locale.term_key("target"), "term.target.qq.group")
        self.assertEqual(locale.term("missing"), "{TERM:missing}")

        # 关键字写法与仅位置参数写法都可用，`name` 同时仍是合法的占位符参数名。
        self.assertEqual(locale.term(name="target"), "群聊")
        self.assertEqual(locale.term_key(name="target"), "term.target.qq.group")
        self.assertEqual(locale.term("people", name="群聊"), "{TERM:people,name=群聊}")
        with self.assertRaises(TypeError):
            locale.term()


class DeriveTermCandidatesTests(unittest.TestCase):
    def test_spec_examples(self) -> None:
        self.assertEqual(
            i18n.derive_term_candidates(("discord", "dm", "channel"), shared=("private",)),
            ("discord.dm.channel", "discord.dm", "private", "discord"),
        )
        self.assertEqual(
            i18n.derive_term_candidates(("qq", "group"), shared=("private",)),
            ("qq.group", "private", "qq"),
        )
        self.assertEqual(i18n.derive_term_candidates(("matrix",)), ("matrix",))

    def test_prefixes_are_truncated_from_the_right(self) -> None:
        self.assertEqual(
            i18n.derive_term_candidates(("a", "b", "c", "d")),
            ("a.b.c.d", "a.b.c", "a.b", "a"),
        )

    def test_shared_tokens_keep_order_and_duplicates_are_removed(self) -> None:
        self.assertEqual(
            i18n.derive_term_candidates(("a", "b"), shared=("b", "c")),
            ("a.b", "b", "c", "a"),
        )

    def test_invalid_tokens_raise_value_error(self) -> None:
        for tokens in ((), [], ("",), ("a.b",), ("a b",), ("a{b",), ("a}b",), ("a:b",)):
            with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                i18n.derive_term_candidates(tokens)
        for shared in ((".",), ("a.b",), ("",), ("a b",)):
            with self.subTest(shared=shared), self.assertRaises(ValueError):
                i18n.derive_term_candidates(("a",), shared=shared)

    def test_tokens_are_not_case_normalized(self) -> None:
        self.assertEqual(i18n.derive_term_candidates(("Discord",)), ("Discord",))


class LocaleStateTests(_LocaleFixture):
    def setUp(self) -> None:
        super().setUp()
        self.write_locale(
            self.locales,
            "zh_cn",
            {"term.target": "场景", "term.target.discord": "频道", "zh_only": "仅中文"},
        )
        self.write_locale(self.locales, "en_us", {"term.target": "context"})
        self.assertEqual(self.load(["zh_cn", "en_us"]), [])

    def test_round_trip_preserves_language_fallbacks_and_candidates(self) -> None:
        locale = i18n.Locale("zh_cn", fallback_lng=["en_us"], term_candidates=("discord.dm", "discord"))

        restored = i18n.Locale.from_state(locale.to_state())

        self.assertEqual(restored.locale, "zh_cn")
        self.assertEqual(restored.fallback_lng, ["en_us"])
        self.assertEqual(restored.term_candidates, ("discord.dm", "discord"))
        self.assertEqual(restored.term("target"), "频道")
        self.assertEqual(restored.term_key("target"), "term.target.discord")

    def test_unbound_and_bound_empty_are_distinguishable(self) -> None:
        unbound = i18n.Locale.from_state(i18n.Locale("zh_cn").to_state())
        bound = i18n.Locale.from_state(i18n.Locale("zh_cn", term_candidates=()).to_state())

        self.assertIsNone(unbound.term_candidates)
        self.assertFalse(unbound.term_scoped)
        self.assertEqual(bound.term_candidates, ())
        self.assertTrue(bound.term_scoped)
        self.assertEqual(unbound.t("term.target"), bound.t("term.target"))

    def test_from_state_tolerates_missing_and_unknown_fields(self) -> None:
        legacy = i18n.Locale.from_state({"locale": "en_us"})

        self.assertEqual(legacy.locale, "en_us")
        self.assertIsNone(legacy.term_candidates)
        self.assertEqual(legacy.fallback_lng, ["zh_cn"])

        tolerant = i18n.Locale.from_state(
            {"locale": "en_us", "term_candidates": ["discord"], "state_version": 3, "unknown": True}
        )

        self.assertEqual(tolerant.term_candidates, ("discord",))
        self.assertEqual(tolerant.fallback_lng, ["zh_cn"])

        with self.assertRaises(ValueError):
            i18n.Locale.from_state({})

    def test_derived_construction_keeps_the_scope(self) -> None:
        locale = i18n.Locale("zh_cn", fallback_lng=["en_us"], term_candidates=("discord",))

        other = locale.with_locale("en_us")

        self.assertEqual(other.locale, "en_us")
        # 回退链里的自指项（en_us）换成派生前的语言，换语言渲染仍能回退到会话语言。
        self.assertEqual(other.fallback_lng, ["zh_cn"])
        self.assertEqual(other.t("zh_only"), "仅中文")
        self.assertEqual(other.term_candidates, ("discord",))
        self.assertTrue(other.term_scoped)
        # 严格本语言命中：en_us 没有变体，得到本语言的基础术语。
        self.assertEqual(other.t("term.target"), "context")

        # 自动推导的回退链同样不会留下自指项。
        self.assertEqual(i18n.Locale("zh_cn").with_locale("en_us").fallback_lng, ["zh_cn"])
        self.assertEqual(i18n.Locale("zh_cn").with_locale("zh_cn").fallback_lng, ["en_us"])

        rescoped = locale.with_term_candidates(())
        unbound = locale.with_term_candidates(None)

        self.assertEqual(rescoped.locale, "zh_cn")
        self.assertEqual(rescoped.term_candidates, ())
        self.assertEqual(rescoped.t("term.target"), "场景")
        self.assertFalse(unbound.term_scoped)
        self.assertEqual(unbound.t("term.target"), "场景")


class TermAuditTests(_LocaleFixture):
    def test_clean_data_returns_no_issues(self) -> None:
        self.write_locale(
            self.locales,
            "zh_cn",
            {
                "term.target": "场景",
                "term.target.private": "私聊",
                "term.target.discord": "频道",
                "core.help.bind.channel": "查看当前{TERM:target}的消息通道：${channel} 号",
                "weekly.message.summary": "{TERM:target,count=${count}} 本周共有 ${count} 条动态。",
                "wiki.hint": '使用 [[ ]] 与 {{ }} 语法，例如 {"accept-language": "en"}',
            },
        )
        self.write_locale(self.locales, "en_us", {"term.target": "context"})

        self.assertEqual(self.load(["zh_cn", "en_us"]), [])
        self.assertEqual(i18n.audit_terms(), [])
        self.assertEqual(i18n.audit_terms(include_unreferenced=True), [])

    def test_missing_base_key_is_reported(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"value": "当前{TERM:target,count=3}"})

        errors = self.load(["zh_cn"])

        self.assertTrue(any('"value" (zh_cn)' in error and "term.target" in error for error in errors))
        self.assertTrue(any(error.startswith(i18n.TERM_AUDIT_PREFIX) for error in errors))

    def test_malformed_placeholders_are_reported(self) -> None:
        self.write_locale(
            self.locales,
            "zh_cn",
            {
                "term.target": "场景",
                "bad.lower": "{term:target}",
                "bad.space": "{TERM: target}",
                "bad.case": "{TERM:Target}",
                "bad.unclosed": "{TERM:target",
                "bad.trailing_comma": "{TERM:target,}",
                "bad.no_equals": "{TERM:target,count}",
                "bad.param_name": "{TERM:target,1st=x}",
            },
        )

        errors = self.load(["zh_cn"])

        for key in (
            "bad.lower",
            "bad.space",
            "bad.case",
            "bad.unclosed",
            "bad.trailing_comma",
            "bad.no_equals",
            "bad.param_name",
        ):
            self.assertTrue(any(key in error for error in errors), key)

    def test_term_values_must_be_plain_text(self) -> None:
        self.write_locale(
            self.locales,
            "zh_cn",
            {
                "term.target": "场景",
                "term.nested": "{TERM:target}",
                "term.i18n": "{I18N:term.target}",
                "term.plain_param": "${name} 的场景",
            },
        )

        errors = self.load(["zh_cn"])

        self.assertTrue(any("term.nested" in error and "plain text" in error for error in errors))
        self.assertTrue(any("term.i18n" in error and "plain text" in error for error in errors))
        self.assertFalse(any("term.plain_param" in error for error in errors))
        self.assertEqual(i18n.Locale("zh_cn").term("plain_param", name="Alice"), "Alice 的场景")

    def test_variant_without_base_key_is_reported(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"term.target.discord": "频道"})

        errors = self.load(["zh_cn"])

        self.assertTrue(any("term.target.discord" in error and "no base key" in error for error in errors))

    def test_variants_in_translation_locales_are_fine(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"term.target": "场景"})
        self.write_locale(self.locales, "en_us", {"term.target.discord": "channel"})

        # 基础语言已声明基础术语，其他语言的变体由 Weblate 逐步补齐，不算问题。
        self.assertEqual(self.load(["zh_cn", "en_us"]), [])

    def test_unreferenced_terms_are_only_reported_on_request(self) -> None:
        self.write_locale(
            self.locales,
            "zh_cn",
            {"term.target": "场景", "term.unused": "未使用", "value": "{TERM:target}"},
        )

        self.assertEqual(self.load(["zh_cn"]), [])
        self.assertEqual(i18n.audit_terms(), [])

        issues = i18n.audit_terms(include_unreferenced=True)

        self.assertTrue(any("term.unused" in issue for issue in issues))
        self.assertFalse(any('"term.target"' in issue for issue in issues))

    def test_audit_terms_accepts_an_explicit_locale_list(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"value": "当前{TERM:missing}"})
        self.write_locale(self.locales, "en_us", {"value": "plain"})
        self.load(["zh_cn", "en_us"])

        self.assertEqual(i18n.audit_terms(["en_us"]), [])
        self.assertTrue(i18n.audit_terms(["zh_cn"]))

    def test_unreferenced_check_sees_references_outside_the_requested_locales(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"term.target": "场景", "value": "{TERM:target}"})
        self.write_locale(self.locales, "en_us", {"value": "plain"})
        self.load(["zh_cn", "en_us"])

        # 引用发生在 zh_cn，只检查 en_us 时不应把 term.target 误判为“从未被引用”。
        self.assertEqual(i18n.audit_terms(["en_us"], include_unreferenced=True), [])
        self.assertEqual(i18n.audit_terms(include_unreferenced=True), [])


class TermDirectoryTests(_LocaleFixture):
    """<locales>/terms/ 是术语的专属来源，与一般语义词条分开维护。"""

    def test_terms_directory_defines_terms_without_the_prefix(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"core.help.bind.channel": "查看当前{TERM:target}的消息通道。"})
        self.write_terms_file(self.locales, "zh_cn", {"target": "场景", "target.qq.group": "群聊"})

        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn", term_candidates=("qq.group", "qq"))

        self.assertEqual(locale.t("core.help.bind.channel"), "查看当前群聊的消息通道。")
        self.assertEqual(locale.term_key("target"), "term.target.qq.group")
        # 术语仍然落在同一个词条命名空间里，查表接口不变。
        self.assertEqual(locale["term.target"], "场景")

    def test_prefixed_and_nested_keys_are_accepted(self) -> None:
        self.write_terms_file(
            self.locales,
            "zh_cn",
            {"term.target": "场景", "target": {"private": "私聊"}, "target.discord": "频道"},
        )
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn")

        self.assertEqual(locale.term("target"), "场景")
        self.assertEqual(i18n.Locale("zh_cn", term_candidates=("private",)).term("target"), "私聊")
        self.assertEqual(i18n.Locale("zh_cn", term_candidates=("discord",)).term("target"), "频道")

    def test_nested_subdirectories_are_scanned(self) -> None:
        self.write_terms_file(self.locales, "zh_cn", {"target": "场景"}, subdirectory="platform")
        self.write_terms_file(self.locales, "zh_cn", {"target.discord": "频道"}, subdirectory="platform/discord")

        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn", term_candidates=("discord",))

        self.assertEqual(locale.term("target"), "频道")

    def test_terms_from_multiple_locale_directories_merge(self) -> None:
        bots = self.root / "bots-locales"
        self.write_locale(self.locales, "zh_cn", {"value": "当前{TERM:target}"})
        self.write_terms_file(self.locales, "zh_cn", {"target": "场景"})
        self.write_terms_file(bots, "zh_cn", {"target.discord": "频道"})

        self.assertEqual(i18n.load_locale_file(["zh_cn"], [str(self.locales), str(bots)]), [])
        self.assertEqual(i18n.Locale("zh_cn", term_candidates=("discord",)).t("value"), "当前频道")

    def test_general_file_copy_is_superseded_by_the_terms_directory(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"value": "当前{TERM:target}", "term.target": "旧场景"})
        self.write_terms_file(self.locales, "zh_cn", {"target": "场景"})

        errors = self.load(["zh_cn"])

        # 迁移期间不静默：两份定义都会被指出，实际生效的是术语目录里的那份。
        self.assert_term_issues(errors, "term.target", "terms directory", str(self.locales / "zh_cn.json"))
        self.assertEqual(i18n.Locale("zh_cn").t("value"), "当前场景")
        # 与其它术语诊断一致，audit_terms=False 时该条目也不出现。
        self.assertEqual(self.load(["zh_cn"], audit_terms=False), [])

    def test_two_terms_files_still_conflict(self) -> None:
        other = self.root / "other-locales"
        self.write_terms_file(self.locales, "zh_cn", {"target": "场景"})
        self.write_terms_file(other, "zh_cn", {"target": "另一个场景"})

        errors = i18n.load_locale_file(["zh_cn"], [str(self.locales), str(other)])

        self.assertTrue(any('Conflict detected for key "term.target"' in error for error in errors))
        self.assertIsNone(i18n.Locale("zh_cn")["term.target"])

    def test_terms_only_locale_is_loaded(self) -> None:
        self.write_terms_file(self.locales, "zh_cn", {"target": "场景"})

        self.assertEqual(self.load(["zh_cn"]), [])
        self.assertIn("zh_cn", i18n.get_available_locales())
        self.assertEqual(i18n.Locale("zh_cn").term("target"), "场景")

    def test_audit_sees_terms_from_the_directory(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"value": "当前{TERM:target}", "bad": "{TERM:missing}"})
        self.write_terms_file(self.locales, "zh_cn", {"target": "场景", "broken": "{TERM:target}"})

        errors = self.load(["zh_cn"])

        # 术语目录里的定义参与“基础键缺失”判定；目录里的术语值同样要接受纯文本校验。
        self.assert_term_issues(errors, "term.missing", 'term value "term.broken"')
        self.assertFalse(any("term.target" in error and "missing" in error for error in errors))

    def test_reload_picks_up_terms_directory_changes(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"value": "当前{TERM:target}"})
        self.write_terms_file(self.locales, "zh_cn", {"target": "场景"})
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn", term_candidates=("qq.group", "qq"))
        self.assertEqual(locale.t("value"), "当前场景")

        self.write_terms_file(self.locales, "zh_cn", {"target": "场景", "target.qq.group": "群聊"})

        self.assertEqual(i18n.Locale.reload(), [])
        self.assertEqual(locale.t("value"), "当前群聊")

    def test_missing_terms_directory_and_unrelated_files_are_ignored(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"term.target": "场景", "value": "当前{TERM:target}"})
        (self.locales / "notes.txt").write_text("ignored")
        (self.locales / i18n.TERMS_DIRECTORY_NAME).mkdir()
        (self.locales / i18n.TERMS_DIRECTORY_NAME / "notes.txt").write_text("ignored")

        self.assertEqual(self.load(["zh_cn"]), [])
        self.assertEqual(i18n.Locale("zh_cn").t("value"), "当前场景")

    def test_broken_terms_file_is_reported(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"value": "plain"})
        terms = self.locales / i18n.TERMS_DIRECTORY_NAME
        terms.mkdir()
        (terms / "zh_cn.json").write_bytes(b"{")

        with redirect_stderr(StringIO()):
            errors = self.load(["zh_cn"])

        self.assertTrue(any("Failed to load" in error and "terms" in error for error in errors))
        self.assertEqual(i18n.Locale("zh_cn").t("value"), "plain")


class TermBuildIntegrationTests(_LocaleFixture):
    def test_build_reports_term_issues_but_still_publishes(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"value": "当前{TERM:missing}"})

        errors = i18n.build_locale_snapshot(["zh_cn"], [str(self.locales)], "term-audit")

        self.assertTrue(any("term.missing" in error for error in errors))

        generation = i18n.connect_locale_snapshot("term-audit")
        manifest = orjson.loads((i18n._named_snapshot_root("term-audit") / "current.json").read_bytes())

        self.assertTrue(generation)
        self.assertFalse(manifest["has_load_failures"])
        self.assertEqual(manifest["generation"], generation)
        self.assertTrue(any("term.missing" in error for error in manifest["errors"]))
        self.assertEqual(i18n.Locale("zh_cn").t("value"), "当前{TERM:missing}")

    def test_build_audit_switch_applies_to_fresh_and_cached_snapshots(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"value": "当前{TERM:missing}"})
        namespace = "term-audit-off"

        self.assertEqual(
            i18n.build_locale_snapshot(["zh_cn"], [str(self.locales)], namespace, audit_terms=False),
            [],
        )
        # 复用同一 generation 时开关依然生效。
        self.assertEqual(
            i18n.build_locale_snapshot(["zh_cn"], [str(self.locales)], namespace, audit_terms=False),
            [],
        )
        # 但审计结果由发布该 generation 的构建决定：缓存里没有术语条目时，后续调用也不会凭空产生。
        self.assertEqual(i18n.build_locale_snapshot(["zh_cn"], [str(self.locales)], namespace), [])

    def test_load_locale_file_reports_term_issues_and_respects_the_switch(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"value": "当前{TERM:missing}"})

        self.assertTrue(any("term.missing" in error for error in self.load(["zh_cn"])))

        self.write_locale(self.locales, "zh_cn", {"value": "当前{TERM:missing_again}"})

        self.assertEqual(self.load(["zh_cn"], audit_terms=False), [])
        self.assertEqual(i18n.Locale("zh_cn").t("value"), "当前{TERM:missing_again}")


class TermSnapshotReloadTests(_LocaleFixture):
    def test_new_variant_takes_effect_after_reload(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"term.target": "场景", "value": "当前{TERM:target}"})
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn", term_candidates=("qq.group", "qq"))

        self.assertEqual(locale.t("value"), "当前场景")

        self.write_locale(
            self.locales,
            "zh_cn",
            {"term.target": "场景", "term.target.qq.group": "群聊", "value": "当前{TERM:target}"},
        )

        self.assertEqual(i18n.Locale.reload(), [])

        # 候选链无需重建：同一个 Locale 对象重载后立即命中新变体。
        self.assertEqual(locale.t("value"), "当前群聊")
        self.assertEqual(locale.term_key("target"), "term.target.qq.group")


class TermFastPathTests(_LocaleFixture):
    def test_text_without_placeholder_short_circuits(self) -> None:
        self.write_locale(self.locales, "zh_cn", {"value": "没有占位符"})
        self.assertEqual(self.load(["zh_cn"]), [])

        locale = i18n.Locale("zh_cn")
        text = "没有占位符"

        self.assertIs(locale._resolve_terms(text), text)

        # 用会抛错的替身证明无占位符路径完全没有进入正则，与 0.1.3 的耗时一致。
        with mock.patch.object(i18n, "TERM_PATTERN", _ExplodingTermPattern()):
            self.assertEqual(locale.t("value"), text)
            self.assertEqual(locale.t_str(text), text)


class _ExplodingTermPattern:
    """任何一次 sub/search 都视为回归。"""

    def sub(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("term regex must not run for text without placeholders")

    def search(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("term regex must not run for text without placeholders")


if __name__ == "__main__":
    unittest.main()
