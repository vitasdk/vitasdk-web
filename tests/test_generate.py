import json
import os
import tempfile
import unittest

from vitasdk_web import generate

STATUS = {
    "schema_version": 2,
    "worlds": [
        {"arch": "vita", "core": "sdk-snapshot-20260812.565.1", "repository": "vita",
         "staging_repository": "vita-staging", "description": "gcc and newlib"},
    ],
    "packages_repo": "vitasdk/packages",
    "packages_revision": "0123456789abcdef0123456789abcdef01234567",
    "jobs": [{"name": "build", "html_url": "https://github.com/run/1",
              "started_at": "2026-08-12T10:00:00Z"}],
    "packages": [
        {"name": "zlib", "version": "1.3.2-2", "repo_version": "1.3.2-1",
         "description": "A lossless data-compression library",
         "url": "https://www.zlib.net/", "licenses": ["Zlib"], "binaries": ["zlib"],
         "depends": [], "rdepends": ["libpng"], "status": "finished", "details": {}},
        {"name": "libpng", "version": "1.6.40-1", "repo_version": "",
         "description": "PNG <reference> library", "url": "", "licenses": [],
         "binaries": ["libpng"], "depends": ["zlib"], "rdepends": [],
         "builds": {"vita": {"status": "failed-to-build",
                             "details": {"desc": "",
                                         "urls": {"build": "https://github.com/run/2"}}}}},
        {"name": "blocked-one", "version": "1.0-1", "repo_version": "0.9-1",
         "description": "", "url": "", "licenses": [], "binaries": ["blocked-one"],
         "depends": [], "rdepends": [], "status": "finished-but-blocked",
         "details": {"desc": "Blocked by: libpng"}},
    ],
    "cycles": {"vita": [["a", "b"]]},
}


