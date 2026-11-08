# 小智设备桥接 · astrbot_plugin_xiaozhi_bridge

把小智（xiaozhi.me / ESP32 语音助手）的 **MCP 接入点**接到 AstrBot：
设备上说的话交给 AstrBot 里的人格来回答，人设、记忆、知识库、工具全部复用，
音色仍然由小智一侧负责。

- 当前版本：`2.0.0`
- 适用 AstrBot：`>= 4.26.1`
- 仓库：<https://github.com/xuanyu1937/astrbot_plugin_xiaozhi_bridge>

---

## 一、它解决什么问题

小智原生只能用自己的角色卡说话，接不上你在 AstrBot 里养的那套人格。
本插件做三件事：

1. 主动连上小智云端的 MCP 接入点（设备不用改任何配置）；
2. 把设备说的话转给本机 AstrBot 的 Open API，取回真实回复；
3. 把回复清洗成「能直接念出口」的纯文本再返回给设备。

顺带补齐了这些周边能力：召唤词接管、上下文合并、会话补写、微信推送、
长任务占位、点歌、运行状态视图。

## 二、整体链路

```
小智设备
   │ 语音
   ▼
小智云端 ──wss──▶ 本插件（主动向外连，不开端口）
                   │
                   ├─ initialize / tools/list / tools/call（JSON-RPC 2.0）
                   │
                   ▼
        POST http://127.0.0.1:6185/api/v1/chat   （Bearer API Key，SSE 解析）
                   │
                   ▼
              AstrBot 人格 / 记忆 / 知识库 / 工具
                   │
                   ▼
        清洗后的纯文本 ──▶ 云端 ──▶ 设备朗读
```

**只出不进**：插件不监听任何端口，唯一的网络方向是「向小智云端发起 wss」和
「向本机 AstrBot 发起 HTTP」。

## 三、安装

### 1. 放文件

```
data/plugins/astrbot_plugin_xiaozhi_bridge/
├─ main.py               入口：Star 子类、指令、对话编排、视图接口
├─ settings.py           配置视图：默认值、派生字段、静音时段判断
├─ state_store.py        运行时状态：原子写 JSON，旧文件自动迁移
├─ chat_api.py           调用 Open API + 文本清洗
├─ mcp_server.py         MCP 客户端、JSON-RPC 分发、重连
├─ session_sync.py       上下文合并与会话补写（全走 ConversationManager）
├─ push.py               推送：节流、去重、退避重试
├─ music.py              点歌：意图识别、搜索打分、下载、缓存清理
├─ views/dashboard/      运行状态视图（index.html / app.js / style.css）
├─ .astrbot-plugin/i18n/ 视图标题的中英文案
├─ _conf_schema.json     配置定义（39 项）
└─ metadata.yaml         插件元数据
```

### 2. 装依赖

```
pip install -r requirements.txt   # httpx、websockets
```

### 3. 启用

面板 → 插件 → 重载 → 确认「小智设备桥接」为启用状态。

### 4. 填三样东西

| 配置项 | 从哪来 |
|---|---|
| `xiaozhi_token` | 小智控制台 → 智能体 → MCP 接入点，复制整条地址或 token |
| `astrbot_api_key` | 本面板 → API Keys 新建，权限给 `chat` 即可 |
| `unified_user_id` | 插件配置里直接选，列表来自面板出现过的用户 |

### 5. 让小智知道要调工具

在小智控制台的「角色介绍 / 系统提示」里加一句：

> 凡是要回答用户、聊天、提问、倾诉，都必须先调用 `ask_astrbot` 工具，
> 拿到返回文本后一字不改地念出来。不需要回答时不要调用。

这句话不写，云端可能自己编答案，不经过 AstrBot。

## 四、配置项详解（39 项）

### 连接相关

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `xiaozhi_token` | string（secret） | 空 | 小智 MCP 接入点 Token。填完整 `ws://` / `wss://` 地址也可以，插件会直接用 |
| `astrbot_api_key` | string（secret） | 空 | 调用本机 `/api/v1/chat` 用，面板创建，权限 `chat` 就够 |
| `astrbot_base_url` | string | `http://127.0.0.1:6185` | 改过面板端口才需要动 |
| `request_timeout` | int | 120 | 等 AstrBot 回答的上限（秒），太小会频繁回占位句 |

