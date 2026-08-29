import io
import tarfile
import json
import os
import re
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

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

# generate() only hits the network when a channel names a snapshot repository
# or a core repository; STATUS and this SERIES have neither, so tests that
# use them exercise the real code path without needing a mock.
ENTRY_POINTS = ("index.html", "downloads.html", "packages.html", "status.html",
                "style.css", "api/status.json")


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
        for name in ENTRY_POINTS:
            self.assertIn(name, self.written)

    def test_the_api_is_the_status_file_itself(self):
        self.assertEqual(json.loads(self.read("api/status.json")), STATUS)

    def test_the_catalogue_lists_every_package(self):
        packages = self.read("packages.html")
        for name in ("zlib", "libpng", "blocked-one"):
            self.assertIn(f'href="package/{name}.html"', packages)

    def test_the_catalogue_shows_the_recipe_version(self):
        packages = self.read("packages.html")
        self.assertIn("1.3.2-2", packages)

    def test_descriptions_are_escaped(self):
        # Recipe metadata is not ours and ends up in the page verbatim.
        packages = self.read("packages.html")
        self.assertNotIn("<reference>", packages)
        self.assertIn("&lt;reference&gt;", packages)

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

    def test_a_checksum_is_not_the_artifact_it_checksums(self):
        # The sidecar sorts first often enough that six of nine download
        # cards linked to 65 bytes of text instead of the archive.
        artifacts = generate.classify_artifacts([
            "vitasdk-bootstrap-x86_64-linux-gnu.tar.bz2.sha256",
            "vitasdk-bootstrap-x86_64-linux-gnu.tar.bz2",
            "vitasdk-core-2026.08.1-1-x86_64-linux-gnu.pkg.tar.xz",
            "vdpm-0.1.3-1-x86_64-linux-gnu.pkg.tar.xz",
            "x86_64-linux-gnu.db",
        ])
        self.assertEqual(artifacts["bootstrap"],
                         "vitasdk-bootstrap-x86_64-linux-gnu.tar.bz2")
        self.assertIn("core", artifacts["sdk"])
        self.assertIn("vdpm", artifacts["vdpm"])

    def test_the_build_status_says_whose_core_it_is_building(self):
        # A series sits still until it is patched, so on any channel but the
        # one being built, none of this is the reader's release.
        page = generate.render_status(
            STATUS, "2026.08", {"core": "sdk-snapshot-20260825.611.1"})
        self.assertIn("not the", page)
        self.assertIn("sdk-snapshot-20260825.611.1", page)
        self.assertIn(STATUS["worlds"][0]["core"], page)
        # Staging holds partial results of the rebuild happening now, which
        # is not this series' rebuild.
        self.assertNotIn("[vita-staging]", page)

    def test_the_build_status_of_the_world_being_built_is_its_own(self):
        page = generate.render_status(
            STATUS, "nightly", {"core": STATUS["worlds"][0]["core"]})
        self.assertNotIn("not the", page)
        self.assertIn("[vita-staging]", page)

    def test_one_install_command_per_system_not_nine(self):
        # Nine copies of one line was nine chances to copy somebody else's.
        page = generate.render_downloads(
            "2026.08",
            {"core": "sdk-snapshot-1", "core_repo": "vitasdk/autobuilds",
             "architectures": {"x86_64-linux-gnu": {}, "x86_64-w64-mingw32": {}}},
            None, None)
        # One radio and one panel per host, plus one more for Docker, and the
        # radio sits beside its panel so the CSS that swaps them needs no
        # script and no host names.
        self.assertEqual(page.count('class="pick"'), 3)
        self.assertEqual(page.count('class="panel"'), 3)
        self.assertIn('type="radio" name="host" id="host-x86-64-linux-gnu"', page)
        self.assertIn('id="host-x86-64-linux-gnu" value="x86_64-linux-gnu" checked', page)
        # Windows phrases it differently, and that is the only difference.
        self.assertIn("bootstrap-vitasdk.ps1", page)
        self.assertIn("bootstrap-vitasdk.sh", page)

    def test_the_panel_is_hidden_until_its_radio_is_checked(self):
        style = self.read("style.css")
        self.assertIn(".panel { display: none;", style)
        self.assertIn(".pick:checked + .panel { display: block; }", style)

    def test_downloads_say_what_the_channel_ships(self):
        page = generate.render_downloads(
            "2026.08", {"core": "sdk-snapshot-1", "core_repo": "r",
                        "architectures": {"x86_64-linux-gnu": {}}},
            None, None, summary="What most homebrew is built against.",
            lock={"sources": {"newlib": "64aa7aa33d4f380451a1f100d19589226cdad334",
                              "gcc": "SHA256=438fd996826b0c82485a29da03a72d71d6e3541a",
                              "vdpm": "v0.1.3"}},
            gcc="15.2.0")
        self.assertIn("15.2.0", page)
        self.assertIn("64aa7aa33d4f", page)
        self.assertIn("v0.1.3", page)
        self.assertIn("What most homebrew is built against.", page)
        # A source hash says nothing to a reader, so it is not shown as one.
        self.assertNotIn("SHA256=", page)
        # Pills, not a bare table -- consistent with the cards and copyboxes
        # around it rather than the one plain-lined table on the page.
        self.assertIn('class="pill"', page)
        self.assertNotIn("<table", page)

    def test_vdpm_links_to_its_release_tag(self):
        # Pinned by tag rather than by commit, so a bare vitasdk/vdpm/commit/
        # URL -- the pattern every other component uses -- would 404.
        page = generate.render_downloads(
            "2026.08", {"core": "sdk-snapshot-1", "core_repo": "r",
                        "architectures": {"x86_64-linux-gnu": {}}},
            None, None, lock={"sources": {"vdpm": "v0.1.4"}})
        self.assertIn('href="https://github.com/vitasdk/vdpm/releases/tag/v0.1.4"', page)

    def test_gcc_has_no_link(self):
        # Read off the installed package's file list, not off a vitasdk
        # commit -- there is nothing of ours to point it at.
        page = generate.render_downloads(
            "2026.08", {"core": "sdk-snapshot-1", "core_repo": "r",
                        "architectures": {"x86_64-linux-gnu": {}}},
            None, None, gcc="15.2.0")
        self.assertIn('<span class="pill">gcc <code>15.2.0</code></span>', page)

    def test_downloads_say_how_to_check_the_signature(self):
        page = generate.render_downloads(
            "2026.08", {"core": "c", "core_repo": "r",
                        "architectures": {"x86_64-linux-gnu": {}}}, None, None)
        self.assertIn("openssl pkeyutl -verify", page)
        self.assertIn("channel-public-key.pem", page)
        self.assertIn("2026.08.json.sig", page)
        # Verified by hand against the served manifest and the shipped key.

    def test_the_compiler_version_comes_off_the_file_list(self):
        listing = "%FILES%\nusr/local/vitasdk/lib/gcc/arm-vita-eabi/15.2.0/cc1\n"
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            info = tarfile.TarInfo("vitasdk-core-2026.08.1-1/files")
            info.size = len(listing)
            tar.addfile(info, io.BytesIO(listing.encode()))
        with mock.patch.object(generate, "fetch_bytes", return_value=archive.getvalue()):
            self.assertEqual(
                generate.compiler_version("r", "t", "x86_64-linux-gnu"), "15.2.0")
        with mock.patch.object(generate, "fetch_bytes", return_value=None):
            self.assertEqual(generate.compiler_version("r", "t", "x86_64-linux-gnu"), "")

    def catalogue(self, status_value, core):
        return generate.render_catalogue(
            STATUS, "In 2026.08", None, [], None, 0, "packages.html",
            {"status": status_value, "core": core})

    def test_a_supported_series_does_not_show_somebody_elses_queue(self):
        # A package can sit in a release for a month and read "waiting for
        # dependencies" because a rebuild of the next thing has not reached it.
        page = self.catalogue("supported", "sdk-snapshot-20260825.611.1")
        self.assertNotIn("<th>Status</th>", page)
        self.assertNotIn("<th>Built</th>", page)
        self.assertIn("sdk-snapshot-20260825.611.1", page)
        self.assertNotIn(STATUS["worlds"][0]["core"], page)

    def test_the_series_being_built_shows_its_queue(self):
        page = self.catalogue("development", "whatever")
        self.assertIn("<th>Status</th>", page)
        self.assertIn("<th>Built</th>", page)
        self.assertIn(STATUS["worlds"][0]["core"], page)

    def test_the_recipe_column_says_it_is_the_recipe(self):
        page = self.catalogue("supported", "c")
        self.assertIn("<th>Recipe</th>", page)
        self.assertNotIn("<th>Version</th>", page)
        self.assertIn("what this release actually serves", page)

    def test_the_columns_and_their_widths_stay_in_step(self):
        # table-layout: fixed reads the colgroup, so one column more or less
        # in the header silently shifts every width after it.
        for status_value in ("supported", "development"):
            page = self.catalogue(status_value, "c")
            head = page[page.index("<thead>"):page.index("</thead>")]
            self.assertEqual(page.count("<col "), head.count("<th>"),
                             f"{status_value}: colgroup and header disagree")

    def test_recently_built_is_news_to_one_channel_only(self):
        # A package built two minutes ago, on the page of a release cut last
        # month, reads as that release having just received it.
        live = generate.render_updates_section(STATUS, True)
        self.assertIn("What the autobuilder has produced most recently", live)
        self.assertNotIn("except a patch of it", live)
        supported = generate.render_updates_section(STATUS, False)
        self.assertNotIn("<table>", supported)
        self.assertIn("except a patch of it", supported)
        # The link is a hash, which is what drives the sub-tabs, so it works
        # with the script and jumps to the section without it.
        self.assertIn('href="releases.html#snapshots"', supported)

    def test_every_link_the_site_writes_points_at_a_file_it_wrote(self):
        # Package pages live at the root and a channel page is two levels
        # down, so every one of its 132 catalogue links 404'd in production.
        # Built with channels, because that is where the depth comes from.
        directory = tempfile.mkdtemp()
        series = {"2026.08": {"status": "supported", "summary": "s", "sequence": 1,
                              "core": "sdk-snapshot-1", "core_repo": "vitasdk/autobuilds",
                              "architectures": {"x86_64-linux-gnu": {}},
                              "packages": "packages-snapshot-1", "deprecated": {}},
                  "nightly": {"status": "development", "summary": "n", "sequence": 2,
                              "core": STATUS["worlds"][0]["core"],
                              "core_repo": "vitasdk/autobuilds",
                              "architectures": {"x86_64-linux-gnu": {}},
                              "packages": "packages-snapshot-2", "deprecated": {}}}
        with mock.patch.object(generate, "core_build_info", return_value=(None, None)), \
                mock.patch.object(generate, "core_lock", return_value=None), \
                mock.patch.object(generate, "compiler_version", return_value=""), \
                mock.patch.object(generate, "fetch_database", return_value=None):
            generate.generate(STATUS, directory, series)
        self.assertTrue(os.path.isdir(os.path.join(directory, "channel", "2026.08")))
        broken = []
        for base, _, files in os.walk(directory):
            for name in files:
                if not name.endswith(".html"):
                    continue
                path = os.path.join(base, name)
                with open(path, encoding="utf-8") as handle:
                    body = handle.read()
                for href in re.findall(r'href="([^"#:]+\.(?:html|css|json))(?:#[^"]*)?"', body):
                    target = os.path.normpath(os.path.join(base, href))
                    if not os.path.exists(target):
                        broken.append((os.path.relpath(path, directory), href))
        self.assertEqual(broken, [])

    def test_pages_are_regenerated_from_scratch(self):
        with open(os.path.join(self.directory, "stale.html"), "w", encoding="utf-8") as handle:
            handle.write("old")
        generate.generate(STATUS, self.directory)
        self.assertFalse(os.path.exists(os.path.join(self.directory, "stale.html")))

    def test_every_page_is_theme_aware(self):
        self.assertIn("prefers-color-scheme", self.read("style.css"))

    def test_index_is_the_downloads_page(self):
        self.assertEqual(self.read("index.html"), self.read("downloads.html"))

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

    def test_no_channels_says_so_on_downloads(self):
        status = {"packages": [], "worlds": [], "packages_repo": "",
                  "jobs": [], "cycles": {}}
        with tempfile.TemporaryDirectory() as directory:
            generate.generate(status, directory)
            with open(os.path.join(directory, "downloads.html"), encoding="utf-8") as handle:
                page = handle.read()
        self.assertIn("No release channel is published yet", page)


