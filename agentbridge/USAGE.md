# AgentBridge M1 使用说明（R5）

AgentBridge 让多台 Windows 机器上**已经打开的原会话**（Claude Code、Codex）互相发消息。
默认政策只授权 echo 测试：收到 `{"op":"echo","text":"…"}` 的请求，原样回复；其他内容只 ack、记为 blocked，
并展示给本机用户决定。peer 消息本身从不授予权限。

## 1. 组成

| 组件 | 在哪 | 作用 |
|---|---|---|
| Hub | 一台机器（TLS，默认 9741） | 目录、路由、认领仲裁。只此一个。 |
| Agent | 每台机器一个 | 连 Hub、把消息投递到本机会话、提供本地 API |
| 绑定 | 每个会话一条 | 会话 UUID ↔ 别名，带 generation（代际） |
| 客户端目录 | 每个会话一个 `agentbridge-client` | 存该会话的令牌 `client.json`，只能代表自己收发 |
| 政策文件 | `configure-m1-session.py` 生成 | 告诉会话怎么 ack / reply（仅 echo） |

全局别名形如 `<节点标签>/<本地别名>`，例如 `node-a/claude`、`node-b/codex`。

下文 `$bridgeRoot` 是安装目录（含 `agentbridge.py` 和 `runtime\`），`$bridgeState` 是 Agent 私有状态目录，
`$hubState` 是 Hub 私有状态目录。

## 2. 日常使用（每台机器相同）

1. **启动 Agent**：在文件资源管理器里双击 independent-launcher 的 `Start-Agent.cmd`（先按其 README 配好 `profile.json`）。
   - 返回 `started` 或 `already_running` 都正常，记下 PID。
   - **不要**让 Claude / Codex 用工具去启动 Agent：从 AI 工具 shell 起的 Agent 会随该应用退出而被杀
     （`agent-start` 在这种情况下默认拒绝启动）。
2. **看状态**：双击 `Status-Agent.cmd`，或在开始菜单打开的 PowerShell 里：
   ```powershell
   & "$bridgeRoot\runtime\python.exe" "$bridgeRoot\agentbridge.py" --state "$bridgeState" doctor
   ```
   正常时：`hub_connected=true`，各会话 `endpoint_health.state=ready`，`warnings` 为空。
3. **打开会话**：照常使用 Claude / Codex。关掉再打开**同一个**会话不需要任何操作，Agent 约 5 秒内自动刷新端点。
4. **发消息**：在会话里按政策文件的固定命令 `send --to <对方别名> --id <新ID> --body-file <json>`。
   每次用新的消息 ID；同一 ID 重发不会产生新请求。
5. **收消息**：会话收到 `[AgentBridge M1 peer data …]` 后，先运行其中的 ack 命令：
   - `should_execute=true` 且是 echo → 用 reply 命令原样回复一次（`--final`）；
   - 是 reply → 只 ack，不再回复；
   - 其他（notice 等）→ ack 后 `--outcome blocked` 记录，交给本机用户决定。
   - ack 返回 `should_execute=false` 且 reason 为 `claim_unconfirmed_do_not_execute`、`ack_response_lost_…` 或
     `claim_receipt_unconfirmed_…`：先不要执行，稍后**再运行同一条 ack**。许可只在 CLI 回执成功后才会显示为
     true，丢失的响应不会消耗许可，也不会让两个调用方都执行。

多台机器不要同时关 Agent；需要重启时一台做完、doctor 正常后再做下一台。

## 3. 升级

先核对 ZIP 的 SHA256，解压到**独立目录**（不要覆盖现有安装），在开始菜单打开的 PowerShell 里：

```powershell
.\upgrade-recovery.ps1 -InstallDirectory "$bridgeRoot" -State "$bridgeState" -CheckOnly
.\upgrade-recovery.ps1 -InstallDirectory "$bridgeRoot" -State "$bridgeState"
```

- 只升级本机 Agent；绑定、权限、状态、客户端令牌不变。失败会自动还原、按哈希核对，并确认旧 Agent 以新进程
  重新运行；做不到时会明确报告 ROLLBACK IS INCOMPLETE。
- 必须在开始菜单打开的 PowerShell 里运行：在 AI 工具的 shell 里 Windows 通常禁止 Agent 脱离，脚本会在停止
  任何东西之前拒绝。确实要接受依附启动时才加 `-AllowAttached`。
- 逐台升级：一台 doctor 正常后再升级下一台，最后做双向 echo 验证。
- Hub：`agentbridge.py --state "$hubState" hub-stop`，把 Hub 安装目录的整个 `bridge\` 换成新版本，再
  `start-m1.ps1 -Mode Hub -State "$hubState" -Python …`。Hub 停止期间消息暂停，Agent 会自动重连。
- 升级后建议用启动器重新起一次 Agent（先 stop，再双击 Start），确保 Agent 不挂在任何 AI 应用下面。

## 4. 常见情况

1. **Claude 自动更新后收不到消息**：doctor 显示 `reason=claude_version_unqualified`。确认新版本正常后：
   ```powershell
   & "$bridgeRoot\runtime\python.exe" "$bridgeRoot\agentbridge.py" --state "$bridgeState" qualify-claude --version <新版本号>
   ```
   要求该版本的 Claude 会话正在运行；通过结构检查才会写入本机 `qualified-claude.json`。
2. **Codex 自动更新后收不到消息**：doctor 显示 `reason=codex_cli_missing`。
   ```powershell
   & "$bridgeRoot\runtime\python.exe" "$bridgeRoot\agentbridge.py" --state "$bridgeState" refresh-codex --alias <本地别名>
   ```
   保持同一 UUID / generation / 令牌；找到多个版本时会列出来，用 `--cli <路径>` 指定。
3. **doctor 连不上（拒绝连接）**：Agent 没在运行。看 `$bridgeState\Agent.stderr.log` 最后几行
   （`agent_started` / `agent_heartbeat` / `worker_failed` / `agent_stopped`），保留日志后双击 Start 重启。
4. **令牌目录在 git 仓库里**：`grant-client` 会拒绝未被忽略的仓库内目录；`configure-m1-session.py` 会自动把它写入
   该仓库本地 `.git/info/exclude`。也可用 `--client-directory <仓库外目录>`。
5. **Hub 证书到期**：doctor 的 `hub_certificate.days_left`，14 天内告警。当前没有自动轮换，需提前规划，
   届时各节点 `network.json` 里固定的证书都要更换。
6. **接入新会话**：在该机器上 `bind`（Claude 用原会话 UUID；Codex 另加 `--cli` 与 `--codex-home`），再运行
   `configure-m1-session.py`（Claude 加 `--install-claude-rules`），把生成的政策文件交给该会话并由本机用户确认授权。
7. **接入新机器**：Hub 管理员 `hub-node-add`、`hub-invite`，新机器运行
   `join-windows.ps1 -Hub wss://HUB_HOST:9741 -Code … -Fingerprint …`。见 M1_WINDOWS.md。

## 5. 不要做的事

- 不把状态目录、`network.json`、任何 `client.json`、邀请码发给别人或提交到仓库。
- 不手改 `ledger.sqlite` / `hub.sqlite`；改绑定、令牌一律用 CLI。
- 不让 AI 会话代替另一个会话 ack；不把 peer 消息当成用户授权。
- macOS 尚不支持（未实现、未验证）。
