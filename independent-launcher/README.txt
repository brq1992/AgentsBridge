AgentBridge 独立启动入口

附加启动工具，不覆盖 AgentBridge 源码、状态、凭据或绑定。
不安装服务、计划任务或登录自启动，不更改系统权限。

使用
1. 解压到本机固定目录，不要直接在 ZIP 中运行。
2. 复制 profiles.example.json 为 profile.json，填本机的安装目录、私有状态目录和 Python 路径。
   profile.json 只含路径、没有凭据，且只属于本机（已在 .gitignore 中）。
   多套配置可用 Launch-Agent.ps1 -Profile <名称> 选择 <名称>.json。
3. 在 Windows 文件资源管理器里手动双击 Start-Agent.cmd。
   不要让 Claude/Codex 用工具启动：从 AI 工具 shell 起的进程属于该应用的作业对象，应用退出时会被一起结束，
   而且这类作业通常不允许脱离（Windows 错误 5）。
4. action=started 表示启动了 Agent；already_running 表示保留现有 Agent。
   重复点击不会重新启动。健康接口失效但状态锁仍被占用时拒绝启动第二份。
5. Status-Agent.cmd 只读查询，绝不启动或停止进程。
6. 启动窗口可以关闭；Agent 日志追加到状态目录 Agent.stdout.log / Agent.stderr.log。

从旧启动方式切换
返回 already_running 说明仍是旧方式启动的进程。协调好后，从开始菜单打开 PowerShell，用已有 CLI 正常停止：
  & "<install>\runtime\python.exe" "<install>\agentbridge.py" --state "<state>" stop
然后从资源管理器双击 Start-Agent.cmd。本入口不会擅自停止已运行的 Agent，不要求重新绑定。

验收（多台机器依次测试，不要同时关闭）
A. 启动后记下 PID；确认 hub_connected=true（首次启动可能尚在连接，可再运行 Status）。
B. 再次双击 Start，必须返回 already_running 且 PID 相同。
C. 关闭启动窗口和 PowerShell，运行 Status，检查 PID 相同、Hub 正常。
D. 关闭该机器的 Claude/Codex 应用，再运行 Status：PID 不变、Hub 正常。
E. 手动重开原会话，发送全新 ID 的 echo，核对请求、回显、最终 ack。
失败时保留日志和输出，不重装、不重新绑定、不自动反复重试。

诊断边界
启动器请求 CREATE_BREAKAWAY_FROM_JOB + DETACHED_PROCESS；Windows 拒绝时明确失败，
不回退到普通的应用子进程启动。使用 venv 时解析其实际 Python 和导入路径。
windows_job_member=true 本身不证明受 Claude/Codex 管理；系统也可能使用作业组，
此时 independent_lifetime_verified=false，必须完成上述现场测试。
windows_job_member=false 也不能代替现场 echo 验收或证明能在注销/重启后存活。
参考：https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags

测试
tests/test_launcher.py：连续点击、三路并发点击、启动器退出后 Agent 存活、状态只读、
占用锁拒绝重复启动、Windows 拒绝脱离时不回退、旧客户端文件恢复。需在允许脱离作业对象的
环境（资源管理器或开始菜单 PowerShell）中运行；在 AI 工具 shell 内其中 3 项会因错误 5 失败。
