AgentBridge M1 R5（2026-11-16）

修复外部对 R4 的独立审查复现的 4 个 P1 和 P2 意见。协议仍为 1.3；
Hub 无改动。R4 未在任何机器安装，R5 取代 R4 作为候选。

P1 修复
1. 并发 ack 重复取得许可：交付结果改为原子消费（UPDATE … WHERE handed=0，rowcount=1 才返回
   should_execute=true），所有等待者中只有一个拿到许可。
2. 旧代际缓存许可：复用的 ack 必须与当前绑定 generation 一致；交付和回执时再校验 generation、
   绑定启用状态与消息有效期，任何一项不符都不给许可（binding_changed / expired）。
3. 响应丢失窗口：新增显式认领回执。CLI 每次 ack 带随机 nonce；Agent 只把许可"提供"给该 nonce，
   不标记已交付；CLI 收到后调用 /ack-receipt，回执成功才输出 should_execute=true。
   - ack 响应丢失（超时、断开）→ 未回执，许可仍可被下一次 ack 取得；
   - 回执响应丢失 → 同一 nonce 重试，回执幂等；
   - 多个调用方同时拿到提供 → 只有一个回执成功。
   Agent 等待上限 20 秒（ACK_WAIT），CLI 等待 45 秒（ACK_TIMEOUT）。
   剩余窗口：回执已在 Agent 落库、但 3 次回执响应全部丢失时，CLI 报 claim_receipt_unconfirmed
   不执行，该许可视为已交付（至多一次，宁可不执行，不会重复执行）；可在 status 中看到。
4. 已 tracked 的令牌：去掉 check-ignore --no-index；另用 git ls-files 拒绝目录里任何已在索引中的
   文件，并提示 git rm --cached、视已提交令牌为已泄露（不自动改历史）。configure 同样拒绝。

P2 处理
5. 脱离作业被拒：agent-start 默认拒绝启动并给出独立启动方法；只有显式 --allow-attached /
   start-m1.ps1 -AllowAttached 才以依附方式启动并告警。upgrade-recovery.ps1 在停止 Agent 之前探测
   （agent-start --check-breakaway，只启动一个空进程），不能独立启动且未给 -AllowAttached 时什么都不改。
   更正 R4 说法：错误 5 只说明该 shell 的作业禁止脱离，不能直接证明此前 Agent 是被 Claude 结束的。
6. 回滚：每次停止都等待进程真正退出（最长 60 秒）；升级后的 Agent 不退出时不还原文件并报告
   "ROLLBACK IS INCOMPLETE"；还原后逐个按备份哈希校验；只在旧 Agent 以新进程启动并应答时才报告
   回滚成功，否则如实报告需手动启动。安装路径上是文件夹时在停止前失败、不改动。
7. 保留期：未交付的认领 2 天后清理（消息有效期最长 24 小时，过期后本就不会交付）；过期的投递任务
   7 天后清理。Hub 侧 messages/events 仍未清理。

测试（核心 95 项全部通过）
- 新增 test_r5.py 9 项：无 nonce 并发、nonce 并发只一个回执、旧代际不复用、提供与回执之间重新绑定、
  过期不交付、真实 HTTP 下 ack 响应丢失后下一次 ack 取得且仅一次、回执响应丢失幂等重试、
  CLI/Agent 超时关系、已 tracked 令牌（server 与 configure）。这 9 项在 R4 代码上全部失败
  （5 failures + 4 errors），在 R5 上全部通过。
- 审查方的 review_regressions.py 3 项在 R5 上原样通过。review_http_timeout.py 直接调用不带 nonce 的
  内部 ack，绕过 CLI 回执，仍会显示 handed=1；真实 CLI 路径由 LostResponseTests 覆盖。
- test_upgrade_rollback.py 4 项（真实 PowerShell）：Hub 消失导致新 Agent 不健康→回滚并核实旧 Agent
  新进程；安装中途复制失败（文件被只读占用）→如实报告回滚不完整、Agent 保持停止；
  文件夹占位→停止前失败不改动；不能独立启动且无 -AllowAttached→不改动。
- 对现有安装运行 -CheckOnly 时发现：嵌入式运行时按安装目录解析 bridge 包，用旧运行时执行新包的
  CLI 会加载旧 CLI。脱离探测因此改为不依赖任一版本的内联检查；test_upgrade.py 的旧安装现在带旧 CLI
  （无 agent-start），该测试在修复前失败、修复后通过。
- 未覆盖：Agent 停止超过 60 秒的慢退出、stop 命令本身失败。
- independent-launcher 未改动；审查方报告的 teardown WinError32 未处理。
