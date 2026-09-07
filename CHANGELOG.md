# 更新日志

## 2.0.0

对标了一批热门 Agent Skills 项目之后的一次加固。架构没变——还是 `web-clip` 抓取、`note-vault` 归档两个 skill，中间靠 clip 包交接——变的是这三样东西各自的可靠性。

### 破坏性变更

**状态目录移出 skill 目录。** clip 包、`config.json`、`persona.md` 现在住在 `$WEB_CLIP_HOME` / `$NOTE_VAULT_HOME`（缺省 `~/.local/share/<名字>`）。首次运行会把老的 `<skill目录>/state/` 整体搬过去，不需要手工处理；搬完之后更新或重装 skill 再也不会碰到用户数据。

**clip 包 manifest 换成带版本的格式。** `meta.json` 里 `schema: 3` 换成 `format: "web-clip/clip-package"` + `format_version: "1.0"`，新增 `files`（包内相对路径）、`body_sha256`，附件条目带上 `media_type` / `bytes` / `sha256`。`note-vault` 只接受它认识的 major 版本，遇到不认识的直接报错而不是猜。手上没归档完的旧包仍然能归档，结果里会带一条 `package predates the versioned format` 的提示。

**去重哈希改为 `content_sha256`，且在图片本地化之前计算。** 原来的 `content_sha1` 算的是落盘正文，图片下没下成功会改变哈希，同一篇文章重抓一次就可能被当成新内容。已归档的老笔记仍然靠 URL 去重，不受影响；`lookup --hash` 现在可以重复传，用来同时比对新旧口径。归档 1.0 之前的旧包时会回落到 `content_sha1`，所以手上没归档完的旧包仍然能正常去重。

### 安全

- 抓取只走 http/https，**每一跳重定向都重新校验**（不再交给 requests 自动跟随），域名解析出的每个地址都必须是公网单播。原来的正则黑名单挡不住 DNS 指向内网、`::ffff:127.0.0.1` 这类 IPv6 内嵌私网地址，以及一跳跳进 `169.254.169.254` 的重定向。
- 页内图片走同一条通道。它们是页面给的、攻击者可控的 URL，之前直接下载。
- 图片按白名单收，**不再保存 SVG**——它是能带脚本的 XML 文档，存进笔记库等于给所有查看器留一个存储型 XSS。payload 看起来是 HTML 或 PDF 一律丢弃。
- 体积封顶：HTML 8 MB、单图 12 MB、PDF 50 MB，重定向最多 5 跳。先看 `Content-Length`，流式读时再兜一次。

### 可靠性

- **frontmatter 按 YAML 规则安全渲染。** 模型写的 `summary` / `usage` 里出现换行、`key: value`、开头的 `-`、`true`、`123`，之前可能变成新的属性字段或者被解析成布尔/数字；现在只在 plain scalar 规则允许且不会被强转类型时才裸写，其余双引号加 JSON 转义，读回来还是同一个字符串。非法键名会被跳过并在结果里报 `dropped_keys`。
- **归档变成一个事务。** 复制附件、写笔记，任何一步失败都会撤销已复制的附件、还原被覆盖的旧附件，然后报 `archive failed and was rolled back`。之前失败会留下孤儿附件，`--force` 覆盖掉的旧附件更是没得恢复。

### 新增

- `clip.py verify <包>`：对着 manifest 校验正文和附件哈希。
- `clip.py gc [--days N] [--all]`：显式回收，不用等自动清理；阈值可用 `WEB_CLIP_GC_DAYS` 调。
- `vault.py doctor`：一次性体检状态目录、画像、库根、frontmatter 健康度。
- `tests/`：32 条离线测试，网络层打桩、抓取管线跑固定 fixture。测的是不变量——SSRF 边界拒绝什么、恶意 frontmatter 值能否原样读回、归档失败后有无残留、正文有没有被动过。
- `evals/trigger-cases.json`：触发词用例集，改 description 时用来验证路由没跑偏。
- `.github/workflows/test.yml`：Python 3.8 / 3.12 上跑编译和测试。
- `web-clip/requirements.txt`：依赖下界写死，不钉上界。
- 两个 `SKILL.md` 的 frontmatter 补上 `license` / `version` / `metadata`，description 补上反向触发条件。
