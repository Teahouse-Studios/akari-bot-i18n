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

每个 namespace 默认只保留当前和上一个 generation。更老的数据库会在 build 或 reader 完成版本切换时清理；Windows 上仍被其他进程打开的文件会在后续操作中重试删除。

### 术语占位符

同一句文案里的名词可能需要按平台与会话类型换称呼（QQ 群聊 → 「群聊」、Discord 频道 → 「频道」、私聊 → 「私聊」）。术语与变体都是普通词条，命名空间固定为 `term.`：

```json
{
  "term.target": "场景",
  "term.target.private": "私聊",
  "term.target.discord": "频道",
  "term.target.qq.group": "群聊",
  "core.help.bind.channel": "查看当前{TERM:target}的消息通道：${channel} 号"
}
```

术语词条推荐放在每个语言目录下的 `terms/` 子目录里，与一般语义词条分开维护（见下一节）。

文案作者只写 `{TERM:<name>}`，不接触变体键名。占位符可携带参数，参数只用于向术语文本插值（`${...}` 缺参数时原样保留），不参与变体选择：

```json
{ "weekly.message.summary": "{TERM:target,count=${count}} 本周共有 ${count} 条动态。" }
```

标签必须大写、名称必须小写（`{term:x}`、`{TERM: Name}` 一律按普通文本保留）；`{TERM:...}` 与 `{I18N:...}` 互不干扰。不提供转义序列，`{{TERM:x}}` 不会被解析。

术语变体由调用方提供的候选链决定，组件不感知任何平台概念：

```python
from akari_bot_i18n.i18n import Locale, derive_term_candidates

candidates = derive_term_candidates(("discord", "dm", "channel"), shared=("private",))
# -> ("discord.dm.channel", "discord.dm", "private", "discord")

locale = Locale("zh_cn", term_candidates=candidates)
locale.t("core.help.bind.channel", channel=3)  # -> 查看当前私聊的消息通道：3 号
```

语义是固定的：

- 依次尝试 `term.<name>.<candidate>`，全部未命中则取基础键 `term.<name>`。
- **变体只在 `Locale` 自身的语言中查找**，不进入 `fallback_lng`，避免把中文名词嵌进英文句子；基础键走正常的本语言 → 回退链语义。
- `term_candidates=None`（默认）表示未绑定会话作用域，`()` 表示已绑定但没有候选；两者都解析为基础术语。可用只读的 `term_scoped` 断言会话渲染用的对象确实绑定了作用域。
- 术语在所有语言中都不存在时**原样保留占位符**（含参数），不附加回退提示，便于直接看到缺失的术语名。

`Locale` 的新增接口：

| 接口 | 说明 |
| --- | --- |
| `Locale(locale, fallback_lng=None, *, term_candidates=None)` | 构造时绑定候选链 |
| `with_locale(tag)` / `with_term_candidates(cands)` | 派生副本，保留作用域与其他设置；换语言时回退链里的自指项会换成派生前的语言 |
| `term(name, **params)` / `term_key(name)` | 解析单个术语 / 返回命中的键（诊断）；`term("target")` 与 `term(name="target")` 都可用，且 `name=` 仍可作为占位符参数名 |
| `term_candidates` / `term_scoped` | 只读候选链与作用域标记 |
| `to_state()` / `Locale.from_state(state)` | 跨进程/RPC 传递状态；字段只增不改，容忍缺字段与未知字段 |

术语在 `t()`（含 dict 分支）与 `t_str()` 中解析，因此所有渲染路径自动生效；`strftime` 的格式串、`__getitem__` / `__contains__` / `data` 不解析术语。

### 术语目录（terms/）

术语是独立的一类词条，默认与一般语义词条混在同一个语言文件里。要分开维护，在每个语言目录下新建 `terms/` 子目录，按语言放文件即可：

```
core/locales/
├── zh_cn.json                 # 一般语义词条
└── terms/
    └── zh_cn.json             # 术语词条
bots/discord/locales/
├── zh_cn.json
└── terms/
    └── zh_cn.json             # 该平台独有的术语
```

`terms/` 里的键默认**省略 `term.` 前缀**，写成术语名本身；已经带前缀的写法同样接受，方便把术语从一般词条文件里整段搬过来。嵌套对象会先扁平化，键里的点号继续表示层级：

```json
{
  "target": "场景",
  "target.private": "私聊",
  "target": {"discord": "频道"},
  "term.target.qq.group": "群聊"
}
```

以上等价于 `term.target`、`term.target.private`、`term.target.discord`、`term.target.qq.group`。`terms/` 下可以再分子目录（如 `terms/platform/zh_cn.json`）用于分组，文件名始终是语言标签。

约定：

- 没有 `terms/` 目录时行为与以前完全一致；一般语言文件里继续写 `term.*` 也照常生效。
- 术语目录是术语的归属地：同一个键同时出现在一般文件和 `terms/` 里时，**以 `terms/` 为准**，并作为术语审计条目报出（提示删除旧副本），因此“先复制、后删除”的迁移过程不会让术语失效。
- 两个 `terms/` 文件（例如两个平台适配器）声明同一个键仍然是冲突：该键的所有副本都会被丢弃并报错，与既有规则一致。
- 术语文件参与内容签名，改动会触发快照重建；解析失败与一般文件一样报 `Failed to load`。
- 术语仍然落在同一个 `term.` 词条命名空间，`Locale` 的查表与候选链逻辑不变。

### 术语校验

`build_locale_snapshot()` 与 `load_locale_file()` 默认开启术语校验（`audit_terms=False` 可关闭），把问题追加到既有的 `errors` 列表：被引用但基础键缺失、占位符写法可疑、术语值含占位符、有变体无基础键。与跨文件键冲突同级别：**报告但不阻断快照发布**，`has_load_failures` 不置位。也可单独调用：

```python
from akari_bot_i18n.i18n import audit_terms

audit_terms()                              # 当前快照的全部语言
audit_terms(["zh_cn"])                     # 指定语言
audit_terms(include_unreferenced=True)     # 附带「基础术语从未被引用」的信息级条目
```

