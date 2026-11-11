# 专用工具与自主调试接口（0.6）

Agent 决定启动什么工具、发送什么内容、怎样判断错误；MCP 负责持有进程和连接、转发输入、录制输出并返回事实。固件命令不注册到 MCP，也不由 MCP 验证语法。

## 工具进程也是会话

`process_open(argv, cwd, env, resource_port, input_target, required_files)` 启动本机工具，立即返回 `session_id`。`argv` 是可执行文件和独立参数数组；Shell 语法需要显式启动对应 Shell。批处理入口由板卡配方描述。`env` 只覆盖子进程环境，值为 null 表示删除该变量，不修改系统或当前 MCP 的环境。

后续统一使用 `session_exchange`、`session_send`、`session_read`、`session_expect` 和录制/回放接口。`session_close` 停止所属进程树；Windows 用监督进程持有 Job，POSIX 用独立进程组。监督进程还监视 MCP 父进程，避免 MCP 异常退出后工具继续占用端口。主动关闭可能截断最后尚未录入的输出；需要完整结束日志时先让工具正常退出，再读到终态。

输入原样到工具的 stdin，输出是工具 stdout/stderr 合流的字节。这里是管道，不是 PTY：不提供终端尺寸、交互桌面或 OS 信号模拟。工具可能自行缓冲或拒绝非终端输入，相关错误原样保留。`session_interrupt` 仍只发送 0x03；如工具不把它作为中断，使用其退出协议或 `session_close`。进程会话不接入要求 POSIX Shell 的 `task_*` 部署执行器。

占用串口的工具应传 `resource_port`。同一 MCP 实例内已有串口或工具会话占用时返回 `resource_busy`，不关闭他人的连接、不自动切换。其他聊天/外部软件的占用由操作系统及工具返回，MCP 不扫描并终止外部进程。未声明 resource_port 的任意工具无法提前推断其硬件占用。

`argv`、工作目录及环境变量名会录入会话元数据，环境变量值不主动持久化。宿主仍可能记录调用参数，工具也可能把变量打印到日志；不要把密码写入 argv。

## 板卡配方

`boards/<id>.json` 的可选 `startups` 保存命名的主机工具入口：`argv`、`env`、`required_files`、支持的 `platforms`、`input_target` 和 `input_notes`。配方中的 `{project_dir}`、`{port}` 由显式参数提供，其他简单占位符来自 `parameters`；波特率默认来自板卡 connection。不要把外部参数直接插入 Shell 代码，优先作为独立 argv 参数或子进程环境变量传入。

1. `board_profiles(profile_id=...)` 查看入口及输入说明。
2. `board_startup_prepare(profile_id, startup, project_dir, port, parameters)` 解析参数、检查必要文件，不启动任何进程。缺参数返回 `needs_input`；缺文件返回 `missing_prerequisites`，不自动安装或构建。
3. 将 `ready.open_args` 交给 `process_open`。启动前再次核对必要文件。配置是入口描述，不是可信代码沙箱；仍按当前项目的授权运行工具。
4. 从 `after=0` 读取启动输出，再按 `next_cursor` 续读。结合工具错误、进程退出码及设备输出选择后续操作。

原有 `connection_prepare → serial_open/ssh_open` 保留；准备结果会提示可用的专用启动入口。不同项目仅提供实际路径和连接参数，板卡入口全局共享。

## ESP32-P4X J19

已有 `idf_monitor` 配方调用实际项目的 `idf.cmd -p PORT -b BAUD monitor --no-reset`，由该项目入口选择 ESP-IDF 和 Python 环境，不调用桌面旧 ESP-IDF 快捷方式。本地项目入口使用健康的 `idf5.5.4_py3.11_env`；旧快捷方式环境曾因 `esptool 4.12.dev1` 不满足 `~=4.12` 激活失败，两者不能混为一谈。

配方要求存在 `idf.cmd` 和 `build/project_description.json`：当前 ESP-IDF 5.5.3 的 monitor 在缺项目描述时可能调用 CMake，因此缺文件直接返回，不为看日志隐式构建。文件存在只代表前置材料齐全，不能证明产物对应当前源码或环境可用。

本机 IDF Monitor 1.10.0 支持管道控制命令，`input_target=tool_control`：

- `session_exchange(data="send <设备所需的一行内容>", newline="lf")`：由 monitor 向设备发送一行，具体行尾由 monitor 配置处理。
- `session_exchange(data="reset", newline="lf")`：显式调用官方 monitor 的复位操作；仅在已授权的调试范围内使用。
- `session_exchange(data="exit", newline="lf")`：请求退出，随后继续读取退出结果。

上述是主机 monitor 的接口说明，不是固件命令列表。其他版本需核对支持情况。直接串口仍按板卡原始转发参数工作；需要任意二进制字节时关闭 monitor，再 `serial_open`。原始串口读取本身不需要激活 ESP-IDF。

## 静默、控制线和重连

`session_list/read` 及 `session_exchange.diagnostics` 提供累计 RX/TX 字节数、最近收发时间、`output_source`、`input_target`、进程 PID/退出码。进程会话的 RX 可能是环境激活日志，不能当作板端日志。`device_ready` 始终保持 unknown，具体就绪证据由 Agent 判断。

一次短监听为零字节后，先检查启动方式、工具错误、设备身份和配置。只有存在等待依据时才继续等待，不默认反复静默监听，也不立即要求用户手动按 RESET。

原始串口需要控制线时，`serial_sequence` 接受明确的 `dtr/rts/delay_seconds` 步骤，返回已完成步骤和操作前游标；读取线程在延迟期间继续采集。`reapply_dtr_after_rts` 为可选的 Windows usbser 控制线重发机制，不是复位方案。任何步骤失败均保留部分执行事实，不自动重试或回滚。

`serial_reconnect` 显式关闭指定的旧串口会话、重新枚举并使用记录的 VID/PID/序列号匹配。多个接口需明确端口；没有唯一 USB 身份时需提供已确认端口；已知身份不匹配不会换到其他板子。返回新 session_id、新游标，不重放旧 TX。打开端口仍可能触发驱动跳变。

当前 0.6 仅完成静态核验；新增进程生命周期、管道交互、控制线及重连尚未进行运行或实板验证。