class TestMissingStatus(unittest.TestCase):

    def test_a_missing_status_file_is_not_a_failure(self):
        # While the autobuilder is being set up there is no status file. That
        # is nothing to render, not a failed render, and the two must not look
        # the same to CI.
        import io
        import contextlib
        import urllib.error

        error = urllib.error.HTTPError("https://example/status.json", 404, "Not Found", {}, None)
        output = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with contextlib.redirect_stdout(output):
                code = generate.main(["generate", "--status", "vitasdk/vitasdk-autobuild"])
        self.assertEqual(code, 2)
        self.assertIn("No status file published yet", output.getvalue())

    def test_a_real_http_error_still_fails(self):
        import urllib.error

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

    def test_the_catalogue_has_a_column_per_world(self):
        packages = self.read("packages.html")
        self.assertIn("<th>vita</th>", packages)
        self.assertIn("<th>vita-musl</th>", packages)

    def test_the_index_says_what_each_world_is_for(self):
        # A bare target name answers nothing. The builder already describes
        # each one, and the catalogue is where somebody chooses between them.
        packages = self.read("packages.html")
        self.assertIn("gcc and newlib", packages)
        self.assertIn("llvm and musl", packages)

    def test_the_counts_say_they_are_not_about_the_selected_release(self):
        # They are the builder's current state, across every release. Read as
        # a description of the release selected above, they would be wrong,
        # and about a target that release may not even serve.
        packages = self.read("packages.html")
        self.assertIn("whichever release is selected", packages)

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

    def test_absolute_reads_out_the_full_stamp(self):
        self.assertEqual(generate.absolute(1_000_000_000), "2001-09-09 01:46 UTC")

    def test_absolute_with_nothing_known(self):
        self.assertEqual(generate.absolute(None), "an unknown time")


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

    def test_the_section_lists_them(self):
        with tempfile.TemporaryDirectory() as directory:
            written = generate.generate(self.status(), directory)
            self.assertIn("packages.html", written)
            with open(os.path.join(directory, "packages.html"), encoding="utf-8") as handle:
                page = handle.read()
        self.assertIn("libpng", page)
        self.assertIn("<th>Downloads</th>", page)
        self.assertIn("Recently built", page)


