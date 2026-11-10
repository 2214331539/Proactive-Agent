# Proactive Video Agent — 参考实现

按 `docs/Design_V2.md` 与《Proactive系统说明_主动性定义标准与系统框架》实现的主动式视频助理：

1. **VLM 只提议**：VLM 拿着“当前状态切片”看画面，只输出状态补丁（`StatePatch`）。
2. **程序裁决**：补丁由单写者程序（State Actor）校验、归约成信念。
3. **机会 → 价值 → 门控**（2026-11-08 起的主路径）：六类来源把“值得注意的情况”变成机会；对每个机会估计
   p、q、L、η、C、H，算出各种说法的开口价值 U 与等待价值 V；只有 U(m*) > max(0, V) 时才开口。
   旧路径（需求 fire → 决策模型直接给 WAIT/ASK/REMIND）仍保留，`proactive.enabled=false` 可切回。

文档（都在 `docs/`）：
- `Proactive系统说明_主动性定义标准与系统框架.md`：什么算主动、怎么衡量价值、系统由哪些组件构成；
- `Proactive系统_长视频实时验收指南.md`：怎么演示、怎么人工核验、实测结果与已知局限；
- `architecture_v3.html`：系统框架的交互式可视化（浏览器直接打开）；
- `Design_V2.md`、`architecture_v2.html`：V2 字段级设计与模块图。

---

## 0. 主动性验收（2026-11-08）

### 0.1 新增的组件（对应设计说明第 5 章）

| 编号 | 组件 | 文件 | 说明 |
|---|---|---|---|
| C6 | UserView | `pa/state/userview.py` | 每个人以为的世界；物品被别人挪了、经手过它的人不知道 → 认知差 |
| C7 | GoalFilter | `pa/state/goals.py` | 出门概率与阶段（坐着 / 起身准备 / 走向门口 / 已出门），动作、拿随身物品、“走吧 / go go go”等证据累加并衰减 |
| C8 | Receptivity | `pa/state/receptivity.py` | 对象本人在说话 → 打扰代价 ×2；赶时间 → q 低；正在问的人不算打扰 |
| C9 | 机会发现 | `pa/opportunity/` | ① 用户请求（含“N 分钟后提醒我”）② 风险模板 ③ 认知差 ⑤ 目标推断 / 在找东西 ⑥ 风险预测 ✦ 开放发现（LLM，每 30 s） |
| C10 | 价值估计 | `pa/value/estimator.py`、`costs.yaml` | 模型只回答概率问题（p_cond、q），损失 / 有效率 / 代价查表 |
| C11 | 价值门控 | 同上 | 沉默（U*≤0）/ 再等等（U*≤V）/ 提示音 / 询问 / 提醒 |
| C13 | 负担管理 | `pa/policy/burden.py` | 累积打扰负担、语音间隔 30 s（截止在即可插队）、同一件事最多 2 次 |
| C17 | 决策日志 | `pa/storage/decision_log.py` | `log.sqlite` 表 `value_decisions`：每次决定（含沉默、等待）的全部中间量 |
| C18 | 评测 | `pa/eval/opportunities.py`、`pa eval` | 人工核验口径（PG_human）+ 标注窗口口径（PG、FAH、提前量） |
| — | 编排 | `pa/proactive/core.py` | 持续估值、草稿与语音预取、投递 |
| — | 语音 | `pa/output/tts.py` | 豆包语音，按文本哈希缓存 mp3 |

### 0.2 怎么验收（推荐：界面）

```powershell
.venv\Scripts\python -m pa ui            # 浏览器打开 http://127.0.0.1:8765
```

1. 视频源选 **living_room_06 全片 · 16 分钟**，或选“🎬 自定义”任选已下载的 M3-Bench 视频、起点和时长；倍速建议 1×。
2. 点 **▶ 实时运行**：系统按真实时间读视频与字幕。画面左下角显示“AI 正在掂量的机会”及其 U / V；
   时间轴上 ◇ 发现机会、橙点“再等等”、绿点提醒、蓝点询问、青点提示音。
3. 右侧“机会”页能看到每个机会的 p 的因子分解、q 的理由、H、三种说法的价值条与等待线；“决策”页记录每次决定变化（包括为什么沉默）。
4. 在主动消息下或“验收”页逐条点 **✓ 有用 / ✗ 没必要 / ⚠ 判断错误**；在系统该说却没说的时刻点 **⚑ 标记漏报**。
   运行结束后用“⟲ 回放核验”继续核验，点“▶ 看前后”跳到消息前 15 s。
5. 统计：`pa eval runs/<run>`（人工核验 + 标注窗口），结果写入 `runs/<run>/eval.json`。

命令行等价方式：

```powershell
.venv\Scripts\python -m pa run --video data/m3bench/videos/robot/living_room_06.mp4 `
  --srt data/m3bench/subtitles/robot/living_room_06.srt --egocentric --config config/deepseek.yaml --mode realtime
