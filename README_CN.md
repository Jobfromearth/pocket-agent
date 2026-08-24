# pocket-agent

**属于你自己的助理。跑在你自己的电脑上。小到一个晚上就能读完。**&nbsp;&nbsp;[**English**](README.md)

我想要一个记得住我的生活、又跑在我自己机器上的助理——不是租来的产品，也不是把最有意思的部分藏在
三层抽象后面的框架。所以我写了「还能称之为严肃 Agent」的最小版本，并且让每一部分都可读：
**一个机制，一个文件。**

它**不需要 API key、不需要任何第三方依赖**，克隆完十秒就能看到它跑起来：

```bash
python -m pocket demo     # 一趟脚本化的巡演：记忆、门控、分诊、一次真实的工具调用
python -m pocket dashboard # 浏览器入口，127.0.0.1:7777，与终端共用同一条消息总线
python -m pocket eval     # 117 条确定性评测，离线、免费，一秒内跑完
python -m pocket gate     # 双套件 + CI 读的裁决文件（eval_report.json）
python -m pocket mcp      # 启一个 MCP server 并真的调一次工具，全程不涉及模型
python -m pocket team     # 三个 worker 共用一块看板：两个并行，一个等依赖
python -m pocket tools    # 模型能调什么，以及哪些可以不问人就跑
python -m pocket          # 真正对话（在 .env 里放一个 key）
```

对话中输入 `/help` `/tools` `/context` `/memory` `/board` `/new`，由 harness 直接回答，
永远不会送到模型那里。

核心**只依赖标准库**，`pocket/*.py` 合计 **6,806 行**；`anthropic` / `openai` 只有在你指定
对应 provider 时才会惰性导入。那个行数不是装饰——[评测里有一条断言它仍然属实](pocket/evals.py)，
`./scripts/line_budget.sh` 会按支柱把它打印出来。

## 里面有什么

| 支柱 | 文件 | 放的是什么 |
|---|---|---|
| **Harness** | `config.py` `session.py` `agent.py` `__main__.py` `hooks.py` | 工作记忆的组装、接线，以及一轮里五个可被 Hook 打断的时刻 |
| **Doors** | `bus.py` `dashboard.py` `telegram.py` | 终端、浏览器、IM，汇聚到一条串行化的会话 |
| **Loop** | `loop.py` `models.py` `tools.py` | reason→act→observe 加两条护栏；一个循环，两种线格式 |
| **Memory** | `memory.py` `db.py` `skills.py` | 语义（FTS5）/ 情景 / 程序性记忆，检索门控，固化，Skill 两级披露 |
| **Context** | `context.py` | 四级治理，从最便宜的开始：外置、贴合、压缩、兜底——每一级都留着回去的路 |
| **Reach** | `mcp.py` `web.py` `subagent.py` | 别人 server 上的 MCP 工具；带守卫的联网；受限子 Agent |
| **Team** | `team.py` | 多个 worker 共用一块看板，按依赖关系调度 |
| **Safety** | `permissions.py` `injection.py` | 拒绝清单、询问人类、按会话授权；不可信输出加围栏、下一次调用升级为需人工确认——拒绝以文本回给模型 |
| **Graph** | `graph.py` | 循环**之外**的结构：并行节点、代码路由、fail-open |
| **Ops** | `trace.py` `evals.py` `judge.py` | JSONL 轨迹（按需镜像到 OTLP）、花费账本、双评测套件、发布门禁 |

状态都在 `.pocket/`：`state.db`（SQLite + FTS5——记忆、日历、聊天记录和团队看板）、
`calendar.ics`、`MEMORY.md`、`artifacts/`、`traces/<日期>.jsonl`、`usage.jsonl`。
全部是你能直接打开的文件。

## 十个值得辩护的决定

1. **检索门控。** 多数 Agent 每轮都查记忆。那不仅慢，而且更糟：不相关的记忆会带偏答案。这里先让
   便宜模型回答一个很窄的问题——*这条消息需要记忆吗？*——决定和理由都会写进轨迹，于是它是可审计
   的，而不是玄学。`memory.py`

2. **所有「裁判形状」的东西都 fail-open。** 门控坏了就照常检索；分诊坏了就走完整循环；图里的节点
   坏了就退回普通循环；摘要失败就保留原本压不掉的上下文。降级只应该付出延迟——绝不付出能力，更不
   丢数据。`agent.py`、`context.py`

