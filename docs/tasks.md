# 部署与验证任务（0.2）

一次提交计划，MCP 后台连续执行上传、程序重启、就绪检查和功能断言。固定步骤不等待模型逐条决策；失败后返回证据，由 Agent 结合代码分析原因、决定下一次改动。模型分析耗时与设备操作耗时由此分开。

这是可选的固定流程执行层。日常 Linux/Windows 终端或 MCU 交互使用 [直接通道](interaction.md)，不要求计划、verify 步骤或命令包装。

## 模块职责

| 层 | 文件 | 职责 |
| --- | --- | --- |
| Agent / Skill | `.agents/skills/board-console/SKILL.md` | 读取需求与源码、修改程序、形成计划及本次修改的断言、分析失败 |
| MCP 接口 | `board_console/server.py` | 提交、等待、查询、取消；缺失信息返回结构化问题 |
| 计划模型 | `board_console/task_models.py` | 步骤类型、参数、期限和必需验证项 |
| 任务执行 | `board_console/tasks.py` | 后台顺序推进、会话独占、去重、状态与证据持久化 |
| 命令与断言 | `board_console/execution.py` | 唯一边界、真实退出码、文本和 JSON 断言 |
| 上传 | `board_console/deploy.py` | 本地哈希、SFTP / 串口传输、远端哈希、文件替换 |
| 会话与传输 | `sessions.py` / `transports.py` | 串口 / SSH、后台接收、互斥写入、断线状态 |
| 证据 | `capture.py` | RX/TX 原始字节、游标检索、离线回放 |

## 调用流程

1. 复用已经确认目标的 `session_id`。尚未连接时走 `connection_prepare` 和打开连接流程；任务要求已经登录的 POSIX Shell。它不会自动输入用户名或密码。
2. Agent 从项目与已有对话获取产物、目标路径、重启方式、就绪探测和验收条件。缺失时调用 `task_validate`，它允许省略会话或计划并返回 `needs_input`；宿主只提问真正无法从已有信息解决的部分。非法字段返回 `invalid_plan`。完整计划的校验也检查当前会话和本地产物，不进行设备 I/O。
3. 使用 `task_schema` 获取 JSON Schema，或参考 `examples/deploy-linux.json`。必要时先校验；完整信息已知时直接 `task_start(session_id, plan, request_id)`，入口本身会校验，无需每轮额外调用 validate。
4. `task_start` 在本地校验与预留任务后返回，设备操作在后台执行。用 `task_wait(task_id, timeout_seconds=30)` 等待，完成会立即返回；等待超时只返回当前状态，不取消任务。无需每条命令后读日志、匹配提示符再调用模型。
5. 失败时读取 `failure.evidence_path`；成功或失败均可读取 `steps[].evidence_path`。默认这些文件在工作区内，可用 `workspace_read` 分页；若配置了工作区外 data_dir，使用宿主文件工具。`task_events` 提供事件时间线，`capture_read` 提供原始字节。

示例文件只用于说明格式，**未执行**。部署示例中的全零 SHA、产物、服务脚本和程序参数都是占位值，必须由项目实际值替换；工具不会替你创造这些接口。`inspect-linux.json` 是已登录 Linux Shell 上的只读检查计划，不是程序修改的验收计划。行结束符也必须按已确认的会话选择。

## 计划与断言

- 必填：`name`、`change_summary`、`newline`、`steps`。Shell 目前只支持 `posix`，会话编码须兼容 ASCII，如 UTF-8。
- `command`：指定 `phase=prepare/restart/verify/diagnose`、命令、超时与 `checks`。每条命令在独立 `sh -c` 中执行，上一条的 `cd` 或未导出的环境修改不会保留；需要时在该命令中明确工作目录。多条 Shell 命令应自行使用 `&&` 等传播错误，否则退出码只有最后一条的含义。
- `upload`：指定工作区内 `local_path`、绝对 POSIX `remote_path`、真实小写 `sha256`、权限 `mode`。串口单文件最多 1 MiB，SSH 最多 256 MiB；空文件不支持。产物须由已有工程工具链生成，本工具不编译。
- `wait_ready`：只使用可重复的只读探测。完整返回但断言不通过时，才在期限内按间隔再探测。单次探测超时、丢失连接或输出超限直接停止，不盲重试。
- `checks.exit_code` 默认 0；`contains` / `not_contains` 是大小写敏感文本断言。`json_equals` 将整段命令输出解析为一个 JSON 值，以 JSON pointer 定位，如 `/version`、`/items/0`；空字符串表示根值。比较区分类型，包括嵌套布尔值与数字，不支持正则或自动忽略日志前缀。
- 最多 32 步、名称唯一，至少一个 `phase=verify` 的命令。结构只能保证存在验证步骤，验证项是否覆盖修改由 Agent 负责。退出码为零不证明功能正确；版本、触发条件与预期结果应共同支撑结论。

