---
name: note-vault
description: >-
  本地 Markdown 知识库的归档与维护：把一份内容（web-clip 产出的 clip 包，或任意 .md 笔记）按目标目录
  已有的归类习惯归档，为每篇写一句话摘要和结合用户身份的使用说明作为笔记 frontmatter 属性，周期性复核
  只改写属性不动正文，并按摘要、使用说明、标签检索。当用户要求整理、归档、存进知识库、收藏、整理收藏夹
  或知识库、从收藏/知识库里找资料、改某篇笔记的摘要或归类时使用。不负责抓网页，抓取由 web-clip 负责；
  用户只是让读一个文件、改正文、或者在代码仓库里找东西时，都不要用它。
license: MIT
version: 2.0.0
metadata:
  requires-python: ">=3.8"
  requires-packages: 无（只用标准库）
  state-dir: $NOTE_VAULT_HOME（默认 ~/.local/share/note-vault）
  consumes: web-clip/clip-package v1
  network: 不联网
---

# Note Vault · 本地知识库

`$VAULT` = `python3 <skill目录>/scripts/vault.py`，`<skill目录>` 指本 SKILL.md 所在的 `note-vault` 目录。全部命令只操作本地文件，不联网。

## 数据模型

一篇笔记 = **frontmatter 属性** + **正文**，两层的可变性完全不同：

- **正文**（`---` 块之后的全部内容）：写入后永不改写、永不删除。
- **卡片属性**（frontmatter 里的字段）：`title` `url` `hash` `site` `author` `published` `type` 由 `commit` 按内容写死；`summary` `usage` `topics` `reviewed_at` `status` 是可变层，只由 `card` 命令写。整理和检索都只操作这一层。

两个字段必须分开，不要合成一段话：`summary` 是**内容**的函数，内容不变就不该改；`usage` 是**内容 × 用户**的函数，用户的身份和项目变了就要重写。

**不要把笔记正文读进上下文**。写摘要靠 clip 卡片，不够就 `$VAULT digest <clip包或笔记.md>` 取分节摘句（约 2.8k 字）；只有用户明确要求精读时才读原文。

## 用户画像

`persona.md` 记录用户的身份、专业、在做的项目，`usage` 据此撰写。用 Read / StrReplace 直接维护。它和 `config.json` 都在 `$NOTE_VAULT_HOME`（默认 `~/.local/share/note-vault`）而**不在 skill 目录里**，升级重装不会丢；确切路径跑 `$VAULT state` 看 `persona.path`。

收集时机只有两个：首次要写 `usage` 而文件不存在时问一次；开始整理流程时问一句有无更新。其余时候沉默复用，不要让归档动作变重。**画像缺失不阻塞归档**——先归档，`usage` 留空，落盘后再问、再补。

## 三条流程

| 用户想做的事 | 读哪个 |
|---|---|
| 把一篇内容存进知识库、归档、改归类 | [workflows/archive.md](workflows/archive.md) |
| 整理收藏夹 / 知识库、复核过期笔记 | [workflows/review.md](workflows/review.md) |
| 从收藏 / 知识库里找资料 | [workflows/search.md](workflows/search.md) |

用户给的是 URL 而手上还没有 clip 包时，先用 **web-clip** 抓取，拿到包路径再走归档流程。

## 通用规则

- 默认库根在 `$VAULT state` 的 `default_root`；为空且用户没指定时，用 AskQuestion 问一次。用 Obsidian 的话要**库的根目录**，不是某个主题子目录。
- 改默认位置：`$VAULT state --set-default-root <目录>`。改复核周期：`$VAULT state --set-review-days N`。
- 用户说归类错了：直接移动 `.md` 文件（卡片就在它的 frontmatter 里），必要时同步图片相对路径；是规则本身有问题才 `profile --refresh`。
- 旧版遗留的 `.card.md` 旁挂卡片仍能被 review / search / 去重识别；对该笔记执行任意 `card` 操作会自动把它并入 frontmatter 并删除旁挂文件。
- `commit` 是一个事务：附件和笔记要么一起落盘，要么一起回滚（被覆盖的附件也会还原）。看到 `archive failed and was rolled back` 就说明什么都没写进去，修好原因原样重跑即可。
- 归档报错、`state` 结果不对劲、或者刚换过机器，先跑 `$VAULT doctor` 看哪一项不健康。

参数全集、卡片字段、profile 字段含义见 [reference.md](reference.md)。