class TestFiltering(unittest.TestCase):

    def test_rows_carry_what_the_filter_needs(self):
        with tempfile.TemporaryDirectory() as directory:
            generate.generate(TWO_WORLDS, directory)
            with open(os.path.join(directory, "packages.html"), encoding="utf-8") as handle:
                packages = handle.read()
        self.assertIn('data-search=', packages)
        self.assertIn('data-worlds="vita vita-musl"', packages)

    def test_a_target_selector_appears_only_with_more_than_one(self):
        with tempfile.TemporaryDirectory() as directory:
            generate.generate(TWO_WORLDS, directory)
            with open(os.path.join(directory, "packages.html"), encoding="utf-8") as handle:
                many = handle.read()
        with tempfile.TemporaryDirectory() as directory:
            generate.generate(STATUS, directory)
            with open(os.path.join(directory, "packages.html"), encoding="utf-8") as handle:
                one = handle.read()
        self.assertIn('id="world"', many)
        self.assertNotIn('id="world"', one)

    def test_the_description_is_searchable_too(self):
        with tempfile.TemporaryDirectory() as directory:
            generate.generate(STATUS, directory)
            with open(os.path.join(directory, "packages.html"), encoding="utf-8") as handle:
                packages = handle.read()
        self.assertIn("lossless data-compression", packages.lower())

    def test_the_catalogue_gains_a_built_column(self):
        status = dict(STATUS, packages=[
            dict(STATUS["packages"][0],
                 builds={"vita": {"status": "finished", "details": {}, "built_at": 200.0}})])
        with tempfile.TemporaryDirectory() as directory:
            generate.generate(status, directory)
            with open(os.path.join(directory, "packages.html"), encoding="utf-8") as handle:
                packages = handle.read()
        self.assertIn("<th>Built</th>", packages)