### 身份与推送

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `unified_user_id` | list | `[]` | 用谁的身份对话；取第一个。兼容旧版的字符串写法 |
| `push_target_umo` | string | 空 | 推送目标会话。留空则用「平台 + 身份」自动拼 |
| `push_platform_id` | string | 空 | 例如 `weixin_personal_ezgt`；留空时按身份推断 |
| `push_to_wechat` | bool | true | 总开关：关掉后只对话、不推送 |
| `push_with_question` | bool | true | 推送时带不带设备原话 |
| `push_bot_tag` | string | `【设备】` | 推送里代表设备的标签 |
| `push_user_tag` | string | `【我】` | 推送里代表 AstrBot 的标签 |
| `mirror_enable` | bool | true | 把一问一答补写进目标会话历史 |
| `quiet_hours` | string | 空 | 静音时段，如 `23:30-07:30`，支持跨零点；该时段不推送 |

### 触发与接管

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `require_trigger` | bool | true | 关掉后设备说什么都由 AstrBot 接 |
| `trigger_words` | list | `["小助手"]` | 出现这些词就接管 |
| `exit_words` | list | `["退出","不聊了","结束","换回小智"]` | 出现即退回小智本体 |
| `trigger_idle_minutes` | int | 30 | 接管后多久没说话自动退回，0 表示不自动退 |
| `user_alias` | string | `哥哥` | 占位句里怎么称呼你 |
| `persona_label` | string | 空 | 人格标签，留空用默认说法 |

### 上下文与会话

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `astrbot_session_id` | string | `xiaozhi-wx` | 设备侧会话固定后缀 |
| `multi_device_enable` | bool | false | 按设备 id 分开记，各设备互不干扰 |
| `context_seed_enable` | bool | true | 回答前把目标会话最近内容合并进设备会话 |
| `context_seed_messages` | int | 60 | 每次拉多少条 |
| `context_max_messages` | int | 200 | 设备会话上限，超了丢最早的 |
| `disable_session_tts` | bool | false | 关掉设备会话的 TTS，只取文本 |

### 长任务

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `long_task_notice` | bool | true | 超过阈值还没答完，先回一句「稍等我一下」 |
| `long_task_threshold_sec` | int | 5 | 占位触发秒数；小智端通常 6~8 秒判超时，建议 4~6 |
| `long_task_remind_sec` | int | 45 | 长任务期间每隔这么久推一句「还在弄」 |

> 占位句不会被当成真实回复写进记录，真正的回答完成后才补写。

### 开机问候

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `drop_boot_greeting` | bool | true | 识别设备上电时那句固定问候 |
| `boot_greeting_mode` | string | `skip` | `skip` 静默不答；`greet` 让 AstrBot 主动说一两句 |

同一句问候 15 秒内出现 3 次会主动打断，避免设备循环刷。

### 工具与点歌

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `ask_tool_name` | string | `ask_astrbot` | 暴露给云端的工具名，改名后要在小智控制台重新同步 |
| `music_enable` | bool | true | 点歌总开关 |
| `music_send_file` | bool | true | 点歌时直接把音频文件发到目标会话 |
| `music_reply_with_url` | bool | true | 回复里带播放直链 |
| `music_cache_ttl` | int | 900 | 同一首歌这段时间内重复点不重新下载 |
| `music_cleanup_enable` | bool | true | 自动清理缓存 |
| `music_cleanup_hours` | int | 6 | 清理间隔（小时） |
| `music_keep_days` | int | 3 | 缓存保留天数 |
| `music_max_mb` | int | 200 | 缓存总容量上限，超了从最旧的删 |

所有数值项读取时都会**钳到安全区间**，写错或留空都不会让插件崩。

## 五、指令

| 指令 | 说明 |
|---|---|
| `/xiaozhi status` | 连接状态、当前模式、今日次数、最近错误 |
| `/xiaozhi push on` | 打开推送 |
| `/xiaozhi push off` | 关闭推送 |
| `/xiaozhi reconnect` | 手动重连 MCP（幂等，不会叠连接） |
| `/小智推送 开` / `关` / `状态` | 兼容旧版入口 |

## 六、运行状态视图

面板 → 插件 → 小智设备桥接 → **小智桥接** 视图。

内容：

- **状态徽标**：绿（全通）/ 黄（部分不通）/ 红（Token 都没填）
- **四项自检**：Token、连接、身份、推送目标；失败项直接给出下一步动作
- **开关**：推送、召唤词接管
- **静音时段**：输入 `23:30-07:30` 保存，支持跨零点
- **按钮**：重新连接、清理缓存
- **运行信息**：发送人身份、推送目标、会话标识、召唤词、今日计数、运行时长、最近错误

