import multiprocessing
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
