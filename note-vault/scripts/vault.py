#!/usr/bin/env python3
"""Local markdown knowledge base: filing, card fields, periodic review, search.

Offline by design — every command touches only local files, never the network.
A note is `frontmatter properties + body`; the body is immutable once written,
the card fields (summary / usage / topics / reviewed_at / status) are not.

Subcommands:
  lookup   check whether a url or content hash is already in the library
  profile  learn a destination directory's filing conventions
  commit   file a clip package (or a plain .md) into the library
  card     create/update a note's card fields in its frontmatter (body untouched)
  digest   sectioned extract of a clip package or a saved note, for summary writing
  review   list notes due for their periodic usage-note refresh
  search   search notes by card fields, falling back to their bodies
  state    read/write the skill's config
  doctor   check that this install is healthy before it eats a commit

Runtime state (config, persona) lives in $NOTE_VAULT_HOME, defaulting to
$XDG_DATA_HOME/note-vault or ~/.local/share/note-vault — never inside the
skill directory, so updating or reinstalling the skill cannot destroy it.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from datetime import datetime


SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_state_home():
    """Config and persona live outside the skill directory, so reinstalling or
    updating the skill cannot wipe learned state. A `state/` left by an older
    install is migrated once, then never looked at again."""
    explicit = os.environ.get("NOTE_VAULT_HOME")
    if explicit:
        home = os.path.abspath(os.path.expanduser(explicit))
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
        home = os.path.join(base, "note-vault")
    legacy = os.path.join(SKILL_ROOT, "state")
    if not os.path.exists(home) and os.path.isdir(legacy):
        try:
            os.makedirs(os.path.dirname(home), exist_ok=True)
            shutil.move(legacy, home)
        except OSError:
            return legacy
    return home


STATE_DIR = resolve_state_home()
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")
PERSONA_PATH = os.path.join(STATE_DIR, "persona.md")
CONFIG_SCHEMA_VERSION = 3

# The clip-package contract shared with web-clip. Packages carrying a major
# version this build does not know are refused rather than half-understood.
PACKAGE_FORMAT = "web-clip/clip-package"
SUPPORTED_PACKAGE_MAJOR = 1

CARD_SUFFIX = ".card.md"
CARD_META_KEYS = ("title", "url", "hash", "site", "author", "published", "type",
                  "topics", "summary", "usage", "saved_at", "reviewed_at", "status")
DEFAULT_REVIEW_DAYS = 30
MIN_BODY_CHARS = 80


# ------------------------------------------------------------------------ util

def atomic_write_json(path, obj):
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def atomic_write_text(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def fail(msg, **extra):
    payload = {"ok": False, "error": msg}
    payload.update(extra)
    emit(payload)
    sys.exit(1)


def visible_len(text):
    return len(re.sub(r"\s+", "", text))


def slugify(title, limit=60):
    text = unicodedata.normalize("NFKC", title or "").strip()
    text = re.sub(r'[\\/:*?"<>|\n\r\t]+', "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:limit] or "untitled").strip()


def today():
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------- config

def load_config():
    os.makedirs(STATE_DIR, exist_ok=True)
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh)
            if not isinstance(cfg, dict):
                raise ValueError("config root is not an object")
        except Exception:
            # Fail safe: keep the broken file aside instead of silently
            # overwriting learned state on the next write.
            backup = CONFIG_PATH + ".corrupt-" + datetime.now().strftime("%Y%m%d-%H%M%S")
            try:
                os.replace(CONFIG_PATH, backup)
            except OSError:
                backup = None
            print("warning: config.json unreadable; backed up to {}".format(backup),
                  file=sys.stderr)
            cfg = {}
    cfg.setdefault("schema_version", CONFIG_SCHEMA_VERSION)
    cfg.setdefault("default_root", "")
    cfg.setdefault("destinations", {})
    review = cfg.setdefault("review", {})
    review.setdefault("days", DEFAULT_REVIEW_DAYS)
    review.setdefault("last_review_at", "")
    return cfg


def save_config(cfg):
    cfg["schema_version"] = CONFIG_SCHEMA_VERSION
    atomic_write_json(CONFIG_PATH, cfg)


def review_days_of(cfg):
    try:
        return max(1, int(cfg["review"].get("days")))
    except (KeyError, TypeError, ValueError):
        return DEFAULT_REVIEW_DAYS


def review_overdue_days(cfg):
    """Days at-or-past the review period, or 0 when not due / clock not started."""
    review = cfg.get("review") or {}
    last = review.get("last_review_at") or ""
    try:
        elapsed = (datetime.now() - datetime.strptime(last, "%Y-%m-%d")).days
    except ValueError:
        return 0
    days = review_days_of(cfg)
    # Aligned with per-card dueness: day `days` itself already counts as due.
    return elapsed - days + 1 if elapsed >= days else 0


# ----------------------------------------------------------------- frontmatter

# Scalars a YAML parser would silently turn into a bool, a number or null.
YAML_AMBIGUOUS_RE = re.compile(
    r"^(?:true|false|yes|no|on|off|null|none|~"
    r"|[-+]?\d[\d_]*(?:\.\d*)?(?:[eE][-+]?\d+)?"
    r"|[-+]?\.(?:inf|nan)|0[box][0-9a-fA-F_]+)$", re.I)
YAML_INDICATORS = "-?:,[]{}#&*!|>'\"%@`"


def yaml_scalar(text):
    """Render a string as a YAML scalar that parses back to the same string.

    The plain (unquoted) form is used only where YAML's plain-scalar rules
    allow it and no parser would coerce the result to a bool or a number;
    everything else is double-quoted with JSON escaping, which YAML reads
    identically. This is what stops a summary containing a newline, a `key:
    value` pair or a leading `-` from injecting fields into the frontmatter.
    """
    if text == "":
        return ""
    unsafe = (
        text != text.strip()
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)
        or text[0] in YAML_INDICATORS
        or ": " in text
        or text.endswith(":")
        or " #" in text
        or bool(YAML_AMBIGUOUS_RE.match(text))
    )
    return json.dumps(text, ensure_ascii=False) if unsafe else text


def safe_key(key):
    """A frontmatter key we can write and read back, or None."""
    key = str(key).strip()
    if not key or key[0] in YAML_INDICATORS:
        return None
    if re.search(r"[:#\x00-\x1f\x7f]", key):
        return None
    return key


def render_frontmatter_lines(pairs, key_order=None, dropped=None):
    lines = []
    for key in (key_order if key_order is not None else list(pairs)):
        if key not in pairs:
            continue
        name = safe_key(key)
        if name is None:
            if dropped is not None:
                dropped.append(str(key))
            continue
        value = pairs[key]
        if isinstance(value, list):
            lines.append("{}:".format(name))
            lines.extend("- {}".format(yaml_scalar(str(v))) for v in value)
        elif value is None or value == "":
            lines.append("{}: ".format(name))
        else:
            lines.append("{}: {}".format(name, yaml_scalar(str(value))))
    return lines


def unquote_scalar(value):
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    # A plain scalar ends at an unquoted ` #` comment.
    return value.split(" #", 1)[0].strip()


def parse_frontmatter(text):
    """Parse a leading YAML block into a flat dict (lists supported)."""
    meta = {}
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return meta, 0
    key = None
    for line in m.group(1).splitlines():
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and key and isinstance(meta.get(key), list):
            meta[key].append(unquote_scalar(item.group(1).strip()))
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            meta[key] = [unquote_scalar(p.strip()) for p in inner.split(",") if p.strip()]
            continue
        meta[key] = unquote_scalar(value) if value else []
    return meta, m.end()


def fm_str(value):
    """Frontmatter values parse to [] when empty; normalize to a string."""
    return "" if isinstance(value, list) else str(value or "")


def update_note_frontmatter(note_path, updates):
    """Rewrite only the note's frontmatter block; body bytes stay untouched."""
    with open(note_path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?\n)---[ \t]*\n?", text, re.S)
    fm_text, body = (m.group(1), text[m.end():]) if m else ("", text)
    kept, skip = [], False
    for line in fm_text.splitlines():
        km = re.match(r"^([^\s#-][^:]*):(\s|$)", line)
        if km:
            skip = km.group(1).strip() in updates
        if not skip:
            kept.append(line)
    order = [k for k in CARD_META_KEYS if k in updates] + \
            [k for k in updates if k not in CARD_META_KEYS]
    dropped = []
    lines = kept + render_frontmatter_lines(updates, order, dropped)
    if not m and body and not body.startswith("\n"):
        body = "\n" + body
    atomic_write_text(note_path, "---\n" + "\n".join(lines) + "\n---\n" + body)
    return dropped


