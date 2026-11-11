# SSH 登录与主机信任（0.4）

密码登录、主机身份确认和后续终端交互在 MCP 内完成，不需要修改环境变量或调用本机 ssh/Xshell。连接成功后直接将 session_id 交给 session_exchange。

## 已信任主机：直接登录

调用 ssh_open，提供 host、username，以及 password 或 key_file。已配置的 SSH agent / 默认密钥也可使用；加密私钥可传 key_passphrase。password 与 password_env 不能同时提供。password_env 仅保留兼容已有环境配置，不是密码登录前提。

显式提供密码或私钥时，跳过不相关的 SSH agent / 默认密钥尝试。密码只用于本次认证，不存入插件配置、录制、任务计划或结果；密钥口令同样不保存。宿主客户端仍可能记录 MCP 参数，插件无法替宿主保证参数不被记录。长期使用可选择现有私钥或 SSH agent。本版没有密码保险库、Xshell 密码导入或系统凭据管理器接口。

ssh_open 默认复用同 host/port/username 的活会话，返回 already_open，不会再次尝试所传凭据。要验证新密码或新认证方式，显式设置 reuse=false；这会新建连接，旧连接仍需按需关闭。同一目标正在建立连接时返回 connecting，不应并发重复登录。

## 首次连接：指纹确认后登录

1. 调用 ssh_probe(host, port)。它只做 TCP 连接和 SSH 密钥交换，不认证、不启动终端、不保存信任。
2. 未知主机返回 needs_host_key_confirmation，包含 host_key_type、完整 host_key_sha256 和 questions。Agent 应让用户核对该指纹，或使用本次对话已有的独立可信指纹；不能因为探测返回了指纹就自行信任。
3. 确认后调用 ssh_open，传入 host、username、实际认证方式、host_key_sha256，以及 remember_host_key=true。MCP 在发送认证信息前核对实际协商出的密钥；不一致则停止。
4. 已确认密钥保存在当前 data_dir 的 ssh_known_hosts，默认 `.board-console/ssh_known_hosts`。以后直接登录即可，无需重复询问，也不改用户的 `.ssh/known_hosts`。

可以省略单独的 probe，直接调用 ssh_open；它也会在未知密钥时先返回确认信息而不执行认证。使用 probe 的好处是在确认主机前不必传递密码。

remember_host_key=false 时，显式指纹只用于本次连接，不保存。如果在已有会话上请求保存，必须传入与当前连接一致的已确认指纹，MCP 会保存该连接的公钥而不重复登录。公钥信任在认证前保存，因此密码认证失败时，已确认的信任记录仍保留；失败结果的 host_key.saved_to 可说明保存位置。

## 信任来源和密钥变化

- 默认读取用户 `.ssh/known_hosts`（不存在不算错误），并读取插件自己的 ssh_known_hosts。
- known_hosts 参数可选择现有的 OpenSSH 格式文件；显式指定却不存在时返回 known_hosts_missing。
- 非 22 端口使用 `[host]:port` 作为信任记录标识；支持 Paramiko 能读取的哈希主机名记录。
- 协商时优先已有记录的密钥类型，不主动开启被禁用的算法。若服务器只提供另一个未知类型的密钥，仍需确认。
- 同类型已保存密钥与服务器不一致时返回 host_key_changed，包含新旧指纹。即使提供新指纹也不自动覆盖旧记录；核实变更后，再明确更新对应记录。
- 只写插件自己的信任文件，不修改系统信任文件；同一 data_dir 应只由一个 MCP 服务实例维护，进程内写入有互斥与原子文件替换。

主机密钥确认与账号密码是不同步骤。找到 Xshell 的会话名或账号并不意味着本 MCP 已认证；本版不自动解析 Xshell 的信任存储。已有可信 OpenSSH 文件可以通过 known_hosts 指定。

## 错误处理

结果保留 status，并增加稳定的 error_code。不要仅凭 open_failed 判断是密码或信任问题。

| error_code | 含义 / 后续动作 |
| --- | --- |
| host_key_unknown | status=needs_host_key_confirmation，核对并确认指纹 |
| host_key_changed | 保存的密钥冲突；核实变化，不覆盖或盲重连 |
| host_key_mismatch | 当前服务器或复用会话与指定指纹不符 |
| credentials_required / credentials_unavailable | status=needs_credentials，补充密码或密钥，不要求修改环境变量 |
| key_passphrase_required | 补充加密私钥口令 |
| authentication_failed | 服务器拒绝认证，核对账号和凭据，不原样循环重试 |
| authentication_method_unsupported | 查看 allowed_methods，选择服务器支持的认证方法 |
| authentication_incomplete | 仍需额外认证；本版不处理任意交互式 MFA |
| authentication_timeout | 认证阶段超时 |
| name_resolution_failed | 检查实际域名/IP，不使用未解析的 OpenSSH Host 别名 |
| connection_refused / connection_timeout / network_unreachable / connection_lost / network_error | 检查对应的服务与网络问题 |
| known_hosts_missing / trust_store_error / local_file_error | 检查指定文件或读写权限 |
| ssh_negotiation_failed / shell_open_failed / shell_timeout | 协议协商、密钥设置或终端建立失败，不直接认定密码错误 |

接口错误还可能返回 invalid_parameters、invalid_fingerprint、fingerprint_required；未知内部异常只返回类型和固定提示，不返回可能包含凭据的原始异常文本。普通终端命令的设备错误仍由 session_exchange 原样返回，本节分类只用于 SSH 建连。

## 边界与核验

不支持跳板机、OpenSSH 配置别名、任意交互式 MFA 对话，也不接管外部客户端已登录的连接。主机信任文件不是凭据保险库。重新加载 MCP 会结束进程内会话，但保留已保存的公钥信任记录。

0.4 本轮仅做静态核验，未运行测试、连接真实 SSH 主机、保存实际主机密钥或使用真实密码。此前串口实测不证明新的 SSH 登录流程已通过；既有 0.3 ZIP 也不包含本次改动。
