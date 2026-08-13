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

class TestTimeInWords(unittest.TestCase):

    def test_scales_from_minutes_to_days(self):
        now = 1_000_000_000
        self.assertEqual(generate.ago(now - 5, now), "just now")
        self.assertEqual(generate.ago(now - 100, now), "1 minute ago")
        self.assertEqual(generate.ago(now - 60 * 45, now), "45 minutes ago")
        self.assertEqual(generate.ago(now - 3600 * 3, now), "3 hours ago")
        self.assertEqual(generate.ago(now - 86400 * 2, now), "2 days ago")

    def test_old_enough_gets_a_date(self):
        now = 1_000_000_000
        self.assertTrue(generate.ago(now - 86400 * 90, now).startswith("on 2"))

    def test_nothing_known(self):
        self.assertEqual(generate.ago(None), "unknown")


class TestFailureDetails(unittest.TestCase):
    """Triage starts on the status page, so the log has to be reachable there."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        generate.generate(STATUS, self.directory)
        with open(os.path.join(self.directory, "status.html"), encoding="utf-8") as handle:
            self.status = handle.read()

    def test_the_failure_row_links_the_build_log(self):
        self.assertIn("https://github.com/run/2", self.status)
        self.assertIn("<th>Log</th>", self.status)

    def test_the_failure_row_names_the_target(self):
        self.assertIn("<th>Target</th>", self.status)


class TestFreshness(unittest.TestCase):
    """A stale copy of the site must not look like a live one."""

    def test_pages_report_when_the_status_was_published(self):
        status = dict(STATUS, generated_at=1_000_000_000)
        with tempfile.TemporaryDirectory() as directory:
            generate.generate(status, directory)
            with open(os.path.join(directory, "index.html"), encoding="utf-8") as handle:
                index = handle.read()
        self.assertIn("Status published", index)
        self.assertIn("2001-09-09", index)

    def test_a_status_without_a_timestamp_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            generate.generate(STATUS, directory)
            with open(os.path.join(directory, "index.html"), encoding="utf-8") as handle:
                index = handle.read()
        self.assertIn("unknown time", index)


class TestRecentlyBuilt(unittest.TestCase):
    """msys2 calls this Repo Updates: what has changed lately."""

    def status(self):
        return dict(STATUS, packages=[
            dict(STATUS["packages"][0],
                 builds={"vita": {"status": "finished", "details": {}, "built_at": 200.0,
                                  "downloads": 5}}),
            dict(STATUS["packages"][1], name="libpng",
                 builds={"vita": {"status": "finished", "details": {}, "built_at": 300.0,
                                  "downloads": 1}}),
        ])

    def test_newest_first(self):
        entries = generate.recently_built(self.status())
        self.assertEqual([e["package"]["name"] for e in entries], ["libpng", "zlib"])

    def test_packages_never_built_are_absent(self):
        self.assertEqual(generate.recently_built(STATUS), [])

    def test_the_page_exists_and_lists_them(self):
        with tempfile.TemporaryDirectory() as directory:
            written = generate.generate(self.status(), directory)
            self.assertIn("updates.html", written)
            with open(os.path.join(directory, "updates.html"), encoding="utf-8") as handle:
                page = handle.read()
        self.assertIn("libpng", page)
        self.assertIn("<th>Downloads</th>", page)


class TestFiltering(unittest.TestCase):

    def test_rows_carry_what_the_filter_needs(self):
        with tempfile.TemporaryDirectory() as directory:
            generate.generate(TWO_WORLDS, directory)
            with open(os.path.join(directory, "index.html"), encoding="utf-8") as handle:
                index = handle.read()
        self.assertIn('data-search=', index)
        self.assertIn('data-worlds="vita vita-musl"', index)

    def test_a_target_selector_appears_only_with_more_than_one(self):
        with tempfile.TemporaryDirectory() as directory:
            generate.generate(TWO_WORLDS, directory)
            with open(os.path.join(directory, "index.html"), encoding="utf-8") as handle:
                many = handle.read()
        with tempfile.TemporaryDirectory() as directory:
            generate.generate(STATUS, directory)
            with open(os.path.join(directory, "index.html"), encoding="utf-8") as handle:
                one = handle.read()
        self.assertIn('id="world"', many)
        self.assertNotIn('id="world"', one)

    def test_the_description_is_searchable_too(self):
        with tempfile.TemporaryDirectory() as directory:
            generate.generate(STATUS, directory)
            with open(os.path.join(directory, "index.html"), encoding="utf-8") as handle:
                index = handle.read()
        self.assertIn("lossless data-compression", index.lower())


class TestSnapshots(unittest.TestCase):
    """The published snapshots are the only history the site can show."""

    def status(self, **extra):
        base = {"generated_at": 1786600000, "worlds": STATUS["worlds"], "packages": [],
                "snapshot_repo": "vitasdk/vitasdk-autobuild"}
        base.update(extra)
        return base

    def test_a_snapshot_links_to_its_release_and_names_its_core(self):
        html = generate.render_snapshots(self.status(
            published_tag="packages-snapshot-20260812.1.1",
            published_snapshots=[{"tag": "packages-snapshot-20260812.1.1",
                                  "published_at": "2026-08-12T18:47:27Z",
                                  "core_snapshot": "sdk-snapshot-20260812.565.1",
                                  "packages_revision": "c3ab29788f379a824f648c8b"}]))
        self.assertIn("https://github.com/vitasdk/vitasdk-autobuild/releases/tag/"
                      "packages-snapshot-20260812.1.1", html)
        self.assertIn("sdk-snapshot-20260812.565.1", html)
        self.assertIn("c3ab297", html)
        self.assertIn("current", html)

    def test_nothing_published_says_so_instead_of_an_empty_table(self):
        html = generate.render_snapshots(self.status(published_snapshots=[]))
        self.assertIn("Nothing has been published yet", html)
        self.assertNotIn("<table>", html)

    def test_an_older_status_file_does_not_claim_nothing_is_published(self):
        html = generate.render_snapshots(self.status())
        self.assertNotIn("Nothing has been published", html)
        self.assertIn("before the site listed", html)

    def test_an_unreadable_date_does_not_hide_the_snapshot(self):
        html = generate.render_snapshots(self.status(
            published_snapshots=[{"tag": "packages-snapshot-1",
                                  "published_at": "whenever"}]))
        self.assertIn("packages-snapshot-1", html)
        self.assertIn("whenever", html)

    def test_the_repository_column_names_the_snapshot(self):
        html = generate.render_index(self.status(
            published_tag="packages-snapshot-20260812.1.1",
            packages=[]))
        self.assertIn("In 20260812.1.1", html)
        self.assertIn('href="snapshots.html"', html)

    def test_without_a_snapshot_the_column_keeps_its_generic_name(self):
        html = generate.render_index(self.status(packages=[]))
        self.assertIn("In repository", html)

    def test_the_page_is_generated(self):
        with tempfile.TemporaryDirectory() as output:
            written = generate.generate(self.status(), output)
        self.assertIn("snapshots.html", written)



class TestDeprecation(unittest.TestCase):
    """A deprecated package is marked where somebody would pick it."""

    def status(self, deprecated):
        return {"generated_at": 1786600000, "worlds": STATUS["worlds"],
                "packages_repo": "vitasdk/packages", "published_snapshots": [],
                "packages": [{"name": "cpython", "version": "2.7-1",
                              "description": "Python 2", "deprecated": deprecated,
                              "builds": {"vita": {"status": "finished", "details": {}}}}]}

    def test_the_catalogue_marks_it(self):
        html = generate.render_index(self.status("Python 2 is unsupported; use cpython3"))
        self.assertIn(">deprecated<", html)
        self.assertIn("use cpython3", html)

    def test_the_package_page_leads_with_it(self):
        status = self.status("Python 2 is unsupported; use cpython3")
        html = generate.render_package(status["packages"][0], status)
        self.assertIn("Deprecated.", html)
        self.assertLess(html.index("Deprecated."), html.index("In the repository"))

    def test_a_normal_package_is_not_marked(self):
        html = generate.render_index(self.status(""))
        self.assertNotIn("deprecated", html)



class TestReleases(unittest.TestCase):
    """The catalogue has to describe what a person can ask for."""

    SERIES = {
        "2026.09": {"status": "supported", "summary": "First stable release",
                    "sequence": 4, "core": "sdk-snapshot-20260812.568.1",
                    "packages": "packages-snapshot-20260813.2.1", "deprecated": {}},
        "nightly": {"status": "development", "summary": "Rebuilt continuously",
                    "sequence": 41, "core": "sdk-snapshot-20260812.568.1",
                    "packages": "packages-snapshot-20260813.2.1", "deprecated": {}},
    }

    def status(self):
        return {"generated_at": 1786600000, "worlds": STATUS["worlds"],
                "packages": [], "published_snapshots": [
                    {"tag": "packages-snapshot-20260813.2.1",
                     "published_at": "2026-08-13T07:30:57Z",
                     "core_snapshot": "sdk-snapshot-20260812.568.1"}],
                "snapshot_repo": "vitasdk/vitasdk-autobuild"}

    def test_a_release_is_named_with_what_it_serves(self):
        html = generate.render_releases(self.status(), self.SERIES)
        self.assertIn("2026.09", html)
        self.assertIn("Supported", html)
        self.assertIn("packages-snapshot-20260813.2.1", html)
        self.assertIn("VITASDK_CHANNEL=2026.09", html)

    def test_no_published_series_is_not_an_error(self):
        html = generate.render_releases(self.status(), {})
        self.assertIn("No release series are published yet", html)

    def test_a_snapshot_says_which_release_serves_it(self):
        # Being the newest and being the one people install are different
        # facts, and only the second matters to a reader.
        html = generate.render_snapshots(self.status(), self.SERIES)
        self.assertIn(">2026.09<", html)

    def test_the_page_is_generated_and_linked(self):
        with tempfile.TemporaryDirectory() as output:
            written = generate.generate(self.status(), output, self.SERIES)
            self.assertIn("releases.html", written)
            with open(os.path.join(output, "index.html"), encoding="utf-8") as handle:
                self.assertIn('href="releases.html"', handle.read())

    def test_unreachable_channels_do_not_stop_the_build(self):
        self.assertEqual(generate.load_channels("https://127.0.0.1:9/channels"), {})



if __name__ == "__main__":
    unittest.main()
