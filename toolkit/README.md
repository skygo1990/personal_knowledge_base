# 个人知识库工具包

在不同电脑和 Agent 之间接续工作：读取已批准的偏好、项目、经验与 Markdown 成品；生成待审提案和人读材料，由本人逐次批准后同步到 Git。

支持 macOS / Linux、Python 3.12。原生 Windows 尚未适配。当前提供 Codex 自动接入；其他支持 stdio MCP 的客户端可手动注册同一服务。不会安装或配置模型服务，目标电脑的 Agent 登录应已可用。

## 一条命令安装

目标电脑须已有 **Git、curl、Codex**；程序会复用 uv，缺少时只在工具私有目录安装 uv，并自动准备 Python 3.12。无 sudo，不修改系统 Python、shell PATH 或 Git 全局配置。

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://github.com/skygo1990/personal_knowledge_base/releases/download/v0.2.0/install.sh | sh
```

安装前会显示范围并要求确认。默认关联本仓库 `main`，这是**公开仓库**；安装仅拉取已有知识，不批准或推送。可先下载并阅读 `install.sh` 再执行；wheel 与依赖清单按 Release 中 SHA256SUMS 校验，依赖安装另校验锁定哈希。校验完整性不等于独立数字签名。

希望像现有电脑一样，在自己确认人读材料后由 Agent 通过专用 MCP 执行入库，可显式启用：

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://github.com/skygo1990/personal_knowledge_base/releases/download/v0.2.0/install.sh | sh -s -- --conversation-approval --github-cli
```

`--github-cli` 要求本机已有并登录 GitHub CLI（`gh`），只让审核服务复用该电脑的登录，不复制旧电脑的凭据；没有 gh 时可以省略，使用本机系统 Git 认证。私有仓库的读取也需要本机已有可用的 Git 认证。

同账户模式不提供操作系统级防绕过。**开启能力不代表批准任何记录**，仍须本人确认每次确定的 `REVIEW.md`、内容指纹及推送目标，宿主规则允许后才可调用专用批准接口。Agent 禁止运行审核 CLI 或代输确认短语。

## 安装结果

- 默认安装至 `~/.local/share/ai-context-hub`，程序环境放在 `apps/v0.2.0`；配置、草稿和审核状态在各自独立目录。
- 保留已有 Codex 配置，添加 `personal_knowledge_base` 和有标记的启动规则；修改前备份到私有 `backups`。不拷贝模型配置、Token、旧待审稿或私有知识进安装包。
- Codex 随会话自动启动 MCP，不占端口，不依赖源码目录，不需要另开常驻服务。
- 软件默认六工具：核心、项目、经验、成品、提案、审核包；显式开启对话入库后为九工具，增加批准、状态和重试。

自检与版本：

```sh
~/.local/share/ai-context-hub/pkb --version
~/.local/share/ai-context-hub/pkb doctor --refresh
```

自检实际启动 MCP、列工具并拉取仓库，只显示提交、同步状态和项目数，不打印知识全文或批准内容。它不证明模型账户已登录，也不测试真实推送。

## 开始使用

1. 新开 Codex 会话，说“读取个人知识库，告诉我当前项目和来源”。只有成功拉取的正式记录才算知识；空库正常，程序不会伪造个人事实。
2. 完成工作后说“总结本次有长期价值的内容，先查已有同主题记录，生成待审提案”。已有主题整合为同 ID、同语义文件名的完整修订，不按日期另建。
3. 打开 Agent 返回的 `REVIEW.md`，检查全文、差异、受众和公开风险。可以要求改稿，修改后重新预览。
4. 若已启用对话入库且当前宿主允许，明确说“批准这份交付入库并推送到这里列明的仓库和分支”。Agent 只能执行这一份批准，不能代你决定内容；未启用时由本人用终端审核。
5. 只有 `synced` 表示远端已收到。下一台电脑刷新读取后才报告本机已更新；网络失败保留原批准批次，重试不重复入库。

**本仓库公开，批准推送会公开正文、元数据、标题、路径和 Git 历史。不要批准私人材料。** 文档可外发许可与真实发布事件仍分开记录；“入库”不自动批准对外分享。

本人终端入口（不是供 Agent 调用的批准工具）：

```sh
~/.local/share/ai-context-hub/pkb review list
~/.local/share/ai-context-hub/pkb review preview --proposal proposal-实际ID
~/.local/share/ai-context-hub/pkb review approve --proposal proposal-实际ID
```

## 自定义与重复安装

- `PKB_HOME=/绝对路径 sh install.sh` 更换安装根目录；`--remote URL --branch 分支名` 指定知识仓库。
- `--client none` 只安装服务、不修改客户端；手动 stdio 命令为该虚拟环境的 `python -m ai_context_hub serve --home /运行目录`。
- `--codex-home /目录` 指定 Codex 配置位置，默认使用 `CODEX_HOME` 或 `~/.codex`。
- `--accept-install` 明确接受安装范围，适合非交互安装；这不是批准任何知识的开关。
- 已有配置和提案不会覆盖。不同仓库、审批或认证模式会拒绝自动更换；更换同名客户端启动路径须显式 `--replace-client`，只替换该 MCP，不改其他服务。
- 从旧源码方式迁移：保持原运行目录，用 `sh install.sh --replace-client`。已有对话批准及认证配置会保留，无需复制／删除批准状态。
- `PKB_BUNDLE_DIR=/已下载文件目录 sh install.sh` 使用本地 Release 资产；目录应含 wheel、requirements.lock、SHA256SUMS，依赖尚未缓存时仍需联网。

安装失败会退出非零，不把“装好程序”说成“已同步”。默认保留安装目录、备份和待审材料供排查，不自动删除。暂停客户端可将该 MCP 的 `enabled` 设为 `false`，不要删除审核状态来停用。

## 开发与分发

源码位于本仓库的 `toolkit/`。`knowledge/` 是现有知识数据，不属于软件包；打包仅白名单包含程序和通用说明，不读取个人提案目录。

```sh
cd toolkit
uv sync --frozen
uv run --frozen pytest -q
uv build
uv export --frozen --no-dev --no-emit-project --no-annotate --no-header -o requirements.lock
```

安装脚本使用锁定依赖及固定 Release，不从知识 Markdown 执行代码。软件版本只用于安装包升级；知识文档仍按同一记录覆盖更新，不引入作品版本管理。

## 验证边界

已在 macOS 的独立空目录运行安装、重复安装、实际 stdio 九工具调用和临时 Git 拉取，并验证已有规则及待审文件保留、正式 Git 未被修改。Linux x86_64 / ARM64 的 Python 3.12 锁定二进制依赖已通过跨平台解析预检；尚未做 Linux 实机端到端运行，不把依赖预检当作实机验收。模型登录、私有仓库授权及真实批准后的推送需要各电脑分别验证。