.venv\Scripts\python -m pa eval runs/<产物目录>
```

### 0.3 挑选更多长视频

```powershell
.venv\Scripts\python -m pa bench m3-download living_room_02 --subs-only   # 只下字幕（100 个视频的字幕已在 data/）
.venv\Scripts\python -m pa bench m3-scan                                # 按“叮嘱 / 忘记 / 找东西 / 出门 / 对机器人说”等线索排序
.venv\Scripts\python -m pa bench m3-download living_room_02 --proxy http://127.0.0.1:7890
```

适合验收的片段（字幕线索）：

| 视频 | 时长 | 主动机会 |
|---|---|---|
| living_room_06 | 16 分钟 | 13:57 “别忘了带伞” → 14:52 才发现忘带 |
| living_room_02 | 34 分钟 | 26:29 “robot remind me in three minutes that the instant noodles is ready” → 29:35 应提醒 |
| bedroom_09 | 33 分钟 | 00:54 “Where is my phone?”；01:08 “remind me to make a coffee in five minutes” |
| office_02 | 30 分钟 | 04:35 找手机；08:43 “don't forget this month's work summary”；09:36 “I forgot my file” |
| bedroom_11 | 38 分钟 | 多次出门准备；38:02 “Don't forget your flower” |

标注窗口文件放在 `data/annotations/<video>.json`（已写 living_room_06、living_room_02），格式见文件内注释。

### 0.4 语音播报（豆包）

- `.env` 里的 `DOUBAO_TTS_API_KEY`。当前 Key 只开通了 **seed-audio**（`/api/v3/tts/create`）：它是生成式音频模型，实测单句 13–25 s、偶尔超过 60 s。
  因此系统在机会“变热”（再等等 / 价值接近 0 且截止临近）时就预生成草稿并合成语音；实测雨伞片段在提醒发出时音频已就绪。
- 在火山引擎控制台为该 Key 开通“豆包语音合成 2.0”后，把 `config/deepseek.yaml` 的 `tts.api` 改成 `tts2`，并选择音色（如 `zh_female_xiaohe_uranus_bigtts`），即可走流式接口（首包约 1 s）。
- 界面右上角可切换 豆包语音 / 浏览器语音 / 不播报；“提示音”模式只响一声，不说话。

### 0.5 已有实测（全 DeepSeek，realtime 1×）

| 运行 | 结果 | 标注口径 |
|---|---|---|
| living_room_06 全片 16 分钟（`runs/ui_lr06_full_20261108_221704`） | 只说 1 句：14:39.5“Sophie、Lily，快点，出门别忘了带伞，好像要下雨。”；前 14 分钟（瑜伽、聊天、约晚饭、叮嘱开会）沉默或等待 | PG 1.0，FAH 0，提前 13.1 s |
| living_room_02 25:00 起 6 分钟（`runs/accept/living_room_02_1500s_20261108_222416`） | 记住“三分钟后提醒我泡面好了”，到点前一直等待；29:28“Jack，三分钟到啦，方便面好啦，快去看看。” | PG 1.0，FAH 0 |

迭代过程与已知局限见 `docs/Proactive系统_长视频实时验收指南.md`。每个配置只跑了 1–2 次，决策模型的概率估计有波动，提醒时刻会前后差十几秒。

在真实长视频上发现并修掉的问题：
- 全屋聚合把多人的“坐着”证据相加，把出门概率压到 0；
- “go go go”被算成催促者自己要出门；
- 旁人说话被算成对象“正在聊天”；
- 一帧 leaving 标签就判“走向门口”，现在需要跨来源佐证；
- 提示音用完后，门控仍选提示音，语音提醒被耽误；
- 刚当面说过的叮嘱被立刻复述；
- 没有时机条件的请求（“10 点开会”）立刻响提示音；
- 同一件事的几句话生成多个机会，现在会合并并保留最明确的那句；
- 东西在别人手里被当成“忘带 / 认知差”；
- 开放发现把推测当安全隐患，现在打五折、封顶、每 10 分钟最多一次。

---

## 1. 现在能做什么（2026-11-07）

| 模块 | 状态 | 实现 | 验证情况 |
|---|---|---|---|
| M1 Source | ✅ | PyAV 解码、抽帧、seek、音频重采样；合成画面源 | ffmpeg 生成的 MP4 测试通过 |
| M2 Frame Gate | ✅ | 帧差变化检测 + 稳定等待 + 语句 / 心跳 / focus 触发 | 单测与剧本 |
| M3 语音 / 身份 | ⚠️ 部分 | M3-Bench 字幕（理想 ASR）；faster-whisper 分块转写（可选）；人脸锚点复用 M3-Agent 的 `extract_faces`（可选） | 字幕解析已测；whisper / insightface 代码已写，**本机未安装、未实测** |
| M4 Perception | ✅ | 状态切片 + 提示词 + OpenAI 兼容 VLM（OpenAI / DashScope / Gemini / Ollama / vLLM）+ 响应缓存；脚本化 VLM；dry 模式 | 脚本化路径全测；真实模型 `gpt-6-sol` 已在 M3-Bench 真实视频上跑通（见 §1.1） |
| M5 State Actor | ✅ | Validator / Reducer / Sequencer / TTL / 交互机会规则 / 时间守卫 / CHECK 完整性 | 10 个单测 + 4 个剧本 |
| M6 Agenda | ✅ | 条件 DSL、模板实例化、依赖索引、边沿检测、预取、调度 | 剧本测试 |
| M7 Decision | ✅ | Jev（按官方文档核实的 HTTP 合同）、LLM 决策器、规则基线；阈值 / 只降不升 / 权限 | LLM 决策器已用 `deepseek-flash` 实测；Jev 用 MockTransport 测了请求与响应，**尚无 Jev Key** |
| M8 Response | ✅ | 模板 + LLM 草稿（claims 声明），armed 预生成，超时回退模板 | 模板路径已测 |
| M9 Policy Gate | ✅ | fire / resolve 复核、依赖版本、claims、权限、限频 | 剧本 |
| M10 Output / Feedback | ⚠️ | 控制台输出 + 剧本化反馈；**WebSocket / UI 未做** | — |
| M11 Event Log | ✅ | SQLite WAL：log / snapshots / traces；因果链 `causation_id` | 每次运行生成 `runs/*/log.sqlite` |
| M12 Consolidator | ⚠️ | 规则摘要（离线）/ LLM 摘要；语音需求编译接口 | 规则路径已测；LLM 路径未实测 |
| M13 LTM | ✅ | SQLite FTS5 情节库，检索满足 `t_avail ≤ now` | 剧本 |
| 运行模式 | ✅ | `sim`（虚拟时钟，按实测时延排程）/ `realtime`（墙钟 + 线程池） | 两种模式结果一致（测试） |
| M3-Bench 工具 | ✅ | 标注已内置；断点续传下载；状态探针（与 M3-Agent 信息截止对齐）+ M3-Agent 判分 prompt | 下载与探针快照已跑；作答 / 判分需要 Key |

`pytest` 共 37 个测试全部通过（离线，不需要任何 Key；其中 9 个覆盖主动决策核心）。

已在真实数据上跑通（无模型 Key，用 `perception.provider=dry`）：M3-Bench `office_05`（1280×720 / 30 fps，0.30 GB，字幕 318 句）。

- 前 300 s 在 sim 模式下 15.8 s 跑完；
- 共产生 105 个感知窗口（≈21 次/分钟）：heartbeat 42、utterance 32、change 30、initial 1；
- 状态探针按 `(before_clip+1)×30 s` 取快照；字幕转写进入情节记忆，例如 90 s 时已能检索到 “the cappuccino today is very delicious”。

### 1.1 本机环境确认（2026-11-07，已用真实模型跑通）

**选型**（`config/local.yaml`，密钥在 `.env`，已被 .gitignore 忽略）：

| 角色 | 模型 / 端点 | 实测 |
|---|---|---|
| 感知 VLM | `gpt-6-sol`（OpenAI 兼容中转 vm2api.tsingyuai.com），`reasoning_effort=low`，6 个并发 worker | 单帧看图 8–13 s；真实窗口（3 帧 + 切片）P50 17.6 s、P95 25 s |
| 决策 | `deepseek-flash`（LLM 决策器，Jev 位置保留：`decision.provider: jev`） | 约 1.8 s，JSON 输出稳定 |
| 回复草稿 | `deepseek-flash`（armed 时预生成） | 约 2 s；剧本中预生成草稿在决策返回时直接发出 |
| 作答 / 判分（M3-Bench） | `gpt-6-sol` | 中转里没有 M3-Agent 原用的 gpt-4o-2024-11-20，判分口径有差异 |

中转上 11 个模型的探测结果见 `tools/probe_models.py`：

- 纯文本小请求的基线约 6–7 s，与 reasoning_effort 无关，因此时延主要来自中转本身；
- `gpt-6-astra` 偶发 502 过载；
- 更换端点或模型后，用同一工具复测即可。

**真实数据验证**：M3-Bench `office_05` 前 95 s，字幕作为 ASR，第一人称模式。共跑 5 轮，前 4 轮各自暴露的问题都已修复并补了单测：

| 轮次 | 发现 | 修复 |
|---|---|---|
| 1 | VLM 新建人物时省略 `cls` → 27 条 schema 拒绝，人物建不出来，30 条后续 op 找不到实体；并发在途的窗口都看到空切片，于是重复新建桌子 / 办公室，得到 16 个实体 | `new PERSON` 默认 `cls=person`；**并发创建去重**：切片之后才创建的同类实体一对一绑定；状态为空或工作池满时丢弃心跳窗口（背压）；PLACE 的 `at` 记为 room |
| 2 | 实体降到 5 个、拒绝为 0；但 presence / activity 每个窗口都在翻转：感知滞后约 24 s，按墙钟算 TTL 必然过期 | 改为按**感知水位线**判断过期（已处理到的视频时刻），并加 60 s 兜底 |
| 3、4 | 背压丢窗口后仍偶发误过期：后发的窗口切片太旧，看不到刚写入的信念 | 只有切片版本 ≥ 信念 `support_version` 的观察才计入水位线 |
| 5 | 状态变化只剩 10 条：4 条来自 view，6 条来自 set，没有误过期；状态连贯（两人在办公室，活动连续） | — |

其余发现：

- 情节记忆原先只收录 VLM 复述过的语句，字幕原文进不来 → 改为直接收录转写（在 t_avail 之后才可见）。
- 预生成草稿描述了人物当时的动作（“你在收拾东西”），到 fired 时已经过时，被 claims 新鲜度检查正确地拒绝 → 提示词改为只围绕物品来说。
- 探针 Q01 “What coffee does Marry need?”（90 s）：5 轮里 4 次答对；1 次作答模型保守地回答“不知道”，而证据其实已在情节记忆里。单题没有统计意义，要批量评测。

**时延结论**：在这个中转上，从感知窗口结束到状态提交约 18 s（P50），远高于设计目标的 1–2.5 s。架构能正确处理（并发、背压、水位线、虚拟时钟），但“遗忘物品”这类提醒会晚到。优先考虑：

- 换直连或更快的视觉端点（用 `tools/probe_models.py` 选型）；
- 每窗口 2 帧、更低分辨率；
- `reasoning_effort` 设为 none。

**慢速 API 下优先用 `--mode realtime`**：调用并行、按 1× 推进。95 s 视频约 2.5 min 跑完；sim 模式是串行调用，同一段要约 5 min。


### 1.2 全 DeepSeek + 实时视频流验收（2026-11-07）

**配置**：`config/deepseek.yaml`。感知、决策、回复、慢环全部用 `deepseek-flash`，Jev 位置保留。

- `deepseek-flash` 支持图像输入，但默认先“思考”：推理 6k–12k 字符，会耗尽 `max_tokens`，正文为空（实测失败率约 30%）。
- 必须关闭思考：`extra_body: {thinking: {type: disabled}}`。
- `deepseek-v4-pro` 看图返回空内容，不使用。

| 指标 | DeepSeek（关思考） | DeepSeek（默认思考） | 中转 gpt-6-sol |
|---|---|---|---|
| 感知窗口时延 P50 / P95 | **1.5 / 2.1–2.4 s** | 4.7 / 9.8 s（30% 空输出） | 17.6 / 25 s |
| 窗口结束 → 状态提交（中位 / p90） | **1.5 / 3.2 s** | — | 18 s |
| 决策 / 回复草稿 | 约 2 s | — | — |

**实时流**：`pa run --video udp:// | rtsp:// | http://…` 会自动进入实时模式。

- 后台线程解码，帧时间戳 = 到达墙钟，不能预读未来；
- 模型调用并行；
- 断流超过 60 s 告警。

用 ffmpeg 把文件模拟成直播流：

```bash
ffmpeg -re -ss 810 -t 90 -i data/m3bench/videos/robot/living_room_06.mp4 -c:v libx264 -preset ultrafast \
       -tune zerolatency -an -f mpegts -listen 1 http://127.0.0.1:23003
pa run --video http://127.0.0.1:23003 --srt data/m3bench/subtitles/robot/living_room_06.srt --srt-offset 810 \
       --egocentric --config config/deepseek.yaml
```

**主动性验收：M3-Bench `living_room_06` “两位女士出门忘带伞”**

- 标注：14:54 提问 “What did the two ladies forget to pick up on their way out the door?” → Umbrellas。
- 从 13:30 起实时推流 90 s。流内时间线：
  - 28.8 s：Jack 说 “So don't forget your umbrellas today!”；
  - 62 s：两位女士起身；
  - 73 s：“Girls quickly go go go”；
  - **82 s：Jack 自己发现她们忘带伞**。
- 判定：82 s 前由系统主动发出含“伞 / umbrella”的消息，算及时命中；其他消息计为可能误报。
- 雨伞**从未出现在机器人视野里**，所以只能走“语音请求 → 需求 → 离开时触发”这条路。

```bash
.venv\Scripts\python tools/live_eval.py --video data/m3bench/videos/robot/living_room_06.mp4 \
   --srt data/m3bench/subtitles/robot/living_room_06.srt --start 810 --duration 90 \
   --keyword 伞 --keyword umbrella --deadline 82 --runs 3 --config config/deepseek.yaml
```

| 批次（每批 3 轮实时流） | 及时命中 | 误报 | 这一批暴露的问题 → 修复 |
|---|---|---|---|
| 1 | 0/3 | 2（笔记本电脑、手机） | ① 遗忘物品模板对“人数 × 物品数”全量建需求；第一人称视角下人物 ID 不断增殖，误报很多 → 只对**经手过**该物品的人建需求（`last_holder`）。② 误报占用了 30 s 全局限频，把正确的伞提醒挤掉（只差 1.4 s）→ 被限频拦下的消息**延后复核发送**，而不是丢弃。③ 决策器把“Jack 已口头提醒过”当成“已完成” → 告诉决策器 `user_request` 是被明确要求的提醒 |
| 2 | 1/3（提前 14.6 s） | 0 | 另 2 轮：需求已编译、已 armed，但 VLM 只给出 walking / standing，没有离开类标签 → 让感知结合对话判断离开（“go go go / don't be late for work”且人已起身 → `packing` / `moving_to_exit`，src=inferred） |
| 3 | **2/3（提前 16.9 s、14.8 s）** | **0** | 剩 1 轮仍因离开标签缺失而没有触发。同一时刻决策层正确地压下了“晚饭”“别迟到”两个语音需求（WAIT） |

此前单轮调试中发现并修复的问题：

- VLM 把“今天别忘了带伞！”的 intent 标成 info，需求没有被编译 → 改为逐句扫描原始转写来编译语音需求，不再依赖 VLM 的 intent 标签；
- 瞬时信号在决策返回前就翻转了，被 Policy Gate 拦下 → fire 在 10 s 宽限内为真即可；
- Policy Gate 把空的 resolve 当成“已完成” → 与触发引擎统一为空条件永不成立。

**结论**：系统能从实时视频流（画面 + 语音）建立状态，并在正确时刻主动发出消息。

- 本例 3 轮中 2 轮提前约 15 s 提醒带伞，0 误报，感知时延约 1.5 s。
- 当前召回受两点限制：
  - 第一人称视角下没有人脸锚点，人物 ID 会增殖；
  - “离开”这类活动标签依赖 VLM，每轮结果不稳定。
- 下一步：接入人脸锚点（`pa/speech/faces.py`，需安装 insightface）；在更多“遗忘”类片段上做批量统计。


---

## 1.3 可视化界面（实时运行 / 回放）

```bash
cd D:\AScience\proactive-agent
.venv\Scripts\python -m pa ui          # 浏览器打开 http://127.0.0.1:8765
```

界面布局：

| 区域 | 内容 |
|---|---|
| 左上：视频 | 数据集视频按 1× 播放，与系统时钟同步；叠加“AI 听到”的字幕；系统发出提醒时右上角弹出 “🤖 AI 主动提醒 / 询问”，可选语音播报 |
| 左中：时间轴 | ◆ 记住请求 · ● 需求触发 · • 决策（灰色 = 暂不打扰）· ⬤ 主动消息；竖线是数据集标注的关键时刻（红色虚线为截止时刻）。回放时可点击时间轴跳转 |
| 左下：AI 看到的画面 | 最近一个感知窗口送给 VLM 的 2–4 帧，以及触发原因、返回时延、补丁操作数 |
| 右上：AI 助理 | 对话式时间线：🧩 从对话里记住的请求、⚑ 触发时机、⚖ 暂不打扰（附理由）、AI 的主动消息（附依据、决策把握与理由）。实时运行时可点“已拿到 / 不需要 / 不是我的”，反馈会送回系统 |
| 右下：标签页 | 状态（实体与信念，刚变化的会高亮）· 需求（每个需求的阶段 idle→armed→fired→deciding→awaiting→resolved）· 决策（每次 warranted 与理由）· 事件（完整日志） |

**场景**（`pa/ui/presets.py`）：

- `living_room_06 · 两位女士出门忘带伞`：真实视频 + 全 DeepSeek，需要 `.env` 里的 DEEPSEEK_API_KEY；
- `office_05 · 办公室日常`：真实视频 + 全 DeepSeek；
- `合成剧本 · 忘带手机`：离线、无需 Key，20 s 内必然出现一次提醒，适合先看清整条链路。

**实时运行**：服务端在后台线程以 realtime 模式运行引擎，事件通过 SSE 推到浏览器，并写入 `runs/ui_*/ui_events.jsonl`。

**回放**：从下拉框选择历史运行，视频与事件按时间同步重放，可拖动、可跳转，不调用模型。

- 有 `ui_events.jsonl` 的运行可精确回放，包括 AI 看到的缩略图；
- 更早的运行从 `log.sqlite` 重建事件；
- 下拉框默认选中一次“及时提醒带伞”的成功运行。

**视频格式**：`living_room_06` 原片是 HEVC（H.265），多数浏览器无法播放。界面会自动为演示片段生成 H.264 预览（`data/ui_clips/`，约 6 s 生成）。感知仍读取原视频，浏览器只负责显示。

**已知情况**：实时运行的结果因 VLM 每次标注的人物活动不同而有波动。批量测试中 3 次有 2 次及时提醒；在界面中实测的那 1 次没有触发：需求已记住并 armed，但 VLM 始终没把人标为离开。想稳定看到完整过程，可以回放成功的那次运行，或使用合成剧本。

## 2. 安装

```bash
cd D:\AScience\proactive-agent
uv venv .venv --python 3.11
uv pip install --python .venv -e ".[dev,bench]"
# 可选：本地 ASR
uv pip install --python .venv faster-whisper
# 可选：人脸锚点。Windows 上 insightface 需要 MSVC 编译；其预训练模型仅限非商业研究
uv pip install --python .venv insightface onnxruntime opencv-python-headless tqdm
```

项目放在纯英文路径下：OpenCV / ONNX 在 Windows 上处理中文路径容易出错。

## 3. 离线快速开始（不需要任何 API Key）

```bash
.venv\Scripts\python -m pytest -q                                # E0：28 个逻辑测试
.venv\Scripts\python -m pa scenario scenarios/forgot_phone.yaml  # 设计文档 §7.3 的 20 秒剧情
.venv\Scripts\python -m pa scenario scenarios/forgot_phone.yaml --mode realtime --speed 2   # 墙钟回放
```

`forgot_phone` 的实际输出（节选）：

```text
[00:03.7] v2 phone_1.location: ? → ON desk_1 (holds) [seen] ← set
[00:03.7] ⚑ need_1(possible_forgotten_item …) → instantiated
[00:10.7] v3 person_1.activity: sitting_working → packing (holds) ← set
[00:10.7] ⚑ need_1 → armed
[00:10.7] ✎ 预生成草稿 dr_0002（REMIND）：你的手机好像还在桌子上，记得带上。
[00:12.3] v4 person_1.activity: packing → moving_to_exit (holds) ← act:MOVING_TOWARD
[00:12.3] ⚑ need_1 → fired
[00:13.1] ⚖ rule-baseline: warranted=0.90 mode=REMIND → REMIND
[00:13.1] 💬 [REMIND] 你的手机好像还在桌子上，记得带上。
[00:16.7] v6 phone_1.location: ON desk_1 → HELD_BY person_1 (holds) ← act:PICK_UP
[00:16.7] ⚑ need_1 → resolved
[00:17.2] v7 phone_1.location: … [user_confirmed] ← user:confirm
```

另外三个剧本是负例 / 难例，各自断言写在 yaml 的 `expect` 里：

- `phone_into_bag`：手机进了包，包被背走 → 不提醒；
- `occluded_then_leave`：遮挡时有人靠近桌子 → 状态转 stale → 只允许 ASK；
- `false_leave`：走向窗边又坐回 → armed 后 disarmed，不提醒。

脚本化 VLM 只是“一个完美、带固定延迟的感知器”，用来验证状态、议程、决策链路的逻辑，**不代表真实感知能力**。

## 4. 接入真实模型

复制 `.env.example` 为 `.env` 并填写 Key。之后用 `--set` 或 `--config` 选择组件：

```bash
# 感知：OpenAI（模型名以你账号可用的为准）
pa run --video x.mp4 --set perception.provider=openai --set perception.model=gpt-5-mini \
       --set perception.extra.max_completion_tokens=800
