AgentBridge M1 R4（2026-11-16）

基于 R3 源码审查快照修复审查发现的问题。协议版本仍为 1.3，
与 R3 Agent / Hub 兼容；Hub 侧只增加了缓存清理和证书到期显示，不升级 Hub 也能用。

修复
- H1 令牌目录进 git：/grant 拒绝位于 git 工作区且未被忽略的客户端目录；
  configure-m1-session.py 新增 --client-directory，并在目录位于仓库内时自动写入该仓库本地
  .git/info/exclude（不改共享 .gitignore）。
- H2 认领后无人执行：本地操作新增 sending 状态与 handed 标记。已发出的 ack 不再提前判失败，
  调用方放弃等待后再次 ack 会拿回 Hub 已做出的那次认领（只返回一次），不会再出现
  "Hub 已 received、无人执行、无回复" 的消息。
- H3 Agent 随调用方退出：start-m1.ps1 改用新命令 agent-start（bridge/spawn.py），
  分离启动并在 Windows 允许时脱离调用方作业对象；不允许时照常启动但明确警告，不宣称独立。
  doctor 新增 windows_job_member 与 warnings。
- M1 Claude 版本白名单：新增管理员命令 qualify-claude --version X，对本机正在运行的该版本
  做结构检查（peerProtocol=1、进程启动时间、管道命名空间、唯一 key 文件）后写入状态目录
  qualified-claude.json，无需改源码重发包。未通过的端点在 doctor 里显示
  reason=claude_version_unqualified 和提示。
- M2 Codex 路径失效：新增管理员命令 refresh-codex --alias X [--cli 路径]。保持 UUID、
  generation、客户端令牌不变，只换 CLI 路径；省略 --cli 时在同一 bin 目录下找唯一的新版本，
  并实际运行 --version 与 queue --help 校验。doctor 显示 reason=codex_cli_missing 和提示。
- M3 hostSessionId：绑定 UUID 等于 hostSessionId 或 sessionId 中任一合法 UUID 即匹配，
  未来版本即使两者都是不同 UUID 也不会误判。
- M4 升级脚本：按本发布 SOURCE_SHA256.json 安装全部文件（运行时除外），先暂存并校验，
  失败自动还原旧文件并重启旧 Agent；记录 INSTALLED_SHA256.json。Hub 进程不被触碰。
- M5 证书到期：doctor 显示 hub_certificate（到期日、剩余天数），14 天内告警；
  hub-status 也显示。证书轮换流程仍需另行设计。
- L1 账本增长：Agent 每小时清理 7 天前已完成的本地操作（未交付的认领结果保留）；
  Hub 每小时清理 7 天前的 RPC 重放缓存。
- L3 非 Windows：客户端目录 0700、client.json 以 0600 原子创建。
- L4 提交前在同一事务内按当前端点重新预检，端点刷新不会让消息写到旧管道。

未修复 / 限制
- L2：Codex CLI 只接受 --message 文本参数（已在 0.169.0-alpha.11.1 上确认），消息正文仍会出现在
  本机进程命令行里；正文只含 peer 数据，不含凭据。
- macOS：未实现、未验证，见源码审查报告。
- independent-launcher 未改动，仍是推荐的手动启动入口。

测试
- 核心 83 项（R3 的 71 项 + 新增 12 项，含真实 PowerShell 升级与回滚测试）全部通过，见
  tests/。
- 在 Claude 工具 shell 内运行时，Windows 拒绝脱离作业对象（错误 5），这既是 H3 的直接证据，
  也是 independent-launcher 自带的 3 项测试在此环境失败的原因；该启动器需在资源管理器或
  开始菜单 PowerShell 中验证。
