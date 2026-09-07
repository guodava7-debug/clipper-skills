#!/usr/bin/env python3
"""Offline tests for web-clip and note-vault.

No test touches the network. The fetcher is exercised through fixtures and a
fake `requests.get`, so the suite is deterministic and safe to run in CI.

Run with:  python3 -m unittest discover -s tests -v
"""

import hashlib
import importlib.util
import json
import os
import stat
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# State homes must be redirected before the modules are imported: both scripts
# resolve (and migrate) their state directory at import time.
_STATE = tempfile.mkdtemp(prefix="skills-test-state-")
os.environ["WEB_CLIP_HOME"] = os.path.join(_STATE, "web-clip")
os.environ["NOTE_VAULT_HOME"] = os.path.join(_STATE, "note-vault")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLIP_PY = os.path.join(REPO, "web-clip", "scripts", "clip.py")
VAULT_PY = os.path.join(REPO, "note-vault", "scripts", "vault.py")
vault = load("vault_under_test", VAULT_PY)
try:
    clip = load("clip_under_test", CLIP_PY)
except SystemExit as exc:              # requests / bs4 missing
    clip = None
    CLIP_IMPORT_ERROR = str(exc)


def run_cli(script, *argv, **kwargs):
    """Run a subcommand and return (exit_code, parsed_json)."""
    env = dict(os.environ)
    env.update(kwargs.get("env") or {})
    proc = subprocess.run([sys.executable, script] + list(argv),
                          capture_output=True, text=True, env=env)
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        payload = {"_stdout": proc.stdout, "_stderr": proc.stderr}
    return proc.returncode, payload


needs_clip = unittest.skipIf(clip is None, "web-clip dependencies not installed")


# --------------------------------------------------------------- web-clip: net

@needs_clip
class TestFetchGuards(unittest.TestCase):
    """The fetcher must refuse anything that is not a public http(s) endpoint."""

    def test_rejects_non_http_schemes(self):
        for url in ("file:///etc/passwd", "gopher://x/1", "ftp://example.com/x",
                    "data:text/html,<b>hi</b>"):
            self.assertIn("scheme", clip.url_rejection(url), url)

    def test_rejects_local_and_private_targets(self):
        for url in ("http://127.0.0.1/", "http://localhost/", "http://[::1]/",
                    "http://0.0.0.0/", "http://10.0.0.5/", "http://192.168.1.1/",
                    "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/",
                    "http://[::ffff:127.0.0.1]/", "http://printer.local/"):
            self.assertNotEqual("", clip.url_rejection(url), url)

    def test_allows_a_public_literal_address(self):
        self.assertEqual("", clip.address_rejection("93.184.216.34"))
        self.assertEqual("", clip.address_rejection("2606:2800:220:1:248:1893:25c8:1946"))

    def test_missing_host(self):
        self.assertNotEqual("", clip.url_rejection("http:///nohost"))


class FakeResponse(object):
    def __init__(self, status=200, headers=None, body=b"", url="https://example.com/"):
        self.status_code = status
        self.headers = headers or {}
        self.url = url
        self._body = body
        self.encoding = "utf-8"
        self.closed = False

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "location" in self.headers

    is_permanent_redirect = is_redirect

    @property
    def content(self):
        return getattr(self, "_content", self._body)

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def close(self):
        self.closed = True