3. **循环只有两个出口。** 模型不再要求工具，或者达到 `max_iterations` 并如实说出来。没有第三种
   结束方式。`loop.py`

4. **MCP 按 2026-07-28 修订实现。** 该修订让 MCP 变成**无状态**：没有 `initialize` 握手、没有
   session id，协议版本与客户端能力都随 `_meta` 走。stdio 上没有 HTTP 状态码可退，所以规范自己
   的建议是用 `server/discover` 探测、失败即视为「这台是旧版」。两条路径都实现了，也都由评测对着
   一个能说两种方言的内置 server 覆盖。`mcp.py`、`examples/demo_server.py`

5. **第三方能力默认受门控。** 每个 MCP 工具和子 Agent 都是 `risk="ask"`：每个会话由人先看一眼。
   一条很短的拒绝清单胜过任何确认。拒绝会像工具错误一样以文本回给模型，于是这一轮换条路继续，而
   不是让进程死掉。`permissions.py`

6. **联网只经过一个带守卫的 opener——而守卫不在 opener 里。** `search_web` 和 `fetch_url` 是
   仅有的两个会离开这台机器的工具，所以规则集中在一个文件里：只允许 http/https、解析出来的地址
   必须是公网地址、每一跳重定向都重新检查、非文本响应直接拒绝而不是硬解码、读取时就截断而不是读完
   再截。地址检查放在**工具**里而不是底下的传输层里，因为传输层正是测试会替换掉的那道缝——跟着缝
   一起消失的守卫等于没有守卫。两个工具都是 `risk="ask"`，而拿回来的东西是信息，永远不是指令。
   `web.py`

7. **上下文窗口是预算，分四级来花——最便宜、最可恢复的先上。** 40KB 的工具结果会写进
   `artifacts/`，只留预览加指针（`read_artifact`）。**一轮之内，预算在每一次模型调用前都检查**，
   旧的工具结果被**就地缩短**：一条消息都不移除，所以 `tool_use` 永远不会丢掉它的 `tool_result`
   变成孤儿。两级都不够时，最旧的历史才折叠成一条摘要——因为那是唯一花模型调用、也是唯一丢细节的
   一级。如果 provider 仍然说提示太长，就狠狠缩短并**只重试一次**：第二次拒绝直接抛出，无限重试
   是把 bug 变成账单。而且「什么都不删」是**对模型**成立的，不只是对拿着 `sqlite3` 的人：折叠后的
   消息里写明了 `read_history`。`context.py`、`loop.py`

8. **委派但不交出控制权。** 子 Agent 就是又一次 `run_loop`，只是任务更窄、工具表更小。它跑在一次
   工具调用里，只有*结果*会回到父级，并且不能再委派。爆炸半径正好是你传进去的那张工具表。
   `subagent.py`

9. **团队是一块看板，不是一群蜂。** 当多个子任务彼此独立时，真正的问题不再是「子 Agent 是什么」，
   而是协作。所以计划是**数据**——key、工具白名单、`needs`——而不是 Agent 之间的对话。DAG 交给本来
   就有的图引擎执行，独立任务在同一波里并行；每个 worker 只收到它声明依赖的那些结果；某个 worker
   失败时，下游被标成 `blocked`，而不是拿着缺失的输入继续跑。看板是 `state.db` 里的一张表，事后
   可读。`team.py`

10. **确定性评测和判分评测永远不共处一个文件、一个 runner、一种含义。**
   「`create_event` 是不是带着正确参数触发了、那一行是不是落库了？」是单元测试——0 或 1，一条挂
   就阻断发布。「回答好不好？」是对着阈值打的分。检索门控用的不是准确率而是**代价加权准确率**：
   漏检会自信地凭空作答，所以它按 4 倍于误检计价。没有 key 时判分套件是 `skipped` 而**不是
   passed**——一个跑不起来的套件绝不能看起来像跑过了。还有一处刻意的反转：仓库里所有裁判都
   fail-open，唯独这里，**判不了的裁判得 0 分**。`evals.py`、`judge.py`

## 多入口，一个会话

