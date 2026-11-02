# 更新日志

> 本文件只记录 `astrbot_plugin_bili_whale` 自身的版本演进，最新在前。

## v0.3.30

- 修「点赞（参数错误（code=4100001））」。`_dynamic_text` 此前把 `basic.comment_id_str`（评论区 oid：视频投稿动态是 aid、专栏动态是 cv 号）当成动态 id，导致动态详情、动态点赞、`t.bilibili.com/…` 链接全部拿错号。现在优先取 `item.id_str`，`oid` 单独计算，解析不出动态 id 的条目直接跳过。
- `dynamic_thumb` 增加 `isdigit()` 校验，脏号不再发给 B站。
- `feed_cache.json` 增加结构版本 `v=2`，旧缓存启动后首次加载即丢弃；点赞失败文案分别标注「视频 / 动态」。

## v0.3.29

- 新增 `/bili_stop`（别名 `/bili暂停`）与 `/bili_resume`（别名 `/bili恢复`）：主人可全线暂停/恢复后台任务，恢复时清零连续轮数并跳过积压私信。
- 同一对象连续自动回复超过 `dm_max_rounds`（默认 6）自动暂停该对象并留提醒。
- 暂停状态落盘 `pause_state.json`（全局 + 按 UID），重启后仍有效。
- 新增 `health_state.json`：后台登录失效、缺 `bili_jct`、被配额/412/429 熔断时记录异常，并在下一轮系统提示里提醒模型转告主人。

## v0.3.28

- 工具返回的地址一律补成完整链接（`https://www.bilibili.com/video/BV…`），BV号保留可直接当参数；`df_bili_debrief` 元信息增加「链接」一行。

## v0.3.27

- 修「动态里分不清谁发的」：`df_bili_dynamics` 每条显式写 `UP主：xxx`，转发动态额外注明 `（转发了 xxx 的动态）`，原作者单独从 `item.orig.modules.module_author` 取。
- `_dynamic_text` 新增 `major.music` 分支，音频卡片的 `label`（歌手/来源）不再混进发布者。

## v0.3.26

- 「我的B站状态」每轮带上硬币余额（复用 `coin_balance()`，缓存 5 分钟），模型不再说「硬币余额我查不到」。
- 新增三个只读工具：`df_bili_favorites`（收藏夹列表与内容）、`df_bili_history`（观看记录 / 稍后再看）、`df_bili_related`（相关推荐）。

## v0.3.25

- 修「关注动态时间线还是刷不出来」：业务错误统一带 `（code=xxx）`，失败按 warning 告警（半小时一次），结果细分为「接口失败 / 0 条 / 解析不出内容」。
- `dynamic_feed` 空列表直接返回，不再换变体反复请求；补齐 `web_location` 与 `features` 参数；转发动态不再整条丢弃。

## v0.3.24

- 主动刷 B站改为决策式：模型先给 `interest` / `like` / `coin` / `fav` / `comment`，再按决定执行；不感兴趣就什么都不做，只有确实想说话时才留评论。
- 只点赞/投币/收藏不消耗每日主动评论额度，未做任何动作不推送打扰。
- 付费/充电专属、评论区已关闭（`12045` / `12002`）归类为「内容本身不让评论」，不再反复告警。
- 新增 `dynamic_thumb`，可只给关注动态点赞不留言。
- 投币前先查余额：新增 `BiliClient.coin_balance()`，余额不足时自动降档并汇报真实投出数量。
- 长期记忆召回对齐 `astrbot_plugin_livingmemory` 真实实现，超时放宽到 10 秒，超时后暂停 10 分钟。

## v0.3.23

- 新增 `on_llm_request` 注入「我的B站状态」：昵称与 UID、后台主动互动开关与来源、最近 5 件自己在B站做过的事。
- 后台成功发评论后写入 `auto_comment_state.json` 的 `recent`（保留 8 条）。
- 关注动态加第三层兜底：时间线与翻主页都失败时，返回 `feed_cache.json` 里上次成功的一批。

## v0.3.22

- 新增 `df_bili_dynamics`，按时间给出关注 UP 的最近动态（作者、类型、正文节选、赞/评数、链接）。
- `dynamic_feed` 补齐网页端占位参数；失败时自动退化：读关注列表，翻最近 6 位 UP 的主页合并成时间线。

