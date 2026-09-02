# AI 情报站 Telegram 自动发布器

每天中国时间上午 9:00，通过 OpenAI Responses API 的网页搜索筛选过去 24 小时的重要 AI 新闻，生成一篇中文简报，并用 Telegram Bot API 一次性发送到频道 `@aiqinbaozhan`。

## 它会做什么

- 每天发布 4～6 条经过时间和来源检查的 AI 新闻。
- 每条包含“发生了什么”“为什么重要”和来源链接。
- 最后给出“今日值得关注的一件事”。
- 使用自己的中文表达，不照抄新闻原文。
- 严格限定过去 24 小时；条目不足、时间不符、URL 无效或帖子超过 Telegram 4096 字限制时直接失败，不发布低质量内容。
- 默认 `DRY_RUN=true`，只在 Actions 日志预览，不发送消息。
- 用中国日期做防重；真实发布前先在仓库写入 `sending` 占位，再调用 Telegram，避免重跑造成当天重复发送。

## 项目结构

```text
.
├─ .github/workflows/publish.yml  # 定时与手动工作流
├─ src/ai_news_bot.py             # 生成、校验、去重与发布
├─ tests/test_ai_news_bot.py      # 离线单元测试
└─ state/publish-state.json       # 当天发送状态（不含密钥）
```

## 第一步：创建 GitHub 仓库

1. 登录 GitHub，点击右上角 **+ → New repository**。
2. Repository name 建议填写 `aiqinbaozhan`。
3. 选择 **Private** 或 **Public** 均可。
4. 如果你准备上传本项目现有代码，创建时不要勾选自动添加 README、`.gitignore` 或 License，以免首次推送冲突。
5. 点击 **Create repository**。

## 第二步：上传代码

在本项目目录打开终端，把下面的 `YOUR_GITHUB_NAME` 替换成你的 GitHub 用户名：

```bash
git init
git add .
git commit -m "Initial commit: AI 情报站自动发布器"
git branch -M main
git remote add origin https://github.com/YOUR_GITHUB_NAME/aiqinbaozhan.git
git push -u origin main
```

也可以在空仓库页面点击 **uploading an existing file**，上传本项目全部文件。请确认 `.github/workflows/publish.yml` 也已上传；某些系统会隐藏以点开头的目录。

## 第三步：准备 Telegram 频道

1. 在 Telegram 联系 `@BotFather`，用 `/newbot` 创建机器人并取得 Bot Token。
2. 打开频道 `@aiqinbaozhan` 的管理设置，把机器人添加为管理员。
3. 至少授予机器人 **Post Messages / 发布消息** 权限。
4. 不要把 Bot Token 发到聊天、Issue、代码、README 或 Actions 普通变量中。

## 第四步：添加 GitHub Secrets 和 Variables

进入仓库 **Settings → Secrets and variables → Actions**。

在 **Secrets** 标签点击 **New repository secret**，逐个添加：

| Name | Secret |
|---|---|
| `OPENAI_API_KEY` | 你的 OpenAI API Key |
| `TELEGRAM_BOT_TOKEN` | BotFather 给出的 Telegram Bot Token |

在 **Variables** 标签点击 **New repository variable**，添加：

| Name | Value |
|---|---|
| `TELEGRAM_CHANNEL` | `@aiqinbaozhan` |
| `DRY_RUN` | `true` |

密钥只应放在 **Secrets**。不要把任何 Token 或 API Key 写入 Variables。

## 第五步：先用 DRY_RUN 测试

1. 打开仓库的 **Actions** 标签。
2. 左侧选择 **发布每日 AI 情报**。
3. 点击 **Run workflow**。
4. `dry_run` 保持为 `true`，再次点击绿色 **Run workflow**。
5. 打开本次运行，展开 **生成并按配置发布**。
6. 日志应显示一篇完整帖子，并出现：`DRY_RUN=true：仅输出预览`。
7. 确认频道没有收到消息。DRY_RUN 不调用 Telegram，也不修改防重状态。