网关的职责是搬字符串。一旦有了第二个网关，三个问题就冒出来了，而它们**不能由网关自己回答**——
谁在说话、两条消息同时到怎么办、还有谁在看。`bus.py` 一次性回答这三个：每条消息带着自己的
`source` 并落进同一个 `Session`；所有轮次由**单个 worker 串行化**，两个入口不可能交叉进同一个
上下文窗口；事件发布给每一个订阅者，所以浏览器里发起的一轮，它的门控决策和工具调用照样流到终端。

```bash
python -m pocket dashboard      # 页面和这个终端，共用一条总线
python -m pocket telegram       # 第三个入口（需要 token 和白名单）
```

页面是一个**无构建步骤**的静态文件，七个面板，每一个都是磁盘上已有东西的投影：`state.db`、
`traces/<日期>.jsonl`、`usage.jsonl`、`eval_report.json`。它只绑 `127.0.0.1`，**没有开关能改**。

`python -m pocket telegram` 不到一百行，因为一个网关就是 `bus.submit()`，外加（如果它想渲染
进度）`bus.subscribe()`。`POCKET_TELEGRAM_ALLOW` 是聊天 id 白名单，而且不是可选项：bot token
指向一个**任何人都能找到并私信**的 bot，而它背后是你的日历。

## Skill 的两级

一个 skill 就是一个 `SKILL.md`。**第一级**是它的**名字和描述**，渲染成一份始终出现在系统提示里的
catalog——每个技能一行，也正是第二级得以存在的前提：模型没法索要一个它不知道存在的正文。
**第二级**是**正文**，而它作为**独立的一条消息**到达，绝不折进系统提示，于是一个任务的指令在轨迹里
可归因、被压缩时可单独丢弃。

两条拉取路径不是竞争关系：关键词匹配器有把握时直接命中，不花额外往返；`read_skill(name)` 是模型在
匹配器判断错时自己调的——那也是**对匹配器难以分词的语言唯一有效的路径**。

## Prompt 注入：控制住，而不是解决掉

`fetch_url` 取回来的页面，可以被写成"读起来像指令"。没有任何检测器能全抓住，所以 `injection.py`
不声称自己能。它做三件事：**分类**——拿不可信输出去比对一组"在搜索结果里没有任何无辜理由出现"的
形状；**加围栏**——可疑内容**保留而非丢弃**，包进横幅明确标注为 DATA，并把发现摊开写明；
**升级**——高风险结果之后，**下一次**工具调用需要人工确认，哪怕它平时不问人，且只升级一次。

换个措辞就能绕开模式匹配。绕不开的是升级，因为它触发于**分数**而不是具体词句，而它卡住的正是
一次注入唯一想要的东西：下一个副作用。

## 图，简述

循环*发现*下一步做什么；图*预先决定*下一步是什么。两者都在。引擎按波次执行节点——同一波的并行节点
必须写不相交的 key，否则直接抛错——路由器是普通 Python 函数，所以模型永远不直接决定控制流。内置的
工作流是分诊：用小模型给消息分类，**同时**并行加载日历，然后路由。`thanks!` 不会吵醒大模型；真正
的任务走的仍是 flag 关掉时那条 `_full_turn`，所以「循环作为节点」不可能和「循环作为默认路径」漂移。

```bash
POCKET_GRAPH_WORKFLOWS=1 python -m pocket
```

## 团队看板，简述

`delegate` 把一个子任务交给一个子 Agent。`assign_team` 接收一份计划——带 `key`、`tools`、`needs`
的 JSON 列表——把每个任务写成 `state.db` 里的一行，然后交给波次调度器：独立任务一起跑，依赖完成
本身*就是*解锁，每个 worker 的系统提示里只带着它点名依赖的那些结果。除此之外 worker 之间什么都不
传：没有点对点闲聊，也没有共享草稿纸。

```bash
python -m pocket team                      # 固定计划，离线，全程没有模型做决定
POCKET_TEAM=1 python -m pocket             # 让模型可以调用 assign_team（会先问你）
sqlite3 .pocket/state.db "select key, status from tasks"    # 事后的看板
```

不合法的计划（成环、依赖不存在、超过 8 个任务）在任何 worker 花掉一个 token 之前就被拒绝，并把
原因以文本回给模型让它改。完整细节，包括它**刻意不做**的事：[docs/teams.md](docs/teams.md)。

