# AgentBridge M1 R8（2026-11-19）

基线：R7。协议仍为 1.3，Hub 不需要升级。

## 包含
1. **macOS 适配**：unix socket 端点、sysctl 启动时间、LOCAL_PEERPID 对端核对、venv 解释器、60 秒 agent-start 超时、health mode `macos-m1-network`。说明见 `M1_MACOS.md`；已在一台 Intel Mac 上验收 Claude。
2. **会话变化检测**：绑定的 Claude 端点不可用时给出原因码 `claude_session_replaced` / `claude_session_not_running` / `claude_session_ambiguous` / `claude_registry_unreadable` 和重绑提示；原因变化时 stderr 记一行 `endpoint_unavailable`。不会自动跟随新 UUID。注册表里 pid 不是正整数的记录记为不可读，不中断其他会话的发现。
3. **token 优化**：
   - 默认生效：精简信封（`AGENTBRIDGE_LEGACY_ENVELOPE=1` 切回旧格式）；`handle` 一步完成认领/执行/回复，结果区分 received / already_received / not_authorised / ack_failed_retry；`--quiet` 精简输出；echo 判定只认 JSON 格式。
   - `agent-start` 会等 Hub 连上；超时仍未连上返回 `hub_connected=false` 和 warning。
   - 默认关闭：`auto_ack_final_echo`（精确 echo 的最终回复由 Agent 持久化 ack、不注入会话）。
   - 实测（仅供参考，不是计费 token，也不是每条消息的节省）：一次测试，5 条收 + 5 条发，比较会话上下文 `context.tokensUsed` 的增长：R7 18903，R8 6013，少约 68%；收约少 56%，批量发约少 85%。N=5、只跑一次，且包含查询用量的回合；R7 组的回复并入了发送方自己的回合，是它的最好情况。
4. **可复现的发布目录**：`tools/build_release.py --runtime <嵌入式 runtime 目录> --out <空目录> [--zip <文件>]`，由 HEAD 的 `agentbridge/` 加 runtime 生成完整目录和新的 `SOURCE_SHA256.json`（`upgrade-recovery.ps1` 用它校验）。

## 验证
- 在生成的发布目录里用嵌入式 runtime 跑全套：169 项，0 失败，0 错误（含升级与回滚）。外部复审独立重跑：168 通过、1 跳过（主机允许 breakaway 时的独立启动拒绝用例）。
- 外部审查通过：源码与发布包。

## 注意
- auto-ack 虽默认关闭，精简信封和 `handle` 的默认行为已经改变；与 R7 节点可以互通。
- 各机器的部署由各自的使用者授权；审查通过不等于部署授权。
