# 安装与配置

Board Console 是由 Agent 启动的 STDIO MCP 服务。安装完成后，每次调用复用当前实例的连接；不需要常驻服务，不修改永久环境变量。

## 通用源码安装

准备 Python 3.11 或更新版本，克隆仓库到长期保留的位置：

```sh
git clone https://github.com/kdkdaa/serial-ssh-MCP.git
cd serial-ssh-MCP
python install.py
```

Linux/macOS 可使用 `python3`，Windows 可使用 `py -3.11`。安装器使用标准库 `venv` 创建仓库下的 `.venv`，再安装固定版本的直接依赖。若系统未提供 venv/pip，需要先通过系统的 Python 安装方式补齐。部分平台的依赖没有预编译包时，pip 可能需要对应编译工具链。

通用模式只输出配置，不修改任何 Agent 设置。例如：

```json
{
  "mcpServers": {
    "board_console": {
      "command": "/absolute/path/serial-ssh-MCP/.venv/bin/python",
      "args": ["/absolute/path/serial-ssh-MCP/launch.py", "--state-root", "/home/user/.board-console"]
    }
  }
}
```

使用安装器输出的实际路径；Windows 的解释器位于 `.venv/Scripts/python.exe`。有些宿主使用不同配置格式或界面，只需对应填写 STDIO 的 `command` 与参数数组 `args`。无需先激活虚拟环境，不要把安装命令填作 MCP 启动命令。

已安装依赖时可运行 `python install.py --skip-dependencies` 重新输出配置。迁移机器应重新安装，不复制 `.venv`。日常启动不联网安装依赖；用户仍需保留源码目录和创建虚拟环境所用的基础 Python。

## Codex 自动注册

```sh
python install.py --codex
```

显式使用 `--codex` 才修改 Codex 用户配置：`$CODEX_HOME/config.toml`，未设置时为 `~/.codex/config.toml`。同时生成本机 `.mcp.json`，不进入 Git。默认数据根目录为该 Codex 用户目录下的 `board-console/`。

已有依赖可加 `--skip-dependencies`。迁移安装位置而需要替换旧同名注册时加 `--replace-existing`；默认保留并拒绝覆盖无法识别的注册。已有启用状态、超时和其他服务器配置会保留。

注册后让宿主重新启动该 MCP，并用 `server_status` 核对实际版本、工作目录和数据位置。不要同时通过用户配置和插件导入重复启动服务，以免争用串口。

源码注册可用虚拟环境 Python 执行 `launch.py configure --check`，只检查拟议配置，不写入、不启动服务。自动注册针对 Codex；其他 Agent 使用通用模式输出的配置。

## 运行位置与平台

- 串口驱动、端口权限和设备必须对 MCP 进程所在系统可见。Linux 可能需要系统授予串口设备访问权限；容器/远程执行环境需要明确的设备映射或远端工具。
- SSH 从 MCP 所在机器访问目标主机；它不自动把远程电脑的 USB 串口映射为本地设备。
- 串口、SSH 和源码安装入口不依赖 PowerShell。板卡的专用启动配方可以有平台限制，`platforms` 按 `win32`、`linux`、`darwin` 区分。
- 内置 ESP32-P4X 的 `idf_monitor` 配方使用 Windows `idf.cmd`。Linux/macOS 应增加使用对应项目工具链的配方，不能直接运行该 Windows 配方。

当前修改经过静态检查；Linux/macOS 的实际安装、依赖兼容性和硬件访问尚未实测。

## 更新与 Windows 可选入口

源码更新后重新运行 `python install.py`，Codex 用户加 `--codex`。重启 MCP 后加载新代码，不在仍占用设备时强制切换版本。

`setup.ps1` 保留为 Windows 源码安装及 Codex 注册的便捷入口。`install.ps1` 仅用于已经构建的 Windows 独立发布目录，不是仓库源码安装入口。

维护者可在已安装依赖及 PyInstaller 的 Windows 构建环境运行 `scripts/build-release.ps1`。生成的 `dist/board-console/` 包含运行时，需完整保留 `board-console.exe`、`_internal/`、`release.json` 及安装脚本；目标机器无需另装 Python。该构建配方不是跨平台二进制发行，仓库不附带旧 ZIP，发布包应通过 GitHub Releases 分发。

## 数据与板卡扩展

通用安装器输出的默认数据根目录为 `~/.board-console`；Codex 自动注册默认使用 `<Codex用户目录>/board-console`。可通过启动参数 `--state-root` 指定其他位置。

```text
数据根目录/
  boards/                         用户板卡配置
  projects/<工作目录哈希>/
    ssh_known_hosts               此项目的主机身份记录
    instances/<实例ID>/
      instance.json
      captures/
      tasks/
```

项目默认取宿主提供的工作目录，也可用 `--workspace` 明确指定。全局注册不会自动获知宿主未传入的项目路径；用 `server_status` 核对实际 workspace。目录分区不是权限沙箱。

活连接、任务和请求去重状态只属于当前进程。`capture_list(scope="project")` 可查本项目历史录制，不能借此控制其他进程的连接。关闭会话后保留录制；磁盘数据不自动轮转。

板卡配置可放在数据根目录 `boards/`，或用 `--profiles-dir` 指定；用户同名配置优先，无效配置报错，不静默回退。见 [板卡扩展](docs/boards.md)。
