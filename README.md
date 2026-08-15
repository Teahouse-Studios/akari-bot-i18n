## AkariBot-I18N

小可使用的本地化组件。

### 运行时存储

`load_locale_file()` 会把 JSON 语言文件编译为版本化 SQLite 快照。多个进程使用同一组语言目录时会共享快照文件，并在其他进程发布新版本后自动刷新只读连接。

快照默认保存在当前用户的临时目录。可通过 `AKARI_BOT_I18N_CACHE_DIR` 指定本机缓存目录；所有 worker 必须使用相同配置。`Locale.data` 和模块级 `locale_data` 现在是只读映射。

加载和使用也可以完全分离。只有 loader 需要访问 JSON：

```python
build_locale_snapshot(["zh_cn", "en_us"], locale_paths, "akari-bot")
```

其他 worker 只连接同一命名空间，不需要调用 `load_locale_file()` 或访问语言目录：

```python
connect_locale_snapshot("akari-bot")
```

命名空间连接会自动跟随 loader 后续发布的新 generation。reader 调用 `Locale.reload()` 时也只会强制重连 manifest，不会读取 JSON。原有 `load_locale_file()` 保留为“构建并连接”的兼容入口。