# 感知：阿里云 Qwen-VL（OpenAI 兼容模式）
pa run --video x.mp4 --set perception.provider=dashscope --set perception.model=qwen-vl-max
# 感知：本机 Ollama（例如 qwen2.5vl:7b；8 GB 显存需量化版）
pa run --video x.mp4 --set perception.provider=ollama --set perception.model=qwen2.5vl:7b
# 决策：Jev（实验时锁定版本）
--set decision.provider=jev --set decision.model=jev-1.14.0
# 决策：用 LLM 作对照
--set decision.provider=llm --set decision.llm_provider=openai --set decision.model=gpt-5-mini
# 回复：LLM 草稿（armed 时预生成）
--set response.provider=llm --set response.llm_provider=openai --set response.model=gpt-5-mini
```

- `pa` 是安装后的命令，等价于 `.venv\Scripts\python -m pa`。
- 不想花钱时，可以先用 `--set perception.provider=dry` 跑一遍：不调模型，只统计窗口频率（用来估算成本）。

在真实视频上运行：

```bash
pa run --video data/m3bench/videos/robot/office_05.mp4 --srt data/m3bench/subtitles/robot/office_05.srt \
       --egocentric --duration 120 --set perception.provider=openai --set perception.model=gpt-5-mini
```

每次运行会在 `runs/<名字>_<时间>/` 下产出：

- `log.sqlite`：所有消息与因果链，可用 SQL 回放；
- `summary.json`：VLM P50 / P95、决策、发送、最终状态；
- `timeline.jsonl`。

## 5. 复用的开源代码 / 服务：能力与边界

| 组件 | 复用方式 | 能做什么 | 边界 / 注意 |
|---|---|---|---|
| **PyAV**（FFmpeg 绑定） | 直接依赖 | 解码、seek、抽帧缩放、音频重采样到 16 kHz | 只负责解码；抽帧节奏与时间对齐由 `VideoSource` 实现 |
| **OpenAI Python SDK** 3.x | 直接依赖 | 统一调用所有 OpenAI 兼容端点（OpenAI / DashScope / Gemini 兼容层 / Ollama / vLLM），多图输入，`response_format=json_object` | 各家对 JSON 模式与图片数量支持不同；本地 Ollama 默认关闭 JSON 模式（`json_mode: none`），靠宽松解析 + Pydantic 校验兜底 |
| **TypeSafe System One（Jev）** | HTTP 直连 `POST https://api.typesafe.ai/v1/systemone`，Bearer `TYPESAFE_API_KEY` | 一次请求两个问题：`noul`（warranted 概率）+ `choice`（WAIT / ASK / REMIND，带 probabilities 与 confidence） | 闭源模型，不能训练。官方文档：按输入 token 计费（$0.042 / Mtok），限流按组织设置，**未公布延迟**。默认上下文 64k。choice 总会选一个选项，所以配了 companion noul。也可用官方 `typesafe-sdk` |
| **M3-Agent**（Apache-2.0） | 拷贝 `mmagent/src/face_extraction.py`（`pa/third_party/m3agent/`，保留许可证）；照搬判分 prompt `prompt_agent_verify_answer_referencing`；沿用 `before_clip` 信息截止约定 | 人脸检测 + ArcFace 嵌入 + 质量分；M3-Bench 的标准判分口径 | **没有 import `mmagent`**：它在导入时就加载所有模型，并读相对路径配置。**没有复用 `voice_processing.py`**：它把整段音频发给 Gemini 做 ASR 与说话人分离，时间戳只到秒级。**没有复用 `face_clustering.py`**：它是离线 HDBSCAN，我们需要在线增量匹配，规则对齐 `VideoGraph.search_img_nodes`。**没有复用 `control.py`**：它需要 vLLM（tensor_parallel=2）+ Azure 配置 |
| **InsightFace buffalo_l** | 通过 M3-Agent 的 `extract_faces` 使用（可选） | RetinaFace 检测 + ArcFace 512 维嵌入 | 预训练模型仅限非商业研究；Windows 编译需要 MSVC |
| **faster-whisper** | 可选依赖 | 按块流式转写（只用已到达的音频，保证因果） | 不做说话人分离；块边界可能切断句子 |
| **M3-Bench 字幕 `.srt`** | 作为“理想 ASR” | 100 段 robot 视频都有字幕；语句在 `end + 500 ms` 后才可用 | 是人工 / 离线字幕，比真实 ASR 好，结果要注明 |
| **SQLite / FTS5** | 标准库 | 事件日志、快照、情节检索 | 单进程；多路摄像头需要换存储 |
| **huggingface_hub** | 可选 | `--method hub` 下载 | 2.x 的 Xet 通道在部分网络下会卡住，默认改用 HTTP 断点续传（支持 `--proxy`） |

