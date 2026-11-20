# Board Console MCP

供 coding Agent 使用的串口、SSH 与开发板工具进程接口。Agent 读取项目源码和协议说明，发送命令或原始字节，再根据设备返回的日志与错误继续调试。MCP 不判断设备命令语法，也不绑定固定协议。

## 功能

- **持续连接**：串口、SSH 终端和本机工具进程；同一会话保留目录和交互状态。
- **直接收发**：文本、十六进制、显式换行符、增量读取、等待输出与中断；命令及错误原样返回。
- **连接准备**：枚举串口与 USB 身份，缺失参数返回待确认信息；SSH 支持密码、密钥和主机指纹核验。
- **日志与回放**：自动录制双向数据，分页查询、搜索和离线回放；回放不会向设备重发指令。
- **部署验证**：可选的 Linux 文件上传、哈希核对、程序重启、就绪等待和断言任务。
- **板卡扩展**：独立 JSON 描述连接、转发参数及专用工具启动方式，无需为每种板型新增 MCP 工具。

Agent 通过 STDIO 启动服务，同一实例复用连接；不安装常驻服务，不跨聊天共享活连接。数据按项目目录和实例分区，这种分区不是安全沙箱。

## 安装

源码安装需要 **Python 3.11 或更新版本**。在运行 MCP 的电脑上克隆仓库，然后安装：

```sh
git clone https://github.com/kdkdaa/serial-ssh-MCP.git
cd serial-ssh-MCP
python install.py
```

Linux/macOS 通常使用 `python3 install.py`，Windows 可用 `py -3.11 install.py`。安装器创建独立 `.venv`、安装依赖并输出通用 STDIO 配置；将其中的 `command` 和 `args` 加入所用 Agent 的 MCP 设置。不同宿主的配置外层格式可能不同，路径使用安装器输出的实际值。

需要自动注册到 Codex 用户配置时：

```sh
python install.py --codex
```

日常调用直接启动已安装环境，不运行安装脚本或下载依赖。安装及设备访问发生在 **MCP 进程所在机器**；远程或容器中的 Agent 不能凭配置直接访问另一台电脑的 USB 串口。

PowerShell 脚本仅作为 Windows 的可选入口。源码提供跨平台入口，Windows 独立二进制构建另见 [安装说明](INSTALL.md)；Linux/macOS 的实际安装和硬件运行尚未验证。不需要 OpenAI API Key。

## 使用

可以对已加载 MCP 的 Agent 说：

> 连接这个项目的开发板，读取启动日志并分析报错。先从项目资料确认连接方式，缺失的信息再问我。

典型流程：

1. `board_profiles` / `connection_prepare` 确认接口和参数。
2. `serial_open`、`ssh_open`，或 `board_startup_prepare` → `process_open` 建立会话。
3. `session_exchange` 发送内容并收取输出；省略 `data`、携带游标可继续读日志。
4. Agent 结合源码分析反馈，调整下一次发送内容；调试结束调用 `session_close`。

例如，对已确认使用 CR 行结束符的 Linux 串口终端调用 `session_exchange`：

```json
{"session_id":"实际会话ID","data":"ls -la","newline":"cr"}
```

MCP 返回缺失参数时，由 Agent 向用户提问；发送成功、收到输出和业务验证成功是不同状态。长操作继续读取，不因一次等待超时就重复发送。

## 添加板卡

在用户数据根目录的 `boards/` 下添加 `<profile_id>.json`；向仓库贡献通用配置时放入仓库 `boards/`。用户同名文件优先，升级程序不会覆盖用户扩展。

```json
{
  "schema_version": 1,
  "name": "Example UART Board",
  "description": "适用于已确认采用 115200 / 8N1 的 UART 控制台",
  "connector": "USB 转串口连接板端 UART",
  "connection": {"transport": "serial", "baudrate": 115200},
  "forwarding": {"encoding": "utf-8", "data_format": "text", "newline": "none"},
  "notes": ["实际端口和发送换行符在连接时确认"],
  "sources": ["填写板卡手册或项目配置的依据"]
}
```

文件名作为 `profile_id`，下一次调用即可读取，无需重启。配置只记录连接依据，不写本机端口、密码或固件命令列表。专用 monitor、烧录器等可通过 `startups` 声明分平台启动配方；未实现的传输类型不能仅靠 JSON 添加。完整字段和步骤见 [板卡扩展](docs/boards.md)。

## 详细说明

- [安装、宿主配置与数据隔离](INSTALL.md)
- [直接交互与游标读取](docs/interaction.md)
- [SSH 认证与主机身份](docs/ssh.md)
- [专用工具进程](docs/processes.md)
- [部署验证任务](docs/tasks.md) · [部署示例](examples/deploy-linux.json)

终端录制保留原始收发内容，不脱敏、不加密。SSH 登录使用认证参数或密钥；通过终端发送的密码会进入录制。编译、烧录使用项目自己的工具链，需要相应操作授权；本项目不提供 MCU 仿真或通用芯片烧录器。