## 接一个 MCP server

`.pocket/mcp.json` —— 和所有 MCP 客户端一样的结构：

```json
{"servers": {"demo": {"command": ["python3", "-m", "pocket.examples.demo_server"]}}}
```

它的工具会以 `mcp__demo__<tool>` 出现，并标记为「先问人」。`python -m pocket mcp` 会写好这份起步
配置、连上去、并真的调用一次，让你亲眼看到协议在跑。

## 接着读哪里

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 一轮对话的完整链路、文件地图、七条不变量 |
| [docs/configuration.md](docs/configuration.md) | 每一个环境变量、provider 与 MCP 设置 |
| [docs/teams.md](docs/teams.md) | 看板：计划结构、调度、失败处理，以及它的边界 |
| [AGENTS.md](AGENTS.md) | 在这个仓库里干活的 Agent（和赶时间的人）该守的规矩 |
| [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [CHANGELOG.md](CHANGELOG.md) | PR 的门槛、信任边界、改了什么 |

## 诚实的边界

- `POCKET_PROVIDER=mock` 是**基于规则的桩，不是模型**。它的存在只是为了让 demo 和评测能离线跑；
  它证明的是 harness 能跑，而不是模型聪明。要真答案，请指向 `anthropic`、`openai`、
  `deepseek` 或 `kimi`。
- 五个核心工具，一个旗舰任务（日程）。每个注册过的工具都会进入**每一次**提示，所以核心刻意保持很
  窄——能力通过 MCP 进来，`assign_team` 也默认关闭（`POCKET_TEAM=1` 才注册）。`search_web` 和
  `fetch_url` 在本会话第一次调用前会问人；`POCKET_WEB=0` 可以把它们从所有提示里移除。
- `fetch_url` 是个标签剥离器，不是浏览器：不跑 JavaScript、不读 PDF、不登录、不带 cookie。
  `search_web` 抓的是 DuckDuckGo 的 HTML 端点，那个端点不欠任何人一份 API 契约——标记一变，
  工具会返回「没有结果」并如实说明，而不是猜。
- 团队的 worker 是本进程里的线程，共用同一个 home 和同一个数据库。没有 git worktree、没有每个
  worker 的沙箱、没有点对点收件箱、也不能中途重新规划：你得到的隔离就是那张工具表。更重的版本请看
  ClawTeam。
- 判分套件只评三条回答质量用例和十二条门控用例。那是**行为退化的冒烟测试，不是 benchmark**——
  而且它花真钱，所以 `python -m pocket eval` 永远不跑它，`python -m pocket gate` 才跑。
- 只有循环里的模型调用被计价写进 `usage.jsonl`；门控、分诊和摘要调用有轨迹但不计价。美元是小价格表
  估出来的，token 才是真的。
- 关键词检索（FTS5），不是向量。对一个人的事实来说，排序过的关键词检索又快、又本地、还能用
  `sqlite3` 直接看。
- MCP 客户端覆盖 stdio、`server/discover`、`tools/list` 和 `tools/call`。Streamable HTTP、
  resources、prompts 和多轮 input request 没有实现——遇到 `input_required` 会如实报告，而不是
  含糊地答一半。

## 出处

四支柱结构，以及少数核心例程（循环的两条护栏、FTS5 查询清洗、图引擎的波次调度），是从
[waku-agent](https://github.com/ShenSeanChen/waku-agent)（MIT）重新实现并裁剪而来——那是功能完整
的版本：仪表盘、语音与聊天网关、可插拔记忆后端、LLM-as-judge 套件。`mcp.py`、`permissions.py`、
`context.py`、`subagent.py`、`team.py` 全部是本仓库自己的。MIT 协议。

另有两个项目是被读过、被致谢、被借鉴的，而不是被依赖的：
[nanobot](https://github.com/HKUDS/nanobot)——终端客户端的 `/` 命令，以及「用脚本让 README 里的
行数不会腐烂」这个习惯（`core_agent_lines.sh`）；
[ClawTeam](https://github.com/HKUDS/ClawTeam)——看板：计划即数据、kanban 状态、会自动解锁的依赖，
以及「宁可说这件事没发生，也不要拿缺失的输入往下跑」这份克制。两者都不是依赖，但都值得完整读一遍。
