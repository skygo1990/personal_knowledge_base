# 个人知识库

跨电脑、跨 Agent 的 Markdown 知识库：读取已确认偏好、项目和经验，生成完整待审材料，经本人逐次批准后同步。

- [`toolkit/`](toolkit/)：可安装的 Python / MCP 工具包及通用使用说明。
- [`knowledge/`](knowledge/)：已批准的知识数据，**不包含在软件安装包中**。
- [下载 v0.2.0 安装包](https://github.com/skygo1990/personal_knowledge_base/releases/tag/v0.2.0)。

## 新电脑安装

macOS / Linux，须已有 Git、curl 和 Codex。安装器在用户私有目录准备 uv、Python 和工具，不需要 sudo：

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://github.com/skygo1990/personal_knowledge_base/releases/download/v0.2.0/install.sh | sh
```

安装时确认范围；安装完成后重开 Codex，说“读取个人知识库，告诉我当前项目和来源”。完整参数、对话批准开关及排错见 [工具包说明](toolkit/README.md)。

**此仓库公开。** 安装不批准任何内容；每次入库仍需本人审核，推送会公开正文、元数据和历史。同账户模式不提供操作系统级防绕过保障。原生 Windows 尚未适配。
