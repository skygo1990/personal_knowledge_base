---
id: core-391caf1a4f9e4d7a94cf6ba9fa10ba0c
type: core
kind: preference
title: 编码实施与验证偏好
scope:
- global
status: active
updated_at: '2026-11-23T10:00:18.625543+00:00'
tags:
- 个人偏好
- 编码
- 验证
aliases:
- AGENTS编码偏好
related_ids:
- deliverable-4c55583fc4274ee982561ac914559b19
supersedes: []
evidence_summary: 主人明确要求将仓库 AGENTS.md 保存为个人偏好；来源：skygo1990/personal_knowledge_base，提交
  080f5e1e0eeee4b926f2ccc0432d543dc7ae4c1a，路径 AGENTS.md。
views:
- coding
strength: constraint
slug: coding-preferences
---
编码前查仓库、分支、已有改动及项目规则，默认当前分支。明确行为和验收，最小修改，遵循现有风格，不顺手重构、预先抽象或增加未要求功能；小工具少文件。
优先标准库和已有依赖，新增先说明必要性并沿用包管理器。Bug先复现定位；不跳过检查或削弱断言。外部接口核对官方资料逐字段比较；两轮无进展换方法。
清理本次无用代码，无关问题只提示；删除须确认。运行最窄有效验证及必需检查，未验证说明影响。提交须授权，中文信息加init/feat/fix/refactor/docs/chore前缀，不加Co-Authored-By。
行为命令变化更新相关文档，不强建文档；交付结果、改动、验证、限制。完整细则见关联原文。