# ------------------------------------------------------------- walking the vault

def iter_markdown(root, limit=6000):
    count = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules",)]
        for name in files:
            if name.endswith(".md") and not name.endswith(CARD_SUFFIX):
                yield os.path.join(base, name)
                count += 1
                if count >= limit:
                    return


def card_path_for(note_path):
    return os.path.splitext(note_path)[0] + CARD_SUFFIX


def read_card(path):
    """Read a legacy sidecar card (kept only for migration and old libraries)."""
    with open(path, encoding="utf-8", errors="ignore") as fh:
        text = fh.read()
    meta, end = parse_frontmatter(text)
    body = text[end:]
    sections = {"摘要": [], "使用说明": []}
    current = None
    for line in body.splitlines():
        h = re.match(r"^#{1,3}\s*(.+?)\s*$", line)
        if h:
            current = h.group(1)
            continue
        if current in sections:
            sections[current].append(line)
    return meta, "\n".join(sections["摘要"]).strip(), "\n".join(sections["使用说明"]).strip()


def iter_cards(root, limit=6000):
    count = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules",)]
        for name in files:
            if name.endswith(CARD_SUFFIX):
                yield os.path.join(base, name)
                count += 1
                if count >= limit:
                    return


def iter_all_cards(root):
    """Yield (note_path, meta) for every filed note.

    Primary source is the note's own frontmatter (marked by saved_at/summary/
    usage keys); legacy .card.md sidecars are folded in until migrated.
    """
    seen = set()
    for path in iter_markdown(root):
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                head = fh.read(8000)
        except OSError:
            continue
        if not head.startswith("---"):
            continue
        meta, _ = parse_frontmatter(head)
        if any(k in meta for k in ("saved_at", "summary", "usage")):
            seen.add(path)
            yield path, meta
    for cpath in iter_cards(root):
        note = cpath[: -len(CARD_SUFFIX)] + ".md"
        if note in seen:
            continue
        try:
            meta, summary, usage = read_card(cpath)
        except OSError:
            continue
        if summary and not fm_str(meta.get("summary")):
            meta["summary"] = summary
        if usage and not fm_str(meta.get("usage")):
            meta["usage"] = usage
        meta["legacy_card"] = cpath
        yield note, meta


def age_days(date_text):
    try:
        return (datetime.now() - datetime.strptime(date_text or "", "%Y-%m-%d")).days
    except ValueError:
        return None


