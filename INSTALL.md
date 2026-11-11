# 在另一台 Windows 电脑上安装

这是 Board Console 0.6.0 源码的安装说明。默认把 MCP 注册到 Codex 用户配置，在任意项目使用同一组工具。此前 ZIP 不包含本次改动，应使用更新后的源码；发行包不包含虚拟环境、本机配置、SSH 凭据或录制数据。

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

4. 脚本创建 `.venv`、安装依赖，把 `board_console` 写入已有 `$CODEX_HOME/config.toml`（未设置时为 `~/.codex/config.toml`），保留其他设置，不修改环境变量。自动移除本安装生成的旧项目级同名注册，另生成 `.mcp.json` 供其他客户端使用。安装不连接硬件。
5. 宿主重新加载 Board Console 后，在业务项目调用 `server_status`，确认版本 0.6.0 及实际 workspace，再调用板卡/连接工具。运行中的旧进程不会自动更新 Python 代码；工具目录刷新以实际返回为准。无需进入插件目录或给每个项目复制配置。用户级注册后不再重复导入同名插件。

## 首次使用

- 串口需要操作系统已识别 USB 转串口设备；用 `serial_list` 枚举，再确认目标端口和波特率，不要照搬其他电脑的 COM 编号。
- 保留安装目录的 `boards/`。特殊板卡可通过 `board_profiles` 选择独立连接文件，再调用 `connection_prepare(profile_id=...)`；文件不含本机端口或凭据，扩展方式见 `docs/boards.md`。
- 打开连接后，使用 `session_exchange` 发送命令或字节并读取反馈。详细参数见 `docs/interaction.md`。
- SSH 可通过 ssh_open 的 password 或现有密钥登录，无需修改环境变量；首次主机身份可用 ssh_probe 查看并确认，之后保存到插件的信任文件。详见 `docs/ssh.md`，不复制他人的 SSH 凭据。
- 同一设备只交给一个服务实例使用，避免项目 MCP 与插件 MCP 同时占用串口。

## 工程位置与迁移

全局配置只固定 Python、launch.py 和用户数据根目录，不固定 `cwd` 或 `--workspace`。启动器使用宿主实际传入的工作目录作为项目上下文；板卡文件始终从安装目录读取。连接时显式传串口或 SSH 主机，会话 ID 仅在创建它的服务进程有效，不跨聊天复用。

录制、任务记录及本插件保存的 SSH 信任记录位于用户数据根目录中按工作目录绝对路径哈希划分的目录。切换项目无需重新安装；同一串口不能被多个进程同时打开，冲突会返回打开失败，不抢占。目录分区不是安全沙箱，同项目不同聊天可查看同目录的历史录制。

`server_status.workspace` 是实际值，不假定所有客户端都会正确传入项目目录。第三方宿主若不传项目上下文，应配置其启动工作目录；通信本身不依赖源码目录。`workspace_read` 和可选任务上传仍限制在实际 workspace，其他源码优先由 Agent 的项目文件工具读取。通用使用说明通过 `board-console://guide` 提供，无需在业务项目安装 Skill。

宿主使用的项目目录必须存在。工程搬迁后，旧聊天如果仍绑定已删除目录，需要在宿主中使用有效工程目录；全局注册不会修复宿主的失效工作目录。

安装完成后不要直接搬动 `.venv` 和绝对路径配置。换机器后从源码重新安装；迁移安装位置时检查用户级旧注册。脚本只自动迁移能明确识别为本安装旧版本的条目，未知的同名配置会保留并报错。

## 已验证范围

已在本开发电脑通过独立 MCP 客户端完成 RK3566 串口交互，包括 cd /、ls、cd Project、ls 的跨调用目录状态，以及只读网络配置查询。新电脑的安装、原生客户端接入、SSH、Windows 远端终端和 STM32 自定义协议尚未实测；部署上传和程序重启也未完成实板验证。

本包提供 Windows 安装脚本，尚未提供经过验证的 Linux/macOS 安装流程。它不是包含全部依赖的离线安装包，也不是双击即用的 EXE。
