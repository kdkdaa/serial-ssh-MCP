# Board Console 0.3

给 coding Agent 使用的本地 MCP 开发板调试插件。无需固定协议：通过持续串口或 SSH 会话收日志、发命令、等待输出，再结合源码分析；每次连接自动录制，支持断开设备后的离线回放。

0.3 将直接交互作为主要入口：Agent 给出命令或字节，`session_exchange` 在同一会话原样发送并收取反馈；MCP 不检查设备命令语法、不包装 Shell、不自动纠错，由 Agent 解释输出和决定下一步。0.2 的部署与断言任务层保留为可选能力。0.3 已完成 RK3566 串口基础交互实测，具体边界见文末。

在其他电脑安装请先阅读 [INSTALL.md](INSTALL.md)。源码包不包含本机虚拟环境、绝对路径配置和录制数据，由新电脑上的安装脚本重新生成环境与配置。

## 日常交互：一次调用发送并读取

连接后，直接调用 MCP 工具，不需要本地 PowerShell/CMD 转发或临时 Python 客户端：

```json
{"session_id":"实际会话ID","data":"ifconfig","newline":"cr"}
```

使用同一个 `session_exchange`，先发 `cd /Project`，检查反馈后再发 `ls -la`，目录状态由原来的板端终端保留。命令错误会作为设备原始输出返回，不要求先构造任务或验证条件。文本行结束符必须依据目标选择，二进制可用 `data_format=hex`。

默认接收窗口最多 1 秒，有 RX 后静默 100 毫秒可提前返回。静默不代表完成；长操作省略 data、携带返回的 `next_cursor` 继续读，避免重复发送。详细字段、超时语义及示例见 [直接交互说明](docs/interaction.md)。

## 已实现的范围

- 最多 8 个活跃串口、SSH 或回放会话；后台接收不会因一次工具调用结束而停止。
- 模糊连接请求返回 `needs_input`、缺失字段和候选设备，Agent 在真正连接时向用户询问。不会擅自选端口或猜波特率。
- 文本/十六进制发送，显式行结束符，Ctrl+C，DTR/RTS 控制。
- `session_exchange` 合并发送与输出收取，保留终端上下文；返回文本/hex、游标、收取结束原因和耗时。
- 游标读取、字面量等待、跨块文本/字节匹配，超时和断连分开报告。
- SQLite 保存双向原始字节、增量解码文本、时间戳和生命周期事件；关键词搜索与按原始时间/快速离线回放。
- 本地工作区文件读取，以及 SSH SFTP 远程文件读取。
- 项目 MCP 配置和 `.agents/skills/board-console/SKILL.md` 调试流程。
- 后台任务独占会话写操作，最多 4 个并发任务；提交 ID 去重、查询、等待与取消。
- SSH SFTP / Linux 串口分块上传，核对 SHA-256 后替换目标文件；命令唯一边界与真实退出码；文本及 JSON 字段断言。
- 任务计划、步骤耗时、断言与失败证据持久化；异常停止，不自动重复上传或回滚。

## 可选：改完程序后自动验证

已授权部署时，Agent 从项目获取产物路径、目标位置、重启命令及验收条件，生成计划并调用 `task_start`。后台按计划自动完成：

`预检 → 上传并校验 → 替换文件 → 程序重启 → 等待就绪 → 验证本次修改`

Agent 使用 `task_wait` 等待结果，仅在失败或需要判断时分析证据。固定步骤之间没有模型往返。分析与修改代码由 Agent 负责；固定执行、超时与断言由 MCP 负责。编译仍使用工程原有工具链。

参见 [任务结构与格式](docs/tasks.md)、[部署计划示例](examples/deploy-linux.json) 和 [只读检查示例](examples/inspect-linux.json)。示例的路径、哈希、命令和断言必须按实际项目配置；缺失信息在执行前通过 `task_validate` 返回 `needs_input`，由 Agent 询问。

## 本机安装与加载

当前开发目录使用 Python 3.11，依赖安装在本项目 `.venv` 内。Windows 执行：