## 6. 用开源数据集 / 基准验收

按 E0 → E4 逐级验收（设计文档 §10）。每一级说明了**现在能否直接跑**。

### E0 逻辑正确性（✅ 现在就能跑，离线）

```bash
.venv\Scripts\python -m pytest -q
```

通过标准：

- 28 个测试全过；
- 同一剧本重复运行结果完全一致；
- 决策只在 fired 之后发起；
- 遮挡后只允许 ASK；
- 负例不提醒。

新增场景时，复制 `scenarios/*.yaml` 并写 `expect`。

### E1 补丁质量（⚠️ 需要 Key；人工标注工具未做）

1. 用真实 VLM 在 M3-Bench robot 或自拍视频上跑 `pa run`。
2. 补丁都在 `runs/*/log.sqlite`，用下面的 SQL 取出：

   ```sql
   SELECT msg_id, t_event_ms, payload_json FROM log WHERE kind IN ('patch', 'patch_rejected') ORDER BY log_seq;
   ```

3. 抽 50–100 个窗口人工标注正确补丁，统计：op 的精确率 / 召回率、ID 绑定准确率、被拒绝 op 的比例、CHECK 漏报率。
4. 用 `summary.json` 里的 `vlm.p50_ms / p95_ms` 比较不同候选 VLM，选定后锁定版本。

