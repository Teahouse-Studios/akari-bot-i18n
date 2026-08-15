import hashlib
import html
import os
import re
import sqlite3
import tempfile
import time
import traceback
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from string import Template
from threading import RLock
from typing import Any

import orjson

from .utils import flatten_dict as flatten

MAX_I18NCODE_DEPTH = 10
MANIFEST_CHECK_INTERVAL = 1.0
SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_LOCK_TIMEOUT = 30.0
OLD_SNAPSHOT_MIN_AGE = 24 * 60 * 60
SNAPSHOTS_TO_KEEP = 3

supported_locales: list[str] = []
_lang_list: list[str] = []
_locales_path: list[str] = []
# 查询锁保护当前 store/连接；加载锁只串行化耗时的快照构建，不阻塞旧快照读取。
_locale_lock = RLock()
_load_lock = RLock()
_store: "_SQLiteLocaleStore | None" = None


class _InterProcessFileLock:
    """使用操作系统字节锁，保证同一快照目录同时只有一个构建者。"""

    def __init__(self, path: Path, timeout: float = SNAPSHOT_LOCK_TIMEOUT):
        self.path = path
        self.timeout = timeout
        self._file: Any = None

    def __enter__(self) -> "_InterProcessFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a+b")
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"\0")
            self._file.flush()

        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._lock()
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._file.close()
                    self._file = None
                    raise TimeoutError(f"Timed out waiting for locale snapshot lock: {self.path}")
                time.sleep(0.05)

    def _lock(self) -> None:
        self._file.seek(0)
        # Windows 和 POSIX 的文件锁 API 不同，但进程退出时都会由内核释放锁。
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def _read_manifest(path: Path) -> dict[str, Any] | None:
    """读取并做最小结构校验；无效 manifest 不应中断仍在工作的旧连接。"""

    try:
        data = orjson.loads(path.read_bytes())
    except (FileNotFoundError, orjson.JSONDecodeError, OSError):
        return None

    if not isinstance(data, dict) or data.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        return None

    db_name = data.get("db_name")
    if not isinstance(db_name, str) or Path(db_name).name != db_name or not db_name.endswith(".db"):
        return None

    return data


def _snapshot_db_path(root: Path, manifest: Mapping[str, Any]) -> Path | None:
    db_name = manifest.get("db_name")
    if not isinstance(db_name, str) or Path(db_name).name != db_name:
        return None

    db_path = (root / db_name).resolve()
    try:
        # manifest 只能引用快照目录内的普通文件，避免路径穿越。
        db_path.relative_to(root.resolve())
    except ValueError:
        return None
    return db_path


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    # 已发布的版本文件永不原地修改，因此可以安全使用 immutable 模式跳过 SQLite 文件锁。
    uri = f"{db_path.resolve().as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA cache_size = -512")
    connection.execute("PRAGMA mmap_size = 67108864")
    return connection


def _validate_connection(connection: sqlite3.Connection) -> None:
    """在切换连接前验证版本、完整性和必要表，避免损坏快照替换健康版本。"""

    user_version = connection.execute("PRAGMA user_version").fetchone()
    if user_version is None or int(user_version[0]) != SNAPSHOT_SCHEMA_VERSION:
        raise sqlite3.DatabaseError("Unsupported locale snapshot schema version")

    quick_check = connection.execute("PRAGMA quick_check").fetchone()
    if quick_check is None or quick_check[0] != "ok":
        raise sqlite3.DatabaseError(f"Locale snapshot quick check failed: {quick_check}")

    connection.execute("SELECT locale FROM locales LIMIT 0")
    connection.execute("SELECT value FROM translations WHERE locale = '' AND key = '' LIMIT 0")


