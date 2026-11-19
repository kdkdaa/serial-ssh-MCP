# 板卡连接文件

每种明确的板卡接口配置放在安装目录 `boards/<profile_id>.json`。文件描述连接、原始转发，以及可选的主机工具启动配方；不保存固件命令、命令校验规则、密码、主机信任或本机 COM 口。文件名就是调用 ID；使用小写字母、数字、下划线或连字符，最多 80 字符。

0.7 另支持用户扩展目录：使用 `--state-root` 时默认为其 `boards/`，或显式 `--profiles-dir`。同名用户文件优先，返回的 profile_path 表明实际来源；无效覆盖返回错误，不回退到其他板卡配置。安装升级只替换内置资源，不改用户扩展。

## Agent 调用

1. `board_profiles()` 列出文件摘要；根据实际板卡及接口选择 ID。型号或接口不明确时询问剩余信息，不仅根据芯片名称套用配置。
2. `board_profiles(profile_id="esp32-p4x-j19")` 读取完整连接说明和来源；已知 ID 时可直接进入下一步。
3. `connection_prepare(profile_id="esp32-p4x-j19", port="实际确认的端口")` 合并配置。显式传入的 `baudrate` 优先于文件。缺少目标端口时返回 `needs_input` 和候选端口；文件不会自动选设备。
4. 返回 `ready` 后，把 `open_args` 传给 `next_tool`（`serial_open` 或 `ssh_open`）。SSH 的认证和信任流程沿用现有接口，另行提供认证参数。`ready` 不代表已连接。
5. 使用返回的 `session_id` 调用 `session_exchange`；发送文本时显式带上准备结果中的 `forwarding` 字段。`data` 完全由 Agent 提供。需要原始二进制时显式改为 `data_format="hex", newline="none"`，结束字节写入数据。
6. 纯接收用 `session_read`，或 `session_exchange` 省略 `data` 并使用返回的 `next_cursor`；纯接收不要附加配置中的 `newline`。连接打开后持续后台录制，不需要为了接收而发送命令。

配置是调用参数来源，不是绑定在会话上的隐式行为。直接调用 `serial_open`、`ssh_open` 和 `session_exchange` 的旧流程保持不变；不同会话可以使用不同板卡配置。所有接收仍是原始输出，Agent 解释错误和决定下一步。

## 添加板卡

新增一个 JSON 文件即可，无需修改 Python、增加型号判断或注册专用工具。示例结构：

```json
{
  "schema_version": 1,
  "name": "板型与具体连接接口",
  "description": "适用的硬件版本及固件配置范围",
  "connector": "物理接口与接线说明",
  "connection": {"transport": "serial"},
  "forwarding": {"encoding": "utf-8", "data_format": "text", "newline": "none"},
  "notes": [],
  "sources": []
}
```

`connection.transport` 支持现有 `serial` / `ssh` 传输：

- 串口可填写 `baudrate`、`bytesize`、`parity`、`stopbits`、`dtr`、`rts`、`rtscts`。缺少波特率会继续询问；其他默认值与 `serial_open` 一致。只填写有依据的设置，未知协议的 `newline` 保持 `none`。
- SSH 的 connection 为 `{"transport":"ssh","port":22}`；host 和 username 在连接时提供，密码、私钥和主机指纹交给现有 SSH 接口。
- `forwarding` 描述编码、文本或 hex、行结束符；hex 必须使用 `newline="none"`。
- `notes` 和 `sources` 是连接说明及参数依据，不是可执行步骤。不保存命令列表，不进行协议封装，不自动登录、探测、复位或重发。
- 可选 `startups` 声明主机工具入口，由 `board_startup_prepare` 准备，再显式 `process_open` 启动；其参数和输入边界见 [专用工具说明](processes.md)。文件本身不会自动执行。

新增或修改 JSON 后，下一次工具调用就会重新读取；不缓存文件。首次加入本功能的 Python 代码需要宿主重新加载 MCP 才能暴露新增工具。`--workspace` 改为其他代码工程不影响读取安装目录下的 `boards/`；迁移或打包时保留整个目录。

配置结构错误通过 MCP 返回；列表里的 `errors` 标识无效文件，不妨碍列出其他有效配置。这里只检查连接配置字段，设备输入的语法、命令是否存在、业务是否成功均由 Agent 判断。未支持的物理传输（例如 SWD）不能仅靠新增 JSON 获得支持。

## ESP32-P4X

`esp32-p4x-j19.json` 对应本地 ESP32P4 项目的 P4X EV / J19 USB Serial/JTAG 配置。它不包含历史 COM21，不包含固件内部命令，也不通用于其他 UART 控制台配置。0.6 加入 `idf_monitor` 启动配方，通过明确的 project_dir 找项目 `idf.cmd`；新配方仅完成静态核验，未连接或复位实板。
