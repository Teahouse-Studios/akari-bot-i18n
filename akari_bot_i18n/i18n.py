import hashlib
import html
import os
import re
import sqlite3
import tempfile
import time
import traceback
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from string import Template
from threading import RLock
from typing import Any, NamedTuple, Protocol

import orjson

from .utils import flatten_dict as flatten

MAX_I18NCODE_DEPTH = 10
MANIFEST_CHECK_INTERVAL = 1.0
SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_LOCK_TIMEOUT = 30.0
SNAPSHOTS_TO_KEEP = 2

# 术语占位符：{TERM:<name>} 或 {TERM:<name>,<param>=<value>,...}。
# 标签必须大写（与 {I18N:...} 风格一致），名称必须小写；双写 {{...}} 不是合法占位符，也不会被解析。
# 参数值允许包含 = : / ? 与空格，但不允许逗号或大括号，因此无需转义即可表达 URL。
TERM_PLACEHOLDER_PREFIX = "{TERM:"
TERM_PATTERN = re.compile(r"(?<!\{)\{TERM:([a-z][a-z0-9_]*)((?:,[A-Za-z_][A-Za-z0-9_]*=[^,{}]*)*)\}")
TERM_PARAM_PATTERN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^,]*)")
# 审计用：参数值额外允许 `${...}` 字面量。文案里的 `{TERM:target,count=${count}}` 会在 kwargs 插值
# 之后变成 `{TERM:target,count=3}`，但静态扫描看到的是未插值形态，不应被误报为语法错误。
TERM_PLAUSIBLE_PATTERN = re.compile(
    r"(?<!\{)\{TERM:([a-z][a-z0-9_]*)((?:,[A-Za-z_][A-Za-z0-9_]*=(?:[^,{}$]|\$(?!\{)|\$\{[^{}]*\})*)*)\}"
)
# 形如术语占位符的片段（大小写不敏感），用于构建期审计；合法的占位符会被排除。
TERM_LIKE_PATTERN = re.compile(r"\{[Tt][Ee][Rr][Mm]:(?:[^{}$]|\$(?!\{)|\$\{[^{}]*\})*\}?")
# term.<name> 或 term.<name>.<candidate>（candidate 本身可以再含点）。
TERM_KEY_PREFIX = "term."
TERM_KEY_PATTERN = re.compile(r"term\.([a-z][a-z0-9_]*)(?:\.(.+))?")
I18NCODE_PATTERN = re.compile(r"\{I18N:([^\s,{}]+)(?:,([^{}]*))?\}")
# 术语审计条目统一前缀，便于调用方在 errors 列表里识别，并让 audit_terms=False 在复用缓存时依然生效。
TERM_AUDIT_PREFIX = "[term] "
# 每个语言目录下的术语子目录：<locales>/terms/<locale>.json 只存放术语词条，
# 与一般语义词条分开维护。目录内的键默认省略 `term.` 前缀（写成 `target.private` 即可），
# 已经带前缀的写法同样接受，便于把术语从一般词条文件里整段搬过来。
TERMS_DIRECTORY_NAME = "terms"
_SOURCE_CATEGORY_LOCALE = "locale"
_SOURCE_CATEGORY_TERMS = "terms"
# 派生候选链时 token 不允许出现的字符：它们会破坏键名或占位符语法。
_INVALID_TERM_TOKEN_CHARS = frozenset(".{}:")

supported_locales: list[str] = []
_lang_list: list[str] = []
_locales_path: list[str] = []
# 查询锁保护当前 store/连接；加载锁只串行化耗时的快照构建，不阻塞旧快照读取。
_locale_lock = RLock()
_load_lock = RLock()
_store: "_SQLiteLocaleStore | None" = None
_connected_namespace: str | None = None


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
        self._db_name = ""
        self._supported_locales: tuple[str, ...] = ()
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
        self._db_name = db_path.name
        declared_locales = manifest.get("supported_locales", [])
        self._supported_locales = (
            tuple(str(locale) for locale in declared_locales) if isinstance(declared_locales, list) else ()
        )
        self._pid = os.getpid()
        if old_connection is not None:
            old_connection.close()
        # reader 关闭旧连接后再补一次清理，尤其用于 Windows 上此前被占用的数据库。
        _cleanup_old_snapshots(self.root, self._db_name)

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

    def supported_locales(self) -> tuple[str, ...]:
        with self._lock:
            self._refresh_if_needed()
            return self._supported_locales or self.available_locales()

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