```powershell
.\setup.ps1
```

脚本只安装依赖和生成本地连接配置，不连接硬件、不运行测试。依赖已安装时可用 `-SkipDependencies`。它会保留其他 MCP 设置；已有同名配置不同则停止并要求检查，不覆盖。

生成的 `.codex/config.toml` 注册项目级 `board_console` MCP。根据 [官方配置说明](https://learn.chatgpt.com/docs/extend/mcp?surface=app)，项目需要被信任；桌面客户端可在 Settings → MCP servers 中管理服务并选择 Restart，使用 `/mcp` 查看连接状态。MCP 注册配置不等于启动或接入成功，宿主必须实际提供 `session_exchange`、`serial_open` 等工具。本机配置直接启动 Python 服务，不需要外层 PowerShell。

2026-11-10 本次检查：项目已被信任、配置存在，但当前聊天仍未提供 Board Console 工具，且没有可调用的重载工具。未确认宿主加载缺口的具体原因。此前独立 SDK 客户端的成功不等于原生工具接入；不要将临时客户端脚本作为日常操作方案。

`.mcp.json` 是其他本地 MCP 客户端/兼容插件格式的配置；`.codex-plugin/plugin.json` 提供本地开发版清单。生成配置包含本机绝对路径，**目前不是可直接分发到另一台机器的安装包**。移动项目或换机器后需检查原配置并重新执行 setup。项目 MCP 与插件模式二选一，不要启动两个实例抢占同一串口。

不需要 OpenAI API Key；由已有 coding Agent 调用这些工具。

## 连接开发板后怎么用

可以直接对已加载插件的 Agent 说：

> 连接我的开发板，持续读取启动日志，定位报错原因。缺少连接信息时再问我。

预期流程：

1. `connection_prepare` 列出必要问题和候选端口，由 Agent 提问。协议文档不是连接前提。
2. 确定目标串口与波特率后 `serial_open`，返回 `session_id` / `capture_id`。
3. `session_read(after=0)` 读取启动输出，之后用返回的 `next_cursor` 持续读取。
4. 根据设备实际接口，用 `session_exchange` 转发已授权的命令或字节，检查输出并继续交互。若只是日志输出或未知二进制接口，先结合代码和文档判断，不盲发 `help`。
5. 报错用 `capture_search` 查找，结合 `workspace_read` 和宿主代码工具分析。
6. 调试过程中复用连接，不因一次短查询结束就关闭；明确结束调试或需要释放端口时 `session_close`，随后可以 `capture_replay` 离线重看。

MCP 返回的 `questions` 不会自己弹出对话框。服务 instructions、工具说明和 Skill 都要求宿主 Agent 将这些问题交给用户，并复用已有答案。

## 工具索引

当前源码注册 26 个工具。0.2 的任务提交/等待已有基础只读命令实测；0.3 的 `session_exchange` 已完成 RK3566 串口基础交互实测。

| 工具 | 用途 |
| --- | --- |
| `connection_prepare` | 只检查连接信息，不连接；缺失时返回 needs_input |
| `serial_list` / `serial_open` | 枚举 USB 身份、打开持续串口 |
| `ssh_open` | 打开持续 SSH 交互终端 |
| `session_list` / `session_read` | 查看会话、按游标读取增量事件 |
| `session_exchange` | 主要交互入口：原样发送并收取原始输出；省略 data 可续读 |
| `session_send` / `console_run` | 原始收发或发送命令并等待字面量输出 |
| `session_expect` | 从指定游标等待文本/hex，最长 30 秒 |
| `session_interrupt` / `serial_control` | Ctrl+C 或明确设置控制线 |
| `session_close` | 停止采集、释放连接、保留录制 |
| `capture_list` / `capture_read` / `capture_search` | 历史录制分页读取与关键词检索 |
| `capture_replay` | 只读离线回放；speed=1 原始时间，0 尽快回放 |
| `workspace_read` / `ssh_read_file` | 本地工作区/远程文件分页读取 |
| `task_schema` / `task_validate` | 计划 Schema、缺失信息澄清、本地产物校验；不写设备 |
| `task_start` | 一次提交部署与验证计划，后台执行 |
| `task_status` / `task_wait` | 任务状态、断言、耗时与失败摘要；单次等待最多 30 秒 |
| `task_events` / `task_cancel` | 任务时间线、请求取消；取消不回滚 |

还有 `board-console://guide` 提供调试 Skill、`board-console://interaction` 提供直接交互说明、`board-console://tasks` 提供可选任务说明。

文本发送不隐式添加换行；`console_run` 必须指定 `newline=lf/cr/crlf/none`。hex 模式将结束符直接写进 hex。匹配是字面量，不支持正则；不会解释 ANSI 控制序列，也不提供完整 TUI 屏幕。

`session_read` 和 `capture_read` 返回 `events`、`rx_text`、`next_cursor`、`has_more`。事件中 `hex` 是原始字节，`text` 是增量解码文本：一个字符跨块时，文本出现在字符完整的那个事件中。游标单位是事件序号，不是字节偏移；多个读取者可使用独立游标。文件读取的 offset 则是字节偏移。

`capture_search` 仅搜索 RX 文本，每个事件最多报告一个匹配，`ending_seq` 标识匹配结束的事件。空结果只覆盖本次扫描范围，`has_more=true` 时还需继续。需要精确核对时读取原始事件。

## SSH

初版使用 Paramiko，在 Windows 原生运行，不依赖本地 PTY。使用实际 host、username；认证使用现有密钥/SSH agent，或通过 `password_env` 指定**服务进程已有的环境变量名**。密码不作为工具参数传递。默认读取用户 `.ssh/known_hosts`，也可显式指定路径；未知或变化的主机密钥会被拒绝，需先通过正常 SSH 流程核实指纹。

尚不自动读取 OpenSSH `Host` 别名、ProxyJump、ProxyCommand，也没有跳板机支持。终端会合并 stdout/stderr；底层 `console_run` 不自动返回命令退出码，0.2 任务层通过独立 Shell 标记获取退出码。SFTP 文件工具使用远程绝对 POSIX 路径。

如果板子连接在远程机器的串口上，这一版可以通过 SSH 执行远端已有串口工具，但尚未提供专门的远程原始串口代理；PTY 输出不能当作无损二进制通道。

## 录制与回放

默认位置：`.board-console/captures/<capture_id>.sqlite3`，已加入 Git 忽略。每个录制包含连接参数、USB 身份（可获取时）、编码、RX/TX 字节和事件。`time_ns` 是 Unix 时间，`elapsed_ns` 是本会话单调时钟的相对时间。

回放读取启动回放时已有的事件快照。新的 replay session 可用同样的 read/expect 工具，TX 只展示历史记录，绝不重新发送到真机。录制中的 TX 自动重发、协议对端模拟、MCU 仿真均未包含在 0.2 中；任务计划是单独提交的操作流程。

单次录制在原始字节、文本与事件开销的累计估算达到 256 MiB 时停止并报告错误；数据库实际文件可能更大。暂未实现自动轮转。每次查询有输出预算，避免把全部日志塞进模型上下文。它是初版调试采集器，尚未验证高速持续流量下的丢字节情况，不能作为高吞吐采集仪的保证。

录制包含终端原始输入输出，不做字节级脱敏，也不加密；不要通过 session_send 输入需要保密的密码，SSH 登录应使用现有认证机制。MCP 进程结束会释放连接，磁盘录制保留；不保证跨进程保持会话或在 Agent 结束后自动唤醒它。

## 和编译、烧录配合

0.2 增加 Linux 程序上传与重启验证，但不自带芯片专用烧录器，也不负责整板 reboot 后重连。已获得相应操作授权时，Agent 可使用宿主终端调用工程现有工具链：

`读取日志 → 修改代码 → 关闭串口并确认释放 → 编译/烧录 → 重新枚举核对设备 → 连接验证`

不能把“允许连接/读日志”当成“允许烧录”。DTR/RTS 的含义依赖接线，不猜复位时序。打开串口前设置 DTR/RTS=false 也不能完全阻止某些 USB 驱动的瞬时跳变。

`sent` 表示传输层接受字节，`matched` 表示找到输出，都不代表业务成功；`timeout` 不表示命令已停止。发送失败时可能部分生效，工具返回 `delivery_uncertain`，不得自动重发。断线不自动换串口或重连。

## 当前核验边界

2026-11-10 经用户授权，已通过独立 stdio MCP 客户端连接真实 RK3566 开发板完成首轮验证。连接参数为 COM5 / 1500000 bps / 8N1 / 无流控；板端返回 Linux 4.19.232、Buildroot，设备树型号为 `CXK7.2-Z TSPI-1F carrier`。

- 8 项基础检查通过：MCP 握手、18 个工具发现、缺失参数澄清、缺失参数不打开端口、技能资源读取、工作区文件读取、越界路径拒绝、录制目录查询。
- 15 项实板检查通过：打开串口、系统查询、8 条连续编号输出、跨块匹配、超时、跨调用续读匹配、中文日志检索、跨块搜索、Ctrl+C、中断后继续执行、游标分页、离线回放一致性、回放拒绝写入、端口释放后重连、所有会话结束。
- 主录制 `06757de521f6469e914bae1ca8425870` 共 73 个事件，RX 4013 字节，TX 530 字节。回放 `be07d98dee6c4bd1a9d9d3b398b45a55` 逐事件核对原始字节、文本、原始时间戳和元数据一致。
- 首次端口被占用时正确返回 Windows 拒绝访问，服务保持可调用并保存失败记录；释放占用后连接成功。

本地证据保存在 `.board-console/reports/mcp-preflight.json`、`rk3566-run2-responses.jsonl` 和 `rk3566-summary.json`；录制在 `.board-console/captures/`。这些本机数据不进入 Git。

本次通过 MCP 客户端调用真实服务，不等于当前 Codex 聊天已原生加载这些工具。SSH、物理拔插、长时间高速流量完整性、编译烧录、原始速度回放时序精度仍未验证。测试未修改固件或板端文件；串口与测试客户端已关闭。

后续 0.2 基础命令实测通过：同一任务执行 `ifconfig` 约 31 毫秒，执行 `pwd; cd Project && pwd && ls -la` 约 32 毫秒，任务耗时约 63 毫秒。`ifconfig` 退出码为 0 但输出为空；`/Project` 成功列出 MediaApp、配置文件、lib 和 logs。独立客户端含启动、连接、关闭共约 1.66 秒。证据为 `.board-console/reports/basic-commands-1794291441951612000.json`。这不验证上传、程序重启、JSON 断言、任务取消或持久化去重。

0.3 开发阶段通过 Python 语法、配置、接口签名与 Skill 格式核验；随后经用户授权，使用 `session_exchange` 在同一串口会话逐条执行 `cd /`、`ls`、`cd Project`、`ls`，四次服务端收发耗时分别为 125、141、125、141 毫秒，合计 532 毫秒，含每次 100 毫秒静默窗口，确认目录状态跨调用保留。之后完成只读网络配置查询，并原样返回缺少 `/etc/resolv.conf` 的设备错误。证据分别为 `.board-console/reports/direct-navigation-1794293455326-responses.jsonl` 和 `network-config-1794293775855-responses.jsonl`；源码发行包不包含这些录制与本机报告。续读边界、发送失败、SSH 和新电脑安装仍未实测，不能用上述结果承诺所有设备的性能。

## 实现参考

- [MCP Python SDK v1 文档](https://py.sdk.modelcontextprotocol.io/v1/)：初版固定使用 1.30.0 维护分支。
- [pySerial API](https://pyserial.readthedocs.io/en/latest/pyserial_api.html)：串口、超时和控制线。
- [Paramiko Client API](https://docs.paramiko.org/en/stable/api/client.html)：SSH、PTY 与 SFTP。
- [Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)：项目级 MCP 配置。

