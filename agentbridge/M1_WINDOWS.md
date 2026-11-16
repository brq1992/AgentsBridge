# Windows M1 运行说明

实现：TLS WebSocket Hub、每用户出站 Agent、原会话注册与目录、持久化收发与回执、节点/项目权限和一次性接入。协议见 M1_PROTOCOL.md。Python 3.11+；网络进程安装 requirements-network.txt，收发 CLI 仍仅用标准库。

v2 Windows x64 接入包自带 Python 3.14.10 和固定依赖，直接使用包内独立运行环境，无需系统 Python、pip 或管理员权限。不会更改系统 PATH 或注册表。来源和校验信息在 RUNTIME_PROVENANCE.json，Python 与依赖许可证随运行包保留。原源码包仍支持通过 `-Python` 指定现有 Python 3.11+。

## 部署形态

- 一台机器运行 Hub（TLS，默认端口 9741，只监听指定网卡）。
- 每台机器一个 Agent；全局目标为 `<节点标签>/<本地别名>`，例如 `node-a/claude`。
- 节点按项目标签授权（例如 `echo`）；跨项目投递被拒绝。
- 私有状态（凭据、账本）放在专用目录，代码包不含这些目录。

## 第二台接入

1. 按交付命令核对 ZIP SHA-256，解压到第二台本地目录。不要长期从共享目录执行可被别人改写的代码。
2. 运行 `join-windows.ps1 -Hub wss://HUB_HOST:9741 -Code 接入码 -Fingerprint 证书指纹`。v2 包直接使用内置运行环境；脚本验证证书、领取节点令牌、启动隐藏 Agent。无需管理员权限、计划任务、系统启动项或远端入站端口。可加 `-CheckRuntime` 只检查运行环境，不领取凭据。
3. 选择列表中的原 Claude UUID。脚本为其工作区添加三个固定 Python/脚本/UUID 的 Bash 规则，仅允许 ack、reply 和发往指定测试会话的 send；保留其他设置、入站策略与权限模式。备份和规则增量存在私有状态。
4. 在原 Codex 测试会话获取 `CODEX_THREAD_ID`、工作目录及 `whoami /user` 的实际工具 SID，输入脚本。客户端仅有该会话的权限；SID 得到源码读/执行权，不开放管理状态。
5. 将脚本打印的授权文字发到所选原会话。它读取本地 M1 echo 政策，发一次请求，后续先 ack 再 echo reply，最终回复只 ack。已有旧审批可能仍需人工释放一次。
6. 回报连接结果和 UUID，不发送令牌、network.json、client.json 或原生 key。

## 管理

在代码目录运行，STATE 为专用私有目录：

```text
python -m venv RUNTIME
RUNTIME/Scripts/python.exe -m pip install -r requirements-network.txt
RUNTIME/Scripts/python.exe agentbridge.py --state HUB_STATE hub-init --host HUB_IP --listen HUB_IP --port 9741
RUNTIME/Scripts/python.exe agentbridge.py --state HUB_STATE hub-node-add --label win-a --project echo --url wss://HUB_IP:9741
RUNTIME/Scripts/python.exe agentbridge.py --state HUB_STATE hub-invite --node NODE_UUID --remote-ip AGENT_IP
start-m1.ps1 -Mode Hub -State HUB_STATE -Python RUNTIME/Scripts/python.exe
start-m1.ps1 -Mode Agent -State AGENT_STATE -Python RUNTIME/Scripts/python.exe
python agentbridge.py --state AGENT_STATE doctor
python agentbridge.py --state AGENT_STATE list
python agentbridge.py --state AGENT_STATE status
RUNTIME/Scripts/python.exe agentbridge.py --state HUB_STATE hub-status
RUNTIME/Scripts/python.exe agentbridge.py --state HUB_STATE hub-node-revoke --node NODE_UUID
python agentbridge.py --state AGENT_STATE stop
RUNTIME/Scripts/python.exe agentbridge.py --state HUB_STATE hub-stop
```

Hub 管理只访问本地私有文件，不开放网络管理 API。hub-stop 由服务进程监控，避免只结束 venv 启动器而留下实际进程。邀请过期可为未接入节点重发；已绑定节点不能再次领取凭据。

网络不通先查 9741。不要关闭防火墙或整体放行 Python。确需规则时管理员仅开放 TCP 9741，来源限定为各 Agent 机器、目标限定为 Hub 机器。

## Claude 原会话关闭后重开

原 UUID 不变但 PID/pipe 已更新时，不要重新 `bind`：重新绑定会改变 generation，旧消息将不能跟随。先打开对应原会话，再由本机 Agent 管理员执行：

```text
python agentbridge.py --state AGENT_STATE refresh-claude --alias LOCAL_ALIAS
```

LOCAL_ALIAS 为本机绑定别名。命令只接受准确原 UUID 的一个存活且版本已认证的端点；保留原逻辑身份和会话客户端权限。仅对未过期、从未开始原生提交的预检阻塞消息请求 Hub 恢复；已提交、已收到和不确定消息不会重投。会话作用域客户端不能运行此命令。

自 R3 起 Agent 每 5 秒自动刷新同一 UUID 的端点，一般无需手动执行。Codex 应用更新后 CLI 路径失效时使用
`refresh-codex --alias LOCAL_ALIAS`；Claude 新版本需先由管理员 `qualify-claude --version X` 批准（见 USAGE.md）。

Claude 收发命令应使用政策里的完整固定前缀，每个 ack/reply 分别执行。把路径改写成变量或合并命令可能产生新的工具审批；旧审批不会因后台规则已经存在而自动释放。

## 回滚与边界

M0 ZIP 保留，切换前已私有备份 SQLite，三轮历史回信全部确认。停止网络 Agent 后，用 `agent` 不带 `--hub-config` 即回到 M0；旧账本和客户端保留。未完成的 Hub 队列不会自动迁入 M0，应先完成或记录。

桌面重启、睡眠唤醒、无人值守启动、自动版本适配、自动证书轮换、生产公网部署和 macOS 尚未验收。系统测试成功不等于真实双机模型会话验收。