后端接口（注册在 `Context.register_web_api`，路由以插件名开头）：

| 路由 | 方法 | 作用 |
|---|---|---|
| `/astrbot_plugin_xiaozhi_bridge/status` | GET | 返回状态 JSON |
| `/astrbot_plugin_xiaozhi_bridge/toggle` | POST | `{name: "push"|"trigger", enabled: bool}` |
| `/astrbot_plugin_xiaozhi_bridge/quiet` | POST | `{quiet_hours: "23:30-07:30"}` |
| `/astrbot_plugin_xiaozhi_bridge/reconnect` | POST | 重新连接 |
| `/astrbot_plugin_xiaozhi_bridge/cleanup` | POST | 清理点歌缓存 |

> 实际访问前缀是 `/api/v1/plugins/extensions/astrbot_plugin_xiaozhi_bridge/<路由>`，
> 前端用相对路径 `status`、`toggle` 这类，由桥接自动拼。
> **Token、身份、推送目标不在视图里改**，请到「插件配置」页。

## 七、提供的 MCP 工具

| 工具 | 入参 | 说明 |
|---|---|---|
| `ask_astrbot`（可改名） | `message` 必填，`session_id` 可选 | 主链路：交给 AstrBot 回答。不需要回答时返回空内容且 `isError=false`，云端保持安静 |
| `send_wechat_message` | `text` | 发到配置的目标会话 |
| `play_music` | `keyword`，`platform` 可选 | 点歌，返回歌名与地址 |
| `bridge_status` | 无 | 桥接自身状态，排障用 |

另有给其它插件用的扩展口：

```python
plugin.mcp.register_tool(name, description, schema, handler)
plugin.mcp.unregister_tool(name)
```

## 八、数据与文件

| 内容 | 位置 |
|---|---|
| 运行时状态 | `data/plugin_data/astrbot_plugin_xiaozhi_bridge/state.json` |
| 点歌缓存 | `data/plugin_data/astrbot_plugin_xiaozhi_bridge/music/` |
| 配置 | 面板「插件配置」，保存走 `AstrBotConfig.save_config()` |

`state.json` 采用**原子写入**（临时文件 + `os.replace`），写一半断电不会留下坏文件。
记录内容：推送开关、接管模式与时间、上下文同步指纹。

**插件安装目录里不会写任何数据**，旧版遗留的 `state.json` 首次启动会自动迁移一次。

## 九、合规说明

| 要求 | 本插件的做法 |
|---|---|
| 目录名与 `metadata.name` 一致 | 都是 `astrbot_plugin_xiaozhi_bridge` |
| 版本语义化 | `2.0.0`，不带 `v` 前缀 |
| 持久化位置 | 只用 `get_astrbot_plugin_data_path()`，绝不写插件目录 |
| 数据库 | **完全不连**，不出现 `sqlite3`；会话与记忆只走 `ConversationManager` |
| 日志 | 只用 `from astrbot.api import logger`，不写日志文件、不 `print` |
| 网络监听 | 不起任何 HTTP/WebSocket **服务端**，只主动向外连 |
| 页面 | `views/dashboard/`，后端只走 `register_web_api`，前端只用桥接 |
| 配置写入 | 只用注入的 `AstrBotConfig`，不碰 `data/config/*.json` |
| 网络库 | `httpx`（异步）+ `websockets`，不用 `requests` |
| 私有接口 | 不调用 `_load_all_configs`、`send_by_session`，不猴子补丁平台事件 |
| 重载 | `terminate()` 取消全部后台任务并断开连接，不残留 |

自检命令（交付前已全部通过，结果 0 命中）：

```bash
grep -rn "sqlite3\|import logging\|print(\|aiohttp\|8765\|/AstrBot/data" *.py
grep -rn "os.system\|subprocess\|eval(" *.py
```

## 十、从旧版升级

旧版插件名是 `astrbot_plugin_device_bridge`，**目录名与插件名不一致**，也不符合当前规范
（自建日志文件、直连数据库写 `preferences` / `api_keys` 表）。2.0.0 的差异：