def find_duplicate(root, urls, hashes):
    root = os.path.expanduser(root)
    if not os.path.isdir(root):
        return None
    if isinstance(hashes, str) or hashes is None:
        hashes = [hashes] if hashes else []
    hashes = [h for h in hashes if h]
    # Both the url the user gave (possibly a short link) and the resolved
    # canonical url count: notes filed from a short link keep the short form.
    needles = []
    for url in ([urls] if isinstance(urls, str) else urls):
        needle = (url or "").split("://", 1)[-1].rstrip("/")
        if needle and needle not in needles:
            needles.append(needle)
    # Boundary guard so …/article does not match …/article-extra.
    needle_re = re.compile(
        "|".join(re.escape(n) + r"(?![\w-])" for n in needles)) if needles else None
    for path in iter_markdown(root):
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                head = fh.read(4000)
        except OSError:
            continue
        if needle_re and needle_re.search(head):
            return {"path": path, "match": "url"}
        if any(h in head for h in hashes):
            return {"path": path, "match": "content_hash"}
    # Filed notes keep url/hash in their frontmatter (covered above);
    # legacy sidecar cards are still scanned until they get migrated.
    for cpath in iter_cards(root):
        try:
            with open(cpath, encoding="utf-8", errors="ignore") as fh:
                head = fh.read(2000)
        except OSError:
            continue
        if (needle_re and needle_re.search(head)) or any(h in head for h in hashes):
            note = cpath[: -len(CARD_SUFFIX)] + ".md"
            return {"path": note if os.path.exists(note) else cpath, "match": "card_url"}
    return None


def cmd_lookup(args):
    cfg = load_config()
    root = args.root or cfg.get("default_root")
    if not root:
        emit({"ok": True, "checked": False, "duplicate": None,
              "hint": "no root given and no default_root set; dedup skipped"})
        return
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        fail("no such directory: {}".format(root))
    dup = find_duplicate(root, [u for u in (args.url, args.canonical_url) if u], args.hash)
    result = {"ok": True, "checked": True, "root": root, "duplicate": dup}
    overdue = review_overdue_days(cfg)
    if overdue:
        result["review_due"] = "上次整理已超期 {} 天，可提醒用户整理知识库".format(overdue)
    emit(result)


# --------------------------------------------------------------------- profile

def sibling_topic_dirs(vault_root, limit=20):
    """Top-level note-bearing dirs of the whole vault, for cross-topic filing."""
    out = []
    try:
        names = sorted(os.listdir(vault_root))
    except OSError:
        return out
    for name in names:
        full = os.path.join(vault_root, name)
        if name.startswith(".") or not os.path.isdir(full):
            continue
        notes = sum(1 for _, _, fs in os.walk(full) for f in fs
                    if f.endswith(".md") and not f.endswith(CARD_SUFFIX))
        if notes:
            out.append({"dir": name, "notes": notes})
        if len(out) >= limit:
            break
    return out


def compact_profile(profile):
    """Trim fields the model does not need in order to pick a path."""
    slim = dict(profile)
    filename = dict(slim.get("filename") or {})
    if "sample" in filename:
        filename["sample"] = filename["sample"][:2]
    if filename:
        slim["filename"] = filename
    slim["top_dirs"] = [
        {**d, "subdirs": d.get("subdirs", [])[:4]} for d in slim.get("top_dirs", [])
    ]
    return slim


def _dir_signature(root):
    try:
        names = sorted(n for n in os.listdir(root) if not n.startswith("."))
    except OSError:
        names = []
    return hashlib.sha1("|".join(names).encode()).hexdigest()[:10]


def cmd_profile(args):
    root = os.path.realpath(os.path.expanduser(args.dir))
    if not os.path.isdir(root):
        fail("not a directory: {}".format(root))
    cfg = load_config()
    sig = _dir_signature(root)
    cached = cfg["destinations"].get(root)
    # A changed top level (new/renamed dirs) invalidates the cache automatically.
    if cached and not args.refresh and cached.get("sig") == sig:
        if args.set_default and cfg.get("default_root") != root:
            cfg["default_root"] = root
            save_config(cfg)
        emit({"ok": True, "cached": True, "path": root, "profile": compact_profile(cached)})
        return

    top = []
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(full):
            continue
        children = [c for c in sorted(os.listdir(full))
                    if not c.startswith(".") and os.path.isdir(os.path.join(full, c))]
        notes = sum(1 for _, _, fs in os.walk(full) for f in fs
                    if f.endswith(".md") and not f.endswith(CARD_SUFFIX))
        if notes == 0:
            # Attachment/system folders (assets, images, …) are not topics.
            continue
        top.append({"dir": name, "notes": notes, "subdirs": children[:8]})

    files = list(iter_markdown(root, limit=400))
    rel_names = [os.path.relpath(p, root) for p in files]
    date_prefix = sum(1 for n in rel_names if re.match(r"^\d{4}-\d{2}-\d{2}", os.path.basename(n)))
    kebab = sum(1 for n in rel_names if re.match(r"^[a-z0-9]+(-[a-z0-9]+)+\.md$", os.path.basename(n)))
    cjk = sum(1 for n in rel_names if re.search(r"[\u4e00-\u9fff]", os.path.basename(n)))

    fm_keys, fm_samples, wiki, md_img, has_index = {}, 0, 0, 0, None
    for path in files[:60]:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read(6000)
        except OSError:
            continue
        wiki += len(re.findall(r"!\[\[", text))
        # Only local images reveal how attachments are linked; remote ones don't.
        md_img += len(re.findall(r"!\[[^\]]*\]\((?!https?://)", text))
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end > 0:
                fm_samples += 1
                for line in text[3:end].splitlines():
                    # Unicode-friendly: CJK frontmatter keys are common.
                    m = re.match(r"^([^\s:#\-][^:]*?)\s*:(\s|$)", line.strip())
                    if m:
                        fm_keys[m.group(1)] = fm_keys.get(m.group(1), 0) + 1
    for candidate in ("index.md", "README.md", "MOC.md", "000 index.md"):
        if os.path.exists(os.path.join(root, candidate)):
            has_index = candidate
            break

    # Obsidian's attachmentFolderPath is relative to the vault root, not the note.
    attachment_dir, vault_root = "", ""
    for candidate in (root, os.path.dirname(root), os.path.dirname(os.path.dirname(root))):
        conf = os.path.join(candidate, ".obsidian", "app.json")
        if os.path.exists(conf):
            vault_root = candidate
            try:
                with open(conf, encoding="utf-8") as fh:
                    attachment_dir = (json.load(fh).get("attachmentFolderPath") or "").strip()
            except Exception:
                pass
            break
    if attachment_dir.startswith("./"):
        attachment_dir = attachment_dir[2:]
    if attachment_dir and vault_root and not attachment_dir.startswith("/"):
        attachment_dir = os.path.join(vault_root, attachment_dir)
    if not attachment_dir:
        for name in ("assets", "attachments", "images", "_assets"):
            if os.path.isdir(os.path.join(root, name)):
                attachment_dir = os.path.join(root, name)
                break

    total = max(1, len(rel_names))
    ordered_keys = [k for k, _ in sorted(fm_keys.items(), key=lambda kv: -kv[1])]
    profile = {
        "folder_strategy": "topic" if top else "flat",
        "numbered_dirs": bool(top) and all(re.match(r"^\d+\s", t["dir"]) for t in top),
        "top_dirs": top,
        "filename": {
            "date_prefix_ratio": round(date_prefix / total, 2),
            "kebab_ratio": round(kebab / total, 2),
            "cjk_ratio": round(cjk / total, 2),
            "sample": [os.path.basename(n) for n in rel_names[:5]],
        },
        "frontmatter_keys": ordered_keys[:12],
        "frontmatter_ratio": round(fm_samples / max(1, len(files[:60])), 2),
        "link_style": "wiki" if wiki > md_img else "markdown",
        "attachment_dir": attachment_dir,
        "vault_root": vault_root,
        "existing_index": has_index,
        # A root that is itself one topic folder inside a larger vault is the
        # classic misfiling trap: everything gets funnelled under that topic.
        "root_is_subdir_of_vault": bool(vault_root) and vault_root != root,
        "vault_top_dirs": sibling_topic_dirs(vault_root) if vault_root and vault_root != root else [],
        "note_count": len(rel_names),
        "learned_at": today(),
        "sig": sig,
    }
    profile["confidence"] = (
        "high" if len(rel_names) >= 12 and profile["frontmatter_ratio"] >= 0.7
        else "medium" if len(rel_names) >= 5 else "low"
    )
    if args.save:
        cfg["destinations"][root] = profile
        if args.set_default:
            cfg["default_root"] = root
        save_config(cfg)
    emit({"ok": True, "cached": False, "path": root,
          "profile": profile if args.full else compact_profile(profile)})