class _SQLiteLocaleStore:
    """当前进程的只读 SQLite 连接，并负责跟随 manifest 刷新 generation。"""

    def __init__(self, root: Path, manifest: Mapping[str, Any]):
        self.root = root
        self.manifest_path = root / "current.json"
        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None
        self._generation = ""
        self._pid = os.getpid()
        self._last_manifest_check = 0.0
        self._install_manifest(manifest)

    @property
    def generation(self) -> str:
        with self._lock:
            self._refresh_if_needed()
            return self._generation

    def _install_manifest(self, manifest: Mapping[str, Any]) -> None:
        db_path = _snapshot_db_path(self.root, manifest)
        generation = manifest.get("generation")
        if db_path is None or not db_path.is_file() or not isinstance(generation, str):
            raise RuntimeError("Invalid locale snapshot manifest")

        expected_size = manifest.get("db_size")
        if isinstance(expected_size, int) and db_path.stat().st_size != expected_size:
            raise RuntimeError("Locale snapshot size does not match its manifest")

        connection = _connect_read_only(db_path)
        try:
            _validate_connection(connection)
        except Exception:
            connection.close()
            raise
        # 必须先完整打开并验证新连接，再替换旧连接，查询始终至少有一个健康版本可用。
        old_connection = self._connection
        self._connection = connection
        self._generation = generation
        self._pid = os.getpid()
        if old_connection is not None:
            old_connection.close()

    def _refresh_if_needed(self, force: bool = False) -> None:
        current_pid = os.getpid()
        if current_pid != self._pid:
            # fork 后不能继续复用父进程创建的 SQLite 连接。
            if self._connection is not None:
                self._connection.close()
            self._connection = None
            self._pid = current_pid
            force = True

        now = time.monotonic()
        # 限制 manifest 的 stat/read 频率，避免每次翻译都访问文件系统。
        if not force and now - self._last_manifest_check < MANIFEST_CHECK_INTERVAL:
            return
        self._last_manifest_check = now

        manifest = _read_manifest(self.manifest_path)
        if manifest is None:
            return
        if self._connection is not None and manifest.get("generation") == self._generation:
            return

        try:
            self._install_manifest(manifest)
        except (OSError, RuntimeError, sqlite3.Error):
            # 新 generation 不可用时继续服务旧 generation，下一轮检查再重试。
            return

    def get(self, locale: str, key: str) -> str | None:
        with self._lock:
            self._refresh_if_needed()
            if self._connection is None:
                return None
            row = self._connection.execute(
                "SELECT value FROM translations WHERE locale = ? AND key = ?",
                (locale, key),
            ).fetchone()
            return None if row is None else str(row[0])

    def available_locales(self) -> tuple[str, ...]:
        with self._lock:
            self._refresh_if_needed()
            if self._connection is None:
                return ()
            rows = self._connection.execute("SELECT locale FROM locales ORDER BY position").fetchall()
            return tuple(str(row[0]) for row in rows)

    def locale_keys(self, locale: str) -> tuple[str, ...]:
        with self._lock:
            self._refresh_if_needed()
            if self._connection is None:
                return ()
            rows = self._connection.execute(
                "SELECT key FROM translations WHERE locale = ? ORDER BY position",
                (locale,),
            ).fetchall()
            return tuple(str(row[0]) for row in rows)

    def locale_length(self, locale: str) -> int:
        with self._lock:
            self._refresh_if_needed()
            if self._connection is None:
                return 0
            row = self._connection.execute(
                "SELECT COUNT(*) FROM translations WHERE locale = ?",
                (locale,),
            ).fetchone()
            return 0 if row is None else int(row[0])

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None


class _LocaleDataView(Mapping[str, str]):
    """兼容 Locale.data 的惰性只读视图，避免重新物化整种语言的字典。"""

    def __init__(self, locale: str):
        self.locale = locale

    def __getitem__(self, key: str) -> str:
        value = _get_value(self.locale, key)
        if value is None:
            raise KeyError(key)
        return value

    def __iter__(self) -> Iterator[str]:
        with _locale_lock:
            if _store is None:
                return iter(())
            return iter(_store.locale_keys(self.locale))

    def __len__(self) -> int:
        with _locale_lock:
            if _store is None:
                return 0
            return _store.locale_length(self.locale)


