# Board Console 0.7 安装与升级

MCP 由 Agent 通过 STDIO 按需启动，一个进程持有自己的连接。每次命令复用原有连接，不启动新 MCP；宿主结束时释放连接和工具进程。不安装常驻服务，不跨聊天共享连接，不修改永久环境变量。

当前仓库已经提供独立发布包的构建与安装源码；0.7 尚未构建发布包，也未验证新安装流程。源码模式仍需要本机 Python。下面的免 Python 安装流程仅适用于维护者实际构建出的目录式发布包。

## 使用独立发布包

1. 完整解压发布目录，保留 `board-console.exe`、`_internal/`、`release.json` 和 `install.ps1`。
2. 在发布目录执行 `./install.ps1`。默认安装到 `%LOCALAPPDATA%/Programs/BoardConsole/versions/<版本>/`，校验清单中的文件后注册 Codex 用户级 MCP。
3. 第一次从其他安装位置迁移时，脚本可能保留并拒绝覆盖旧同名配置。明确要替换该注册时使用 `./install.ps1 -ReplaceExisting`。后续由新安装器登记过的版本可根据安装记录识别并升级。
4. 保留 `enabled`、超时、工具过滤、环境变量及其他用户设置。若原配置禁用该服务器，安装不会自动启用它。
5. 下次宿主启动 MCP 时使用新注册的可执行文件。正在运行的会话继续使用原版本，不在安装时强制停止或接管连接。实际加载版本以 `server_status.version` 为准。

发布程序包含其 Python 与依赖。用户无需安装 Python、pip、uv 或 PyInstaller；日常启动不安装依赖、不联网更新、不改 PATH。设备驱动及 ESP-IDF 等项目工具链仍由对应项目准备，不与 MCP 的运行时混用。

安装采用版本目录，不覆盖运行中的旧程序。同版本文件内容不一致时拒绝覆盖，应发布新版本。旧版本不自动删除，确认相关 Agent 会话结束后再清理；用户录制和板卡扩展不在版本目录中。安装记录、Codex 配置及 `.mcp.json` 分别原子替换，不保证多个文件构成一次事务；写入失败应保留已安装文件并重新执行安装。

## 从源码开发

1. 准备 Python 3.11 和 Python Launcher。
2. 在源码目录执行 `./setup.ps1`：创建项目 `.venv`、安装 requirements，再注册用户级 MCP。已有环境可用 `-SkipDependencies` 仅执行配置更新。
3. 安装位置迁移且明确替换旧注册时，使用 `-ReplaceExisting`。同位置重复安装不会因为已有 `enabled=true` 而失败。

源码模式仍依赖源码目录和本机基础 Python，不能把 `.venv` 当作独立发布包搬到其他电脑。已有调用路径 `python launch.py --state-root ...` 保持兼容。`python launch.py --version` 与 `python launch.py configure` 使用轻量入口，不导入完整 MCP/SSH 服务。

`python launch.py configure --check`（发布版为 `board-console.exe configure --check`）仅检查注册结果并列出将变动的文件，不写配置、不启动 MCP 或访问硬件。

## 维护者构建发布包

这是显式构建步骤，Agent 正常启动不会执行。先在构建环境安装 `requirements.txt` 中的依赖和 PyInstaller（本配方按 6.22 系列接口编写），再执行：

```powershell
./scripts/build-release.ps1
```

产物位于 `dist/board-console/`，可整体压缩发布。使用目录式打包以避免单文件启动时解包；worker 通过打包程序自身的内部入口启动，不再调用目标电脑的 Python。构建脚本生成文件 SHA-256 清单，并读取产物版本；这不代替新机器安装、运行或硬件验证。

当前构建配方面向 Windows，不是跨平台二进制发行。直接依赖已有固定版本，完整的可复现构建锁定与发布验证仍需在正式发行前完成。

## 项目与实例隔离

全局注册固定程序入口及用户数据根目录，不固定业务项目路径。项目上下文默认取宿主提供的工作目录，也可明确提供 `--workspace`；它不是安全沙箱。`server_status` 返回 workspace、project_data_dir、instance_id 和实际 data_dir。

默认数据结构：

```text
用户数据根目录/
  boards/                         用户板卡扩展
  projects/<工作目录哈希>/
    ssh_known_hosts               此项目已确认的 SSH 主机身份
    instances/<随机实例ID>/
      instance.json
      captures/
      tasks/
```

每次 MCP 启动创建新实例目录。session_id、task_id 和 task_start 的 request_id 在当前实例内使用；不跨实例复用、恢复或重放。新实例不继承旧实例的请求去重记录，不能在不检查实际设备状态的情况下重新提交旧部署。历史任务保留在原实例目录供文件工具读取。

`capture_list()` 默认只列当前实例，`scope="project"` 可显式查看本项目的历史录制，包括 0.6 的旧目录。历史 capture_id 可以用于 read/search/replay，但不能用于控制连接。非本实例的活动记录显示 owner_unknown，表示未知，不能据此声称另一个聊天已断线。

会话结束后关闭 SQLite 写入连接，后续查询临时以只读方式打开。当前实例最多保留约 128 个最近结束的会话对象，旧会话从内存回收后使用 capture 工具查询录制。日志仍使用原有单录制预算；本次没有改成日志轮转或无损无限采集。

多个聊天同时连接同一串口时，各自独立调用；操作系统返回占用错误，不共享连接、不抢占、不终止其他软件。SSH 会话同样由各自实例持有。

## 板卡与项目工具

内置板卡位于程序资源目录。通过 `--state-root` 注册时，用户板卡默认位于该根目录的 `boards/`；也可用 `--profiles-dir` 指定。用户同名文件覆盖内置默认值，无效覆盖文件返回配置错误，不静默回退。版本升级不覆盖用户板卡。

ESP32 的 monitor、烧录器等由项目入口选择环境，使用 process_open 后继续通过统一会话接口收发。说明见 `docs/processes.md`。连接所需的 COM 口、SSH 主机等由 Agent 根据实际项目确认，不写进通用安装包。

## 本次核验边界

0.6 的日常使用已由用户反馈测试正常。0.7 的实例布局、安装入口、冻结 worker 与句柄回收只做静态检查；未构建、未安装发布包、未运行测试或操作实板，也未重载当前 Codex 会话。
