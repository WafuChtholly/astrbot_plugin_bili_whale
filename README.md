# 🐋 大肥鱼B站鲸鱼 · astrbot_plugin_bili_whale

> 让你的 AstrBot 真正「看懂」一条 B 站视频，并且能替你回私信、盯评论区、自己刷 B 站。

这是一个 AstrBot 插件：把一条 B 站视频的**元信息、官方字幕、弹幕热区、热评、抽帧画面**
整理成结构化文本交给大模型；也可以在你授权后，用你的 B 站账号自动回复私信与评论、
主动点赞/投币/收藏/关注/发动态。读路径移植自开源项目
[dsh-bili-whale](https://github.com/Alterssss/dsh-bili-whale)，全部用 Python 重新实现。

- 作者：`dafeiyu-tools`
- 版本：`0.3.30`
- 支持平台：`aiocqhttp`（QQ，配合 NapCat / Lagrange 等）
- 许可：MIT

---

## ✨ 功能特性

### 只读（默认开启，不需要登录也能用）

- **视频速读**：标题、UP 主、播放/点赞/投币/收藏、简介、分P、时长，一次给全
- **官方字幕**：优先 AI 字幕，带时间戳输出，适合总结与二次创作
- **弹幕热区**：按时间分桶统计，自动剔除刷屏弹幕，找出最热闹的几段并附样例
- **热评/新评**：按热度或时间取评论，含点赞数与楼层
- **抽帧看画面**：ffmpeg 直连只拉需要的片段（不落整片），最多 16 帧，交给视觉模型
- **搜索 / UP 主页 / 关注动态 / 收藏夹 / 观看历史 / 稍后再看 / 相关推荐**
- 内置 **WBI 签名、请求间隔限速、412 熔断、读写配额**，并支持「熔断后自动降级」

### 写操作（全部默认关闭，需登录 Cookie 且包含 `bili_jct`）

- **点赞 / 投币（1~2 枚）/ 收藏 / 分享 / 一键三连**
- **关注 / 取关 UP 主**
- **发纯文本动态**（1~2000 字，公开可见）
- **私信监听与自动回复**：轮询与指定 UID 的私信，按所选模型与人设回复
- **评论区监听与自动回复**：监听「回复我的」和/或「@我的」，逐条报告到主人会话
- **主动互动**：按间隔自己拉热门 / 排行榜 / 关注动态，自主判断是否点赞、投币、收藏或留言

### 「她知道自己是谁」

插件会把**当前登录的 B 站账号（昵称/UID）** 与**后台最近 5 件做过的事**注入系统提示词，
被问到「你给谁点过赞」「在谁那儿留了言」时模型能直接答上来，不会说成「插件替我发的」。

### 安全阀

- `/bili_stop` 一键全线暂停后台动作，`/bili_resume` 恢复；暂停状态重启后仍有效
- 同一对象连续自动回复超过 `dm_max_rounds` 轮自动暂停该对象
- 每日私信 / 评论 / 主动评论各自独立配额，可设 0 表示不限制
- 后台登录失效、缺 `bili_jct`、被配额熔断时，会在下一轮系统提示里告知模型

---

## 📦 目录结构

```
astrbot_plugin_bili_whale/
├── main.py              # 插件主体：命令、LLM 工具、后台循环
├── bili_client.py       # B 站客户端：WBI 签名、限速、字幕/弹幕/评论/搜索/写操作
├── metadata.yaml        # 插件元数据（插件市场读取）
├── _conf_schema.json    # WebUI 配置页 schema
├── requirements.txt     # 依赖
├── README.md
├── CHANGELOG.md
├── LICENSE
└── .gitignore
```

运行时数据（Cookie、缓存、暂停状态等）写到 AstrBot 的
`data/plugin_data/astrbot_plugin_bili_whale/` 目录，不会污染插件目录。

---

## 🚀 安装

### 方式一：AstrBot 插件市场（推荐）

在 AstrBot WebUI → 插件市场搜索「大肥鱼B站鲸鱼」或 `astrbot_plugin_bili_whale`，点击安装。

### 方式二：手动安装

1. 把整个 `astrbot_plugin_bili_whale` 目录放进 AstrBot 的 `data/plugins/`
2. 在 WebUI → 插件管理里重载，或重启 AstrBot
3. 依赖会自动安装；若失败请在 AstrBot 环境中执行：

```bash
pip install -r data/plugins/astrbot_plugin_bili_whale/requirements.txt
```

### 系统依赖

抽帧依赖 `ffmpeg`。插件会自动使用 `imageio-ffmpeg` 自带的二进制，通常无需单独安装；
若你的环境缺少它，请自行安装 ffmpeg 并确保在 PATH 中。

---

## ⚙️ 快速配置

最小可用配置只需要两项：

| 配置项 | 说明 |
|---|---|
| `owner_scope` | 主人 QQ 号（如 `123456789`），或完整 UMO（私聊发 `/sid` 获取）。该会话调用工具视为主人，留空则不限制调用者 |
| `cookie` | B 站 Cookie，可只填 `SESSDATA=...`；不填也能读，但更容易被 412 限流 |

**不想手抠 Cookie？** 只填 `owner_scope`，然后在主人私聊里发 `/bili_login`，
插件会发二维码，手机 B 站扫码确认后 Cookie 自动写回插件配置（失败则落到
`credentials.json`）。登录成功后返回的 UID 可以填进 `owner_mid`，用于脱敏和私信对象。

想开启写操作与后台监听，请继续阅读下方配置表。

---

## 💬 指令

| 指令 | 别名 | 权限 | 说明 |
|---|---|---|---|
| `/bili_login` | `/bili扫码` | 主人私聊 | 生成登录二维码，扫码后把 Cookie 写回配置，3 分钟内有效 |
| `/bili_stop` | `/bili暂停` | 主人 | 全线暂停后台私信、评论回复与主动互动 |
| `/bili_resume` | `/bili恢复` | 主人 | 恢复后台活动，并清零私信连续轮数与对象单独暂停 |

> 主人判定：先比对会话 UMO，再比对发送者 QQ 号；`owner_scope` 留空时任何人都可使用指令。

---

## 🛠️ LLM 工具

插件注册了 13 个函数工具，模型会在聊天中按需调用（无需用户记指令）。

### 只读

| 工具 | 参数 | 说明 |
|---|---|---|
| `df_bili_debrief` | `bvid, page=1, with_frames=True` | 一次看懂视频：元信息 / 字幕 / 弹幕热区 / 热评 / 抽帧；是否抽帧由 `vision_enabled` 决定 |
| `df_bili_subtitles` | `bvid, page=1` | 只取官方字幕，带时间戳；无字幕则直说 |
| `df_bili_comments` | `bvid, sort="hot", limit=8` | 热评或最新评论，`sort` 取 `hot`/`new`，`limit` 1~20 |
| `df_bili_search` | `keyword, limit=8` | 搜索视频，返回 BV 号 / 链接 / 标题 / UP 主 / 播放量 / 时长 |
| `df_bili_space` | `link="", videos=10, dynamics=8` | UP 主主页：昵称、签名、粉丝数 + 最新投稿与动态；接受主页链接或 UID |
| `df_bili_dynamics` | `count=8` | 关注动态时间线；接口被风控时自动改翻关注列表主页，全站不可用时给缓存并注明时间 |
| `df_bili_favorites` | `folder="", page=1` | 列出收藏夹，或翻某个收藏夹（需登录 Cookie） |
| `df_bili_history` | `kind="history", limit=10` | `history` 观看历史 / `later` 稍后再看（需登录 Cookie） |
| `df_bili_related` | `bvid, limit=8` | 视频页相关推荐 |
| `df_bili_quota` | `action="status"` | 读取请求配额与熔断状态 |

### 写操作（受开关约束）

| 工具 | 参数 | 前置开关 |
|---|---|---|
| `df_bili_react` | `bvid, action="like", coins=1` | `react_enabled` + 登录 Cookie（`like`/`coin`/`fav`/`share`/`triple`） |
| `df_bili_follow` | `bvid="", mid="", action="follow"` | `react_enabled` + 登录 Cookie（`follow`/`unfollow`） |
| `df_bili_publish_dynamic` | `text` | `publish_enabled` + 登录 Cookie（1~2000 字纯文本） |

> `with_frames` 参数保留仅为兼容旧调用，实际抽帧由 `vision_enabled` 控制。

---

## 🔧 配置项

配置页由 `_conf_schema.json` 生成，全部字段如下（数值项超出范围会被自动夹取）。

### 基础

| 键 | 类型 | 默认 | 范围 | 说明 |
|---|---|---|---|---|
| `enabled` | bool | `true` | — | 总开关，关闭后所有 `df_bili_*` 工具直接拒绝 |
| `owner_scope` | string | 空 | — | 主人 QQ 号或完整 UMO |
| `owner_name` | string | 空 | — | 主人昵称，出现在标题/评论里时替换为「主人」 |
| `owner_mid` | string | 空 | — | 主人 B 站 UID，脱敏用；开启私信监听时即对话另一方 |
| `cookie` | string | 空 | — | B 站 Cookie（`secret`） |

### 限速与读取

| 键 | 类型 | 默认 | 范围 |
|---|---|---|---|
| `request_interval_ms` | int | `600` | 0~5000 |
| `hourly_reads` | int | `600` | 1~6000 |
| `daily_reads` | int | `3000` | 1~20000 |

### 内容抽取

| 键 | 类型 | 默认 | 范围 | 说明 |
|---|---|---|---|---|
| `max_video_mb` | int | `200` | 1~200 | 直连抽帧失败时整片下载的兜底上限 |
| `max_frames` | int | `9` | 1~16 | 单次抽帧张数 |
| `subtitles_max_chars` | int | `4000` | 0~20000 | 字幕截断字数，0 表示不输出字幕正文 |
| `comments_count` | int | `8` | 0~20 | debrief 附带评论条数，0 表示不取 |
| `danmaku_bucket_s` | int | `30` | 5~300 | 弹幕分桶时长 |
| `danmaku_top` | int | `5` | 0~20 | 输出几个弹幕热区，0 表示不分析 |
| `vision_enabled` | bool | `true` | — | 是否理解抽帧画面 |
| `vision_provider_id` | string | 空 | — | 视觉模型；留空退回 `astrbot_plugin_face_convert` 的百炼通道 |
| `max_vision_calls_per_run` | int | `3` | 0~8 | 单次 debrief 最多几批视觉调用，0 表示不调用 |

### 私信监听

| 键 | 类型 | 默认 | 范围 | 说明 |
|---|---|---|---|---|
| `dm_enabled` | bool | `false` | — | 启用私信监听与自动回复 |
| `dm_interval_s` | int | `60` | 20~3600 | 轮询间隔 |
| `dm_provider_id` | string | 空 | — | 私信回复模型，留空只读不回 |
| `dm_persona` | string | 空 | — | 私信回复人设 |
| `dm_daily_limit` | int | `30` | 1~200 | 每日回复上限 |
| `dm_cooldown_s` | int | `60` | 0~3600 | 两条回复最小间隔，0 不限制 |
| `dm_max_rounds` | int | `6` | 1~50 | 连续自动回复轮数上限，超出自动暂停该对象 |

### 评论区监听

| 键 | 类型 | 默认 | 范围 | 说明 |
|---|---|---|---|---|
| `comment_listen_enabled` | bool | `false` | — | 启用评论区监听 |
| `comment_scope` | string | `reply` | `reply`/`at`/`both` | 监听「回复我的」/「@我的」/两者 |
| `comment_interval_s` | int | `60` | 20~3600 | 轮询间隔 |
| `comment_reply_provider_id` | string | 空 | — | 评论回复模型，留空只报告不回复 |
| `comment_persona` | string | 空 | — | 评论回复人设 |
| `comment_reply_daily_limit` | int | `20` | 0~200 | 每日评论回复上限，0 不限制 |

### 主动互动

| 键 | 类型 | 默认 | 范围 | 说明 |
|---|---|---|---|---|
| `auto_comment_enabled` | bool | `false` | — | 自己刷内容主动互动 |
| `auto_comment_interval_s` | int | `1800` | 300~86400（内部夹取 60~86400） | 两次主动互动最短间隔 |
| `auto_comment_source` | string | `popular` | `popular`/`ranking`/`dynamic`/`random` | 内容来源 |
| `auto_comment_provider_id` | string | 空 | — | 主动评论模型 |
| `auto_comment_persona` | string | 空 | — | 主动评论人设 |
| `auto_comment_daily_limit` | int | `5` | 0~50 | 每日主动评论上限；纯点赞/收藏不占额度 |

### 写操作与体验

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `publish_enabled` | bool | `false` | 允许模型发动态（公开写操作，建议仅主人会话） |
| `react_enabled` | bool | `false` | 允许点赞/投币/收藏/分享/关注（需 `bili_jct`） |
| `memory_recall_enabled` | bool | `true` | B 站回复时通过 LivingMemory 召回长期记忆（未安装则自动跳过） |
| `chat_window_enabled` | bool | `true` | 为每个私信/评论对象建立独立会话，保留最近 8 轮 |

---

## ⚠️ 风控与免责声明

- 插件内置请求间隔、每小时/每日读取上限与 412 熔断，但**无法保证不被 B 站风控**。
  请保守设置写操作频率，尤其是评论与私信。
- 点赞、投币、收藏、关注、发动态、评论、私信都是**对真实账号的公开写操作**，
  一旦执行无法撤回，请仅在明确授权后开启。
- 请遵守 B 站用户协议与相关法律法规，不要用于刷量、营销、骚扰等行为。
- 本项目与哔哩哔哩官方无关，不提供任何形式的账号安全保证。

---

## 🙏 致敬与出处

- 读路径移植自 [Alterssss/dsh-bili-whale](https://github.com/Alterssss/dsh-bili-whale)（MIT）
- 运行于 [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- 可选搭配 [livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)
  做长期记忆召回，[proactive_chat](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat)
  做主动聊天
- 「大肥鱼」是 DeepSeek 蓝鲸的社区二创形象，非官方角色

## 📄 License

[MIT](LICENSE) © 2026 dafeiyu-astrbot contributors
