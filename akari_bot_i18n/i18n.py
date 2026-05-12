import html
import re
import traceback
from pathlib import Path
from string import Template
from threading import RLock
from typing import Any

import orjson

from .utils import flatten_dict as flatten

# Load all locale files into memory

# We might change this behavior in the future and read them on demand as
# locale files get too large

MAX_I18NCODE_DEPTH = 10

# 全局状态管理
supported_locales: list[str] = []
_lang_list: list[str] = []
_locales_path: list[str] = []
_locale_lock = RLock()
locale_data: dict[str, dict[str, str]] = {}


def load_locale_file(
    lang_list: list[str],
    locales_path: list[str] | None = None,
    reload: bool = False,
) -> list[str]:
    with _locale_lock:
        for lang in lang_list:
            if lang not in supported_locales:
                supported_locales.append(lang)

        if not reload:
            for lang in lang_list:
                if lang not in _lang_list:
                    _lang_list.append(lang)

            if locales_path:
                for p in locales_path:
                    if p not in _locales_path:
                        _locales_path.append(p)

        err_prompt: list[str] = []

        # 用于记录 key 来源
        key_source_map: dict[str, dict[str, list[str]]] = {}

        # staging 区
        new_locale_data: dict[str, dict[str, str]] = {}

        if locales_path:
            for modules_locales_file in locales_path:
                dir_path = Path(modules_locales_file)

                if not dir_path.is_dir():
                    continue

                for lang_file_path in dir_path.iterdir():
                    if (
                        not lang_file_path.is_file()
                        or not lang_file_path.name.endswith(".json")
                    ):
                        continue

                    lang_key = lang_file_path.stem

                    try:
                        with open(lang_file_path, "rb") as f:
                            raw_data = orjson.loads(f.read())

                        flat_data = {
                            k: str(v)
                            for k, v in flatten(raw_data).items()
                            if v is not None
                        }

                        if lang_key not in new_locale_data:
                            new_locale_data[lang_key] = {}

                        if lang_key not in key_source_map:
                            key_source_map[lang_key] = {}

                        for k, v in flat_data.items():

                            # 记录 key 来源
                            if k not in key_source_map[lang_key]:
                                key_source_map[lang_key][k] = []

                            key_source_map[lang_key][k].append(
                                str(lang_file_path)
                            )

                            # 写入 staging 数据
                            new_locale_data[lang_key][k] = v

                    except Exception as e:
                        traceback.print_exc()
                        err_prompt.append(
                            f"Failed to load {lang_file_path}: {e}"
                        )

        # 冲突检测
        for lang_key, keys in key_source_map.items():
            conflicted_keys = [
                k
                for k, sources in keys.items()
                if len(sources) > 1
            ]

            for k in conflicted_keys:
                sources = keys[k]

                # 删除冲突 key
                if (
                    lang_key in new_locale_data
                    and k in new_locale_data[lang_key]
                ):
                    del new_locale_data[lang_key][k]

                err_prompt.append(
                    f'Conflict detected for key "{k}":'
                )

                err_prompt.extend(sources)

        # 原子替换
        locale_data.clear()
        locale_data.update(new_locale_data)

        return err_prompt

def get_available_locales() -> list[str]:
    return list(locale_data.keys())


class Locale:
    """
    创建一个本地化对象。
    """
    def __init__(self, locale: str, fallback_lng: list[str] | None = None):
        self.locale = locale
        if fallback_lng is None:
            fallback_lng = [l for l in supported_locales if l != locale]
        self.fallback_lng = fallback_lng

    @property
    def data(self) -> dict[str, str]:
        return locale_data.get(self.locale, {})

    def __getitem__(self, key: str) -> str | None:
        return self.data.get(key)

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def reload(self) -> list[str]:
        return load_locale_file(_lang_list, _locales_path, reload=True)

    def get_string_with_fallback(
        self,
        key: str,
        fallback: bool = True,
        locale_failed_prompt: bool = True,
    ) -> str:
        # 1. 当前语言
        val = self.data.get(key)

        if val is not None:
            return val

        # 2. fallback
        if fallback:
            for lng in self.fallback_lng:
                fallback_dict = locale_data.get(lng, {})

                val = fallback_dict.get(key)

                if val is not None:
                    return val

        # 3. fallback failed
        if locale_failed_prompt:
            if key == "error.i18n.fallback":
                return f"{{I18N:{key}}}"

            return (
                f"{{I18N:{key}}}"
                + self.t(
                    "error.i18n.fallback",
                    fallback=fallback,
                    locale_failed_prompt=False,
                )
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
                if ft := key.get(self.locale):
                    return str(ft)
                if fallback and "_fallback_locale" in key:
                    return str(key["_fallback_locale"])
                return str(key) + self.t("error.i18n.fallback", locale_failed_prompt=False, _fallback_locale=self.locale)

            localized = self.get_string_with_fallback(key, fallback, locale_failed_prompt)
            return Template(localized).safe_substitute(**kwargs)

    def t_str(self, text: str, fallback: bool = True, locale_failed_prompt: bool = False, **kwargs: dict[str, Any]) -> str:
        """
        替换字符串中的本地化键名。

        :param text: 字符串。
        :param fallback: 是否使用 fallback。（默认为 True）
        :param locale_failed_prompt: 是否添加本地化失败提示。（默认为False）
        :returns: 本地化后的字符串。
        """
        with _locale_lock:
            def match_i18ncode(match):
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
                    for k, v in param_pairs:
                        local_kwargs[html.unescape(k)] = html.unescape(v)
                all_kwargs = {**kwargs, **local_kwargs}
                t_value = self.t(
                    key,
                    fallback=fallback,
                    locale_failed_prompt=locale_failed_prompt,
                    **all_kwargs
                )
                return t_value if isinstance(t_value, str) else full

            prev_text = None
            depth = 0
            while prev_text != text and depth < MAX_I18NCODE_DEPTH:
                prev_text = text
                text = re.sub(r"\{I18N:([^\s,{}]+)(?:,([^\{\}]*))?\}", match_i18ncode, text)
                depth += 1
            return text


__all__ = ["Locale", "load_locale_file", "get_available_locales"]