# ---------------------------------------------------------------------- commit

def check_package(record, src):
    """Refuse a clip package this build cannot read correctly.

    Version-checking here rather than at every field access means a future
    web-clip that renames things fails loudly on the first commit instead of
    quietly filing notes with half the metadata missing.
    """
    fmt = record.get("format")
    if fmt is None and "schema" in record:
        return ["package predates the versioned format; filed on a best-effort basis"]
    if fmt != PACKAGE_FORMAT:
        fail("not a {} package (found format={!r})".format(PACKAGE_FORMAT, fmt), path=src)
    version = str(record.get("format_version") or "")
    try:
        major = int(version.split(".")[0])
    except ValueError:
        fail("unreadable format_version {!r}".format(version), path=src)
    if major != SUPPORTED_PACKAGE_MAJOR:
        fail("clip package format {} is not supported by this note-vault "
             "(expects major {})".format(version, SUPPORTED_PACKAGE_MAJOR), path=src,
             hint="升级 note-vault 或用同代 web-clip 重新剪藏")
    return []


def read_source(source):
    """Accept a clip package directory or a plain .md file; return (body, record)."""
    src = os.path.realpath(os.path.expanduser(source))
    if os.path.isdir(src):
        meta_path = os.path.join(src, "meta.json")
        if not os.path.isfile(meta_path):
            fail("not a clip package (need content.md + meta.json): {}".format(src))
        try:
            with open(meta_path, encoding="utf-8") as fh:
                record = json.load(fh)
        except (ValueError, OSError) as exc:
            fail("unreadable clip manifest: {}".format(exc), path=meta_path)
        if not isinstance(record, dict):
            fail("clip manifest is not a JSON object", path=meta_path)
        notes = check_package(record, src)
        # Paths in the manifest are relative to the package, so a package that
        # was moved or copied still resolves.
        files = record.get("files") if isinstance(record.get("files"), dict) else {}
        body_path = os.path.join(src, files.get("content") or "content.md")
        if os.path.commonpath([os.path.realpath(body_path), src]) != src:
            fail("clip manifest points outside the package", path=body_path)
        if not os.path.isfile(body_path):
            fail("clip package has no body at {}".format(body_path), path=src)
        with open(body_path, encoding="utf-8") as fh:
            body = fh.read()
        expected = record.get("body_sha256")
        if expected and hashlib.sha256(body.encode()).hexdigest() != expected:
            notes.append("content.md no longer matches the manifest hash; it was edited after clipping")
        record["clip_dir"] = src
        record["_notes"] = notes
        return body, record, src
    if not src.endswith(".md") or not os.path.isfile(src):
        fail("source must be a clip package directory or a .md file: {}".format(src))
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    meta, end = parse_frontmatter(text)
    body = text[end:].lstrip("\n")
    heading = re.search(r"^#\s+(.+)$", body, re.M)
    record = {
        "title": fm_str(meta.get("title")) or (heading.group(1).strip() if heading else
                                               os.path.splitext(os.path.basename(src))[0]),
        "url": fm_str(meta.get("url")),
        "site": fm_str(meta.get("site")),
        "author": fm_str(meta.get("author")),
        "published": fm_str(meta.get("published")),
        "type": fm_str(meta.get("type")) or "笔记",
        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "chars": visible_len(body),
        "assets": [],
        "_notes": [],
    }
    return body, record, None