class _LocaleDataRegistry(Mapping[str, Mapping[str, str]]):
    def __getitem__(self, locale: str) -> Mapping[str, str]:
        if locale not in get_available_locales():
            raise KeyError(locale)
        return _LocaleDataView(locale)

    def __iter__(self) -> Iterator[str]:
        return iter(get_available_locales())

    def __len__(self) -> int:
        return len(get_available_locales())


# Compatibility facade for callers that read the old module-level mapping.
# It is intentionally read-only so SQLite remains the source of truth.
locale_data: Mapping[str, Mapping[str, str]] = _LocaleDataRegistry()


def _source_files(locales_paths: tuple[str, ...]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[Path] = set()
    for locales_path in locales_paths:
        directory = Path(locales_path)
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.iterdir(), key=lambda path: path.name):
            if not candidate.is_file() or candidate.suffix != ".json":
                continue
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                result.append(resolved)
    return tuple(result)


def _source_signature(
    languages: tuple[str, ...], locales_paths: tuple[str, ...], source_files: tuple[Path, ...]
) -> str:
    digest = hashlib.sha256()
    for language in languages:
        digest.update(b"language\0")
        digest.update(language.encode("utf-8"))
        digest.update(b"\0")
    for locales_path in locales_paths:
        digest.update(b"path\0")
        digest.update(str(Path(locales_path).resolve()).encode("utf-8"))
        digest.update(b"\0")
    for source_file in source_files:
        stat = source_file.stat()
        digest.update(str(source_file).encode("utf-8"))
        digest.update(f"\0{stat.st_size}\0{stat.st_mtime_ns}\0".encode())
        # 内容哈希保证显式 reload 能识别“大小和 mtime 均未变化”的文件替换。
        digest.update(source_file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _snapshot_root(languages: tuple[str, ...], locales_paths: tuple[str, ...]) -> Path:
    """为同一组语言和来源生成稳定目录，使不同 worker 能找到同一个 manifest。"""

    configured_base = os.environ.get("AKARI_BOT_I18N_CACHE_DIR")
    if configured_base:
        base = Path(configured_base)
    else:
        user_suffix = f"_{os.getuid()}" if hasattr(os, "getuid") else ""
        base = Path(tempfile.gettempdir()) / f"akari_bot_i18n{user_suffix}"
    identity = hashlib.sha256()
    for language in languages:
        identity.update(f"language:{language}\0".encode())
    for locales_path in locales_paths:
        identity.update(f"path:{Path(locales_path).resolve()}\0".encode())
    return base / identity.hexdigest()[:24]


def _build_snapshot(
    root: Path,
    source_files: tuple[Path, ...],
    source_signature: str,
) -> tuple[Path, dict[str, Any]]:
    generation = uuid.uuid4().hex
    db_name = f"locales.{generation}.db"
    temp_db = root / f".{db_name}.tmp"
    final_db = root / db_name
    errors: list[str] = []
    has_load_failures = False
    connection: sqlite3.Connection | None = None

    try:
        connection = sqlite3.connect(temp_db)
        # 数据库尚未发布，即使进程崩溃也可直接丢弃，因此构建阶段关闭 journal/synchronous。
        connection.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            PRAGMA temp_store = MEMORY;
            CREATE TABLE raw_translations (
                position INTEGER PRIMARY KEY AUTOINCREMENT,
                locale TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                source TEXT NOT NULL
            );
            CREATE TABLE locales (
                locale TEXT PRIMARY KEY,
                position INTEGER NOT NULL
            ) WITHOUT ROWID;
            """
        )

        locale_position = 0
        for source_file in source_files:
            locale = source_file.stem
            # 单文件使用 savepoint；解析失败时不会把该文件的半批数据写入快照。
            connection.execute("SAVEPOINT locale_file")
            try:
                raw_data = orjson.loads(source_file.read_bytes())
                if not isinstance(raw_data, dict):
                    raise TypeError("locale file root must be a JSON object")

                connection.execute(
                    "INSERT OR IGNORE INTO locales(locale, position) VALUES (?, ?)",
                    (locale, locale_position),
                )
                rows = (
                    (locale, key, str(value), str(source_file))
                    for key, value in flatten(raw_data).items()
                    if value is not None
                )
                connection.executemany(
                    "INSERT INTO raw_translations(locale, key, value, source) VALUES (?, ?, ?, ?)",
                    rows,
                )
                connection.execute("RELEASE SAVEPOINT locale_file")
                locale_position += 1
            except Exception as error:
                has_load_failures = True
                connection.execute("ROLLBACK TO SAVEPOINT locale_file")
                connection.execute("RELEASE SAVEPOINT locale_file")
                traceback.print_exc()
                errors.append(f"Failed to load {source_file}: {error}")

        # 先在 staging 表保留来源信息，才能报告跨目录重复键并将冲突键整体排除。
        conflicts = connection.execute(
            """
            SELECT locale, key
            FROM raw_translations
            GROUP BY locale, key
            HAVING COUNT(*) > 1
            ORDER BY MIN(position)
            """
        ).fetchall()
        for locale, key in conflicts:
            errors.append(f'Conflict detected for key "{key}":')
            sources = connection.execute(
                "SELECT source FROM raw_translations WHERE locale = ? AND key = ? ORDER BY position",
                (locale, key),
            ).fetchall()
            errors.extend(str(source[0]) for source in sources)

        # 只把无冲突记录写入最终只读表，然后删除包含冗余来源信息的 staging 表。
        connection.executescript(
            """
            CREATE TABLE translations (
                locale TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY(locale, key)
            ) WITHOUT ROWID;
            INSERT INTO translations(locale, key, value, position)
            SELECT locale, key, MIN(value), MIN(position)
            FROM raw_translations
            GROUP BY locale, key
            HAVING COUNT(*) = 1;
            CREATE INDEX translations_locale_position ON translations(locale, position);
            DROP TABLE raw_translations;
            """
        )
        connection.execute(f"PRAGMA user_version = {SNAPSHOT_SCHEMA_VERSION}")
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise sqlite3.DatabaseError(f"Locale snapshot integrity check failed: {integrity}")
        connection.close()
        connection = None

        with open(temp_db, "r+b") as database_file:
            os.fsync(database_file.fileno())
        # 版本数据库先落盘并改名，manifest 最后发布，reader 不会看到半成品。
        os.replace(temp_db, final_db)
        _fsync_directory(root)

        manifest = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "generation": generation,
            "db_name": db_name,
            "db_size": final_db.stat().st_size,
            "source_signature": source_signature,
            "errors": errors,
            "has_load_failures": has_load_failures,
        }
        return final_db, manifest
    except Exception:
        if connection is not None:
            connection.close()
        temp_db.unlink(missing_ok=True)
        final_db.unlink(missing_ok=True)
        raise


def _publish_manifest(root: Path, manifest: Mapping[str, Any]) -> None:
    manifest_path = root / "current.json"
    temp_manifest = root / f".current.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp_manifest, "wb") as manifest_file:
            manifest_file.write(orjson.dumps(manifest))
            manifest_file.flush()
            os.fsync(manifest_file.fileno())
        # 同目录 os.replace 是 generation 切换的原子提交点。
        os.replace(temp_manifest, manifest_path)
        _fsync_directory(root)
    finally:
        temp_manifest.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _cleanup_old_snapshots(root: Path, current_db_name: str) -> None:
    cutoff = time.time() - OLD_SNAPSHOT_MIN_AGE
    candidates = sorted(root.glob("locales.*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
    # 保留最近几个版本，覆盖 reader 已读旧 manifest、但尚未打开数据库的短暂窗口。
    protected_names = {candidate.name for candidate in candidates[:SNAPSHOTS_TO_KEEP]}
    protected_names.add(current_db_name)
    for candidate in candidates:
        if candidate.name in protected_names:
            continue
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
        except (FileNotFoundError, PermissionError, OSError):
            continue


def _ensure_snapshot(
    root: Path,
    languages: tuple[str, ...],
    locales_paths: tuple[str, ...],
) -> tuple[dict[str, Any], list[str]]:
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700)
    # 锁覆盖签名检查、构建和 manifest 发布，其他进程只会复用构建结果。
    with _InterProcessFileLock(root / ".snapshot-publish.lock"):
        for attempt in range(3):
            try:
                source_files = _source_files(locales_paths)
                signature_before = _source_signature(languages, locales_paths, source_files)
            except OSError:
                if attempt == 2:
                    raise
                continue
            manifest = _read_manifest(root / "current.json")
            current_db = _snapshot_db_path(root, manifest) if manifest is not None else None
            if (
                manifest is not None
                and manifest.get("source_signature") == signature_before
                and current_db is not None
                and current_db.is_file()
            ):
                # 源内容未变化时直接复用当前 generation，避免重复解析 JSON。
                manifest_errors = manifest.get("errors", [])
                errors = [str(error) for error in manifest_errors] if isinstance(manifest_errors, list) else []
                return manifest, errors

            final_db, new_manifest = _build_snapshot(root, source_files, signature_before)
            try:
                refreshed_files = _source_files(locales_paths)
                signature_after = _source_signature(languages, locales_paths, refreshed_files)
            except OSError:
                final_db.unlink(missing_ok=True)
                if attempt == 2:
                    raise
                continue
            if signature_after != signature_before:
                # 构建期间源文件发生变化，丢弃结果并基于新内容重试。
                final_db.unlink(missing_ok=True)
                continue

            new_errors_value = new_manifest.get("errors", [])
            new_errors = [str(error) for error in new_errors_value] if isinstance(new_errors_value, list) else []
            if new_manifest.get("has_load_failures") and manifest is not None and current_db is not None:
                # reload 失败时保留 last-known-good，避免临时写坏 JSON 清空线上翻译。
                final_db.unlink(missing_ok=True)
                return manifest, new_errors

            _publish_manifest(root, new_manifest)
            _cleanup_old_snapshots(root, str(new_manifest["db_name"]))
            return new_manifest, new_errors

    raise RuntimeError("Locale files changed repeatedly while building the SQLite snapshot")


def _get_value(locale: str, key: str) -> str | None:
    with _locale_lock:
        if _store is None:
            return None
        return _store.get(locale, key)


def load_locale_file(
    lang_list: list[str],
    locales_path: list[str] | None = None,
    reload: bool = False,
) -> list[str]:
    global _store

    with _load_lock:
        with _locale_lock:
            for language in lang_list:
                if language not in supported_locales:
                    supported_locales.append(language)

            if not reload:
                for language in lang_list:
                    if language not in _lang_list:
                        _lang_list.append(language)
                if locales_path:
                    for path in locales_path:
                        if path not in _locales_path:
                            _locales_path.append(path)

            languages = tuple(lang_list)
            locales_paths = tuple(locales_path or [])
            registered_languages = tuple(_lang_list) or languages
            registered_paths = tuple(_locales_path) or locales_paths

        # 已注册来源决定共享 manifest 的位置；普通 load 仍只导入本次传入的路径。
        root = _snapshot_root(registered_languages, registered_paths)
        manifest, errors = _ensure_snapshot(root, languages, locales_paths)
        try:
            new_store = _SQLiteLocaleStore(root, manifest)
        except (OSError, RuntimeError, sqlite3.Error):
            latest_manifest = _read_manifest(root / "current.json")
            if latest_manifest is None or latest_manifest.get("generation") == manifest.get("generation"):
                raise
            new_store = _SQLiteLocaleStore(root, latest_manifest)

        with _locale_lock:
            # 构建在锁外完成，最后仅用一次指针/连接交换提交到当前进程。
            old_store = _store
            _store = new_store
            if old_store is not None:
                old_store.close()

        return errors


def get_available_locales() -> list[str]:
    with _locale_lock:
        if _store is None:
            return []
        return list(_store.available_locales())


class Locale:
    """创建一个本地化对象。"""

    def __init__(self, locale: str, fallback_lng: list[str] | None = None):
        self.locale = locale
        if fallback_lng is None:
            fallback_lng = [language for language in supported_locales if language != locale]
        self.fallback_lng = fallback_lng

    @property
    def data(self) -> Mapping[str, str]:
        return _LocaleDataView(self.locale)

    def __getitem__(self, key: str) -> str | None:
        return _get_value(self.locale, key)

    def __contains__(self, key: str) -> bool:
        return _get_value(self.locale, key) is not None

    @staticmethod
    def reload() -> list[str]:
        return load_locale_file(_lang_list, _locales_path, reload=True)

    def get_string_with_fallback(
        self,
        key: str,
        fallback: bool = True,
        locale_failed_prompt: bool = True,
    ) -> str:
        with _locale_lock:
            value = _get_value(self.locale, key)
            if value is not None:
                return value

            if fallback:
                for language in self.fallback_lng:
                    value = _get_value(language, key)
                    if value is not None:
                        return value

            if locale_failed_prompt:
                if key == "error.i18n.fallback":
                    return f"{{I18N:{key}}}"
                return f"{{I18N:{key}}}" + self.t(
                    "error.i18n.fallback",
                    fallback=fallback,
                    locale_failed_prompt=False,
                )

            return f"{{I18N:{key}}}"

    def t(self, key: str | dict, fallback: bool = True, locale_failed_prompt: bool = True, **kwargs: Any) -> str:
        """
        获取本地化字符串。

        :param key: 本地化键名。
        :param fallback: 是否使用 fallback。（默认为 True）
        :param locale_failed_prompt: 是否添加本地化失败提示。（默认为 True）
        :returns: 本地化字符串。
        """
        with _locale_lock:
            if isinstance(key, dict):
                if translated := key.get(self.locale):
                    return str(translated)
                if fallback and "_fallback_locale" in key:
                    return str(key["_fallback_locale"])
                return str(key) + self.t(
                    "error.i18n.fallback",
                    locale_failed_prompt=False,
                    _fallback_locale=self.locale,
                )

            localized = self.get_string_with_fallback(key, fallback, locale_failed_prompt)
            return Template(localized).safe_substitute(**kwargs)

    def t_str(
        self,
        text: str,
        fallback: bool = True,
        locale_failed_prompt: bool = False,
        **kwargs: dict[str, Any],
    ) -> str:
        """
        替换字符串中的本地化键名。

        :param text: 字符串。
        :param fallback: 是否使用 fallback。（默认为 True）
        :param locale_failed_prompt: 是否添加本地化失败提示。（默认为False）
        :returns: 本地化后的字符串。
        """
        with _locale_lock:

            def match_i18ncode(match: re.Match[str]) -> str:
                full = match.group(0)
                key = html.unescape(match.group(1))
                params_str = match.group(2)
                local_kwargs = {}
                if params_str:
                    params_str = self.t_str(
                        params_str,
                        fallback=fallback,
                        locale_failed_prompt=locale_failed_prompt,
                    )
                    param_pairs = re.findall(r"(\w+)=([^,]+)", params_str)
                    for local_key, value in param_pairs:
                        local_kwargs[html.unescape(local_key)] = html.unescape(value)
                all_kwargs = {**kwargs, **local_kwargs}
                translated = self.t(
                    key,
                    fallback=fallback,
                    locale_failed_prompt=locale_failed_prompt,
                    **all_kwargs,
                )
                return translated if isinstance(translated, str) else full

            previous_text = None
            depth = 0
            while previous_text != text and depth < MAX_I18NCODE_DEPTH:
                previous_text = text
                text = re.sub(r"\{I18N:([^\s,{}]+)(?:,([^{}]*))?}", match_i18ncode, text)
                depth += 1
            return text


def _reset_state_for_testing() -> None:
    global _store
    with _locale_lock:
        if _store is not None:
            _store.close()
        _store = None
        supported_locales.clear()
        _lang_list.clear()
        _locales_path.clear()


__all__ = ["Locale", "load_locale_file", "get_available_locales"]
