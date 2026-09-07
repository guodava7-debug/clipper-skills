# 归档流程

把一份内容存进知识库。输入是 web-clip 产出的 clip 包路径，或任意一个 `.md` 文件。

内容判断在 `commit` 之前做，卡片字段在 `commit` 之后写——`topics` 和目录名用的是同一套词汇，主题定不下来就选不对目录。

## 1. 去重

    $VAULT lookup --root <库根> --url <url> [--hash <clip 卡片的 hash>]

有 `duplicate` 就问用户：跳过、还是覆盖。链接还没抓的话，这一步放在抓取之前，省得白抓。

## 2. 学归类习惯

    $VAULT profile <库根>

已学过返回缓存，一级目录变了会自动重学，`--refresh` 强制重学。

**先看 `root_is_subdir_of_vault`**：为 true 说明当前根只是某个大库里的一个主题目录（如 `02 AI-Note`），此时必须拿内容主题和该目录的主题比一比——不属于它就改用 `vault_top_dirs` 里更合适的一级目录当根，对新根再跑一次 `profile`，不要硬塞。

`confidence` 为 high 或 medium 就直接照用；只在 low、目录为空、或规则与用户明确说法冲突时才确认。

## 3. 想清楚卡片内容

基于 clip 卡片的 `outline` 和 `excerpt` 写。信息不足（论文、长教程）先 `$VAULT digest <clip包>`，仍然不读正文。

- **summary**：一句话说清「讲了什么」。
- **topics**：1–3 个，**优先复用 profile 的 `top_dirs` 里已有的目录名**，不要另造近义词。
- **usage**：用 Read 读 `persona.md`，写清「这篇对用户的哪个身份 / 项目有什么用、什么场景会用到」，而不是又一句内容复述。画像里没有可关联的信息就写通用场景，不要编造用户背景；`persona.md` 不存在就先留空，第 6 步再补。

## 4. 定归档位置

复用 `top_dirs` 里已有的主题目录。文件名跟随 `filename` 各项比例（`date_prefix_ratio` 为 0 就不要加日期前缀）。frontmatter 只用 `frontmatter_keys` 里已有的字段并保持其格式。

**`top_dirs` 里没有一个目录装得下这篇，是「根目录选错了」的信号，不是「该新建目录」的信号**：先回到第 2 步在整个库里找归宿；确实全库都没有合适分类，才用 AskQuestion 让用户定（新建目录还是放进哪个现有目录），不要自己造一级目录。

## 5. 落盘

    $VAULT commit <clip包路径> --dest <完整的.md路径> --root <库根> \
      --assets-dir <profile.attachment_dir，缺省 assets> --link-style <profile.link_style> \
      --frontmatter '<JSON 对象>' [--body-prefix '来源：[标题](url)'] [--force]

`--frontmatter` 传目标库自己的习惯字段（`createTime` 等），它们排在卡片字段之前。`title` `url` `hash` `site` `author` `published` `type` `saved_at` `reviewed_at` `status` 由 commit 自动写入。附件从 clip 包拷进 `--assets-dir` 并重写链接，Obsidian 本地附件通常用 `--link-style wiki`。

`duplicate` 时退出码为 2。`--force` 只在目标路径与重复项是同一文件时才是真覆盖；重复在别处时新副本照存，结果里给出 `duplicate_kept`，要不要删旧副本由用户决定。

附件和笔记是一个事务：报 `archive failed and was rolled back` 就说明什么都没写进去（被覆盖的旧附件也已还原），修掉原因后原样重跑即可，不要手工去补文件。结果里带 `notes` 说明 clip 包被改过、带 `dropped_keys` 说明 `--frontmatter` 里有非法键名，都要转告用户。

## 6. 写卡片

    $VAULT card <笔记.md> --summary '…' --usage '…' --topics '主题1,主题2'

`--usage` 会自动把 `reviewed_at` 刷成当天。

`persona.md` 此前不存在的话，现在用 AskQuestion 问一次身份 / 专业 / 在做的事，写进 `persona.md`，再 `card --usage` 补上个性化说明。用户不愿提供就在 `persona.md` 记一行「用户选择不提供个人信息」，此后不再问。

## 7. 汇报

保存路径、摘要、使用说明、主题标签、图片拷贝数、重复项处理结果。结果里带 `review_due` 时顺带提醒用户该整理知识库了。

clip 包不需要清理，web-clip 会自己回收。