class AssetTransaction:
    """Copy files into the vault so that any failure can be undone completely.

    Archiving touches several places at once — attachments, the note, then the
    config clock. Without this, a crash between the copy and the note write
    leaves orphan attachments behind, and an overwrite (`--force`) destroys the
    previous versions with nothing to restore.
    """

    def __init__(self):
        self.created = []       # paths that did not exist before
        self.replaced = []      # (target, backup) for files we overwrote
        self._backup_root = None

    def _backup(self, target):
        if self._backup_root is None:
            self._backup_root = tempfile.mkdtemp(prefix="note-vault-tx-")
        keep = os.path.join(self._backup_root, "{}-{}".format(
            len(self.replaced), os.path.basename(target)))
        shutil.copy2(target, keep)
        self.replaced.append((target, keep))

    def copy(self, source_file, target):
        if os.path.exists(target):
            self._backup(target)
        else:
            self.created.append(target)
        shutil.copy2(source_file, target)

    def rollback(self):
        for path in reversed(self.created):
            try:
                os.remove(path)
            except OSError:
                pass
        for target, keep in reversed(self.replaced):
            try:
                shutil.copy2(keep, target)
            except OSError:
                pass
        self.created, self.replaced = [], []

    def close(self):
        if self._backup_root:
            shutil.rmtree(self._backup_root, ignore_errors=True)
            self._backup_root = None


def relocate_assets(body, clip_dir, dest, assets_dir, link_style, tx):
    """Copy the package's assets next to the note and repoint the links."""
    src = os.path.join(clip_dir, "assets") if clip_dir else ""
    if not src or not os.path.isdir(src):
        return body, {"copied": 0, "failed": []}
    asset_dir = os.path.normpath(assets_dir if os.path.isabs(assets_dir)
                                 else os.path.join(os.path.dirname(dest), assets_dir))
    os.makedirs(asset_dir, exist_ok=True)
    copied, failed = [], []
    for name in sorted(os.listdir(src)):
        source_file = os.path.join(src, name)
        if not os.path.isfile(source_file):
            continue
        try:
            tx.copy(source_file, os.path.join(asset_dir, name))
            copied.append(name)
        except OSError as exc:
            failed.append({"file": name, "reason": str(exc)[:80]})
    rel = os.path.relpath(asset_dir, os.path.dirname(dest)).replace(os.sep, "/")
    for name in copied:
        old = "assets/" + name
        new = name if rel == "." else "{}/{}".format(rel, name)
        if link_style == "wiki":
            body = re.sub(r"!\[[^\]]*\]\(" + re.escape(old) + r"\)",
                          lambda _m, n=name: "![[{}]]".format(n), body)
        body = body.replace("({})".format(old), "({})".format(new))
    return body, {"copied": len(copied), "failed": failed, "dir": asset_dir}


def cmd_commit(args):
    body, record, clip_dir = read_source(args.source)

    front = {}
    if args.frontmatter:
        try:
            front = json.loads(args.frontmatter)
        except json.JSONDecodeError as exc:
            fail("invalid --frontmatter json: {}".format(exc))
        if not isinstance(front, dict):
            fail("--frontmatter must be a JSON object")
    # A near-empty clip package means the fetch failed; a hand-written note is
    # allowed to be as short as its author wants.
    if clip_dir and record.get("kind") != "pdf" and not args.allow_short \
            and visible_len(body) < MIN_BODY_CHARS:
        fail("clip body is only {} chars — the fetch likely failed; re-clip it, "
             "or pass --allow-short to file it anyway".format(visible_len(body)))

    dest = os.path.realpath(os.path.expanduser(args.dest))
    root = os.path.realpath(os.path.expanduser(args.root)) if args.root else os.path.dirname(dest)
    if os.path.commonpath([dest, root]) != root:
        fail("destination escapes root", dest=dest, root=root)
    if dest.endswith(os.sep) or os.path.isdir(dest):
        fail("dest must be a file path ending in .md", dest=dest)
    if not dest.endswith(".md"):
        dest += ".md"

    # Pre-1.0 packages only carry the old sha1; accepting both keeps dedup
    # working for clips taken before the format change.
    content_hash = record.get("content_sha256") or record.get("content_sha1") or ""
    dup = find_duplicate(root, [record.get("url"), record.get("canonical_url")],
                         [record.get("content_sha256"), record.get("content_sha1")])
    if dup and not args.force:
        emit({"ok": False, "duplicate": dup,
              "hint": "pass --force to overwrite or choose another dest"})
        sys.exit(2)
    if os.path.exists(dest) and not args.force:
        emit({"ok": False, "error": "file exists", "path": dest, "hint": "pass --force to overwrite"})
        sys.exit(2)
    dest_existed = os.path.exists(dest)

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    asset_report = {"copied": 0, "failed": []}
    dropped_keys = []
    # Attachments and the note are one unit: either both land or neither does.
    tx = AssetTransaction()
    try:
        if args.assets != "none":
            body, asset_report = relocate_assets(
                body, clip_dir, dest, args.assets_dir, args.link_style, tx)

        # Deterministic card metadata is written here; summary / usage / topics
        # are the mutable layer and belong to `card`.
        stamp = today()
        derived = {
            "title": record.get("title", ""),
            "url": record.get("url", ""),
            "hash": content_hash,
            "site": record.get("site", ""),
            "author": record.get("author", ""),
            "published": record.get("published", ""),
            "type": record.get("type", ""),
            "saved_at": stamp,
            "reviewed_at": stamp,
            "status": "active",
        }
        for key, value in derived.items():
            if value not in (None, "", []):
                front.setdefault(key, value)

        parts = ["\n".join(["---"] + render_frontmatter_lines(front, dropped=dropped_keys) + ["---"])]
        if args.body_prefix:
            parts.append(args.body_prefix)
        parts.append(body.strip())
        failures = len(record.get("asset_failures") or [])
        if failures:
            parts.append("> 说明：{} 张图片未能保存（防盗链或已失效），可访问原文查看。".format(failures))
        atomic_write_text(dest, "\n\n".join(p for p in parts if p).strip() + "\n")
    except OSError as exc:
        tx.rollback()
        fail("archive failed and was rolled back: {}".format(exc), dest=dest,
             hint="附件和笔记都没有落盘，修好原因后原样重跑即可")
    except BaseException:
        tx.rollback()
        raise
    finally:
        tx.close()

    cfg = load_config()
    if not cfg["review"]["last_review_at"]:
        # Start the periodic-review clock at the first ever commit.
        cfg["review"]["last_review_at"] = stamp
        save_config(cfg)

    result = {
        "ok": True,
        "note": dest,
        "chars": visible_len(body),
        "assets": asset_report,
        "overwritten": dest_existed,
        "next": "写卡片：vault.py card '{}' --summary … --usage … --topics …".format(dest),
    }
    if record.get("_notes"):
        result["notes"] = record["_notes"]
    if dropped_keys:
        result["dropped_keys"] = dropped_keys
    if dup and os.path.realpath(dup["path"]) != dest:
        result["duplicate_kept"] = dup["path"]
        result["hint"] = "原有副本仍保留在上述路径；是否删除旧副本由用户决定"
    overdue = review_overdue_days(cfg)
    if overdue:
        result["review_due"] = "上次整理已超期 {} 天，可提醒用户整理知识库".format(overdue)
    emit(result)


