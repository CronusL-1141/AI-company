---
name: os-meeting
description: 查看和创建 AI Team OS 会议
---

# /os-meeting — 会议管理

## 用法

- `/os-meeting` — 列活跃会议：`meeting_list(status="active")`
- `/os-meeting <meeting_id>` — 看某场会议：`meeting_read_messages(meeting_id)`，按
  `round_number` 分组展示；要确认是否全员发过言用 `meeting_attendance_check(meeting_id)`
- `/os-meeting create <主题>` — **走技能 meeting-facilitate**，见下

## create 必须走技能

`meeting_create` 只写库，**不会拉任何一个 agent 到场**。只调它就向用户汇报"会议已创建"，
出勤校验会显示 0 人发言，而你会去查 agent 为什么不说话——其实没人被 spawn 过。

选模板、spawn 真实参与者、签到、推进轮次、conclude 并把决策上墙，完整流程在技能
**meeting-facilitate** 里。