class _LocaleSource(NamedTuple):
    """一个语言来源文件；`terms` 标记它是否来自术语子目录。"""

    path: Path
    terms: bool


def _source_files(locales_paths: tuple[str, ...]) -> tuple[_LocaleSource, ...]:
    """扫描语言目录：顶层 *.json 是一般词条，`terms/` 子目录（可再分层）里的 *.json 是术语词条。"""

    result: list[_LocaleSource] = []
    seen: set[Path] = set()

    def add(candidate: Path, is_terms: bool) -> None:
        if not candidate.is_file() or candidate.suffix != ".json":
            return
        resolved = candidate.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        result.append(_LocaleSource(resolved, is_terms))

    for locales_path in locales_paths:
        directory = Path(locales_path)
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.iterdir(), key=lambda path: path.name):
            add(candidate, False)
        terms_directory = directory / TERMS_DIRECTORY_NAME
        if terms_directory.is_dir():
            # 术语目录允许再分子目录（如 terms/platform/zh_cn.json），文件名仍然是语言标签。
            for candidate in sorted(
                terms_directory.rglob("*.json"),
                key=lambda path: path.relative_to(terms_directory).as_posix(),
            ):
                add(candidate, True)
    return tuple(result)


def _source_signature(
    languages: tuple[str, ...], locales_paths: tuple[str, ...], source_files: tuple[_LocaleSource, ...]
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
    for source in source_files:
        source_file = source.path
        stat = source_file.stat()
        digest.update(str(source_file).encode("utf-8"))
        digest.update(f"\0{stat.st_size}\0{stat.st_mtime_ns}\0".encode())
        # 内容哈希保证显式 reload 能识别“大小和 mtime 均未变化”的文件替换。
        digest.update(source_file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _normalize_term_key(key: str) -> str:
    """术语文件里的键默认省略 `term.` 前缀；已带前缀的写法保持不变。"""

    return key if key.startswith(TERM_KEY_PREFIX) else f"{TERM_KEY_PREFIX}{key}"


def _snapshot_cache_base() -> Path:
    configured_base = os.environ.get("AKARI_BOT_I18N_CACHE_DIR")
    if configured_base:
        return Path(configured_base)
    user_suffix = f"_{os.getuid()}" if hasattr(os, "getuid") else ""
    return Path(tempfile.gettempdir()) / f"akari_bot_i18n{user_suffix}"


def _snapshot_root(languages: tuple[str, ...], locales_paths: tuple[str, ...]) -> Path:
    """为旧版组合 API 生成稳定目录，使不同 worker 能找到同一个 manifest。"""

    identity = hashlib.sha256()
    for language in languages:
        identity.update(f"language:{language}\0".encode())
    for locales_path in locales_paths:
        identity.update(f"path:{Path(locales_path).resolve()}\0".encode())
    return _snapshot_cache_base() / identity.hexdigest()[:24]


def _named_snapshot_root(namespace: str) -> Path:
    """命名空间只参与哈希，不能被当作路径片段逃逸缓存根目录。"""

    if not namespace:
        raise ValueError("Locale snapshot namespace cannot be empty")
    identity = hashlib.sha256(f"namespace:{namespace}".encode()).hexdigest()[:24]
    return _snapshot_cache_base() / f"named-{identity}"


def _build_snapshot(
    root: Path,
    languages: tuple[str, ...],
    source_files: tuple[_LocaleSource, ...],
    source_signature: str,
    audit_terms: bool = True,
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
                source TEXT NOT NULL,
                category TEXT NOT NULL
            );
            CREATE TABLE locales (
                locale TEXT PRIMARY KEY,
                position INTEGER NOT NULL
            ) WITHOUT ROWID;
            """
        )

        locale_position = 0
        for source in source_files:
            source_file = source.path
            locale = source_file.stem
            category = _SOURCE_CATEGORY_TERMS if source.terms else _SOURCE_CATEGORY_LOCALE
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
                    (
                        locale,
                        _normalize_term_key(key) if source.terms else key,
                        str(value),
                        str(source_file),
                        category,
                    )
                    for key, value in flatten(raw_data).items()
                    if value is not None
                )
                connection.executemany(
                    "INSERT INTO raw_translations(locale, key, value, source, category) VALUES (?, ?, ?, ?, ?)",
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
            sources = connection.execute(
                "SELECT source, category FROM raw_translations WHERE locale = ? AND key = ? ORDER BY position",
                (locale, key),
            ).fetchall()
            term_sources = [str(source) for source, category in sources if category == _SOURCE_CATEGORY_TERMS]
            if len(term_sources) == 1:
                # 术语目录是术语的归属地：同名的一般词条按“尚未搬迁的旧副本”处理，由术语文件胜出。
                # 仍然报出来，避免同一术语在两处长期各写一份。
                connection.execute(
                    "DELETE FROM raw_translations WHERE locale = ? AND key = ? AND category != ?",
                    (locale, key, _SOURCE_CATEGORY_TERMS),
                )
                if audit_terms:
                    stale = [str(source) for source, category in sources if category != _SOURCE_CATEGORY_TERMS]
                    errors.append(
                        f'{TERM_AUDIT_PREFIX}"{key}" is also defined in {", ".join(stale)}; '
                        f"using the terms directory definition in {term_sources[0]}"
                    )
                continue
            errors.append(f'Conflict detected for key "{key}":')
            errors.extend(str(source) for source, _category in sources)

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
        # 术语校验在快照发布前运行，直接读取构建连接，结果并入 errors（报告但不阻断发布）。
        if audit_terms:
            try:
                errors.extend(
                    _audit_terms_with(
                        _ConnectionLocaleReader(connection),
                        locales=None,
                        base_locale=languages[0] if languages else None,
                        include_unreferenced=False,
                    )
                )
            except Exception as error:
                # 审计只是诊断，任何自身故障都不允许阻断快照发布。
                errors.append(f"{TERM_AUDIT_PREFIX}term audit failed: {error}")
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
            "supported_locales": list(languages),
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
    candidates_with_mtime: list[tuple[float, Path]] = []
    for candidate in root.glob("locales.*.db"):
        try:
            candidates_with_mtime.append((candidate.stat().st_mtime, candidate))
        except (FileNotFoundError, OSError):
            continue
    candidates = [candidate for _, candidate in sorted(candidates_with_mtime, key=lambda item: item[0], reverse=True)]

    # 保留 current 和一个回退版本；更老的文件在每次 build/reader 切换时立即回收。
    protected_names = {candidate.name for candidate in candidates[:SNAPSHOTS_TO_KEEP]}
    protected_names.add(current_db_name)
    for candidate in candidates:
        if candidate.name in protected_names:
            continue
        try:
            candidate.unlink()
        except (FileNotFoundError, PermissionError, OSError):
            # Windows 会拒绝删除其他进程仍打开的数据库，后续 refresh/build 会再次尝试。
            continue


def _ensure_snapshot(
    root: Path,
    languages: tuple[str, ...],
    locales_paths: tuple[str, ...],
    audit_terms: bool = True,
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
                return manifest, _filter_term_audit_errors(errors, audit_terms)

            final_db, new_manifest = _build_snapshot(
                root,
                languages,
                source_files,
                signature_before,
                audit_terms=audit_terms,
            )
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


class _LocaleReader(Protocol):
    """术语审计需要的只读词条访问器。"""

    def get(self, locale: str, key: str) -> str | None: ...

    def keys(self, locale: str) -> tuple[str, ...]: ...

    def locales(self) -> tuple[str, ...]: ...


class _GlobalLocaleReader:
    """把当前进程已连接的快照适配成审计所需的只读访问器。"""

    def get(self, locale: str, key: str) -> str | None:
        return _get_value(locale, key)

    def keys(self, locale: str) -> tuple[str, ...]:
        with _locale_lock:
            if _store is None:
                return ()
            return _store.locale_keys(locale)

    def locales(self) -> tuple[str, ...]:
        with _locale_lock:
            if _store is None:
                return ()
            return _store.available_locales()


class _ConnectionLocaleReader:
    """直接读取尚未激活的构建连接，使审计看到与最终快照完全一致的数据。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, locale: str, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM translations WHERE locale = ? AND key = ?",
            (locale, key),
        ).fetchone()
        return None if row is None else str(row[0])

    def keys(self, locale: str) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT key FROM translations WHERE locale = ? ORDER BY position",
            (locale,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def locales(self) -> tuple[str, ...]:
        rows = self._connection.execute("SELECT locale FROM locales ORDER BY position").fetchall()
        return tuple(str(row[0]) for row in rows)


def _term_references(value: str) -> list[str]:
    """取出值里所有术语占位符引用的术语名。"""

    return [match.group(1) for match in TERM_PLAUSIBLE_PATTERN.finditer(value)]


def _malformed_term_fragments(value: str) -> list[str]:
    """取出值里形如术语占位符但不合法的片段（含未闭合形式）。"""

    return [
        match.group(0)
        for match in TERM_LIKE_PATTERN.finditer(value)
        if TERM_PLAUSIBLE_PATTERN.fullmatch(match.group(0)) is None
    ]


def _audit_terms_with(
    reader: _LocaleReader,
    locales: Sequence[str] | None,
    base_locale: str | None,
    include_unreferenced: bool,
) -> list[str]:
    """对给定访问器做静态术语校验；返回人类可读的问题清单（无问题返回空列表）。"""

    available = reader.locales()
    targets = list(available) if locales is None else [locale for locale in locales if locale in available]
    if base_locale is None or base_locale not in available:
        base_locale = targets[0] if targets else None
    reporting = set(targets)

    issues: list[str] = []
    referenced: set[str] = set()

    # 引用始终从全部已加载语言收集，这样按语言过滤审计时不会把别处的引用误判为“从未被引用”；
    # 问题本身只为请求检查的语言报告。
    for locale in available:
        report = locale in reporting
        for key in reader.keys(locale):
            value = reader.get(locale, key)
            if value is None:
                continue

            # 2. 占位符写法可疑（合法的 {TERM:...} 会被排除，双写 {{...}} 与无关文本不受影响）。
            if report:
                for fragment in _malformed_term_fragments(value):
                    issues.append(
                        f'{TERM_AUDIT_PREFIX}"{key}" ({locale}) contains a malformed term placeholder: {fragment}'
                    )

            if key.startswith("term."):
                # 3. 术语值必须是纯文本；${param} 是正当用法，不在此列。
                if report and (TERM_PLAUSIBLE_PATTERN.search(value) or I18NCODE_PATTERN.search(value)):
                    issues.append(
                        f'{TERM_AUDIT_PREFIX}term value "{key}" ({locale}) must be plain text '
                        "but contains a placeholder"
                    )
                continue

            # 1. 被引用但基础键缺失：缺失占位符的唯一来源，必须在构建期拦住。
            for name in _term_references(value):
                referenced.add(name)
                if report and base_locale is not None and reader.get(base_locale, f"term.{name}") is None:
                    issues.append(
                        f'{TERM_AUDIT_PREFIX}"{key}" ({locale}) references {{TERM:{name}}} '
                        f'but base key "term.{name}" is missing in base locale "{base_locale}"'
                    )

    if base_locale is not None:
        for locale in targets:
            for key in reader.keys(locale):
                match = TERM_KEY_PATTERN.fullmatch(key)
                if match is None or match.group(2) is None:
                    continue
                # 4. 声明了变体但基础语言里没有基础键，说明术语词条不完整。
                if reader.get(base_locale, f"term.{match.group(1)}") is None:
                    issues.append(
                        f'{TERM_AUDIT_PREFIX}term variant "{key}" ({locale}) has no base key '
                        f'"term.{match.group(1)}" in base locale "{base_locale}"'
                    )

        # 5. 信息级：基础键存在但从未被任何值引用（术语也可能由代码直接调用，故默认关闭）。
        if include_unreferenced:
            for key in reader.keys(base_locale):
                match = TERM_KEY_PATTERN.fullmatch(key)
                if match is None or match.group(2) is not None or match.group(1) in referenced:
                    continue
                issues.append(f'{TERM_AUDIT_PREFIX}term "{key}" in base locale "{base_locale}" is never referenced')

    return list(dict.fromkeys(issues))


def audit_terms(locales: Sequence[str] | None = None, *, include_unreferenced: bool = False) -> list[str]:
    """
    扫描词条值中的术语引用并返回问题清单（人类可读），无问题返回空列表。

    :param locales: 要检查的语言；None 表示当前快照中的全部语言。
    :param include_unreferenced: 是否附带「基础术语从未被引用」的信息级条目。（默认为 False）
    :returns: 问题清单。
    """

    with _locale_lock:
        base_source = _store.supported_locales() if _store is not None else tuple(supported_locales)
    base_locale = base_source[0] if base_source else None
    return _audit_terms_with(_GlobalLocaleReader(), locales, base_locale, include_unreferenced)


def _filter_term_audit_errors(errors: list[str], audit_terms_enabled: bool) -> list[str]:
    """复用已发布快照时，让 audit_terms=False 依然不返回术语审计条目。"""

    if audit_terms_enabled:
        return errors
    return [error for error in errors if not error.startswith(TERM_AUDIT_PREFIX)]


def _activate_snapshot(root: Path, manifest: Mapping[str, Any], replace_supported: bool = False) -> str:
    """先建立健康连接，再用一次短临界区替换当前进程的 store。"""

    global _store

    try:
        new_store = _SQLiteLocaleStore(root, manifest)
    except (OSError, RuntimeError, sqlite3.Error):
        # manifest 可能在 ensure 返回后被另一个发布者更新；失败时重读 current 再尝试一次。
        latest_manifest = _read_manifest(root / "current.json")
        if latest_manifest is None or latest_manifest.get("generation") == manifest.get("generation"):
            raise
        new_store = _SQLiteLocaleStore(root, latest_manifest)

    with _locale_lock:
        if replace_supported:
            supported_locales.clear()
            supported_locales.extend(new_store.supported_locales())
        old_store = _store
        _store = new_store
        if old_store is not None:
            old_store.close()
    return new_store.generation


def build_locale_snapshot(
    lang_list: list[str],
    locales_path: list[str],
    namespace: str,
    *,
    audit_terms: bool = True,
) -> list[str]:
    """构建并发布命名快照，但不连接或修改当前进程的翻译状态。

    :param audit_terms: 是否在构建期校验术语引用并并入返回的 errors。（默认为 True）
    """

    languages = tuple(lang_list)
    locales_paths = tuple(locales_path)
    root = _named_snapshot_root(namespace)
    with _load_lock:
        _, errors = _ensure_snapshot(root, languages, locales_paths, audit_terms=audit_terms)
    return errors


def connect_locale_snapshot(namespace: str) -> str:
    """只连接已发布的命名快照；reader 不需要访问 JSON 源目录。"""

    global _connected_namespace

    root = _named_snapshot_root(namespace)
    with _load_lock:
        # 若 builder 正在首次发布，等待其释放锁后再读取 manifest。
        with _InterProcessFileLock(root / ".snapshot-publish.lock"):
            manifest = _read_manifest(root / "current.json")
        if manifest is None:
            raise FileNotFoundError(f'Locale snapshot namespace "{namespace}" has not been built')
        generation = _activate_snapshot(root, manifest, replace_supported=True)
        with _locale_lock:
            _connected_namespace = namespace
        return generation


def load_locale_file(
    lang_list: list[str],
    locales_path: list[str] | None = None,
    reload: bool = False,
    *,
    audit_terms: bool = True,
) -> list[str]:
    global _connected_namespace

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
        manifest, errors = _ensure_snapshot(root, languages, locales_paths, audit_terms=audit_terms)
        # 构建在锁外完成，最后仅用一次指针/连接交换提交到当前进程。
        _activate_snapshot(root, manifest)
        with _locale_lock:
            _connected_namespace = None

        return errors


def get_available_locales() -> list[str]:
    with _locale_lock:
        if _store is None:
            return []
        return list(_store.available_locales())


def _split_strftime_format(fmt: str) -> list[tuple[bool, str]]:
    """
    将 strftime 格式串拆分为 (是否为指令, 文本) 序列。

    :param fmt: strftime 格式串。
    :returns: 拆分后的序列。
    """
    tokens: list[tuple[bool, str]] = []
    literal: list[str] = []
    index = 0
    length = len(fmt)
    while index < length:
        # 末尾孤立的 '%' 在部分平台上会直接报错，因此按字面文本处理
        if fmt[index] != "%" or index + 1 == length:
            literal.append(fmt[index])
            index += 1
            continue
        end = index + 1
        # 跳过标志位与字段宽度
        while end < length and (fmt[end] in "-_0^#" or fmt[end] in "0123456789"):
            end += 1
        if end == length:
            literal.append(fmt[index:])
            break
        if literal:
            tokens.append((False, "".join(literal)))
            literal.clear()
        tokens.append((True, fmt[index : end + 1]))
        index = end + 1
    if literal:
        tokens.append((False, "".join(literal)))
    return tokens


def safe_strftime(dt: Any, fmt: str) -> str:
    """
    使用可能包含非 ASCII 字符的格式串格式化时间对象。

    Windows 上 datetime.strftime() 会先把整个格式串按进程 ANSI 代码页编码，
    因此 `%Y년 %m월 %d일` 这类格式串会抛出 UnicodeEncodeError（启用 UTF-8 模式也无法避免）。
    本函数只把 ASCII 的 strftime 指令交给 strftime()，其余字面文本原样保留，
    从而在所有平台上得到相同结果。字段宽度等修饰符的支持情况仍与平台一致。

    :param dt: 具有 strftime() 的日期或时间对象。
    :param fmt: strftime 格式串，允许包含非 ASCII 字面文本。
    :returns: 格式化后的字符串。
    """
    if fmt.isascii():
        return dt.strftime(fmt)
    return "".join(dt.strftime(token) if is_directive else token for is_directive, token in _split_strftime_format(fmt))


def _validate_term_token(token: str) -> str:
    """候选链 token 必须能直接拼进 `term.<name>.<candidate>`：违规直接抛错，不静默容错。"""

    if not isinstance(token, str) or not token:
        raise ValueError("Term candidate tokens must be non-empty strings")
    if any(char in _INVALID_TERM_TOKEN_CHARS or char.isspace() for char in token):
        raise ValueError(f'Invalid term candidate token: "{token}"')
    return token


def derive_term_candidates(tokens: Sequence[str], *, shared: Sequence[str] = ()) -> tuple[str, ...]:
    """
    把「层级作用域 + 共享 token」展开为有序候选链（最具体在前）。

    :param tokens: 会话作用域 token，最外层在前（如 `("discord", "dm", "channel")`）。
    :param shared: 追加在层级前缀之后、最外层 token 之前的共享 token（如 `("private",)`）。
    :returns: 去重后的候选链，供 `Locale(term_candidates=...)` 使用。
    """

    validated = [_validate_term_token(token) for token in tokens]
    if not validated:
        raise ValueError("Term candidate tokens cannot be empty")
    shared_tokens = [_validate_term_token(token) for token in shared]

    candidates = [".".join(validated[:length]) for length in range(len(validated), 1, -1)]
    candidates.extend(shared_tokens)
    candidates.append(validated[0])

    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return tuple(result)


def _format_term_placeholder(name: str, params: Mapping[str, object] | None = None) -> str:
    """还原字面量占位符文本，用于术语缺失时的原样输出与诊断。"""

    suffix = "".join(f",{key}={value}" for key, value in (params or {}).items())
    return f"{TERM_PLACEHOLDER_PREFIX}{name}{suffix}}}"


def _coerce_str_sequence(value: Any, field: str) -> list[str] | None:
    """容忍缺字段/未知字段地还原状态里的字符串序列。"""

    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return [str(item) for item in value]
    raise ValueError(f'Locale state field "{field}" must be a sequence of strings or None')


class Locale:
    """创建一个本地化对象。"""

    def __init__(
        self,
        locale: str,
        fallback_lng: list[str] | None = None,
        *,
        term_candidates: Sequence[str] | None = None,
    ) -> None:
        """
        :param locale: 语言标签。
        :param fallback_lng: 回退语言链；为 None 时按当前快照的已声明语言自动推导。
        :param term_candidates: 有序术语候选链，最具体在前。None 表示未绑定会话作用域，
            `()` 表示已绑定作用域但没有候选；两者都回落到基础术语 `term.<name>`。
        """

        self.locale = locale
        if fallback_lng is None:
            with _locale_lock:
                fallback_source = _store.supported_locales() if _store is not None else tuple(supported_locales)
            fallback_lng = [language for language in fallback_source if language != locale]
        self.fallback_lng = fallback_lng
        self._term_candidates: tuple[str, ...] | None = (
            None if term_candidates is None else tuple(str(candidate) for candidate in term_candidates)
        )

    @property
    def data(self) -> Mapping[str, str]:
        return _LocaleDataView(self.locale)

    @property
    def term_candidates(self) -> tuple[str, ...] | None:
        """只读术语候选链；None 表示该对象未绑定会话作用域。"""

        return self._term_candidates

    @property
    def term_scoped(self) -> bool:
        """只读：是否已绑定会话作用域（`term_candidates is not None`）。"""

        return self._term_candidates is not None

    def __getitem__(self, key: str) -> str | None:
        return _get_value(self.locale, key)

    def __contains__(self, key: str) -> bool:
        return _get_value(self.locale, key) is not None

    def with_locale(self, locale: str) -> "Locale":
        """换语言渲染，保留术语候选链与回退链（不要用 `Locale(tag)` 重新构造）。

        回退链里指向新语言自身的项没有意义（新语言查不到时会原地回退），
        因此把它换成派生前的语言，保证换语言渲染仍能回退到会话语言。
        """

        fallback_lng = list(self.fallback_lng)
        if locale in fallback_lng:
            fallback_lng = [language for language in fallback_lng if language != locale]
            if self.locale != locale and self.locale not in fallback_lng:
                fallback_lng.append(self.locale)
        return type(self)(locale, fallback_lng, term_candidates=self._term_candidates)

    def with_term_candidates(self, candidates: Sequence[str] | None) -> "Locale":
        """换术语候选链，保留语言与其他设置；传 None 表示解绑作用域。"""

        return type(self)(self.locale, list(self.fallback_lng), term_candidates=candidates)

    def to_state(self) -> dict[str, Any]:
        """导出可跨进程/RPC 传递的状态；字段只增不改。"""

        return {
            "locale": self.locale,
            "fallback_lng": list(self.fallback_lng),
            "term_candidates": None if self._term_candidates is None else list(self._term_candidates),
        }

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> "Locale":
        """从 `to_state()` 的结果还原；容忍缺字段、未知字段与旧版 `{"locale": ...}` 表示。"""

        locale = state.get("locale")
        if not isinstance(locale, str) or not locale:
            raise ValueError('Locale state must contain a non-empty "locale" string')
        fallback_lng = _coerce_str_sequence(state.get("fallback_lng"), "fallback_lng")
        term_candidates = _coerce_str_sequence(state.get("term_candidates"), "term_candidates")
        return cls(locale, fallback_lng, term_candidates=term_candidates)

    def _lookup_term(self, name: str) -> tuple[str, str] | None:
        """按候选链查找术语，返回 `(命中的键, 术语文本)`；未命中返回 None。

        变体只在本语言中查找（不进入回退链），基础键走正常回退语义。
        """

        with _locale_lock:
            if self._term_candidates is not None:
                for candidate in self._term_candidates:
                    key = f"term.{name}.{candidate}"
                    value = _get_value(self.locale, key)
                    if value is not None:
                        return key, value

            base_key = f"term.{name}"
            for language in (self.locale, *self.fallback_lng):
                value = _get_value(language, base_key)
                if value is not None:
                    return base_key, value
        return None

    def term_key(self, name: str) -> str | None:
        """返回术语命中的词条键（诊断用）；术语不存在时返回 None。"""

        found = self._lookup_term(name)
        return None if found is None else found[0]

    def term(self, name: str | None = None, /, **params: object) -> str:
        """
        解析单个术语；参数只用于向术语文本插值。

        :param name: 术语名（`term.<name>` 里的 `<name>`）。声明为仅位置参数，
            这样 `name` 也能作为占位符参数名（`{TERM:target,name=群聊}`）使用；
            `term(name="target")` 这种关键字写法同样被接受。
        :returns: 术语文本；术语不存在时返回原样占位符文本 `{TERM:name,...}`。
        """

        if name is None:
            name = params.pop("name", None)
            if name is None:
                raise TypeError("term() missing 1 required argument: 'name'")

        found = self._lookup_term(name)
        if found is None:
            return _format_term_placeholder(name, params)
        return self._render_term(found[1], params)

    @staticmethod
    def _render_term(text: str, params: Mapping[str, object]) -> str:
        """把占位符自带的参数插值进术语文本；缺参数时 `${...}` 原样保留，不报错。"""

        return Template(text).safe_substitute(**params) if params else text

    def _resolve_terms(self, text: str) -> str:
        """单趟替换文本中的 `{TERM:...}` 占位符；未命中的占位符原样保留（含参数）。"""

        # 快速路径：绝大多数文案不含占位符，避免正则开销。
        if TERM_PLACEHOLDER_PREFIX not in text:
            return text

        def replace_term(match: re.Match[str]) -> str:
            found = self._lookup_term(match.group(1))
            if found is None:
                # 术语不存在时，原样保留占位符
                return match.group(0)

            params_str = match.group(2)
            params = dict(TERM_PARAM_PATTERN.findall(params_str)) if params_str else {}
      
            # style 是保留参数，不传给术语插值逻辑
            style = params.pop("style", "").strip().lower()

            # 先处理 count 等其他参数，再转换大小写
            value = self._render_term(found[1], params)

            style_aliases = {
                "t": "title",
                "title": "title",
                "u": "upper",
                "upper": "upper",
                "l": "lower",
                "lower": "lower",
                "c": "capitalize",
                "capitalize": "capitalize",
                "s": "swapcase",
                "swapcase": "swapcase",
            }

            style = style_aliases.get(style)

            if style == "title":
                value = value.title()
            elif style == "upper":
                value = value.upper()
            elif style == "lower":
                value = value.lower()
            elif style == "capitalize":
                value = value.capitalize()
            elif style == "swapcase":
                value = value.swapcase()
            return value

        return TERM_PATTERN.sub(replace_term, text)

    @staticmethod
    def reload() -> list[str]:
        with _locale_lock:
            namespace = _connected_namespace
        if namespace is not None:
            # 分离模式下 reload 只强制重连最新 manifest，不允许 reader 回头读取 JSON。
            connect_locale_snapshot(namespace)
            manifest = _read_manifest(_named_snapshot_root(namespace) / "current.json")
            manifest_errors = manifest.get("errors", []) if manifest is not None else []
            return [str(error) for error in manifest_errors] if isinstance(manifest_errors, list) else []
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
                    return self._resolve_terms(str(translated))
                if fallback and "_fallback_locale" in key:
                    return self._resolve_terms(str(key["_fallback_locale"]))
                return self._resolve_terms(
                    str(key)
                    + self.t(
                        "error.i18n.fallback",
                        locale_failed_prompt=False,
                        _fallback_locale=self.locale,
                    )
                )

            localized = self.get_string_with_fallback(key, fallback, locale_failed_prompt)
            # 先做调用方 kwargs 插值，再解析术语：kwarg 值里的占位符也会生效，
            # 而术语占位符自带的参数只作用于术语文本。
            return self._resolve_terms(Template(localized).safe_substitute(**kwargs))

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

            # 先解析输入文本里的术语；术语文本按纯文本插入，其中的占位符不会被二次扫描
            # （构建期由 audit_terms() 报错），因此术语替换不计入 {I18N:} 的递归深度。
            text = self._resolve_terms(text)

            previous_text = None
            depth = 0
            while previous_text != text and depth < MAX_I18NCODE_DEPTH:
                previous_text = text
                text = I18NCODE_PATTERN.sub(match_i18ncode, text)
                depth += 1
            return text

    def strftime(
        self,
        dt: Any,
        key: str,
        fallback: bool = True,
        locale_failed_prompt: bool = True,
        **kwargs: Any,
    ) -> str:
        """
        使用本地化格式串格式化时间对象。

        :param dt: 具有 strftime() 的日期或时间对象。
        :param key: 本地化键名，其值为 strftime 格式串。
        :param fallback: 是否使用 fallback。（默认为 True）
        :param locale_failed_prompt: 是否添加本地化失败提示。（默认为 True）
        :returns: 格式化后的字符串。
        """
        # 格式串不参与术语解析（占位符只用于文案值）。
        localized = self.get_string_with_fallback(key, fallback, locale_failed_prompt)
        return safe_strftime(dt, Template(localized).safe_substitute(**kwargs))


def _reset_state_for_testing() -> None:
    global _connected_namespace, _store
    with _locale_lock:
        if _store is not None:
            _store.close()
        _store = None
        _connected_namespace = None
        supported_locales.clear()
        _lang_list.clear()
        _locales_path.clear()


__all__ = [
    "Locale",
    "audit_terms",
    "build_locale_snapshot",
    "connect_locale_snapshot",
    "derive_term_candidates",
    "get_available_locales",
    "load_locale_file",
    "safe_strftime",
]
