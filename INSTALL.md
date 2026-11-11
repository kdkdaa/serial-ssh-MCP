# 在另一台 Windows 电脑上安装

这是 Board Console 0.4.0 源码的安装说明，项目包含 MCP 服务、Skill、安装脚本和说明。此前生成的 0.3.0 ZIP 不包含 0.4 的 SSH 改动，需要使用更新后的源码。源码发行包不包含 Python 运行环境、第三方依赖、本机 MCP 配置、SSH 凭据或设备录制数据。

## 安装

1. 安装 Python 3.11，并包含 Python Launcher（`py` 命令）。首次安装依赖需要访问 Python 包源。
2. 将源码完整放到准备长期使用的目录，例如 `D:\Tools\board-console`；如果来源是 ZIP，请先完整解压。保留 `.agents`、`.codex-plugin` 等点号开头的目录，不要直接在 ZIP 内运行脚本。
3. 在解压目录打开 PowerShell，执行：

   ```powershell
   .\setup.ps1
   ```

   如果当前 PowerShell 执行策略阻止运行脚本，可仅对这次安装进程使用：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
   ```

4. 脚本会创建 `.venv`、安装 `requirements.txt` 中的依赖，并按新电脑的实际路径生成 `.codex/config.toml` 与 `.mcp.json`。安装不会连接设备或执行设备命令。
5. 在使用的 Agent 客户端中加载 MCP。Codex 可使用受信任项目中的 `.codex/config.toml`；其他支持 STDIO MCP 的客户端可参考生成的 `.mcp.json`。若使用插件导入，也应先执行安装，再导入这个已生成配置的文件夹。只有工具实际可见、可调用，才代表接入成功。

## 首次使用

- 串口需要操作系统已识别 USB 转串口设备；用 `serial_list` 枚举，再确认目标端口和波特率，不要照搬其他电脑的 COM 编号。
- 打开连接后，使用 `session_exchange` 发送命令或字节并读取反馈。详细参数见 `docs/interaction.md`。
- SSH 可通过 ssh_open 的 password 或现有密钥登录，无需修改环境变量；首次主机身份可用 ssh_probe 查看并确认，之后保存到插件的信任文件。详见 `docs/ssh.md`，不复制他人的 SSH 凭据。
- 同一设备只交给一个服务实例使用，避免项目 MCP 与插件 MCP 同时占用串口。

## 工程位置与迁移

默认 `--workspace` 是解压目录，本地文件读取与任务上传受此目录范围约束。若要用于另一个代码工程，在客户端 MCP 配置中把 `--workspace` 后的路径改为该工程目录；Python 可执行文件及 launch.py 仍指向本安装目录。若使用项目级配置，需要让客户端在实际工程中加载相应 MCP 配置；Skill 也需要由客户端加载。

安装完成后不要直接搬动 `.venv` 和已生成的绝对路径配置。换路径或换电脑时，使用本源码 ZIP 重新解压安装。脚本发现已有同名但不同的配置会停止并保留原文件，不会静默覆盖。

## 已验证范围

已在本开发电脑通过独立 MCP 客户端完成 RK3566 串口交互，包括 cd /、ls、cd Project、ls 的跨调用目录状态，以及只读网络配置查询。新电脑的安装、原生客户端接入、SSH、Windows 远端终端和 STM32 自定义协议尚未实测；部署上传和程序重启也未完成实板验证。

本包提供 Windows 安装脚本，尚未提供经过验证的 Linux/macOS 安装流程。它不是包含全部依赖的离线安装包，也不是双击即用的 EXE。
