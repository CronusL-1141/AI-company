# 外部 Codex 执行用量接入

用于Claude、脚本等直接执行codex exec时留最小回执：谁拉起的、关联哪个任务、每轮用量。需要安装包含codex-exec子命令的新OS版本；旧的直接调用不会自动改写或被拦截。

用量页的套餐估算和/usage只读Codex会话日志。不带--ephemeral时会话日志照常落盘，用量照常进入统计；带--ephemeral时不留会话日志，这次用量只在回执里，入口会在stderr提示。后台运行卡在等待输入是因为stdin没关，加`< /dev/null`即可，不需要--ephemeral。

## 调用方式

取得真实项目/任务UUID，把原codex exec后面的参数放在--后，不再重复写exec：

```sh
aiteam codex-exec run \
  --project-id '<项目 UUID>' \
  --task-id '<任务 UUID>' \
  --caller-harness claude-code \
  --caller-session-id '<调用方会话 ID>' \
  -- -s read-only -o '<结果文件>' '原来的任务描述' < /dev/null
```

project-id可省略，记为未关联执行；task-id必须与同项目的project-id一起提供。caller-harness可选codex/claude-code/other/unknown，默认unknown。不按cwd猜项目，不按当前登录追认历史账号。

其他选项：--api-url（或AITEAM_API_URL，默认http://127.0.0.1:8000）、--executable（默认codex）、--spool-db（私有SQLite路径）。源码环境使用Python 3.12执行`PYTHONPATH=src python3 -m aiteam.cli.app codex-exec ...`，安装环境用aiteam命令。

强制原生--json，stdout透传JSONL，提示写stderr，stdin透传，退出码沿用子进程。原调用方若只消费纯文字，需要改为读取JSONL中的消息事件，不能把整个stdout直接当答案。

## 留账与重放

先在本地留执行身份再启动Codex，过程中保存最小回执。默认账本为$CODEX_HOME/data/ai-team-os/external-exec.db，未设置CODEX_HOME则在~/.codex/data/ai-team-os下。新建目录700，数据库600；拒绝数据库符号链接、设备文件及不能无损表达的SQLite URL路径。

API暂不可用或版本不匹配时继续执行并保留待传账据；只有明确项目/任务不存在、归属不匹配等关联拒绝才阻止启动。运行前拒绝记为本地rejected并退出64，不再混进网络重试。命令结束尝试上传，失败后手动重放：

```sh
aiteam codex-exec replay --api-url http://127.0.0.1:8000
# 自定义spool时，run与replay都传同一个 --spool-db
```

重复上传幂等，确认后保留本地原账。replay默认100条，--limit范围1-10000。网络未确认或仍有批次外记录时退出2；已执行但关联错误的账据经服务端确认后隔离保存，保留原请求项目/任务ID，首次报告退出64，已确认账据不再循环重试；无待处理项时退出0。run仍保留实际Codex退出码，关联隔离另写stderr。已合法登记的精确重试不因项目后来删除而丢失回执。

每次调用都有随调用结束的进程监督，不启动全局守护。适配器收到SIGINT、SIGTERM、SIGHUP或意外退出时，监督进程核对身份并保护共享OS API家族，再冻结、强停允许清理的执行进程，不运行其退出处理器。清理前保留直接子进程的PID所有权，已回收PID不再用于发现或发信号；不对混有受保护进程的组整体发信号。退出码取真实原生子进程结果（强停通常为137），不借监督进程退出码代填。SIGKILL后已经提交的回执保留；结束时核对回执清单，终态拒绝新回执，完全相同的旧回执仍可幂等重放。

进程监督要求POSIX、可靠的进程身份读取和不回收子进程的退出观察能力，当前实测macOS；不具备条件时在启动Codex前退出127。被收养或双fork脱离的后代可能在首次采样前就不可见，因此所有强制中断都记录process_cleanup_unverified，即使本次已识别的执行进程都已停止。未知身份及共享API家族保留，不扩大终止范围。若原生退出码未取得，账据保留null，CLI退出75。监督进程自身被强杀时无法自救，可能遗留存活或暂停的执行进程；缺少最终状态包不能证明原生进程已结束。

适配器在监督清理期间死亡时会补存中断终态；但监督进程已退出、适配器尚未提交终态的间隙仍可能遭遇崩溃。此时已提交回执仍在，状态可能留在running，不能据此认定仍在执行。当前不保证任意时点强杀都自动补齐终态。

仅保存执行/线程/回合标识、显式归属、时间、状态、白名单token及已观测元数据。不保存prompt、代码、工具输出、原始error正文或完整stdout。stdout/stderr照常给调用方，由调用方决定其日志方式。

## 查看结果与口径

Dashboard用量页先读精简外部执行列表，展开才读回执并分页，每页最多100条。显示当前页有用量回执的调用占比，缺失为“—”，原生报告0保留0；若未能证明为有效零测量，则标为zero_unverified，不能据此认定没有消耗。该分母不是账户全部调用。输入含缓存，reasoning包含在输出，不能再加一遍；仅显示原生total，不自行编造总量。

按需对账：`usage_reconciliation(project_id="...", since="...Z", until="...Z", limit=500)`，或GET /api/external-execs/reconciliation。每来源最多扫描500条轻量证据；MCP只返回摘要及每桶最多3例，并限制16KB。HTTP桶用bucket_offset/bucket_limit分页（最多50条），来源总数、样本内桶数量和两种截断分别展示。Codex子agent按自身cc_tool_use_id对账，不借root的session_id。缺计价元数据与元数据齐全但未核价分开显示。没有任何证据的旧ephemeral执行不可恢复。

套餐剩余百分比继续取原生账户限额观测。外部事件不保证账号、模型及服务档位，未知就保持unknown，不用CLI请求参数或当前账户代填。新原生回执保守标为thread_aggregate，未提供counting_level时为unknown，历史显式turn_aggregate仍兼容；恢复线程时不把累计值当新增回合用量。不同调用回执与rollout逐请求数不能直接相加，也不自动混进套餐容量估算或实付金额。

## HTTP入口

| 方法 | 路径 | 要点 |
| --- | --- | --- |
| POST | /api/external-execs | 明确invocation_id、source_namespace及调用方元数据，校验项目/任务 |
| POST | /api/external-execs/quarantine | 明确失效关联的原登记及reason_code；服务端复验后保存未关联证据，不覆盖合法登记 |
| GET | /api/external-execs | project_id可选，limit默认100上限500，offset默认0，fields默认compact不带receipts |
| GET | /api/external-execs/{id} | receipt_offset默认0、receipt_limit默认100上限100，返回receipts_count及截断 |
| POST | /api/external-execs/{id}/thread | thread_id |
| POST | /api/external-execs/{id}/receipt | 稳定receipt_id/thread_id及白名单ExternalExecReceipt |
| POST | /api/external-execs/{id}/finish | status、exit_code、finished_at、诊断码及可选receipt_ids封存清单 |
| GET | /api/external-execs/reconciliation | project_id、source_namespace、带时区since/until；limit默认500上限500；bucket_offset默认0，bucket_limit默认20上限50；summary可选 |

写入默认精简回执，fields=all附带最多100条回执；更多回执通过详情分页读取。精确类型见OpenAPI与aiteam.types.ExternalExec*。MCP仅暴露只读对账，不提供远程执行接口。