| 项目 | 旧版 | 2.1.1 | 修复三处推送漏点；推送改由视图开关单点控制；状态页新增 RPC 与推送诊断 |
| 2.1.0 |
|---|---|---|
| 插件名 | `astrbot_plugin_device_bridge` | `astrbot_plugin_xiaozhi_bridge` |
| 本机端口 | 开 `8765` 提供接口 | **不开端口**，状态在视图里看 |
| 日志 | 自建 `xiaozhi.log` | 统一走 AstrBot 日志 |
| 数据 | 写在插件目录 | `data/plugin_data/...` |
| 数据库 | 直连写 `preferences`、`api_keys` | 完全不连 |
| API Key | 自动往 `api_keys` 表插一条 | **要求手动创建**，插件不代劳 |

升级步骤：停用旧插件 → 删旧目录 → 装新版 → 重新填 Token 与 API Key → 重载。
旧的 `state.json` 若还在，会自动迁移一次。

## 十一、常见问题

**Q1：设备说「我这边卡了一下」**
回复超过设备端超时（通常 6~8 秒）。把 `long_task_threshold_sec` 调到 4~5，
让插件先回「稍等我一下」；还不行降到 3。

**Q2：提示「还没配置 AstrBot API Key」**
面板 → API Keys → 新建，权限给 `chat`，把 key 填进 `astrbot_api_key` 后重载。
插件不会自动创建 key。

**Q3：视图里连接显示「离线」**
按顺序看：`xiaozhi_token` 是否填对 → 本机能不能出网 → 点「重新连接」→
看面板日志里 `[xiaozhi_bridge] MCP 断开` 后面那句具体原因。

**Q4：设备说话没反应，也不报错**
大概率是没命中召唤词。要么说带 `小助手` 的句子，要么把 `require_trigger` 关掉。

**Q5：回复被我自己的记忆污染了**
调小 `context_seed_messages`，或关掉 `context_seed_enable` 只让设备会话独立记。

**Q6：推送发不出去**
看三件事：`push_to_wechat` 是否开、`push_target_umo` 是否为空、
当前是否在 `quiet_hours` 里。视图的「推送目标」一行会显示实际拼接结果。

**Q7：点歌没反应**
需要先装 `astrbot_plugin_music`。没装时插件只回一句可读提示，不会报错。

**Q8：改了配置不生效**
面板里改完会自动重载；若没重载，`/xiaozhi reconnect` 只重连 MCP，不重读配置，
请在面板点重载。

**Q9：想换工具名**
改 `ask_tool_name` 后，必须到小智控制台重新同步一次工具列表，否则云端还在调旧名字。

## 十二、排障速查

```bash
# 插件是否加载
面板 → 插件 → 找「小智设备桥接」，看是否启用

# 状态接口（把 <key> 换成本机面板的 API Key）
curl -H "Authorization: Bearer <key>" \
  http://127.0.0.1:6185/api/v1/plugins/extensions/astrbot_plugin_xiaozhi_bridge/status

# 日志过滤
面板 → 日志 → 搜 [xiaozhi_bridge]
```

日志里常见的几句：

| 日志 | 含义 |
|---|---|
| `MCP 已连接` | 握手成功 |
| `MCP 断开：xxx（Ns 后重连）` | 网络或 Token 问题，看冒号后内容 |
| `还没填小智 Token，MCP 不启动` | 配置缺失 |
| `对话失败：...` | 本机 API 调不通或 Key 权限不足 |
| `推送失败(第N次)：...` | 平台侧拒绝，通常是平台适配器没运行 |

## 十三、开发与扩展

**加一个 MCP 工具**

```python
from astrbot.api import logger


def register_extra(plugin):
    async def handler(args: dict) -> str:
        return f"你说了：{args.get('text', '')}"

    ok = plugin.mcp.register_tool(
        name="my_tool",
        description="演示用工具",
        schema={"type": "object", "properties": {"text": {"type": "string"}}},
        handler=handler,
    )
    logger.info("注册扩展工具：%s", ok)
```

**约定**

- 工具 handler 是 `async def handler(args: dict) -> str`；
- 返回空字符串表示「本轮不用回答」，云端会保持安静；
- 抛异常会被兜住并转成 `isError=true`，不会断开 MCP 连接。

## 十四、版本记录

| 版本 | 变更 |
|---|---|
| 2.0.0 | 按 AstrBot 官方插件规范整体重构：不开端口、不连数据库、不自建日志；改名并对齐仓库；新增视图、点歌、多设备记忆 |
| 1.6.x | 早期版本，名为 `astrbot_plugin_device_bridge`，已废弃 |

## 十五、许可

MIT，见 `LICENSE`。