默认整任务期限 180 秒、最大 1800 秒；每一步还有自己的期限。命令线编码后最多 3500 字节、单次事务收集输出最多 1 MiB。输出过大时停止并报告，原始 capture 仍可按游标检索。期限是协作式的；正在进行的驱动 I/O 或本地文件 I/O 可能导致实际返回稍晚，不能当作实时硬件看门狗。

## 上传与命令完成

上传前核对本地 SHA-256。SSH 使用 SFTP；串口使用 Linux Shell 的 `base64 -d` 分块传输。两者均要求目标有 `sha256sum`、`chmod`、`mv`，且目标目录已存在。任务使用目标目录内唯一 staging 文件，核对传输流和远端哈希后执行 chmod 与 mv 替换。整个计划不是事务：替换成功后重启或验证失败，不自动回滚。中途失败保留 staging 路径供分析；这些文件不会自动清理。

每条 Shell 事务输出随机开始/结束标记和退出码。标记中的控制字节由板端 printf 生成，命令回显中的转义文本不会被当成完成标志。串口和 SSH PTY 的 stdout/stderr、后台日志仍可能混合；该机制不是通道隔离，也不支持 TUI。严格 JSON 检查需要被测命令的输出窗口内没有其他日志混入。

任务持有整个会话的写锁，普通发送与控制线写入返回 busy。读取、检索与录制继续工作；显式中断和关闭仍可终止设备交互。不要从其他工具向同一任务插入命令。

## 状态、去重与取消

状态为 `queued → running → succeeded / failed / cancelled`，服务器重启后原非终态记录以 `interrupted` 返回，不自动续跑。

`request_id` 就是 `task_id`，为 1～64 个 ASCII 字母、数字、下划线或连字符，首字符为字母或数字，不能用 Windows 保留设备名，统一规范为小写。同 ID、同计划、同连接元数据的再次提交只返回原任务；计划或目标不同则拒绝。目标比对使用连接时保存的元数据，不能保证设备物理身份永不改变。失败任务也不会因重复提交而重跑；分析完设备现状后，用新 ID 才能提交新尝试。任务目录创建后若初始证据保存失败，也保留 ID 预留，避免盲目重复执行。

0.7 的去重范围是当前实例 data_dir 中保留的任务记录；这是防重复提交机制，不是设备操作的严格 exactly-once 保证。每次启动创建新实例，旧 request_id 不具有跨实例去重保证；不得在重启后不检查设备现状就再次提交旧部署。历史任务保留在旧实例目录，使用宿主文件工具读取，不恢复连接。每个实例最多同时运行 4 个任务，各使用自己的会话；多个实例互不共享设备连接。

`task_cancel` 请求在命令等待或传输块之间停止。取消不撤销已完成操作；取消已完成任务也不会改变结果。Shell 等待超时、取消或断线会尝试 Ctrl+C 并设置 `requires_reconnect`，阻止后续自动任务使用状态不明的会话。Ctrl+C 不保证停止后台进程；重新连接前需检查设备实际状态。SFTP 取消关闭本次传输通道，不保证移除已经写入的 staging 文件。

发送失败或发布完成标记丢失时，证据报告 `delivery_uncertain`；若不确定发生在最终发布命令，报告 `publication_may_have_occurred`。不得把未收到回执解释成未执行，也不得自动重发。

## 持久化证据与范围

默认 `.board-console/tasks/<task_id>/` 保存 `plan.json`、`result.json`、`events.jsonl`、`step-NN.json` 和失败时的 `failure.json`。结果包含任务与步骤耗时、断言、capture ID 和相关游标；失败命令包含输出尾部。终态之后可按需读取完整证据，避免每次状态查询返回整个日志。任务文件与原始录制一样可能包含敏感命令及输出，不做自动脱敏或轮转。

当前支持 Linux **程序重启**，不支持整板 reboot 后自动重连、芯片专用烧录、BootROM 下载、自定义二进制协议任务或硬件模拟。原始串口收发、录制和离线回放仍可用于非 Shell 设备。程序部署成功不等于烧录成功；离线回放也不等于设备重新执行成功。

0.2 已通过 RK3566 实板的两步基础只读任务：ifconfig 与进入 /Project 列目录，总耗时约 63 毫秒，证据在 `.board-console/reports/basic-commands-1794291441951612000.json`。上传、程序重启、JSON 断言、取消和同 ID 去重仍未运行验证。后续授权的实板验证应覆盖这些能力；SSH 与串口上传需分别验证。0.3 直接通道已完成基础目录操作与只读网络查询，详见 README 的核验记录。
