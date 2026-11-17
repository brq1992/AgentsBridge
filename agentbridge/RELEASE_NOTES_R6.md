AgentBridge M1 R6（2026-11-16）

修复外部对 R5 的复审中的 P1 与回滚缺口，并加入 Claude 漏收消息的补救。
协议仍为 1.3；Hub 无改动。R5 未在任何机器安装，R6 取代 R5 作为候选。

P1：新旧 CLI 混用重复执行
- net_ops 新增 consumed_by，记录谁拿到了执行许可：'legacy'（无 nonce 的旧客户端）、具体 nonce（回执成功）、
  'closed'（未授予任何人）。所有消费路径统一写入。
- 回执只有两种成功：许可仍未消费且确实提供给了该 nonce；或该 nonce 本人此前已成功（幂等重放）。
  旧客户端消费、其他 nonce 或已关闭的许可一律拒绝，不再把 handed=1 当成"本 nonce 已成功"。
- 剩余窗口不变：回执已落库但 3 次回执响应全丢时 CLI 不执行（至多一次，不是"保证执行"）。

P2：健康接口不可达时的回滚
- 升级脚本从启动脚本自身输出记录新 Agent 的 PID，不依赖 HTTP 健康接口。
- 还原前：doctor 报告的 PID 与记录的 PID 都必须确认退出；随后用不依赖任一版本的内联探测确认 agent.lock 空闲
  （最长 60 秒）。任一无法证明时不还原文件，报告 "ROLLBACK IS INCOMPLETE"（锁被占用时明确写 agent.lock）。

Claude 漏收消息
- 原因（读 Claude Code 2.1.325 可执行文件得出）：会话忙时，Claude 会把外来 peer 消息 hold 住，缓冲满时把最早的
  当作过期丢弃，关闭时也会过期；回执只发给它自己的会话间发送方，对我们直接写管道的方式不返回。
  已用新 write_pipe 对真实会话管道验证：读写打开可用，但 3 秒内无回执。
- Agent 现在跟踪"已写入管道但未被 ack 认领"的 Claude 消息：会话 registry 显示 idle 时重新通知，
  1/3/10 分钟后各一次、最多 3 次；消息过期、已认领或绑定代际变化后不再通知。重发带明确的"re-notification"
  标记；执行仍由 ack 认领控制，重复通知不会导致重复执行。重发不向 Hub 报告为新投递。
- doctor 新增 unclaimed_messages（超过 1 分钟未认领的消息、已重发次数）及警告。
- write_pipe 在写后最多 1.5 秒非阻塞读取回执，只保留结构字段（不回显正文），记入 last_receipt。

测试（核心 107 项全部通过）
- test_r6.py 6 项：先 offer 后旧客户端再回执、先回执后旧客户端、旧客户端先于任何 offer、真实 CLI 在回执前插入
  旧客户端、真实 CLI 先于旧客户端、新旧客户端各 3 个并发重复 5 轮只有 1 个许可。6 项在 R5 上全部失败
  （4 failures + 2 errors），在 R6 上通过。
- 审查方的 review_receipt_regressions.py 2 项、review_regressions.py 3 项原样通过；review_mixed_cli.py 不再复现
  （其 "race not reproduced" 断言触发）。
- test_upgrade_rollback.py 新增：新 Agent 健康不可达且另一进程持有 agent.lock → 不还原、报告不完整、文件保持已安装。
- test_renotify.py 5 项：idle 才重发、退避与上限、忙时不发、已认领/过期/重新绑定不发、unclaimed 报告、回执只留结构字段。
- 未覆盖：60 秒以上慢退出、stop 命令失败；independent-launcher 的 WinError32 teardown 未处理；Claude 侧 hold
  行为来自对可执行文件的阅读，未有官方文档。