## v0.3.21

- 修「主动评论失败：」后面空白。aiohttp 总超时抛出的 `asyncio.TimeoutError` 字符串为空，绕过 `except BiliError` 冒到后台。现在统一转成「请求超时（20 秒无响应）」类 `BiliError`，日志带异常类名。

## v0.3.20

- `auto_comment_source` 新增 `random`，每次在热门、排行榜、关注动态里等概率抽一个。
- 新增只读工具 `df_bili_space`：给 UID 或主页链接，返回昵称、签名、粉丝/关注/投稿数、最近投稿与最近动态；三路各自容错。

## v0.3.19

- 新增 `df_bili_follow`（按 BV号/av号 或 UID 关注/取关）。
- `auto_comment_source` 新增 `dynamic`：拉关注动态时间线，按 `dyn:<id>` 去重后生成评论并发布。
- 新增 `df_bili_publish_dynamic`，开关 `publish_enabled`（默认关闭），新版接口失败回退旧版。
- 视频评论提示词补上字幕摘要与抽帧画面理解，动态互动补上正文与卡片信息。
- 长视频抽帧改用官方进度条缩略图 + ffmpeg `crop`，失败才回退直连 seek / 整片下载。
- 主动推送改为第一人称「我刚……留了一条自己的评论」，并说明是自己发而非别人代发。

## v0.3.18

- 新增自己刷视频主动评论：后台拉热门或全站排行，跳过已评过的视频，结合人设、长期记忆与历史窗口生成公开评论后发布。默认关闭，每日上限默认 5 条，失败不重试同一条。
- 发布成功后写入该 UP 主的会话窗口，并向主人推送【B站·主动评论】报告并补进会话历史。
- 客户端新增 `popular()` / `ranking()`。

## v0.3.17

- B站私信与评论回复现在会召回 QQ 侧长期记忆（LivingMemory），只用于语气与背景，明确禁止复述私事与账号信息；新增开关 `memory_recall_enabled`。
- 新增 B站独立聊天窗口：每个私信对象、评论对象各占一条会话（`bili_whale:FriendMessage:<UID>` / `bili_whale:CommentMessage:<UID>`），保留最近 8 轮；新增开关 `chat_window_enabled`。

## v0.3.16

- 主动推送不再「发出去就忘」：评论监听报告发送成功后，以 `assistant` 身份补进当前会话历史。

## v0.3.15

- `owner_scope` 支持直接填纯 QQ 号（如 `123456789`），不必再抄完整 UMO。评论监听推送目标仍要求完整 UMO，填裸 QQ 时会在告警里提示补全。

## v0.3.14

- 抽出 `is_owner`：UMO 相等直接放行，否则比对发送者 QQ 号与 `owner_scope` 末段；拒绝时日志打出实际会话、发送者与配置值，`/bili_login` 同样改用该判定。

## v0.3.13

- 修 `df_bili_search` 搜中文返回无关结果：WBI 签名后的 query 不再交给 aiohttp `params` 二次编码，直接拼在 URL 上（字幕与消息中心请求同样受益）。

## v0.3.12

- 私信里 `msg_type=7` 的分享卡片不再被判「未识别的消息类型」，转成 `[分享了内容] 标题，作者：xxx` 参与上下文；卡片带 BV号/b23 链接仍会读取视频素材。

## v0.3.11

- 写操作 Cookie 补齐 `DedeUserID`（此前缺它是 403「账号异常」的根因）。扫码登录会一并提取，老配置在写操作前用 `nav` 的 `mid` 自动补齐。

## v0.3.10

- 修投币 403「账号异常,操作失败」：写操作统一改用「登录态 + 空 `buvid3`/`buvid4`」的请求口径；仍被判 403 时自动换新签发并做过 Gaia 激活的指纹重试一次。接口报错文案统一带 `code`。

## v0.3.9

- 私信回复不再默认对方是主人：提示词改为「发信人不一定是账号主人」，聊天记录里对方标「对方」，也不再自称仆人。

## v0.3.8

