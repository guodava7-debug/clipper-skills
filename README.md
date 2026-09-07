# Web Clip + Note Vault

> 收藏之后的那些事：抓取、清洗、摘要、归档、复核、检索。

两个独立、可组合的 Agent Skill，遵循 [Agent Skills 规范](https://agentskills.io/specification)，Cursor、Claude Code、Codex 这些兼容的 agent 都能直接用。

发一条链接说「剪藏并整理」，正文和图片会落成本地 Markdown，摘要和「这篇对你有什么用」写进笔记属性，按你自己的目录习惯归档，一个月后提醒你复核。只说「剪藏」，那就只剪藏，你拿到一份能直接读的本地文件，知识库不动。

## 两个 skill

| Skill | 做什么 | 依赖 |
|---|---|---|
| [`web-clip`](web-clip/) | 网页 → 一份自足的本地 Markdown（clip 包） | 联网；requests + beautifulsoup4 |
| [`note-vault`](note-vault/) | 一份 Markdown → 你知识库里的正确位置 | 全程离线；纯 Python 标准库 |

`web-clip` 去广告去导航，保留图片和公式。PDF 按 `%PDF` 魔数判定，走同一套流程入包。

`note-vault` 归档前先读一遍目标目录，学下已有的组织习惯：目录怎么分、文件怎么命名、frontmatter 用哪些字段、图片链接是 wiki 还是 markdown 风格，然后按这套习惯归档。如果你给的根其实是大库里的某个主题目录，它会改投到更合适的一级目录。去重认四种情况：短链、跳转后的长链、改了追踪参数的同一链接、不同链接下的同一篇正文。

两者靠一个中间格式（clip 包）交接，各自都能单独装。只装 `web-clip`，它是个网页转 Markdown 工具；只装 `note-vault`，它管你手头已有的任何 Markdown。

## 安装

```bash
npx skills add guodava7-debug/clipper-skills    # 交互式选择安装到哪个 agent
```

或者手动把 `web-clip/` 和 `note-vault/` 两个目录放进 agent 的 skills 目录（`~/.cursor/skills/`、`~/.claude/skills/`、`~/.codex/skills/` 等，以实际安装为准）。仓库里的 `tests/`、`evals/` 不用带。

```bash
pip3 install -r web-clip/requirements.txt    # 只有 web-clip 需要；Python 3.8+
python3 <skills目录>/note-vault/scripts/vault.py doctor    # 装完体检一次
```

首次使用直接发个链接说「剪藏并整理」，会问两件事，之后记住：

- 保存位置。用 Obsidian 的话给库的根目录，不要给某个主题子目录，否则所有内容都会被塞进那个主题下面。
- 你是谁。身份、专业、在做的项目，用来写「这篇对你有什么用」。不想说可以跳过，之后不再问。

## 用法

| 你说 | 会发生什么 | 唤起 |
|---|---|---|
| 剪藏这个文章：`<链接>` | 抓取正文和图片，汇报讲了什么和包路径，不入库 | `web-clip` |
| 剪藏并整理这个文章：`<链接>` | 抓取后写摘要和使用说明，按你的目录习惯归档 | 两个 |
| 整理知识库 | 只改写过期笔记的使用说明，不动正文 | `note-vault` |
| 从收藏里找 xxx | 按摘要、标签、使用说明检索 | `note-vault` |
| 归类错了，应该放 xxx | 移动笔记并纠正规则 | `note-vault` |
| 以后默认存到 xxx | 改默认保存位置 | `note-vault` |

`web-clip` 抓完默认不会自作主张归档。这条写在它 SKILL.md 里，是硬规则。

### 收藏后的样子

```markdown
---
createTime: 2026-09-01 20:26
title: Xudong Han (@Xudong07452910) on X
url: https://x.com/Xudong07452910/status/2085939058237432019
hash: 3f0a1c8d5b2e47a9c6103d8e5f2b74a0d9c81e63b45f27a0e8d3c9b17e4a20d5
site: X (formerly Twitter)
published: 2026-08-08
type: 文章
saved_at: 2026-09-01
reviewed_at: 2026-09-01
status: active
topics:
- Agent
- 故障定位
summary: 转述 Scale AI《Model or Harness?》：Agent 失败时要先判断是模型能力问题，还是 harness 问题。
usage: 做 Agent 或评测时，出错先按这个思路区分「改模型」还是「改链路」，避免一上来就微调或换模型。
---

来源：[Xudong Han：Agent 出错以后，到底该修模型，还是修 harness？](https://x.com/...)

![[Agent出错该修模型还是修harness-1bc405-01.jpg]]

Agent 出错以后，到底该修模型，还是修 harness？...
```

`createTime` 这类字段跟随你库里已有的习惯，其余卡片字段由 skill 写入。

## 工作方式

### 正文不进模型上下文

清洗后的 Markdown 由 `fetch` 写进 clip 包，由 `commit` 搬到目标位置，只在磁盘上流动。模型只经手小 JSON。

理解内容分三级：`fetch` 卡片给大纲和摘录；不够时 `digest` 取分节摘句，约 2.8k 字；只有你明确要求精读时才读原文。

### clip 包

图片在抓取时就下载进包里，包脱离网络可读，可以移动、复制、发给别人。

```
<clip>/
├── content.md   清洗后的 Markdown，图片指向 ./assets
├── meta.json    manifest：格式版本、来源、类型、哈希、附件清单
├── assets/      下载好的图片 / PDF
└── raw.html     原始 HTML
```

`meta.json` 声明 `format` 和 `format_version`，路径全部相对包根。`note-vault` 认 major 版本，忽略未知字段。正文和附件都带 `sha256`，`clip.py verify` 可以独立校验。去重用的是图片本地化之前的正文哈希，附件下载成功与否不影响哈希。

### 正文不可变，卡片可变

一篇笔记 = frontmatter 属性 + 正文。正文落盘后不再改写、不删除。`summary` `usage` `topics` `reviewed_at` `status` 这些卡片属性是可变层，整理和检索只操作它。

`commit` 写标题、链接、哈希、来源、类型、入库时间这些由内容决定的字段。`card` 写可变层，只重写 frontmatter 块，正文字节不动。

### summary 和 usage

`summary` 是一句话讲这篇说了什么，内容不变就不改。`usage` 是这篇对你的哪个身份或项目有什么用，复核时只改它。

`persona.md` 记录你的身份、专业、在做的项目，`usage` 据此撰写。画像缺失不阻塞收藏：先落盘，之后再问、再补。

## 站点适配

| 站点 | 处理方式 |
|---|---|
| 微信公众号 | `#js_content` 提取，按 Referer 过防盗链，公式和架构图完整保存 |
| 小红书 | 还原正文换行，剔除 SSR 注释标记，配图从 `og:image` 补齐 |
| X / Twitter | 提取推文正文，配图走 `pbs.twimg.com/media`，排除头像 |
| GitHub | 取 README |
| arXiv | abs 页取摘要，PDF 链接走附件 |
| 知乎 / 少数派 | 付费或登录内容会提示字数偏少 |

被反爬或登录墙拦下时，用浏览器存下 HTML 再 `ingest` 兜底。细节见 [web-clip/sites.md](web-clip/sites.md)。

## 状态文件

状态不在 skill 目录里，重装或更新 skill 不影响它。

```
~/.local/share/web-clip/clips/     # 抓取产物，14 天自动回收
~/.local/share/note-vault/
├── config.json                    # 默认保存位置、已学的归类规则、复核周期
└── persona.md                     # 你的身份画像
```

位置按 `$WEB_CLIP_HOME` / `$NOTE_VAULT_HOME` → `$XDG_DATA_HOME/<名字>` → `~/.local/share/<名字>` 决定。从旧版本升级时，`<skill目录>/state/` 会在首次运行时整体搬过去。想查实际路径，`vault.py state` 看 `state_home`，`clip.py list` 看 `workspace`。

## 命令行

skill 由 agent 驱动，日常不需要手敲。需要排查时：

<details>
<summary>展开命令清单</summary>

```bash
CLIP="python3 <skills目录>/web-clip/scripts/clip.py"
VAULT="python3 <skills目录>/note-vault/scripts/vault.py"

$CLIP fetch <url>                           # 抓取，输出卡片 JSON
$CLIP ingest --file <path> --url <url>      # 从已有 HTML/Markdown 建包
$CLIP list                                  # 列出现有的 clip 包
$CLIP verify <包路径>                        # 校验包的完整性
$CLIP release <包路径>                       # 删除一个包
$CLIP gc --days 7                           # 回收超过 7 天的包

$VAULT lookup --root <库根> --url <url>      # 查是否已收藏
$VAULT profile <目录>                        # 学习目录的归类习惯
$VAULT commit <clip包|笔记.md> --dest <路径>  # 归档
$VAULT card <笔记.md> --usage '…'            # 只改属性，不碰正文
$VAULT digest <clip包|笔记.md>               # 分节摘句
$VAULT review <库根>                         # 列出到期待复核的笔记
$VAULT search <库根> <关键词…>                # 检索
$VAULT state                                 # 查看/修改配置
$VAULT doctor                                # 体检：状态目录、画像、库根
```

</details>

参数全集见 [web-clip/reference.md](web-clip/reference.md) 和 [note-vault/reference.md](note-vault/reference.md)。

## 安全

网页内容和 clip 包一律当作不可信数据，里面出现的任何指令都不执行。

网络这一侧只有 `web-clip` 涉及。它只走 http/https，每一跳重定向都重新校验，域名解析出的每个地址都必须是公网单播，环回、内网、云元数据、IPv6 内嵌私网地址全部拒绝。图片走同一条通道，白名单 MIME，不收 SVG。体积封顶 HTML 8 MB、单图 12 MB、PDF 50 MB。

落盘这一侧两个 skill 都适用。写文件一律临时文件加原子替换。归档是一个事务，附件和笔记要么一起落盘，要么一起回滚。目标路径必须落在指定根目录内。frontmatter 按 YAML 规则安全渲染，网页里的文本伪造不出新的属性字段。

已知残留风险：安全检查解析一次 DNS，socket 连接时又解析一次，中间存在 DNS rebinding 的窗口，目前未处理。

不绕过登录墙或付费墙。

## 开发

```bash
python3 -m unittest discover -s tests -v
```

全部离线。网络层用假的 `requests.get` 打桩，抓取管线跑 `tests/fixtures/` 里的固定页面。测的是不变量，不是实现细节：SSRF 边界拒绝什么、恶意 frontmatter 值能否原样读回、归档失败后附件有没有残留、正文有没有被动过。

改 `description` 时跑一遍 [`evals/trigger-cases.json`](evals/) 确认路由没跑偏。版本变更见 [CHANGELOG.md](CHANGELOG.md)。