@needs_clip
class TestRedirectHandling(unittest.TestCase):
    """A public url must not be able to bounce the fetcher into the intranet."""

    def setUp(self):
        self.calls = []
        self.original = clip.requests.get
        clip.requests.get = self.fake_get
        self.addCleanup(setattr, clip.requests, "get", self.original)

    def fake_get(self, url, **kwargs):
        self.calls.append(url)
        return self.responses.pop(0)

    def test_redirect_into_localhost_is_refused(self):
        self.responses = [
            FakeResponse(302, {"location": "http://127.0.0.1:8080/admin"}),
            FakeResponse(200, {"content-type": "text/html"}, b"<html>secret</html>"),
        ]
        with self.assertRaises(clip.FetchRefused) as caught:
            clip.safe_get("http://93.184.216.34/start")
        self.assertIn("127.0.0.1", str(caught.exception))
        self.assertEqual(1, len(self.calls), "must not request the private hop")

    def test_redirect_loop_is_bounded(self):
        self.responses = [FakeResponse(302, {"location": "http://93.184.216.34/next"})
                          for _ in range(20)]
        with self.assertRaises(clip.FetchRefused) as caught:
            clip.safe_get("http://93.184.216.34/start")
        self.assertIn("redirects", str(caught.exception))
        self.assertLessEqual(len(self.calls), clip.MAX_REDIRECTS + 1)

    def test_oversized_body_is_refused_by_declared_length(self):
        self.responses = [FakeResponse(
            200, {"content-type": "text/html", "content-length": str(99 * 1024 * 1024)}, b"x")]
        with self.assertRaises(clip.FetchRefused):
            clip.safe_get("http://93.184.216.34/big")

    def test_oversized_body_is_refused_while_streaming(self):
        self.responses = [FakeResponse(200, {"content-type": "text/html"},
                                       b"x" * (clip.MAX_HTML_BYTES + 1024))]
        with self.assertRaises(clip.FetchRefused):
            clip.safe_get("http://93.184.216.34/big")


