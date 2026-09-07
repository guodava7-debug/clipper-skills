# Web Clip 参考

## clip 包

一次抓取产出一个自足的目录，是本 skill 对外的唯一交付物：

```
<clip>/
├── content.md   清洗后的 Markdown，图片指向 ./assets
├── meta.json    元数据（见下）
├── assets/      下载好的图片 / PDF
└── raw.html     原始 HTML（fetch 默认保留，ingest 默认不留）
```

默认写在 `$WEB_CLIP_HOME/clips/<12位十六进制>/`，目录名由规范化 URL 的哈希决定，因此重抓同一个链接会原地覆盖。`--out <目录>` 可以把包写到别处（例如导出给用户自己看）。

`$WEB_CLIP_HOME` 缺省是 `$XDG_DATA_HOME/web-clip`，再缺省 `~/.local/share/web-clip`——**运行期状态一律不放在 skill 目录里**，升级、重装、同步 skill 都不会碰到用户的包。老版本留在 `<skill目录>/state/` 的数据会在首次运行时整体搬过去。

超过 14 天的包在下次 fetch/ingest 时自动回收，用 `WEB_CLIP_GC_DAYS` 改阈值；`gc` 手动触发，`release` 立即删单个包且只允许删 workspace 内的包。

### 包格式契约

`meta.json` 里 `format` 恒为 `web-clip/clip-package`，`format_version` 形如 `<major>.<minor>`。消费方（note-vault）**认 major、忽略未知字段**：minor 升级只加字段，major 变了就该报错而不是猜。包内所有路径都写在 `files` 里且相对包根，所以整个目录可以随便移动或复制。

`body_sha256` 是 `content.md` 落盘时的哈希，`verify` 和 note-vault 都用它判断包有没有被改过。

### meta.json 字段

| 字段 | 含义 |
|---|---|
| `format` / `format_version` | 包格式标识与版本，当前 `web-clip/clip-package` `1.0` |
| `files` | `{content, assets_dir, raw_html}`，全部为包内相对路径 |
| `clip_id` / `clip_dir` | 包名与写包时的绝对路径（仅供参考，以相对路径为准） |
| `url` / `final_url` / `canonical_url` | 原始链接 / 跳转后 / 去掉追踪参数的规范链接 |
| `title` `site` `author` `published` `lang` | 页面元数据，`published` 已归一为 `YYYY-MM-DD` |
| `type` | 脚本判定：`文档 / 教程 / 文章 / 新闻 / 论文 / 讨论 / 视频` |
| `kind` | `page` 或 `pdf` |
| `extractor` | 命中的提取路径，排查抓取质量时看它 |
| `chars` | 正文可见字符数 |
| `assets` | `[{file, url, alt, media_type, bytes, sha256}]`，已落盘的附件 |
| `asset_failures` | `[{url, reason}]`，下载失败的图片 |
| `pdf` | PDF 包才有，形如 `assets/xxx.pdf` |
| `counts` | images / links / code_blocks / tables / headings |
| `content_sha256` | **内容身份**：图片本地化之前的正文哈希，供下游去重 |
| `body_sha256` | `content.md` 实际落盘内容的哈希，供完整性校验 |
| `fetched_at` `elapsed_ms` `warnings` | 抓取时间、耗时、告警 |

`content_sha256` 和 `body_sha256` 分开是有意的：图片这次下成功、下次被防盗链挡住，落盘正文会不一样，但**这篇文章还是同一篇**。去重必须用前者，否则重抓同一个链接会被当成新内容。

## 命令参数

**fetch** `<url>`
- `--out PATH` 把包写到指定目录，缺省写进 workspace
- `--assets download|none`（默认 download）
- `--outline N` 卡片大纲条数，默认 12
- `--excerpt N` 卡片摘录字符数，默认 160
- `--no-keep-raw` 不保留原始 HTML

**ingest** `--file PATH --url URL`
接受 HTML 或已转好的 Markdown（自动判别），用于 fetch 被拦截时。可补 `--title/--site/--author/--published`，其余参数同 fetch。

**list**
列出 workspace 里的包（路径、标题、链接、抓取时间、字数）。忘了包路径时用它。

**verify** `<包路径>`
对着 manifest 校验：格式版本、`content.md` 是否匹配 `body_sha256`、每个附件在不在且哈希对不对。包被移动过、手改过，或归档报错时用它定位。有问题退出码 `1`。

**release** `<包路径>`
删除一个包。路径必须落在 workspace 内，否则拒绝。

**gc**
`--days N` 删掉早于 N 天的包（缺省取 `WEB_CLIP_GC_DAYS`，再缺省 14），`--all` 清空 workspace。

退出码：`0` 成功，`1` 错误。

## 三级理解梯子

抓取本身不产生任何理解，卡片只是原料。判断这篇讲了什么，按成本从低到高：

1. **fetch 卡片**（`outline` + `excerpt`）——绝大多数情况够用
2. **`vault.py digest <clip包>`**——分节摘句，约 2.8k 字，写摘要时用
3. **原文**——只有用户明确要求精读时才读

跳过前两级直接读正文，会让单次剪藏的 token 成本上升一个数量级，而且必然出现改写、遗漏和幻觉。

## 图片本地化

图片在 fetch 阶段就下载，命名为 `<标题片段>-<URL哈希6位>-<序号>.<扩展名>`：URL 哈希保证多篇笔记共用一个附件目录时不会互相覆盖，名字里不含空格和链接标点，因此 Markdown 链接和 Obsidian wiki 链接都不会被打断。下载走原文 Referer 以过防盗链。

图片 URL 来自页面，属于攻击者可控输入，因此和主页面走同一条安全通道：逐跳校验重定向、只认公网地址、按白名单收 MIME（**不收 SVG**）、payload 像 HTML 或 PDF 就丢弃、单图 12 MB 封顶。失败的图片会在 `content.md` 末尾留一行说明，并记进 `asset_failures`。

## 网络边界

| 限制 | 值 |
|---|---|
| 协议 | 只允许 http / https |
| 目标地址 | 域名解析出的**每一个**地址都必须是公网单播；环回、私网、链路本地、云元数据、IPv6 里内嵌的私网地址全部拒绝 |
| 重定向 | 最多 5 跳，**每一跳都重新校验**，不交给 requests 自动跟随 |
| 体积 | HTML 8 MB，单图 12 MB，PDF 50 MB；先看 `Content-Length`，流式读时再兜一次 |

被拒绝时报 `refused to fetch`，这是策略结论，不要用 ingest 或浏览器绕过。

已知残留风险：安全检查解析一次 DNS，实际连接时 socket 又解析一次，掌握域名的攻击者可以用 DNS rebinding 在两次之间换掉地址。彻底修需要把校验过的 IP 钉进连接，暂未做——所以 clip 包始终按不可信输入对待。

## 依赖

```bash
pip3 install -r requirements.txt
```