# ----------------------------------------------------------------------- cards

def cmd_card(args):
    note = os.path.abspath(os.path.expanduser(args.note))
    if note.endswith(CARD_SUFFIX):
        fail("pass the note's .md path, not the card itself", path=note)
    if not note.endswith(".md"):
        fail("expected a .md note path", path=note)
    if not os.path.isfile(note):
        fail("no such note: {}".format(note))
    with open(note, encoding="utf-8", errors="ignore") as fh:
        meta, _ = parse_frontmatter(fh.read(8000))

    updates = {}
    legacy = card_path_for(note)
    if os.path.exists(legacy):
        # Fold the old sidecar card into the note's frontmatter, then drop it.
        lmeta, lsummary, lusage = read_card(legacy)
        lmeta.pop("note", None)
        if lsummary:
            lmeta.setdefault("summary", lsummary)
        if lusage:
            lmeta.setdefault("usage", lusage)
        for k, v in lmeta.items():
            if k not in meta and v not in (None, "", []):
                updates[k] = v

    stamp = today()
    if "saved_at" not in meta and "saved_at" not in updates:
        updates["saved_at"] = stamp
    if "status" not in meta and "status" not in updates:
        updates["status"] = "active"
    if args.meta:
        try:
            parsed = json.loads(args.meta)
        except json.JSONDecodeError as exc:
            fail("invalid --meta json: {}".format(exc))
        if not isinstance(parsed, dict):
            fail("--meta must be a JSON object")
        updates.update(parsed)
    if args.topics is not None:
        updates["topics"] = [t.strip() for t in args.topics.split(",") if t.strip()]
    if args.status:
        updates["status"] = args.status
    if args.summary is not None:
        updates["summary"] = args.summary
    if args.usage is not None:
        updates["usage"] = args.usage
        updates["reviewed_at"] = stamp
    if args.touch_review:
        updates["reviewed_at"] = stamp

    dropped = update_note_frontmatter(note, updates)
    result = {"ok": True, "note": note,
              "status": updates.get("status") or fm_str(meta.get("status")),
              "reviewed_at": updates.get("reviewed_at") or fm_str(meta.get("reviewed_at"))}
    if dropped:
        result["dropped_keys"] = dropped
        result["hint"] = "这些字段名不是合法的 frontmatter key，已跳过"
    if os.path.exists(legacy):
        os.remove(legacy)
        result["migrated_sidecar"] = legacy
    emit(result)


def cmd_digest(args):
    target = os.path.abspath(os.path.expanduser(args.target))
    path = os.path.join(target, "content.md") if os.path.isdir(target) else target
    if not os.path.isfile(path):
        fail("no clip package or markdown file at: {}".format(args.target))
    with open(path, encoding="utf-8", errors="ignore") as fh:
        text = fh.read()
    m = re.match(r"^---\n.*?\n---\n?", text, re.S)
    if m:
        text = text[m.end():]
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)

    def lead(block, sentences=2, cap=240):
        block = re.sub(r"\^?\[(?:edit|citation needed|note \d+|\d+)\]\)?", "", block, flags=re.I)
        flat = re.sub(r"\s+", " ", re.sub(r"[>*`|]", "", block)).strip()
        parts = re.split(r"(?<=[。！？!?])\s*|(?<=\.)\s+", flat)
        return " ".join(p for p in parts[:sentences] if p)[:cap].strip()

    chunks = re.split(r"^(#{1,6}\s+.+)$", text, flags=re.M)
    out = []
    if chunks[0].strip():
        out.append(lead(chunks[0], sentences=3, cap=400))
    for i in range(1, len(chunks) - 1, 2):
        snippet = lead(chunks[i + 1])
        out.append(chunks[i].strip() + ("\n" + snippet if snippet else ""))
        if sum(len(x) for x in out) > args.chars:
            break
    digest = "\n\n".join(x for x in out if x)[: args.chars]
    emit({"ok": True, "source_chars": visible_len(text), "digest": digest})


