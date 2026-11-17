AgentBridge M1 R7（2026-11-16）

修复外部对 R6 的复审中重新通知的 P2。协议仍为 1.3；Hub 无改动。
R6 未在任何机器安装，R7 取代 R6 作为候选。R6 的其余内容（单一消费者、回滚锁检查）不变。

重新通知的尝试记账
- 顺序改为：① 只做 preflight（不写管道），失败不算一次尝试；② 在写管道之前，用一次事务以比较并设置的方式把
  notify_count+1、notified_at、renotify_state='attempting' 写入；③ 写管道，结果只标记为 'sent' 或 'uncertain'，
  不会退回已计的次数。
- 因此写入后出错、部分写入或进程在写入与记账之间崩溃，都已计入退避和上限；同一时间多个 pass 只有一个能占到尝试。
- Agent 启动时把遗留的 'attempting' 标为 'uncertain'（已计数，不重发也不退款）。
- doctor 的 unclaimed_messages 同时显示 renotify_state。
- 上限仍是每条消息最多 3 次（1/3/10 分钟），只在会话 idle、消息未过期、未认领、绑定代际未变时进行；
  执行仍由 ack 认领控制。

测试（核心 111 项全部通过）
- test_renotify.py 新增 4 项：写后异常计数并退避且总数不超过 3、preflight 失败不计数、写入中崩溃后重启仍计数且
  遵守退避、并发 pass 只占一次。这 4 项在 R6 上全部失败（2 failures + 2 errors），在 R7 上通过。
- 审查方的 review_renotify_regression.py、review_receipt_regressions.py、review_regressions.py 原样通过。
- 仍未验证：真实 Claude 会话 busy/idle/hold 行为（来自对可执行文件的阅读，mock 测试不能证明现场漏收已解决，
  需安装后实测）；60 秒以上慢退出、stop 命令失败；independent-launcher 的 WinError32 teardown。