如果失败，日志只会输出经过脱敏的错误，不会输出请求头或密钥。常见原因是 OpenAI API 账户未启用计费、模型无权限、搜索暂时失败，或严格筛选后不足 4 条合格新闻。

## 第六步：切换成真实发布

完成预览后：

1. 回到 **Settings → Secrets and variables → Actions → Variables**。
2. 把 `DRY_RUN` 的值从 `true` 改成 `false`。
3. 定时任务会在每天 `01:00 UTC` 运行，即中国时间 `09:00`。

GitHub 的 cron 任务有时会因平台排队晚几分钟启动，但 cron 配置本身就是中国时间上午 9:00。

## 第七步：手动触发一次真实测试

1. 打开 **Actions → 发布每日 AI 情报 → Run workflow**。
2. 把 `dry_run` 选为 `false`。
3. 点击 **Run workflow**。
4. 成功后日志会显示 Telegram 的 `message_id`，`state/publish-state.json` 会被工作流自动提交为当天的 `published` 状态。

同一中国日期再次用 `false` 运行时，程序会在生成内容之前跳过，既不重复调用 OpenAI，也不重复发送 Telegram。

> 手动运行表单中的 `dry_run` 会覆盖仓库变量 `DRY_RUN`；定时运行使用仓库变量。

## 防重复与故障恢复

真实发布采用“先占位、后发送”策略：

1. GitHub Actions 的 `concurrency` 保证同一时间只有一个发布任务执行。
2. 发布前把当天状态提交为 `sending`。
3. Telegram 返回明确成功和 `message_id` 后，再把状态改成 `published`。
4. 当天状态为 `sending` 或 `published` 时，后续运行都跳过。

这样即使 Telegram 已收到消息、但工作流在保存最终状态前断开，也不会重复发送。代价是：如果 Telegram 请求结果不明确，程序宁可停止当天自动重试，也不冒险重复发帖。

遇到 `sending` 状态的恢复方法：

1. 先人工检查频道当天是否已经有帖子。
2. 如果已经发布，不要重跑；可以把状态文件中的 `status` 手动改为 `published`。
3. 如果确认频道没有帖子，才可把 `state/publish-state.json` 恢复为：

```json
{
  "date": null,
  "status": "never_published"
}
```

4. 提交修改后，再手动运行一次 `dry_run=false`。

## 安全设计

- 程序只从环境变量读取密钥；GitHub Actions 从 Repository Secrets 注入。
- 代码、README、初始状态文件和测试数据不包含真实密钥。
- 错误日志会替换已加载的 OpenAI、Telegram 和 GitHub Token，并额外过滤常见 Token 格式。
- 不会打印 API 请求头。
- `GITHUB_TOKEN` 由 GitHub Actions 自动生成，用于写入防重状态，不需要你创建。
- 工作流只有 `contents: write` 权限。

## 本地运行（可选）

需要 Python 3.11 或更高版本。项目只使用 Python 标准库，无需安装第三方依赖。

运行测试：

```bash
python -m unittest discover -s tests -v
```

本地 DRY_RUN 预览：

```bash
export OPENAI_API_KEY="在你自己的终端设置，不要写入文件"
export DRY_RUN="true"
python src/ai_news_bot.py
```

Windows PowerShell：

```powershell
$env:OPENAI_API_KEY = "在你自己的终端设置，不要写入文件"
$env:DRY_RUN = "true"
python src/ai_news_bot.py
```

## 可选调整

- 默认模型是 `gpt-5.4-mini`。如需临时更换，可在运行环境设置 `OPENAI_MODEL`；无需改代码。
- 发布时间由 `.github/workflows/publish.yml` 的 `cron` 控制。GitHub 使用 UTC，当前 `0 1 * * *` 对应中国时间每天 09:00。