### E2 状态探针：M3-Bench robot（✅ 流程已就绪；作答与判分需要 Key）

数据：

- 100 段机器人第一人称视频（共 125.7 GB，单段 0.3–3.4 GB），每段带字幕；
- 标注已内置在 `data/m3bench/annotations/`；
- 1276 个 QA 带 `timestamp` / `before_clip`，其中约 292 个是 "Where…" 类状态问题；
- 许可证 CC BY-NC-SA 4.0（非商用）。

```bash
# 本机已配置好（.env + config/local.yaml）时：
pa bench m3-probes office_05 --duration 95 --all-questions --config config/local.yaml --mode realtime \
   --answer openai:gpt-6-sol --judge openai:gpt-6-sol

pa bench m3-list                                              # 每段视频的 QA 数 / 状态类问题数
pa bench m3-download office_05 --proxy http://127.0.0.1:7890  # 最小的一段（0.30 GB）+ 字幕
pa bench m3-probes office_05 --duration 600 \
   --set perception.provider=openai --set perception.model=gpt-5-mini \
   --answer openai:gpt-5-mini --judge openai:gpt-4o-2024-11-20
```

协议（与 M3-Agent 对齐）：

- 探针时刻 = `(before_clip+1)×30 s`，与 M3-Agent 检索只用 `clip ≤ before_clip` 的记忆一致。
- 作答模型**只能看到**该时刻的状态快照与此前已可用的情节。
- 判分沿用 M3-Agent `control.py` 实际使用的 `prompt_agent_verify_answer_referencing`（GPT-4o，回复含 "yes" 记为正确，空答案记为错误）。