def compact_card(note_path, meta):
    item = {
        "note": note_path,
        "title": fm_str(meta.get("title")) or os.path.splitext(os.path.basename(note_path))[0],
        "type": fm_str(meta.get("type")),
        "topics": meta.get("topics") if isinstance(meta.get("topics"), list) else [],
        "site": fm_str(meta.get("site")),
        "url": fm_str(meta.get("url")),
        "saved_at": fm_str(meta.get("saved_at")),
        "reviewed_at": fm_str(meta.get("reviewed_at")),
        "status": fm_str(meta.get("status")),
        "summary": fm_str(meta.get("summary")),
        "usage": fm_str(meta.get("usage")),
    }
    if meta.get("legacy_card"):
        item["legacy_card"] = meta["legacy_card"]
    return item


def resolve_root(args_root, cfg):
    root = args_root or cfg.get("default_root")
    if not root:
        fail("no root given and no default_root set; pass a root or run `state --set-default-root`")
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        fail("no such directory: {}".format(root))
    return root


def cmd_review(args):
    cfg = load_config()
    if args.done:
        cfg["review"]["last_review_at"] = today()
        save_config(cfg)
        emit({"ok": True, "last_review_at": cfg["review"]["last_review_at"],
              "next_due_in_days": cfg["review"]["days"]})
        return
    root = resolve_root(args.root, cfg)
    days = args.days or review_days_of(cfg)
    total, due_total, items = 0, 0, []
    to_skip = max(0, args.offset)
    for note, meta in iter_all_cards(root):
        total += 1
        age = age_days(fm_str(meta.get("reviewed_at")) or fm_str(meta.get("saved_at")))
        if not (args.all or age is None or age >= days):
            continue
        due_total += 1
        if to_skip > 0:
            to_skip -= 1
            continue
        if len(items) < args.limit:
            items.append(compact_card(note, meta))
    emit({"ok": True, "root": root, "period_days": days, "total_cards": total,
          "due_total": due_total, "returned": len(items), "offset": args.offset,
          "has_more": due_total > args.offset + len(items), "items": items,
          "hint": "改写 usage 后逐条 `card <note> --usage ...`；has_more 为 true 时加 --offset 继续；"
                  "确认 due_total 归零后再 `review --done`"})


def cmd_search(args):
    cfg = load_config()
    root = resolve_root(args.root, cfg)
    terms = [t.lower() for t in args.query if t.strip()]
    if not terms:
        fail("empty query")
    scored = []
    for note, meta in iter_all_cards(root):
        topics = meta.get("topics") if isinstance(meta.get("topics"), list) else []
        fields = [
            (3, (fm_str(meta.get("title")) + " " + os.path.basename(note)).lower()),
            (2, (" ".join(topics) + " " + fm_str(meta.get("summary"))).lower()),
            (1, (fm_str(meta.get("usage")) + " " + fm_str(meta.get("site"))).lower()),
        ]
        score = sum(w for term in terms for w, hay in fields if term in hay)
        if score:
            stale = 1 if fm_str(meta.get("status")) == "stale" else 0
            scored.append((stale, -score, compact_card(note, meta)))
    scored.sort(key=lambda x: (x[0], x[1]))
    hits = [item for _, _, item in scored[: args.limit]]

    deep_hits = []
    if args.deep or not hits:
        seen = {h["note"] for h in hits}
        for path in iter_markdown(root):
            if path in seen:
                continue
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    body = fh.read()
            except OSError:
                continue
            low = body.lower()
            pos = min((low.find(t) for t in terms if t in low), default=-1)
            if pos < 0:
                continue
            snippet = re.sub(r"\s+", " ", body[max(0, pos - 60): pos + 120]).strip()
            deep_hits.append({"note": path, "snippet": snippet})
            if len(deep_hits) >= args.limit:
                break
    emit({"ok": True, "query": " ".join(args.query), "root": root,
          "cards": hits, "body_matches": deep_hits})


def cmd_state(args):
    cfg = load_config()
    if args.set_default_root:
        new_root = os.path.realpath(os.path.expanduser(args.set_default_root))
        if not os.path.isdir(new_root):
            fail("default root must be an existing directory: {}".format(new_root))
        cfg["default_root"] = new_root
        save_config(cfg)
    if args.clear_default_root:
        cfg["default_root"] = ""
        save_config(cfg)
    if args.set_review_days:
        cfg["review"]["days"] = max(1, args.set_review_days)
        save_config(cfg)
    if args.forget:
        cfg["destinations"].pop(os.path.realpath(os.path.expanduser(args.forget)), None)
        save_config(cfg)
    emit({
        "ok": True,
        "state_home": STATE_DIR,
        "config_path": CONFIG_PATH,
        "default_root": cfg["default_root"],
        "review": {
            "days": review_days_of(cfg),
            "last_review_at": cfg["review"]["last_review_at"],
            "overdue_days": review_overdue_days(cfg),
        },
        "persona": {
            "path": PERSONA_PATH,
            "exists": os.path.isfile(PERSONA_PATH),
        },
        "known_destinations": {
            path: {
                "folder_strategy": p.get("folder_strategy"),
                "frontmatter_keys": p.get("frontmatter_keys", [])[:8],
                "link_style": p.get("link_style"),
                "attachment_dir": p.get("attachment_dir"),
                "confidence": p.get("confidence"),
                "top_dirs": [d["dir"] for d in p.get("top_dirs", [])],
            }
            for path, p in cfg["destinations"].items()
        },
    })


