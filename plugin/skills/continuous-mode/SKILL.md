---
name: continuous-mode
description: 启动持续工作模式——Leader自动循环领取和执行任务
---

# 持续工作模式协议

启动后按以下循环工作：

## 启动

1. 调用 `loop_start(team_id)` 启动循环
2. 调用 `taskwall_view(team_id)` 查看任务全景

## 循环

3. 调用 `loop_next_task(team_id)` 获取最高优先级任务
4. 如果有任务：
   - 分析任务需求
   - 动态添加合适的临时成员执行（必须用team_name）
   - 监督执行，完成后Kill临时成员
   - 调用 `PUT /api/tasks/{id}/complete` 标记完成
   - 回到步骤3
5. 如果无任务：
   - 调用 `loop_review(team_id)` 触发回顾讨论
   - 组织会议讨论新方向（动态添加合适参与者）
   - 将讨论结论转为新任务
   - 回到步骤3

## 暂停/恢复

- 收到 `[CONTEXT WARNING]` → `loop_pause` → 保存进度到memory → 提醒用户compact
- 用户说"继续" → `loop_resume` → 从断点继续

## 原则

- 统筹并行推进，不等一个完成再开下一个
- 常驻QA+Bug-fixer始终保持，只Kill临时成员
- 任务不足时讨论方向，不能没事找事干