输出：

- 不给 `--answer/--judge` 时，只导出每个探针时刻的状态快照（`probes.jsonl`），便于人工检查“系统当时以为东西在哪”。
- 给了，就同时输出准确率。

建议对比的组：

- 本系统完整版；
- 惯性属性改用统一 TTL（消融 A2）；
- 去掉 CHECK（A3）；
- 去掉音频（A6，即不给 `--srt`）。

更多状态探针数据（可选）：

- **EgoLifeQA**（HF `lmms-lab/EgoLife`，未设访问门槛）：每题有 `query_time`；Jake 的 500 题中 EntityLog 类 125 题，4 选 1 规则判分。
- **EOC-Bench**（HF `CircleRadon/EOC-Bench`，Apache-2.0）：问题在视频最后一帧提出，片段较短。
- **TeleEgo**：每题有触发时刻，仅限研究使用。

### E3 主动时机（⚠️ 需要补一个适配器）

本系统的需求来自状态模板（例如遗忘物品），而下面的公开基准是“用户先提一个开放问题，系统在合适时刻回答”。要直接评测，需要先实现“用户问题 → 观察型需求（query need）”适配器（见下一步）。导出格式已就绪（`pa/bench/pvqa.py`）。

- **ProactiveVideoQA**（`wangyueqian/ProactiveVideoQA`）：
  - 子集：EGO 326 题、WEB 500、TV 500、VAD 101。
  - 输出 MMDuet 格式 `pred.jsonl`，用官方 `pauc/judge_llm_scores_openai.py` 打分，得到 PAUC。
  - 判分用 GPT-4.1，走 Batch API。它的 `batch_input` 分支多传了一个参数，需要本地改一行。
  - 落在任何 `reply_timespan` 之外的回复不会被扣分，所以误报要单独统计。