- 评论区自动回复支持视频之外的内容：`business_id` 与评论区 `type` 同码（1 视频、12 专栏、17 动态等），`reply_add` 接收 `comment_type` 原样带上；报告标题按类型显示。

## v0.3.7

- 按 bili-toolbox 官方实现校准写操作：投币固定附带点赞（`select_like=1`），表单同时带 `aid` 与 `bvid`，`Referer` 用 BV 页，`multiply` 控制币数；`df_bili_react` 新增 `share` 动作。

## v0.3.6

- 投币「非法访问」的根因是只投币不点赞：`select_like=0` 会被风控判 `-401`，改为 `select_like=1`。
- `Credentials` 新增 `buvid4`，登录指纹齐全时只用登录那一对。
- 一键三连改走官方 `x/web-interface/archive/like/triple`，失败才回退逐项执行。

## v0.3.5

- 新增 `df_bili_react`：点赞、投币（1/2 枚）、收藏、一键三连。默认关闭，需打开 `react_enabled`，只对主人会话生效，需要 Cookie 里的 `bili_jct`。

## v0.3.4

- QQ 里的 B站分享卡片可进模型上下文：新增前置钩子 `qq_card_to_text`，从 `Json/Share/Unknown` 组件与 `raw_message` 的 `json/xml/share` 段里识别 BV号、`av` 号、`bilibili://video/` 与 `b23.tv` 短链（短链会还原成 BV号）。

## v0.3.3

- 私信里分享的视频可读取：BV号、`av` 号、`bilibili://video/`、`b23.tv` 短链都能识别，复用 `df_bili_debrief` 的完整读取链路；读取失败或超时只降级成一句说明，不阻断回信。`BiliClient.view()` 支持 aid，新增 `resolve_video()`。

## v0.3.2

- 修评论监听因 `data.cursor` 是对象而报 `int() argument must be ... not 'dict'`：改为只取最新一页、靠 `seen` 里的 id 去重。

## v0.3.1

- 评论监听从「只通知」改为「自动回复 + 报告」：新增 `reply_add`，按条目里的 `subject_id/root_id/source_id` 接在对应评论下回复。
- 模型与人设可独立配置（`comment_reply_provider_id` + `comment_persona`），每日上限 `comment_reply_daily_limit`。
- 报告改为逐条即时推送，失败只影响「我的回复」字段，不阻断后续推送。

## v0.3.0

- 新增 B站私信监听与自动回复：后台按间隔轮询与 `owner_mid` 的私信，交模型生成回复后发回；带互关校验、每日上限、冷却与 280 字截断。首次启动只建基线。
- 新增评论区监听：按 `comment_scope` 轮询消息中心，新消息连同正文、之前的评论与链接推送到 `owner_scope`。
- 客户端新增 `nav/relation/fetch_session_msgs/send_msg/msgfeed` 与 `_post_form`。

## v0.2.0

- 新增 `_conf_schema.json`，配置从自建 `control.json` 换成 WebUI 插件配置页，非法值告警回落默认。
- 新增 `/bili_login`（别名 `/bili扫码`）：主人私聊发命令出二维码，手机扫码确认后自动写回 Cookie 并重建客户端。
- 依赖新增 `qrcode`、`pillow`。
- 412 风控缓解：无 Cookie 时自动申请匿名指纹；请求成功即清零熔断计数；会话开启 `trust_env` 支持代理。
- 播放流单独处理并三级回退；播放流 412 不再触发全局熔断。
- 抽帧改为 ffmpeg 直连 `-ss` 取流，只拉需要的片段；直连失败才整片下载。
- `with_frames` 参数保留但忽略，是否抽帧由 `vision_enabled` 决定。
- 画面理解支持 `vision_provider_id` 自选多模态模型，留空回退 `face_convert` 的百炼通道。

## v0.1.0

- 首个版本：纯 Python 复刻 `dsh-bili-whale` 的读路径，提供 `df_bili_debrief`（元信息 + 官方字幕 + 弹幕热区 + 热评 + 抽帧）、`df_bili_subtitles`、`df_bili_comments`、`df_bili_search`、`df_bili_quota` 五个只读工具。
- 带 WBI 签名、600ms 请求间隔、时/日配额与 412 熔断。
