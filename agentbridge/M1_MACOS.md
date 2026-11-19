# macOS M1 运行说明

已验收范围：**Intel Mac 上的 Claude**（Claude 2.1.326，macOS 26.7.1，x86_64）。不包括 Apple Silicon、Mac 上的 Codex、生命周期与恢复。

协议、Hub 和 Windows 节点不变。macOS 用的是同一份源码，代码里按平台分支。

## 和 Windows 的区别

| | Windows | macOS |
|---|---|---|
| 运行环境 | 包内自带 Python 3.14 | 自建 venv（Python 3.11+，已验证 3.13.15） |
| Claude 原生端点 | 命名管道 `\\.\pipe\LOCAL\cc-msg-…` | unix socket，例如 `/tmp/cc-socks/<pid>.sock` |
| 端点校验 | 管道前缀 | 绝对路径、socket 类型、属主是当前用户、组和其他用户无权限 |
| 进程启动时间 | `procStartFt` | `procStart`：UTC ctime，24 个字符，由 sysctl `kp_proc.p_starttime` 换算 |
| 写入前对端核对 | — | 用 `LOCAL_PEERPID` 读取对端 PID，PID 和启动时间都必须与绑定一致，否则一个字节都不发 |
| `agent-start` 超时 | 20 秒 | 60 秒（会等 Hub 连上；超时仍未连上时返回 `hub_connected=false` 和 warning，以 `doctor` 为准） |
| `doctor` 的 mode | `windows-m1-network` | `macos-m1-network` |

## 安装

```sh
D="$HOME/Library/Application Support/AgentBridge-M1"
mkdir -p "$D" && chmod 700 "$D"
/usr/local/bin/python3 -m venv "$D/venv"          # 或其他 Python 3.11+
"$D/venv/bin/python3" -m pip install -r requirements-network.txt
# 核对包的 SHA-256 后，把包解压到 "$D/mac-port"。不要从共享目录直接运行代码。
V="$D/venv/bin/python3"; A="$D/mac-port/agentbridge.py"; S="$D/state"
```

## 接入（一次性）

1. Hub 管理员执行 `hub-invite --node <节点> --remote-ip <Mac 的源 IP>`。邀请 1 小时内有效。
2. 用私密渠道把 `invitation.json` 和 `hub-cert.pem` 交给 Mac 用户。Mac 用户把两个文件放进权限 0700 的目录，文件权限设为 0600，然后删除所有中转副本。不要传长期凭据 `enrollment/*.json`。
3. 先核对证书指纹，再执行：

```sh
"$V" "$A" --state "$S" node-enroll --help        # 按帮助传入证书、指纹和邀请文件
```

## 运行

```sh
"$V" "$A" --state "$S" agent-start --hub-config "$S/network.json"
"$V" "$A" --state "$S" doctor                     # mode=macos-m1-network, hub_connected=true
"$V" "$A" --state "$S" --tool claude --session <UUID> qualify-claude --version <版本>
"$V" "$A" --state "$S" --tool claude --session <UUID> bind --alias claude --project <项目>
"$V" "$A" --state "$S" grant-client --alias claude --directory "$D/claude/agentbridge-client"
"$V" "$A" --state "$S" stop
```

- 会话 UUID 和版本取自 `~/.claude/sessions/<pid>.json` 里的 `sessionId` 和 `version` 字段。
- 客户端目录不能在任何 git 仓库里，也不能在 state 目录里。
- 注入会话的 ack/reply 命令使用 venv 的解释器。

## 自检

```sh
"$V" "$D/mac-port/tests/mac_device_check.py"
```

这个脚本会起一个临时 socket 服务器，测试 4 种情况：

- 对端正确；
- 对端正确并返回回执；
- 对端 PID 错误：必须 0 字节写入；
- 对端连上后立即断开：不能卡住。

全部通过时输出 `PASS`。脚本只用假 token，不读取 `~/.claude`，也不连接 Agent 或 Hub。

## 已知限制

- ctime 的日期在 1–9 号时用空格补位（例如 `Nov  5`），要等到真实的月初日期才能验证。
- 原生发送遇到断开时记为 `delivery_uncertain`，不会重投。这一点依据代码路径判断，没有端到端测试过。