- **ESTP-Bench**（ModelScope `zhangyl9/ESTP-Bench`，需要登录）：
  - 第一人称；每题有提问时刻，答案需落在有效区间内；
  - 指标 ESTP-F1 同时考虑答案质量、及时性与误报，最贴近本系统；
  - 题型含物体定位 / 物体状态变化。
- **StreamingBench Proactive Output**：±2 s 的时机检查。它把答案放进了提示词里，只适合做健全性检查。

不依赖适配器、现在就能做的是自标：

- 在 M3-Bench robot 和自拍固定机位视频上标注介入窗口与负例区间，格式见设计文档 §10.3；
- 用 `runs/*/summary.json` 的 deliveries 计算事件级 P / R / F1、提前量、每小时误报次数。

### E4 时延与成本（✅）

- `summary.json`：VLM 调用次数、P50 / P95、决策次数；
- `timeline.jsonl`：从动作发生（或 fired）到首字可见的时间；
- 先用 `--set perception.provider=dry` 估算每分钟窗口数，再乘以单次调用成本；
- 最终时延数字要用 `--mode realtime` 在 1× 下复测。

## 7. 目录

```text
pa/
  schemas/      全部消息与状态的 Pydantic 定义
  source/       M1 视频 / 音频 / 合成画面        gate/      M2 帧门控
  speech/       M3 字幕 / whisper / 人脸锚点     perception/ M4 切片、提示词、VLM 客户端
  state/        M5 Validator / Reducer / Sequencer / StateStore
  agenda/       M6 DSL、模板、触发引擎            decision/  M7 上下文、Jev / LLM / 规则
  response/     M8 模板与 LLM 草稿               policy/    M9 发送前复核
  storage/      M11 SQLite 日志与补丁缓存         slow/      M12 慢环
  memory/       M13 情节库                       runtime/   引擎、配置、CLI
  bench/        M3-Bench / ProactiveVideoQA 工具  third_party/m3agent/ 复用代码（Apache-2.0）
scenarios/      离线剧本（E0）                   tests/     pytest
data/m3bench/   标注（内置）、视频与字幕（下载）   docs/      设计文档与可视化
```

## 8. 下一步

1. **E1 标注工具**：从 `log.sqlite` 导出窗口帧与补丁，做一个简单的标注页。
2. **query need 适配器**：把用户问题编译成“在相关证据出现时回答”的需求，接上 ProactiveVideoQA 与 ESTP-Bench。
3. **说话人分离**：3D-Speaker ERes2NetV2 + 声纹锚点，并实现 `link` 的确认流程。
4. **WebSocket 接口**：`/runtime` 推送 Delta、NeedEdge、Delivery，让 `docs/architecture_v2.html` 显示真实运行。
5. **消融脚本**：一键跑 A1–A7。
6. **第一人称规则补全**：房间级交互机会规则（设计文档 §4.7 中的补充规则）。