@needs_clip
class TestAssetDownload(unittest.TestCase):
    """Image urls come from the page, so they are attacker-controlled input."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="clip-assets-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.original = clip.safe_get
        self.addCleanup(setattr, clip, "safe_get", self.original)

    def test_svg_is_not_saved(self):
        clip.safe_get = lambda url, **kw: (
            clip.attach_body(FakeResponse(200, {"content-type": "image/svg+xml"}),
                             b'<svg onload="alert(1)"></svg>'), url)
        saved, failed = clip.download_assets(
            [{"url": "https://cdn.example.com/a.svg", "index": 1, "alt": ""}],
            self.dir, "note", "https://example.com/")
        self.assertEqual([], saved)
        self.assertIn("not an allowed image", failed[0]["reason"])

    def test_html_disguised_as_png_is_rejected(self):
        clip.safe_get = lambda url, **kw: (
            clip.attach_body(FakeResponse(200, {"content-type": "image/png"}),
                             b"<html><script>x</script></html>"), url)
        saved, failed = clip.download_assets(
            [{"url": "https://cdn.example.com/a.png", "index": 1, "alt": ""}],
            self.dir, "note", "https://example.com/")
        self.assertEqual([], saved)
        self.assertIn("markup", failed[0]["reason"])

    def test_real_png_is_saved_with_its_hash(self):
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        clip.safe_get = lambda url, **kw: (
            clip.attach_body(FakeResponse(200, {"content-type": "image/png"}), png), url)
        saved, failed = clip.download_assets(
            [{"url": "https://cdn.example.com/a.png", "index": 1, "alt": "pic"}],
            self.dir, "note", "https://example.com/")
        self.assertEqual([], failed)
        self.assertEqual(64, len(saved[0]["sha256"]))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "assets", saved[0]["file"])))

    def test_refused_url_is_reported_not_raised(self):
        def refuse(url, **kw):
            raise clip.FetchRefused("non-public address 127.0.0.1")
        clip.safe_get = refuse
        saved, failed = clip.download_assets(
            [{"url": "http://127.0.0.1/a.png", "index": 1, "alt": ""}],
            self.dir, "note", "https://example.com/")
        self.assertEqual([], saved)
        self.assertEqual(1, len(failed))


# ----------------------------------------------------------- web-clip: package

@needs_clip
class TestClipPackage(unittest.TestCase):
    """The package is a contract; its shape is tested, not just its contents."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="clip-pkg-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def ingest(self, fixture, extra=()):
        out = os.path.join(self.dir, "pkg")
        code, payload = run_cli(CLIP_PY, "ingest", "--file", os.path.join(FIXTURES, fixture),
                                "--url", "https://example.com/article",
                                "--out", out, "--assets", "none", *extra)
        self.assertEqual(0, code, payload)
        return out, payload

    def test_manifest_is_versioned_and_self_describing(self):
        out, _ = self.ingest("article.html")
        with open(os.path.join(out, "meta.json"), encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(clip.PACKAGE_FORMAT, record["format"])
        self.assertEqual(1, int(record["format_version"].split(".")[0]))
        # Every declared path is relative, so the package can be moved.
        for value in record["files"].values():
            if value:
                self.assertFalse(os.path.isabs(value), value)
        self.assertEqual(64, len(record["content_sha256"]))

    def test_verify_passes_on_a_fresh_package_and_fails_after_tampering(self):
        out, _ = self.ingest("article.html")
        code, payload = run_cli(CLIP_PY, "verify", out)
        self.assertEqual(0, code, payload)
        with open(os.path.join(out, "content.md"), "a", encoding="utf-8") as fh:
            fh.write("\ninjected\n")
        code, payload = run_cli(CLIP_PY, "verify", out)
        self.assertEqual(1, code)
        self.assertTrue(payload["problems"])

    def test_content_hash_ignores_asset_outcomes(self):
        """Re-clipping the same page must not look like new content just
        because an image happened to 403 that time."""
        first, _ = self.ingest("article.html")
        second = os.path.join(self.dir, "pkg2")
        code, _ = run_cli(CLIP_PY, "ingest", "--file", os.path.join(FIXTURES, "article.html"),
                          "--url", "https://example.com/article",
                          "--out", second, "--assets", "none")
        self.assertEqual(0, code)
        with open(os.path.join(first, "meta.json"), encoding="utf-8") as fh:
            a = json.load(fh)["content_sha256"]
        with open(os.path.join(second, "meta.json"), encoding="utf-8") as fh:
            b = json.load(fh)["content_sha256"]
        self.assertEqual(a, b)

    def test_gc_removes_only_stale_packages(self):
        out, _ = self.ingest("article.html")
        code, payload = run_cli(CLIP_PY, "gc", "--days", "365")
        self.assertEqual(0, code, payload)
        self.assertEqual([], payload["released"])


# ------------------------------------------------------------ note-vault: yaml

class TestFrontmatterSafety(unittest.TestCase):
    """Card fields are model-written free text; they must never become syntax."""

    HOSTILE = [
        "多行\nusage: 我被注入了",
        "- 看起来像列表项",
        "key: value 冒号加空格",
        "  前后有空格  ",
        "true",
        "123",
        "null",
        "#看起来像注释",
        "尾部冒号:",
        "带 # 注释的正文",
        "{flow: mapping}",
        "[flow, seq]",
        "*anchor",
        "!!python/object",
        '引号"和\\反斜杠',
    ]

    def round_trip(self, pairs):
        text = "---\n" + "\n".join(vault.render_frontmatter_lines(pairs)) + "\n---\nbody\n"
        meta, end = vault.parse_frontmatter(text)
        return meta, text[end:]

    def test_hostile_values_round_trip_without_leaking(self):
        for value in self.HOSTILE:
            meta, body = self.round_trip({"summary": value, "status": "active"})
            self.assertEqual(value, meta["summary"], value)
            self.assertEqual("active", meta["status"], value)
            self.assertEqual("body\n", body)

    def test_injection_does_not_create_new_fields(self):
        meta, _ = self.round_trip({"summary": "x\nusage: 我被注入了\nstatus: stale"})
        self.assertNotIn("usage", meta)
        self.assertNotIn("status", meta)

    def test_lists_round_trip(self):
        meta, _ = self.round_trip({"topics": ["AI", "- 陷阱", "a: b", "多行\n项"]})
        self.assertEqual(["AI", "- 陷阱", "a: b", "多行\n项"], meta["topics"])

    def test_plain_values_stay_unquoted(self):
        lines = vault.render_frontmatter_lines(
            {"title": "普通标题", "url": "https://example.com/a?b=1", "saved_at": "2026-09-07"})
        self.assertIn("title: 普通标题", lines)
        self.assertIn("url: https://example.com/a?b=1", lines)
        self.assertIn("saved_at: 2026-09-07", lines)

    def test_unsafe_keys_are_dropped_not_written(self):
        dropped = []
        lines = vault.render_frontmatter_lines(
            {"status": "active", "bad: key": "x", "#comment": "y"}, dropped=dropped)
        self.assertEqual(["status: active"], lines)
        self.assertEqual({"bad: key", "#comment"}, set(dropped))


# ---------------------------------------------------------- note-vault: commit

class VaultCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vault-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "vault")
        os.makedirs(self.root)
        self.state = os.path.join(self.tmp, "state")
        self.env = {"NOTE_VAULT_HOME": self.state}

    def make_package(self, body="正文" * 80, assets=(), **overrides):
        pkg = tempfile.mkdtemp(dir=self.tmp, prefix="clip-")
        os.makedirs(os.path.join(pkg, "assets"), exist_ok=True)
        for name, data in assets:
            with open(os.path.join(pkg, "assets", name), "wb") as fh:
                fh.write(data)
        with open(os.path.join(pkg, "content.md"), "w", encoding="utf-8") as fh:
            fh.write(body)
        record = {
            "format": "web-clip/clip-package",
            "format_version": "1.0",
            "title": "测试文章",
            "url": "https://example.com/post",
            "canonical_url": "https://example.com/post",
            "site": "example.com",
            "type": "文章",
            "kind": "page",
            "assets": [{"file": n} for n, _ in assets],
            "asset_failures": [],
            "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "files": {"content": "content.md", "assets_dir": "assets", "raw_html": None},
        }
        record.update(overrides)
        with open(os.path.join(pkg, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        return pkg

    def commit(self, pkg, dest, *extra):
        return run_cli(VAULT_PY, "commit", pkg, "--dest", dest,
                       "--root", self.root, *extra, env=self.env)


class TestCommit(VaultCase):
    def test_body_survives_verbatim(self):
        body = "# 标题\n\n段落一\n\n- 列表\n"
        pkg = self.make_package(body=body)
        dest = os.path.join(self.root, "note.md")
        code, payload = self.commit(pkg, dest, "--allow-short")
        self.assertEqual(0, code, payload)
        with open(dest, encoding="utf-8") as fh:
            written = fh.read()
        self.assertIn(body.strip(), written)

    def test_unknown_package_major_is_refused(self):
        pkg = self.make_package(format_version="9.0")
        code, payload = self.commit(pkg, os.path.join(self.root, "future.md"))
        self.assertEqual(1, code)
        self.assertIn("not supported", payload["error"])

    def test_foreign_manifest_is_refused(self):
        pkg = self.make_package(format="some-other-tool/v1")
        code, payload = self.commit(pkg, os.path.join(self.root, "foreign.md"))
        self.assertEqual(1, code)
        self.assertIn("format", payload["error"])

    def test_edited_body_is_reported(self):
        pkg = self.make_package()
        with open(os.path.join(pkg, "content.md"), "a", encoding="utf-8") as fh:
            fh.write("\n偷偷加的一句\n")
        code, payload = self.commit(pkg, os.path.join(self.root, "edited.md"))
        self.assertEqual(0, code, payload)
        self.assertTrue(any("hash" in n for n in payload.get("notes", [])))

    def test_duplicate_url_blocks_a_second_commit(self):
        pkg = self.make_package()
        self.assertEqual(0, self.commit(pkg, os.path.join(self.root, "one.md"))[0])
        code, payload = self.commit(pkg, os.path.join(self.root, "two.md"))
        self.assertEqual(2, code)
        self.assertEqual("url", payload["duplicate"]["match"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "two.md")))

    def test_destination_cannot_escape_the_root(self):
        pkg = self.make_package()
        code, payload = self.commit(pkg, os.path.join(self.tmp, "outside.md"))
        self.assertEqual(1, code)
        self.assertIn("escapes root", payload["error"])


class TestCommitIsAtomic(VaultCase):
    """A failure mid-archive must leave the vault byte-for-byte as it was."""

    def setUp(self):
        VaultCase.setUp(self)
        # Attachments go somewhere writable, the note somewhere read-only, so
        # the note write fails only after the assets are already in place.
        self.attachments = os.path.join(self.root, "attach")
        self.locked = os.path.join(self.root, "locked")
        os.makedirs(self.attachments)
        os.makedirs(self.locked)
        os.chmod(self.locked, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, self.locked, stat.S_IRWXU)

    def archive(self, pkg):
        return self.commit(pkg, os.path.join(self.locked, "note.md"),
                           "--assets-dir", self.attachments)

    def test_failed_note_write_leaves_no_orphan_assets(self):
        pkg = self.make_package(assets=[("pic-01.png", b"\x89PNG" + b"0" * 32)])
        code, payload = self.archive(pkg)
        self.assertEqual(1, code, payload)
        self.assertIn("rolled back", payload["error"])
        self.assertEqual([], os.listdir(self.attachments),
                         "assets were copied but the note was never written")

    def test_overwritten_attachment_is_restored_on_failure(self):
        original = os.path.join(self.attachments, "pic-01.png")
        with open(original, "wb") as fh:
            fh.write(b"ORIGINAL")
        pkg = self.make_package(assets=[("pic-01.png", b"REPLACEMENT")])
        code, _ = self.archive(pkg)
        self.assertEqual(1, code)
        with open(original, "rb") as fh:
            self.assertEqual(b"ORIGINAL", fh.read())


class TestCard(VaultCase):
    def test_card_updates_never_touch_the_body(self):
        body = "# 标题\n\n正文正文正文\n"
        pkg = self.make_package(body=body)
        dest = os.path.join(self.root, "note.md")
        self.assertEqual(0, self.commit(pkg, dest, "--allow-short")[0])
        code, payload = run_cli(VAULT_PY, "card", dest,
                                "--summary", "一句话\n还有一行",
                                "--usage", "写周报时可以引用: 见第 2 节",
                                "--topics", "AI,工具", env=self.env)
        self.assertEqual(0, code, payload)
        with open(dest, encoding="utf-8") as fh:
            text = fh.read()
        meta, end = vault.parse_frontmatter(text)
        self.assertEqual("一句话\n还有一行", meta["summary"])
        self.assertEqual(["AI", "工具"], meta["topics"])
        self.assertIn(body.strip(), text[end:])


class TestDoctor(VaultCase):
    def test_doctor_reports_state_location(self):
        code, payload = run_cli(VAULT_PY, "doctor", "--root", self.root, env=self.env)
        self.assertIn(code, (0, 1))
        self.assertEqual(self.state, payload["state_home"])
        names = {c["check"] for c in payload["checks"]}
        self.assertIn("state_outside_skill", names)


class TestStateIsExternal(unittest.TestCase):
    """State must not live inside the skill directory, or an update wipes it."""

    def test_state_home_is_outside_the_skill(self):
        for module, skill in ((vault, "note-vault"), (clip, "web-clip")):
            if module is None:
                continue
            home = module.STATE_DIR if skill == "note-vault" else module.STATE_HOME
            skill_root = os.path.realpath(os.path.join(REPO, skill))
            self.assertNotEqual(
                skill_root,
                os.path.commonpath([os.path.realpath(home), skill_root]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