def cmd_doctor(args):
    """Answer "is this install healthy?" in one call, so a broken state
    directory or an unreadable vault surfaces before it eats a commit."""
    cfg = load_config()
    checks, problems = [], []

    def check(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            problems.append(name)

    check("python", sys.version_info >= (3, 8), sys.version.split()[0])
    writable = os.access(STATE_DIR, os.W_OK) if os.path.isdir(STATE_DIR) else False
    check("state_dir_writable", writable, STATE_DIR)
    check("state_outside_skill",
          os.path.commonpath([os.path.realpath(STATE_DIR), os.path.realpath(SKILL_ROOT)])
          != os.path.realpath(SKILL_ROOT),
          "state lives in {}; set NOTE_VAULT_HOME to move it".format(STATE_DIR))
    check("config_readable", os.path.isfile(CONFIG_PATH) or True,
          "{} (schema {})".format(CONFIG_PATH, cfg.get("schema_version")))
    check("persona", os.path.isfile(PERSONA_PATH),
          PERSONA_PATH if os.path.isfile(PERSONA_PATH)
          else "缺少人设文件，usage 只能写通用说明")

    root = args.root or cfg.get("default_root")
    if root:
        root = os.path.abspath(os.path.expanduser(root))
        check("default_root", os.path.isdir(root), root)
    else:
        check("default_root", False, "未设置；跑 `state --set-default-root <dir>`")

    scanned = broken = 0
    if root and os.path.isdir(root):
        for note, meta in iter_all_cards(root):
            scanned += 1
            summary = fm_str(meta.get("summary"))
            # A summary that swallowed the next line means the note's
            # frontmatter was written by an older, unsafe renderer.
            if summary and ("\n" in summary or summary.rstrip().endswith(":")):
                broken += 1
        check("frontmatter_parses", broken == 0,
              "{} 张卡片，{} 张 frontmatter 可疑".format(scanned, broken))

    emit({"ok": not problems, "state_home": STATE_DIR, "checks": checks,
          "problems": problems,
          "hint": "problems 为空即可正常使用" if not problems else "先修掉 problems 里的项"})


def main():
    parser = argparse.ArgumentParser(prog="vault.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("lookup", help="check whether a url/hash is already filed")
    k.add_argument("--root", help="library root (defaults to default_root)")
    k.add_argument("--url")
    k.add_argument("--canonical-url")
    k.add_argument("--hash", action="append", default=[],
                   help="content hash from the clip package; repeatable")
    k.set_defaults(func=cmd_lookup)

    p = sub.add_parser("profile", help="learn a destination directory's conventions")
    p.add_argument("dir")
    p.add_argument("--refresh", action="store_true", help="ignore cached profile")
    p.add_argument("--save", action="store_true", default=True)
    p.add_argument("--no-save", dest="save", action="store_false")
    p.add_argument("--set-default", action="store_true", help="also set as default_root")
    p.add_argument("--full", action="store_true", help="emit every learned field")
    p.set_defaults(func=cmd_profile)

    c = sub.add_parser("commit", help="file a clip package or .md into the library")
    c.add_argument("source", help="clip package directory, or a .md file")
    c.add_argument("--dest", required=True, help="full target .md path")
    c.add_argument("--root", help="library root (dedup + path guard)")
    c.add_argument("--frontmatter", help="JSON object of frontmatter fields, written first")
    c.add_argument("--body-prefix", help="text inserted above the body, e.g. a source line")
    c.add_argument("--assets", choices=["copy", "none"], default="copy")
    c.add_argument("--assets-dir", default="assets")
    c.add_argument("--link-style", choices=["markdown", "wiki"], default="markdown")
    c.add_argument("--allow-short", action="store_true",
                   help="file even when the body is under {} chars".format(MIN_BODY_CHARS))
    c.add_argument("--force", action="store_true")
    c.set_defaults(func=cmd_commit)

    d = sub.add_parser("card", help="create/update a note's card fields in its frontmatter")
    d.add_argument("note", help="the note's .md path")
    d.add_argument("--summary", help="one-line summary of what the piece says")
    d.add_argument("--usage", help="personalized usage note (also bumps reviewed_at)")
    d.add_argument("--topics", help="comma-separated topics")
    d.add_argument("--status", choices=["active", "stale"], help="mark outdated cards stale")
    d.add_argument("--meta", help="JSON object merged into the frontmatter")
    d.add_argument("--touch-review", action="store_true", help="bump reviewed_at without edits")
    d.set_defaults(func=cmd_card)

    g = sub.add_parser("digest", help="sectioned extract of a clip package or a saved note")
    g.add_argument("target", help="clip package directory or a .md file path")
    g.add_argument("--chars", type=int, default=2800, help="digest length budget")
    g.set_defaults(func=cmd_digest)

    r = sub.add_parser("review", help="list notes due for their periodic refresh")
    r.add_argument("root", nargs="?", help="library root (defaults to default_root)")
    r.add_argument("--days", type=int, help="override the review period")
    r.add_argument("--all", action="store_true", help="list every card, not only due ones")
    r.add_argument("--limit", type=int, default=40)
    r.add_argument("--offset", type=int, default=0, help="skip N due cards (pagination)")
    r.add_argument("--done", action="store_true", help="close this review round (reset the clock)")
    r.set_defaults(func=cmd_review)

    q = sub.add_parser("search", help="search notes by card fields under a root")
    q.add_argument("root")
    q.add_argument("query", nargs="+")
    q.add_argument("--deep", action="store_true", help="also grep note bodies")
    q.add_argument("--limit", type=int, default=10)
    q.set_defaults(func=cmd_search)

    o = sub.add_parser("doctor", help="check that this install is healthy")
    o.add_argument("--root", help="library root to inspect (defaults to default_root)")
    o.set_defaults(func=cmd_doctor)

    t = sub.add_parser("state", help="show or update skill config")
    t.add_argument("--set-default-root")
    t.add_argument("--clear-default-root", action="store_true")
    t.add_argument("--set-review-days", type=int, help="periodic review interval, default 30")
    t.add_argument("--forget", help="drop a learned destination profile")
    t.set_defaults(func=cmd_state)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
