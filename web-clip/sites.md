# 站点差异与抓取排查

抓取正常时不需要看这里。`warnings` 非空、字数明显偏少、图片大量失败时再查。

## 已适配站点

- **微信公众号**：`#js_content` 提取，图片走 `data-src`，公式与架构图都是图片，下载时按原文 Referer 过防盗链，实测可全部保存。`site` 记为「公众号名 · 微信公众号」，发布时间从 `#publish_time` 或页面里的 `var ct` 时间戳还原。偶发返回验证码页，脚本自动重试三次，仍失败就走 `ingest`。
- **小红书**：`#detail-desc` 提取。正文里的字面换行会还原为分行；Vue SSR 的注释标记（`<!--[-->` 等）不会漏进正文；包住整段文字或嵌套链接的 `<a>` 只保留文本（超过 120 可见字符即降级）。笔记配图不在正文 DOM 而在 `og:image` meta 里，脚本会自动收集并插到正文开头。
- **X / Twitter**：用 `article` 提取推文正文，配图走 `og:image` 的 `pbs.twimg.com/media`，头像和站点图会被自然排除。无 JS 时通常拿得到 og 描述和一张主图，长线程和多图可能不全。
- **GitHub**：取 README，`site` 记为 GitHub，无发布日期。
- **arXiv**：abs 页只取摘要；要全文就直接抓 PDF 链接。
- **知乎、少数派等**：付费或登录内容抓到的字数会明显偏少，卡片 `chars` 很小时提醒用户，不要假装抓全了。
- **视频 / iframe / 音频**：非广告域名的嵌入内容保留为「[视频：站点](链接)」形式的来源链接，不再直接丢弃。

## PDF

按 `%PDF` 魔数判定，不看扩展名——`.pdf` 链接返回 HTML 时会自动走 HTML 管线，任意链接返回 PDF 内容都会打成 PDF 包。上限 50 MB。

PDF 包的 `content.md` 只是一行占位链接，`kind` 为 `pdf`，真正的文件在 `assets/` 里。需要写摘要时用 Read 工具读那个 PDF。

## 反爬兜底

1. `fetch` 报 `blocked by anti-bot or login wall`，或 `warnings` 提示正文过短
2. 用 WebFetch 取该链接——它把内容写进一个文件并返回路径，**不要读这个文件**
3. `$CLIP ingest --file <那个路径> --url <原链接>`

JS 渲染的页面 WebFetch 也拿不到内容，改用浏览器工具打开后存下 HTML，同样 `ingest`。

`ingest` 会自动判别输入是 HTML 还是已转好的 Markdown，两种都能吃。元数据可能缺失，用 `--title/--site/--author/--published` 补。

## 提取质量

卡片里的 `extractor` 说明命中了哪条路径：

- 站点名（`wechat`、`xiaohongshu`…）：命中站点适配，质量最好
- `generic:<选择器>`：命中通用选择器，一般可靠
- `heuristic`：按文本密度和链接密度打分挑出来的，可能带上侧栏
- `fallback:body`：整页兜底，`warnings` 会提示，正文里大概率混着导航和页脚

后两种情况下如果用户在意干净度，改走浏览器加 `ingest` 通常更好。