class TestSnapshots(unittest.TestCase):
    """The published snapshots are the only history the site can show."""

    def status(self, **extra):
        base = {"generated_at": 1786600000, "worlds": STATUS["worlds"], "packages": [],
                "snapshot_repo": "vitasdk/vitasdk-autobuild"}
        base.update(extra)
        return base

    def test_a_snapshot_links_to_its_release_and_names_its_core(self):
        html = generate.render_snapshots_section(self.status(
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

    def test_the_release_badge_does_not_swallow_the_other_columns(self):
        # The loop that badges a snapshot with the release serving it used to
        # bind over the snapshot it was iterating, so every column read after
        # it came from a series entry instead and the row rendered empty.
        html = generate.render_snapshots_section(
            self.status(
                published_tag="packages-snapshot-20260812.1.1",
                published_snapshots=[{"tag": "packages-snapshot-20260812.1.1",
                                      "published_at": "2026-08-12T18:47:27Z",
                                      "core_snapshot": "sdk-snapshot-20260812.565.1",
                                      "packages_revision": "c3ab29788f379a824f648c8b"}]),
            {"2026.09": {"status": "supported",
                         "packages": "packages-snapshot-20260812.1.1"}})
        self.assertIn(">2026.09<", html)
        self.assertIn("sdk-snapshot-20260812.565.1", html)
        self.assertIn("c3ab297", html)
        self.assertNotIn("unknown", html)

    def test_nothing_published_says_so_instead_of_an_empty_table(self):
        html = generate.render_snapshots_section(self.status(published_snapshots=[]))
        self.assertIn("Nothing has been published yet", html)
        self.assertNotIn("<table>", html)

    def test_an_older_status_file_does_not_claim_nothing_is_published(self):
        html = generate.render_snapshots_section(self.status())
        self.assertNotIn("Nothing has been published", html)
        self.assertIn("before the site listed", html)

    def test_an_unreadable_date_does_not_hide_the_snapshot(self):
        html = generate.render_snapshots_section(self.status(
            published_snapshots=[{"tag": "packages-snapshot-1",
                                  "published_at": "whenever"}]))
        self.assertIn("packages-snapshot-1", html)
        self.assertIn("whenever", html)

    def test_the_catalogue_names_the_channel_it_shows(self):
        # A single column now, named after the channel selected in the header,
        # not one column per release.
        status = self.status(published_tag="packages-snapshot-20260812.1.1",
                             packages=[{"name": "zlib", "version": "1.3.2-2",
                                        "builds": {"vita": {"status": "finished",
                                                            "details": {}}}}])
        series = {"2026.09": {"status": "supported", "summary": "", "core": "",
                              "packages": "snap-1"}}
        html = generate.render_packages(
            status, "2026.09", series["2026.09"], [], series,
            {"snap-1": {"zlib": {"version": "1.3.1-1", "description": ""}}}, depth=2)
        self.assertIn("In 2026.09", html)
        self.assertIn("1.3.1-1", html)

    def test_an_unattributed_snapshot_still_says_something_useful(self):
        status = self.status(published_tag="packages-snapshot-20260812.1.1", packages=[])
        html = generate.render_packages(status, None, None, [], {}, {}, depth=0)
        self.assertIn("In the last snapshot", html)
        self.assertIn('href="releases.html#snapshots"', html)

    def test_without_a_snapshot_the_column_keeps_its_generic_name(self):
        status = self.status(packages=[])
        html = generate.render_packages(status, None, None, [], {}, {}, depth=0)
        self.assertIn("In repository", html)

    def test_the_snapshots_view_is_generated(self):
        with tempfile.TemporaryDirectory() as output:
            written = generate.generate(self.status(), output)
            self.assertIn("packages.html", written)
            with open(os.path.join(output, "packages.html"), encoding="utf-8") as handle:
                self.assertIn("Recently built", handle.read())


class TestDeprecation(unittest.TestCase):
    """A deprecated package is marked where somebody would pick it."""

    def status(self, deprecated):
        return {"generated_at": 1786600000, "worlds": STATUS["worlds"],
                "packages_repo": "vitasdk/packages", "published_snapshots": [],
                "packages": [{"name": "cpython", "version": "2.7-1",
                              "description": "Python 2", "deprecated": deprecated,
                              "builds": {"vita": {"status": "finished", "details": {}}}}]}

    def test_the_catalogue_marks_it(self):
        html = generate.render_catalogue(self.status("Python 2 is unsupported; use cpython3"),
                                         "In repository", None, [], None, 0, "packages.html")
        self.assertIn(">deprecated<", html)
        self.assertIn("use cpython3", html)

    def test_the_package_page_leads_with_it(self):
        status = self.status("Python 2 is unsupported; use cpython3")
        html = generate.render_package(status["packages"][0], status)
        self.assertIn("Deprecated.", html)
        self.assertLess(html.index("Deprecated."), html.index("Published"))

    def test_a_normal_package_is_not_marked(self):
        html = generate.render_catalogue(self.status(""), "In repository", None, [], None, 0,
                                         "packages.html")
        self.assertNotIn("deprecated", html)


class TestReleases(unittest.TestCase):
    """The catalogue has to describe what a person can ask for."""

    SERIES = {
        "2026.09": {"status": "supported", "summary": "First stable release",
                    "sequence": 4, "core": "sdk-snapshot-20260812.568.1",
                    "core_repo": "vitasdk/autobuilds", "architectures": {},
                    "packages": "packages-snapshot-20260813.2.1", "deprecated": {}},
        "nightly": {"status": "development", "summary": "Rebuilt continuously",
                    "sequence": 41, "core": "sdk-snapshot-20260812.568.1",
                    "core_repo": "vitasdk/autobuilds", "architectures": {},
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
        html = generate.render_releases_section(self.status(), self.SERIES)
        self.assertIn("2026.09", html)
        self.assertIn("Supported", html)
        self.assertIn("packages-snapshot-20260813.2.1", html)
        self.assertIn("VITASDK_CHANNEL=2026.09", html)

    def test_no_published_series_is_not_an_error(self):
        html = generate.render_releases_section(self.status(), {})
        self.assertIn("No release series are published yet", html)

    def test_a_snapshot_a_release_serves_is_never_missing(self):
        # The status file can be older than the snapshot a release points at.
        status = self.status()
        status["published_snapshots"] = []
        html = generate.render_snapshots_section(status, self.SERIES)
        self.assertIn("packages-snapshot-20260813.2.1", html)
        self.assertIn(">2026.09<", html)

    def test_a_snapshot_says_which_release_serves_it(self):
        # Being the newest and being the one people install are different
        # facts, and only the second matters to a reader.
        html = generate.render_snapshots_section(self.status(), self.SERIES)
        self.assertIn(">2026.09<", html)

    def test_the_channel_pages_are_generated_and_linked(self):
        with tempfile.TemporaryDirectory() as output:
            written = generate.generate(self.status(), output, self.SERIES)
            self.assertIn(os.path.join("channel", "nightly", "packages.html"), written)
            self.assertIn(os.path.join("channel", "2026.09", "packages.html"), written)
            with open(os.path.join(output, "index.html"), encoding="utf-8") as handle:
                index = handle.read()
        # nightly is "development", so it is the default the site root shows.
        self.assertIn('href="channel/nightly/downloads.html"', index)

    def test_a_release_gets_a_column_even_if_the_status_never_listed_it(self):
        # The status file is written per series, so the snapshot a release
        # serves is missing from it whenever another series produced the file.
        # Reading only that list left the catalogue saying "In nightly" and
        # nothing about the release people actually install.
        status = self.status()
        status["published_snapshots"] = []
        status["snapshot_repo"] = "vitasdk/vitasdk-autobuild"
        original = generate.fetch_database
        generate.fetch_database = lambda repo, tag, name: (
            {"zlib": {"version": "1.3.2-2", "description": ""}} if tag else None)
        try:
            with tempfile.TemporaryDirectory() as output:
                generate.generate(status, output, self.SERIES)
                with open(os.path.join(output, "channel", "2026.09", "packages.html"),
                         encoding="utf-8") as handle:
                    packages = handle.read()
                snapshot_pages = os.listdir(os.path.join(output, "snapshot"))
        finally:
            generate.fetch_database = original
        self.assertIn("In 2026.09", packages)
        self.assertIn("packages-snapshot-20260813.2.1.html", snapshot_pages)

    def test_a_channels_world_is_read_from_its_manifest(self):
        # Missing from series would send Docker links for every softfp
        # channel to the default world's repository.
        def respond(url, timeout=None):
            if url.endswith("index.json"):
                return io.BytesIO(
                    b'{"channels":{"nightly-softfp":{"status":"development"}}}')
            return io.BytesIO(b'{"world":"vita_softfp"}')

        with mock.patch("urllib.request.urlopen", respond):
            series = generate.load_channels("https://example.invalid/channels")
        self.assertEqual(series["nightly-softfp"]["world"], "vita_softfp")

    def test_a_channel_without_a_world_field_defaults_to_vita(self):
        def respond(url, timeout=None):
            if url.endswith("index.json"):
                return io.BytesIO(
                    b'{"channels":{"nightly":{"status":"development"}}}')
            return io.BytesIO(b'{}')

        with mock.patch("urllib.request.urlopen", respond):
            series = generate.load_channels("https://example.invalid/channels")
        self.assertEqual(series["nightly"]["world"], "vita")

    def test_unreachable_channels_do_not_stop_the_build(self):
        # No index is no series yet, which is how a site looks before the
        # first release: nothing to describe is not a broken deployment.
        with mock.patch.object(generate, "RETRY_BACKOFF", 0):
            self.assertEqual(generate.load_channels("https://127.0.0.1:9/channels"), {})

    def test_a_blip_is_retried_before_it_is_believed(self):
        calls = []

        def flaky(url, timeout=None):
            calls.append(url)
            if len(calls) < 3:
                raise urllib.error.URLError("reset by peer")
            return io.BytesIO(b'{"channels":{}}')

        with mock.patch.object(generate, "RETRY_BACKOFF", 0):
            with mock.patch("urllib.request.urlopen", flaky):
                self.assertEqual(generate.fetch_json("https://example.invalid/i.json"),
                                 {"channels": {}})
        self.assertEqual(len(calls), 3)

    def test_a_channel_the_index_names_and_cannot_be_read_stops_the_build(self):
        # Rendering it as empty is what put "No release channel is published
        # yet" on a live page whose channel was published and fine.
        def index_only(url, timeout=None):
            if url.endswith("index.json"):
                return io.BytesIO(
                    b'{"channels":{"2026.08":{"status":"supported"}}}')
            raise urllib.error.URLError("reset by peer")

        with mock.patch.object(generate, "RETRY_BACKOFF", 0):
            with mock.patch("urllib.request.urlopen", index_only):
                with self.assertRaises(generate.ChannelUnreadable):
                    generate.load_channels("https://example.invalid/channels")

    def test_a_channel_that_is_really_gone_is_not_retried(self):
        # A 404 is an answer, not a failure to get one, so asking again can
        # only give the same one more slowly.
        calls = []

        def missing(url, timeout=None):
            calls.append(url)
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        with mock.patch.object(generate, "RETRY_BACKOFF", 0):
            with mock.patch("urllib.request.urlopen", missing):
                self.assertIsNone(
                    generate.fetch_json("https://example.invalid/gone.json"))
        self.assertEqual(len(calls), 1, "a 404 was asked for more than once")


class TestSnapshotBrowsing(unittest.TestCase):
    """What a published snapshot contains is a different question from what
    is being built, and only the snapshot can answer it."""

    def database(self, entries):
        import io, tarfile
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, version, desc in entries:
                body = (f"%NAME%\n{name}\n\n%VERSION%\n{version}\n\n"
                        f"%DESC%\n{desc}\n").encode()
                info = tarfile.TarInfo(f"{name}-{version}/desc")
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
        return buffer.getvalue()

    def test_a_database_is_read_the_way_pacman_wrote_it(self):
        data = self.database([("zlib", "1.3.2-2", "A compression library")])
        self.assertEqual(generate.read_database(data),
                         {"zlib": {"version": "1.3.2-2",
                                   "description": "A compression library"}})

    def test_the_selector_offers_both_questions(self):
        html = generate.view_selector("building", [{"tag": "packages-snapshot-1"}])
        self.assertIn("Building now", html)
        self.assertIn("packages-snapshot-1", html)
        self.assertIn("selected", html)

    def test_a_snapshot_reads_as_the_release_it_belongs_to(self):
        # Nobody knows what packages-snapshot-20260813.2.1 is; what they know
        # is which release they are on. A release is a toolchain, so the
        # provenance is what attributes a snapshot to one.
        series = {"2026.09": {"core": "core-1", "packages": "snap-2"}}
        current = {"tag": "snap-2", "core_snapshot": "core-1",
                   "published_at": "2026-08-13T07:30:57Z"}
        older = {"tag": "snap-1", "core_snapshot": "core-1",
                 "published_at": "2026-08-13T01:00:00Z"}
        foreign = {"tag": "snap-0", "core_snapshot": "core-0",
                   "published_at": "2026-08-12T01:00:00Z"}
        self.assertIn("2026.09", generate.snapshot_label(current, series))
        self.assertIn("current", generate.snapshot_label(current, series))
        self.assertIn("2026.09", generate.snapshot_label(older, series))
        self.assertNotIn("current", generate.snapshot_label(older, series))
        self.assertIn("earlier toolchain", generate.snapshot_label(foreign, series))

    def test_the_tag_stays_reachable_because_it_is_what_reproduces_a_build(self):
        html = generate.view_selector(
            "building", [{"tag": "snap-2", "core_snapshot": "core-1"}],
            series={"2026.09": {"core": "core-1", "packages": "snap-2"}})
        self.assertIn('title="snap-2"', html)

    def test_no_snapshots_means_no_selector_at_all(self):
        self.assertEqual(generate.view_selector("building", []), "")

    def test_a_snapshot_page_lists_what_is_in_it(self):
        entry = {"tag": "packages-snapshot-1", "core_snapshot": "core-1"}
        contents = {"zlib": {"version": "1.3.2-2", "description": "Compression"}}
        html = generate.render_snapshot(entry, contents, {"generated_at": 1786600000},
                                        [entry], {"2026.09": {"packages": "packages-snapshot-1"}})
        self.assertIn("zlib", html)
        self.assertIn("1.3.2-2", html)
        self.assertIn("core-1", html)
        self.assertIn("2026.09", html)

    def test_an_unreadable_snapshot_costs_one_page_not_the_site(self):
        self.assertIsNone(generate.fetch_database("vitasdk/nope", "nope", "vita"))


class TestHostHelpers(unittest.TestCase):
    """Downloads reads real host triples and an opaque artifact list."""

    def test_host_label_reads_the_triple(self):
        self.assertEqual(generate.host_label("x86_64-linux-gnu"), "Linux x86_64")
        self.assertEqual(generate.host_label("x86_64-linux-musl"), "Linux x86_64 (musl)")
        self.assertEqual(generate.host_label("aarch64-linux-gnu"), "Linux aarch64")
        self.assertEqual(generate.host_label("arm64-apple-darwin"), "macOS arm64")
        self.assertEqual(generate.host_label("x86_64-w64-mingw32"), "Windows x86_64")
        self.assertEqual(generate.host_label("x86_64-unknown-freebsd"), "FreeBSD x86_64")

    def test_an_unrecognised_triple_falls_back_to_itself(self):
        self.assertEqual(generate.host_label("riscv64-unknown-plan9"),
                         "riscv64-unknown-plan9")

    def test_classify_artifacts_finds_the_three_kinds(self):
        found = generate.classify_artifacts([
            "vitasdk-core-0.1-1-x86_64-linux-gnu.pkg.tar.xz",
            "vdpm-0.1.0-1-x86_64-linux-gnu.pkg.tar.xz",
            "x86_64-linux-gnu.db",
            "x86_64-linux-gnu.files",
            "vitasdk-bootstrap-x86_64-linux-gnu.tar.bz2",
        ])
        self.assertEqual(found["bootstrap"], "vitasdk-bootstrap-x86_64-linux-gnu.tar.bz2")
        self.assertEqual(found["vdpm"], "vdpm-0.1.0-1-x86_64-linux-gnu.pkg.tar.xz")
        self.assertEqual(found["sdk"], "vitasdk-core-0.1-1-x86_64-linux-gnu.pkg.tar.xz")

    def test_classify_artifacts_skips_pacman_database_files(self):
        found = generate.classify_artifacts(["x86_64-linux-gnu.db", "x86_64-linux-gnu.files"])
        self.assertEqual(found, {})

    def test_classify_artifacts_on_an_empty_list(self):
        self.assertEqual(generate.classify_artifacts([]), {})


class TestDownloads(unittest.TestCase):
    """Every published host, with what to run and what to fetch directly."""

    ITEM = {"core": "sdk-snapshot-1", "core_repo": "vitasdk/autobuilds",
           "architectures": {"x86_64-linux-gnu": {}, "x86_64-w64-mingw32": {}}}

    def test_no_channel_means_nothing_to_download(self):
        html = generate.render_downloads(None, None, None, None)
        self.assertIn("No release channel is published yet", html)

    def test_a_host_absent_from_the_manifest_never_appears(self):
        # There is no data source naming hosts nobody has built for yet;
        # "coming soon" placeholders would have to be invented.
        html = generate.render_downloads("nightly", self.ITEM, None, None)
        self.assertIn("x86_64-linux-gnu", html)
        self.assertIn("x86_64-w64-mingw32", html)
        self.assertNotIn("coming soon", html)

    def test_a_published_host_links_its_classified_artifacts(self):
        manifest = {"hosts": [
            {"name": "x86_64-linux-gnu", "build_id": "b", "artifacts": [
                "vitasdk-bootstrap-x86_64-linux-gnu.tar.bz2",
                "vitasdk-core-1-x86_64-linux-gnu.pkg.tar.xz",
                "vdpm-1-x86_64-linux-gnu.pkg.tar.xz"]},
        ]}
        html = generate.render_downloads("nightly", self.ITEM, manifest, 1_700_000_000)
        self.assertIn("vitasdk-bootstrap-x86_64-linux-gnu.tar.bz2", html)
        self.assertIn("vdpm-1-x86_64-linux-gnu.pkg.tar.xz", html)
        self.assertIn("Built ", html)

    def test_the_docker_panel_shows_the_same_build_date_as_the_hosts(self):
        # It ships exactly what the core panels do, from the same build.
        html = generate.render_downloads("nightly", self.ITEM, None, 1_700_000_000)
        self.assertEqual(html.count("Built "), len(self.ITEM["architectures"]) + 1)

    def test_an_old_snapshot_without_release_json_still_shows_the_host(self):
        # release.json only exists on snapshots published from now on; an
        # older one degrades to a bare link and no build date, not a crash.
        html = generate.render_downloads("nightly", self.ITEM, None, None)
        self.assertIn("published", html)
        self.assertNotIn("Built ", html)
        self.assertIn("releases/tag/sdk-snapshot-1", html)

    def test_windows_gets_the_powershell_bootstrap(self):
        html = generate.render_downloads("nightly", self.ITEM, None, None)
        self.assertIn("bootstrap-vitasdk.ps1", html)
        self.assertIn("bootstrap-vitasdk.sh", html)

    def test_the_install_command_names_the_channel(self):
        html = generate.render_downloads("2026.09", self.ITEM, None, None)
        self.assertIn("VITASDK_CHANNEL=2026.09", html)

    def test_docker_defaults_to_the_vita_repository(self):
        html = generate.render_downloads("nightly", self.ITEM, None, None)
        self.assertIn("vitasdk/vitasdk:nightly", html)
        self.assertNotIn("vitasdk-softfp", html)

    def test_docker_softfp_uses_its_own_repository_and_drops_the_suffix(self):
        item = {**self.ITEM, "world": "vita_softfp"}
        html = generate.render_downloads("nightly-softfp", item, None, None)
        self.assertIn("vitasdk/vitasdk-softfp:nightly", html)
        self.assertNotIn("vitasdk-softfp:nightly-softfp", html)

    def test_docker_is_a_card_in_the_same_picker(self):
        html = generate.render_downloads("nightly", self.ITEM, None, None)
        self.assertIn('id="host-docker" value="docker"', html)
        self.assertIn('data-for="docker"', html)

    def test_docker_offers_all_four_variants(self):
        html = generate.render_downloads("nightly", self.ITEM, None, None)
        for tag in ("vitasdk/vitasdk:nightly bash", "vitasdk/vitasdk:nightly-non-root bash",
                   "vitasdk/vitasdk:nightly-minimal bash",
                   "vitasdk/vitasdk:nightly-minimal-non-root bash"):
            self.assertIn(tag, html)

    def test_every_command_is_copyable(self):
        # Bootstrap (one per host -- ITEM has two), the four Docker variants,
        # the sample build, and the signature check -- copy_block wraps them.
        html = generate.render_downloads("nightly", self.ITEM, None, None)
        self.assertEqual(html.count('class="copybox"'), 2 + 4 + 1 + 1)
        self.assertEqual(html.count('class="copy"'), html.count('class="copybox"'))


class TestDockerRepositoryAndTag(unittest.TestCase):
    """Which Docker Hub repository and tag a channel's world resolves to."""

    def test_default_world(self):
        self.assertEqual(generate.docker_repository_and_tag("nightly", "vita"),
                         ("vitasdk/vitasdk", "nightly"))

    def test_softfp_drops_its_channel_suffix(self):
        self.assertEqual(
            generate.docker_repository_and_tag("nightly-softfp", "vita_softfp"),
            ("vitasdk/vitasdk-softfp", "nightly"))

    def test_an_unknown_world_falls_back_to_vita(self):
        self.assertEqual(generate.docker_repository_and_tag("nightly", "made-up"),
                         ("vitasdk/vitasdk", "nightly"))


class TestChrome(unittest.TestCase):
    """The header a reader sees on every page: channel, tabs, context band."""

    SERIES_ONE = {"nightly": {"status": "development"}}
    SERIES_TWO = {"nightly": {"status": "development"}, "2026.09": {"status": "supported"}}

    def test_a_single_channel_shows_no_picker(self):
        html = generate.chrome("", self.SERIES_ONE, "nightly", "Downloads", "")
        self.assertNotIn("channel-pill", html)

    def test_more_than_one_channel_shows_a_picker(self):
        html = generate.chrome("", self.SERIES_TWO, "nightly", "Downloads", "")
        self.assertIn("channel-pill", html)
        self.assertIn(">nightly<", html)
        self.assertIn(">2026.09<", html)

    def test_the_active_tab_is_marked(self):
        html = generate.chrome("", self.SERIES_ONE, "nightly", "Packages", "")
        self.assertIn('class="tab current" href="channel/nightly/packages.html"', html)

    def test_world_selector_never_appears_with_one_world(self):
        # Mirrors worlds_of: a second world (e.g. vita-softfp) would need a
        # selector of its own, but nothing here invents one that has no data.
        html = generate.chrome("", self.SERIES_ONE, "nightly", "Downloads", "")
        self.assertNotIn('id="world"', html)

    def test_core_band_names_the_snapshot_and_date(self):
        band = generate.core_band("nightly", {"core": "sdk-snapshot-1"}, 1_700_000_000)
        self.assertIn("nightly", band)
        self.assertIn("sdk-snapshot-1", band)
        self.assertIn("2023-11-14", band)

    def test_core_band_without_a_date_says_nothing_about_it(self):
        band = generate.core_band("nightly", {"core": "sdk-snapshot-1"}, None)
        self.assertIn("sdk-snapshot-1", band)
        self.assertNotIn("built", band)

    def test_a_bare_link_lands_on_the_newest_supported_series(self):
        # What the installer picks when nobody names a channel, so that a
        # command copied from the landing page installs what it says.
        series = {
            "2026.02": {"status": "deprecated"},
            "2026.08": {"status": "supported"},
            "2026.05": {"status": "supported"},
            "nightly": {"status": "development"},
        }
        self.assertEqual(generate.default_channel_of(series), "2026.08")

    def test_a_bare_link_falls_back_when_nothing_is_supported(self):
        series = {"nightly": {"status": "development"}}
        self.assertEqual(generate.default_channel_of(series), "nightly")

    def test_the_install_command_is_one_line_off_the_release(self):
        snippet = generate.bootstrap_snippet("2026.08", windows=False)
        self.assertIn("releases/latest/download/bootstrap-vitasdk.sh", snippet)
        self.assertIn("VITASDK_CHANNEL=2026.08 bash", snippet)
        self.assertEqual(snippet.count("\n"), 0)

    def test_the_windows_command_saves_the_script_before_running_it(self):
        # Piping into iex would drop the parameters the installer takes.
        snippet = generate.bootstrap_snippet("2026.08", windows=True)
        self.assertIn("bootstrap-vitasdk.ps1", snippet)
        self.assertNotIn("iex", snippet)

    def test_packages_band_finds_the_publish_date(self):
        band = generate.packages_band(
            "nightly", {"packages": "snap-1"},
            [{"tag": "snap-1", "published_at": "2026-08-12T18:47:27Z"}])
        self.assertIn("snap-1", band)
        self.assertIn("2026-08-12", band)


if __name__ == "__main__":
    unittest.main()