class TestGenerate(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.written = generate.generate(STATUS, self.directory)

    def read(self, relative):
        with open(os.path.join(self.directory, relative), encoding="utf-8") as handle:
            return handle.read()

    def test_writes_a_page_per_package(self):
        for name in ("zlib", "libpng", "blocked-one"):
            self.assertIn(os.path.join("package", f"{name}.html"), self.written)

    def test_writes_the_entry_points(self):
        for name in ("index.html", "status.html", "style.css", "api/status.json"):
            self.assertIn(name, self.written)

    def test_the_api_is_the_status_file_itself(self):
        self.assertEqual(json.loads(self.read("api/status.json")), STATUS)

    def test_index_lists_every_package(self):
        index = self.read("index.html")
        for name in ("zlib", "libpng", "blocked-one"):
            self.assertIn(f'href="package/{name}.html"', index)

    def test_index_shows_both_versions(self):
        index = self.read("index.html")
        self.assertIn("1.3.2-2", index)
        self.assertIn("1.3.2-1", index)

    def test_descriptions_are_escaped(self):
        # Recipe metadata is not ours and ends up in the page verbatim.
        index = self.read("index.html")
        self.assertNotIn("<reference>", index)
        self.assertIn("&lt;reference&gt;", index)

    def test_package_page_links_dependencies_both_ways(self):
        zlib = self.read(os.path.join("package", "zlib.html"))
        self.assertIn('href="libpng.html"', zlib)
        libpng = self.read(os.path.join("package", "libpng.html"))
        self.assertIn('href="zlib.html"', libpng)

    def test_package_page_links_the_failed_build_log(self):
        self.assertIn("https://github.com/run/2",
                      self.read(os.path.join("package", "libpng.html")))

    def test_blocked_packages_explain_themselves(self):
        page = self.read(os.path.join("package", "blocked-one.html"))
        self.assertIn("Blocked by: libpng", page)
        self.assertIn("held back", page)

    def test_status_page_lists_workers_failures_and_cycles(self):
        status = self.read("status.html")
        self.assertIn("https://github.com/run/1", status)
        self.assertIn("libpng", status)
        self.assertIn("a", status)
        self.assertIn("Dependency cycles", status)

    def test_status_page_documents_the_staging_repository(self):
        status = self.read("status.html")
        self.assertIn("[vita-staging]", status)
        self.assertIn("releases/download/staging", status)
        # The warning is the point: staging can hold partial rebuilds.
        self.assertIn("partial results", status)

    def test_pages_are_regenerated_from_scratch(self):
        with open(os.path.join(self.directory, "stale.html"), "w", encoding="utf-8") as handle:
            handle.write("old")
        generate.generate(STATUS, self.directory)
        self.assertFalse(os.path.exists(os.path.join(self.directory, "stale.html")))

    def test_every_page_is_theme_aware(self):
        self.assertIn("prefers-color-scheme", self.read("style.css"))

    def test_counts(self):
        self.assertEqual(generate.counts(STATUS["packages"], "vita", STATUS["worlds"]),
                         {"finished": 1, "failed-to-build": 1, "finished-but-blocked": 1})


class TestEmptyStatus(unittest.TestCase):

    def test_a_catalogue_with_no_packages_still_renders(self):
        status = {"packages": [], "worlds": [], "packages_repo": "",
                  "jobs": [], "cycles": {}}
        with tempfile.TemporaryDirectory() as directory:
            written = generate.generate(status, directory)
        self.assertIn("index.html", written)

class TestMissingStatus(unittest.TestCase):

    def test_a_missing_status_file_is_not_a_failure(self):
        # While the autobuilder is being set up there is no status file. That
        # is nothing to render, not a failed render, and the two must not look
        # the same to CI.
        import io
        import contextlib
        import urllib.error
        from unittest import mock

        error = urllib.error.HTTPError("https://example/status.json", 404, "Not Found", {}, None)
        output = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with contextlib.redirect_stdout(output):
                code = generate.main(["generate", "--status", "vitasdk/vitasdk-autobuild"])
        self.assertEqual(code, 2)
        self.assertIn("No status file published yet", output.getvalue())

    def test_a_real_http_error_still_fails(self):
        import urllib.error
        from unittest import mock

        error = urllib.error.HTTPError("https://example/status.json", 500, "Boom", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(SystemExit):
                generate.main(["generate", "--status", "vitasdk/vitasdk-autobuild"])

TWO_WORLDS = {
    "schema_version": 2,
    "worlds": [
        {"arch": "vita", "core": "core-newlib", "repository": "vita",
         "staging_repository": "vita-staging", "description": "gcc and newlib"},
        {"arch": "vita-musl", "core": "core-musl", "repository": "vita-musl",
         "staging_repository": "vita-musl-staging", "description": "llvm and musl"},
    ],
    "packages_repo": "vitasdk/packages",
    "packages_revision": "abc123",
    "jobs": [],
    "packages": [
        {"name": "zlib", "version": "1.3.2-2", "repo_version": "", "description": "",
         "url": "", "licenses": [], "binaries": ["zlib"], "depends": [], "rdepends": [],
         "builds": {"vita": {"status": "finished", "details": {}},
                    "vita-musl": {"status": "failed-to-build", "details": {}}}},
        {"name": "taihen", "version": "1.0-1", "repo_version": "", "description": "",
         "url": "", "licenses": [], "binaries": ["taihen"], "depends": [], "rdepends": [],
         "builds": {"vita": {"status": "finished", "details": {}}}},
    ],
    "cycles": {"vita": [], "vita-musl": []},
}


class TestTwoWorlds(unittest.TestCase):
    """One world today, but the catalogue has to read with two."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        generate.generate(TWO_WORLDS, self.directory)

    def read(self, relative):
        with open(os.path.join(self.directory, relative), encoding="utf-8") as handle:
            return handle.read()

    def test_the_index_has_a_column_per_world(self):
        index = self.read("index.html")
        self.assertIn("<th>vita</th>", index)
        self.assertIn("<th>vita-musl</th>", index)

    def test_a_package_shows_a_different_state_per_world(self):
        page = self.read(os.path.join("package", "zlib.html"))
        self.assertIn("Built", page)
        self.assertIn("Failed", page)

    def test_a_package_missing_from_a_world_says_so(self):
        page = self.read(os.path.join("package", "taihen.html"))
        self.assertIn("not built for this target", page)

    def test_the_status_page_lists_a_staging_repository_per_world(self):
        status = self.read("status.html")
        self.assertIn("[vita-staging]", status)
        self.assertIn("[vita-musl-staging]", status)

    def test_failures_say_which_world(self):
        status = self.read("status.html")
        self.assertIn("<th>Target</th>", status)
        self.assertIn("vita-musl", status)

    def test_counts_are_per_world(self):
        self.assertEqual(generate.counts(TWO_WORLDS["packages"], "vita", TWO_WORLDS["worlds"]),
                         {"finished": 2})
        self.assertEqual(generate.counts(TWO_WORLDS["packages"], "vita-musl",
                                         TWO_WORLDS["worlds"]),
                         {"failed-to-build": 1})


if __name__ == "__main__":
    unittest.main()
