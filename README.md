# Paper Digest Pipeline

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

一个面向 arXiv 和会议论文的自动化流水线：按研究偏好发现候选论文，用 LLM 对每篇论文做明确的 `include/exclude` 判定，再读取入选论文的 PDF 正文并生成“问题背景、Motivation、核心 Idea、实现级 Method、实验及其说明、结论”六段式总结。

## 核心信息流

`论文源 → 最多 2000 篇候选 → 硬排除词 → LLM 分批二元判断 → 最多保留 500 篇 → 最多 N 篇全文总结 → Markdown/LaTeX/PDF → 邮件`

- 筛选不按相关性分数排序。`selection.decision_policy` 是最终收录边界，模型必须为每篇论文返回 `include` 或 `exclude` 及原因。
- `discovery.max_candidates`、`selection.max_selected_papers` 和 `review.max_papers` 相互独立。保留 500 篇不会自动总结 500 篇。
- 全文总结默认读取 PDF，并在附录、补充材料、致谢或参考文献前停止；无法取得全文时会明确标为摘要证据。
- `selection-decisions.json` 保存全部筛选决定，便于校验准确性。

## 本地运行

项目的所有非敏感参数都集中在带中文注释的 [`config.toml`](config.toml) 中。

唯一例外是每日触发的 cron 时间：GitHub 必须在 workflow YAML 加载前读取它，因此不能从 TOML 动态取得，需在 `.github/workflows/daily-digest.yml` 的 `cron` 一行修改。论文源、偏好、数量、模型、预算、输出和邮件参数仍全部由 `config.toml` 管理。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[pdf]"
$env:PAPER_DIGEST_API_KEY = "你的模型服务密钥"
paper-digest run --config config.toml
```

指定日期或临时覆盖总结数量：

```powershell
paper-digest run --config config.toml --date 2026-07-30 --max-papers 10
```

零模型调用的规则预览：

```powershell
paper-digest run --config config.toml --dry-run
```

若要总结全部入选论文，把 `config.toml` 中的 `review.max_papers` 改为与 `selection.max_selected_papers` 相同，例如 500。费用和运行时间也会相应增加。

## 部署到 GitHub Actions

仓库已包含 [`.github/workflows/daily-digest.yml`](.github/workflows/daily-digest.yml)。默认每天 `01:30 UTC` 触发，即北京时间约 `09:30`。GitHub 的定时任务可能因平台排队延后几分钟，并不保证秒级准时。

### 1. 调整单一配置文件

直接编辑并提交 `config.toml`。常改参数包括：

- `preferences.interests`：用自然语言写研究对象、问题和方法偏好。
- `preferences.include_keywords`：缩写、别名、任务名和模型家族，只作正向提示。
- `preferences.exclude_keywords`：不可撤销的硬排除，应尽量保守。
- `preferences.categories`：arXiv 服务端检索类别；过窄会在 LLM 筛选前漏掉论文。
- `discovery.date = "yesterday"`：每日任务推荐值。
- `review.max_papers`：每次真正读取 PDF 并生成六段式总结的数量。
- `email.provider`：选择 `qq`、`gmail` 或 `netease`，SMTP 地址、端口和加密方式会自动配置。

#### 参数配置速查

下面按实际执行顺序说明 `config.toml` 中最重要的参数：

| 配置项 | 作用 | 调整建议 |
|---|---|---|
| `project.language` | 六段式总结使用的语言 | 中文用 `zh-CN`，英文可用 `en` |
| `project.output_dir` | 产物根目录 | Actions 保持 `outputs` 即可 |
| `preferences.interests` | LLM 筛选的主提示 | 每条写一个清晰方向，包含对象、问题和偏好方法 |
| `preferences.include_keywords` | 正向别名和术语提示 | 放缩写、模型名、任务名；不要求论文必须命中 |
| `preferences.exclude_keywords` | LLM 前的硬排除 | 只放百分之百不想看的短语，宁缺毋滥 |
| `preferences.categories` | arXiv 服务端类别过滤 | 为保证召回率可覆盖 `cs.AI/cs.LG/cs.CL/cs.CV/cs.MM/cs.IR/cs.RO/stat.ML` |
| `discovery.source` | 论文来源 | 每日 arXiv 用 `arxiv`；会议可用 `openreview`、`crossref` 或 `json` |
| `discovery.date` | arXiv 提交日期 | 每日邮件固定用 `yesterday`，避免当天数据尚未完整 |
| `discovery.max_candidates` | 最多进入筛选的论文数 | 完整扫描上限可设 2000；触顶时 manifest 会提示 |
| `selection.max_selected_papers` | 二元判断后最多保留数 | 当前建议 500；它不是总结数量 |
| `selection.llm_batch_size` | 单次筛选请求论文数 | 40 是成本、输出长度和稳定性的折中值 |
| `selection.decision_policy` | include/exclude 的最终边界 | 明确“哪些必须收、哪些必须排”，不要写模糊打分要求 |
| `review.max_papers` | 每日做全文总结的数量 | 初次部署先设 1，稳定后改为 10 或所需数量 |
| `backend.base_url/model` | 模型接口和模型名 | DeepSeek 示例已配置；换服务商时一并修改 |
| `backend.api_key_env` | API Key 所在环境变量名 | Actions 保持 `PAPER_DIGEST_API_KEY`，这里不能填 Key 本身 |
| `fulltext.download_pdf` | 是否读取论文 PDF | 高质量总结保持 `true` |
| `fulltext.max_main_text_chars` | 主文截断上限 | 默认 180000；仍会优先在附录/参考文献前停止 |
| `budget.max_total_tokens` | 单次任务 token 硬上限 | 大规模总结前按篇数和模型上下文调高 |
| `budget.max_estimated_usd` | 单次任务美元预算硬上限 | 应与真实单价一起维护，避免意外消费 |
| `output.compile_pdf` | 是否生成 PDF | Actions 已安装 XeLaTeX，建议保持 `true` |
| `email.enabled` | 是否发送邮件 | 启用每日邮件设为 `true` |
| `email.provider` | 邮箱服务商预设 | `qq`、`gmail`、`netease`；`auto` 可按发件地址识别，`custom` 用于其他邮箱 |
| `email.smtp_host/port/security` | SMTP 手动覆盖 | 三种内置服务商保持空值、`0`、`auto`；只有自定义邮箱才需要填写 |
| `email.max_attachment_mb` | 单个附件大小上限 | 常见邮箱用 20 MB 较稳妥；超限文件仍可从 Artifact 下载 |

`decision_policy` 的写法尤其重要。推荐按“收录条件 A/B + 排除条件 + 证据不足如何处理”的结构写，例如：论文的主要贡献必须直接研究多模态学习或 OPD；只是在应用背景中顺带提及多模态的论文排除；仅凭标题和摘要无法确认时排除。模型会把同一规则应用到每个批次，不会为单批次设置配额。

### 2. 添加 GitHub Secrets

进入仓库 `Settings → Secrets and variables → Actions → New repository secret`，添加：

| Secret | 用途 |
|---|---|
| `PAPER_DIGEST_API_KEY` | DeepSeek 或其他兼容模型服务的 API Key |
| `PAPER_DIGEST_SMTP_USERNAME` | 发件邮箱完整地址 |
| `PAPER_DIGEST_SMTP_PASSWORD` | 邮箱生成的 SMTP 授权码，不是网页登录密码 |
| `PAPER_DIGEST_EMAIL_TO` | 收件邮箱；多个地址用英文逗号分隔 |
| `PAPER_DIGEST_EMAIL_FROM` | 可选；一般与发件邮箱相同，未配置时自动使用 username |

不要把上述值写进 `config.toml`、`.env`、工作流或提交历史。

### 3. 选择邮件服务商

三种邮箱使用相同的 GitHub Secret 名称，只需要修改 `config.toml` 中的一行。`smtp_host = ""`、`smtp_port = 0` 和 `security = "auto"` 保持不变。

QQ 邮箱：

```toml
[email]
enabled = true
provider = "qq"
```

`PAPER_DIGEST_SMTP_USERNAME` 填完整 QQ 邮箱地址，例如 `example@qq.com`；密码 Secret 填 QQ 邮箱设置中生成的 SMTP 授权码。

Google Gmail：

```toml
[email]
enabled = true
provider = "gmail"
```

`PAPER_DIGEST_SMTP_USERNAME` 填完整 Gmail 地址。Google 账号需要先开启两步验证，再创建 App Password，并把该 App Password 放入 `PAPER_DIGEST_SMTP_PASSWORD`。不要使用 Google 登录密码。

网易邮箱：

```toml
[email]
enabled = true
provider = "netease"
```

程序会根据 `PAPER_DIGEST_SMTP_USERNAME` 的后缀自动支持 `@163.com`、`@126.com`、`@yeah.net`、`@vip.163.com` 和 `@vip.126.com`。密码 Secret 填网易邮箱设置中生成的客户端授权码。

内置预设如下：

| 邮箱 | Host | Port | Security | 说明 |
|---|---:|---:|---|---|
| QQ 邮箱 | `smtp.qq.com` | `465` | `ssl` | 在邮箱设置中开启 SMTP 并生成授权码 |
| Gmail | `smtp.gmail.com` | `465` | `ssl` | 开启两步验证后创建 App Password |
| 网易 163 | `smtp.163.com` | `465` | `ssl` | 开启 SMTP 并使用客户端授权码 |
| 网易 126 | `smtp.126.com` | `465` | `ssl` | 根据发件地址自动识别 |
| 网易 yeah.net | `smtp.yeah.net` | `465` | `ssl` | 根据发件地址自动识别 |

其他企业邮箱可设 `provider = "custom"`，并明确填写 `smtp_host`、`smtp_port` 和 `security = "ssl"` 或 `"starttls"`。

### 4. 启用和测试

打开仓库的 `Actions` 页面，选择 `Daily paper digest`，点击 `Run workflow`。第一次建议手动指定一个日期并把总结数量临时设为 1，确认模型调用、PDF 生成和邮件发送全部正常。验证通过后无需其他操作，计划任务会每天运行。

每次运行都会：

1. 安装 Python、PDF 解析依赖和中文 XeLaTeX；
2. 使用 `config.toml` 抓取、筛选和总结论文；
3. 成功、部分完成或失败时都尝试发邮件；
4. 将 `outputs/`、`run-result.json` 和 `run.log` 保存为 30 天的 Actions Artifact；
5. 生成失败、只完成一部分或邮件发送失败时将任务标红。

对于公开仓库，GitHub 可能在仓库连续 60 天无活动后自动禁用 scheduled workflow；届时在 Actions 页面重新启用即可。

### 5. 正式启动每日邮件服务

完成一次手动试跑后，按下面清单确认即可正式启用：

1. `config.toml` 中设置 `discovery.date = "yesterday"` 和 `email.enabled = true`。
2. 把 `review.max_papers` 从试跑的 1 调整为日常需要的数量，例如 10。
3. 确认上述 4 个必需 Secrets 均已添加，SMTP 使用的是授权码/App Password。
4. 确认仓库 `Actions` 页面没有显示 workflows 被禁用；若有提示，点击启用。
5. 保持 `.github/workflows/daily-digest.yml` 位于默认分支。之后无需保持本地电脑开机，任务运行在 GitHub 托管机器上。

默认发送时间为北京时间约 09:30。要改为北京时间每天 08:00，把 workflow 中 cron 改成 `0 0 * * *`；北京时间 = UTC + 8。修改后提交并推送到默认分支才会生效。

邮件主题会包含状态、项目名和配置日期；正文列出候选数、include/exclude 数、计划/完成总结数和失败数。成功时附 `digest.pdf` 与 `digest.md`，部分完成或失败时还会附 `run.log`。邮件附件缺失或过大时，可在对应 Actions 运行页下载完整 Artifact。

## 手动测试邮件

先设置邮件环境变量，再复用某次运行结果：

```powershell
$env:PAPER_DIGEST_SMTP_USERNAME = "sender@example.com"
$env:PAPER_DIGEST_SMTP_PASSWORD = "邮箱授权码"
$env:PAPER_DIGEST_EMAIL_TO = "receiver@example.com"
paper-digest email --config config.toml --result outputs\arxiv-YYYY-MM-DD\manifest.json --status success
```

## 来源与输出

- `arxiv`：按提交日期和类别查询官方 Atom API。
- `crossref`：按会议名和日期范围检索 proceedings 元数据。
- `openreview`：按 venue ID 获取已接收或全部投稿。
- `json`：读取规范化本地论文列表。

每个任务目录包含 `candidates.json`、`selection-decisions.json`、`selected.json`、`state.json`、单篇检查点、PDF、`manifest.json` 和配置要求的 digest 文件。重复运行相同来源与日期时会复用已完成总结；使用 `--force` 才会重新生成。

## 成本控制

筛选阶段只发送标题、类别和截断摘要；只有最终总结目标才下载全文。`budget.max_total_tokens` 与 `budget.max_estimated_usd` 是硬门槛。请按实际服务商价格维护单价；单价为 0 时只执行 token 限制。

## License

本项目采用 [Apache License 2.0](LICENSE)。它允许商用、修改、再发布和私有使用，同时要求保留许可证与版权声明，并提供明确的专利授权。对于可能被集成、二次开发的 AI 工具项目，它比 MIT 多了一层清晰的专利保护。
