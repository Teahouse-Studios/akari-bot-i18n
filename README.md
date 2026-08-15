## AkariBot-I18N

小可使用的本地化组件。

### 运行时存储

`load_locale_file()` 会把 JSON 语言文件编译为版本化 SQLite 快照。多个进程使用同一组语言目录时会共享快照文件，并在其他进程发布新版本后自动刷新只读连接。

快照默认保存在当前用户的临时目录。可通过 `AKARI_BOT_I18N_CACHE_DIR` 指定本机缓存目录；所有 worker 必须使用相同配置。`Locale.data` 和模块级 `locale_data` 现在是只读映射，翻译内容应通过 JSON 文件和 `Locale.reload()` 更新。
