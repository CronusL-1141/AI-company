# Codex 按需启动与发布生效验收

## 问题与范围

原生 HTTP MCP 的 headers helper 只检查已运行 API；用户退出承载 API 的终端后，再打开 Codex 无人恢复服务。本候选将启动与终端生命周期分离，并保留独立请求并发治理。HTTP/2 已由用户决定不发布：候选不含代理脚本、可选依赖、证书配置或代理启动参数。

## 启动与归属

用户通过 Codex 适配器显式安装或更新后，helper 使用绝对路径调用 runtime。runtime 在连接时按需启动 API，stdout 只输出协议数据，诊断写 stderr；不创建定时器、系统服务或重启守护。已健康的外部 API 只复用，不认领；不兼容的端口占用明确拒绝，不抢端口。

独占启动锁覆盖探活、预绑定 socket、启动、进程登记和就绪；子进程在 exec 前自行写下 PID、启动时间、UID、精确命令与目标，避免父进程被中断后丢失归属。停止操作只发送 TERM 给身份完全匹配的所属进程，超时不强杀。旧 PID 被复用时绝不向无关进程发信号。

## 更新后的实际生效

安装副本与运行中进程分开验收。所属 API 启动时记录源码内容指纹，status 对照当前源码，报告 source_changed / restart_required。旧记录无指纹、外部实例未登记时标记未知，不能声称已更新。更新文件不自动终止共享服务；需要重启时应打印明确操作提示，并在用户授权后仅重启所属 API，再验证新指纹与实际 MCP 调用。指纹表示启动时源码快照，不代替实际业务回归。

## 验收矩阵

- 空 HOME/CODEX_HOME 安装：MCP 配置、helper、Hook 副本、声明与备份完整。
- 真实已发布版本更新：旧安装元信息与无元信息路径分别验证，定制项和第三方配置保持不变；重复更新幂等，写入失败整批回滚。
- 运行验收：随机端口、隔离数据库，48 路冷连接只有一个 API；启动者退出或中断后连接恢复；API 被终止后下一次连接恢复。
- 原生客户端：临时配置，无模型 turn；发现工具、真实健康调用、退出再进入复用所属进程。
- 卸载：只删除明确属于适配器的项目，保留账号数据、认证文件、第三方 Hook/MCP 与用户修改。
- 仅本次实测平台可声明通过；POSIX runtime 不宣称 Windows 验收通过。

## 显式操作

```sh
python3 scripts/codex_runtime.py configure --api-url http://127.0.0.1:8000 \
  --runtime-dir ~/.codex/ai-team-os/runtime --codex-home ~/.codex
python3 scripts/codex_runtime.py status --api-url http://127.0.0.1:8000 \
  --runtime-dir ~/.codex/ai-team-os/runtime
```

configure 支持多个 --config-file，选定文件必须位于 Codex home 内且不是符号链接。仅修改本适配器 helper 与启动预算，保留其他 TOML 内容，写前备份、失败回滚。源码和系统 Python 必须继续存在。API 日志位于 runtime 目录 api.log。
