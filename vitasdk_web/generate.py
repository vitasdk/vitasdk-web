"""Generates the package catalogue as plain files.

The catalogue is read-only, so it does not need to be a service. Everything
here turns one status.json, published by vitasdk-autobuild, into a directory
that GitHub Pages can serve: no hosting, no credentials, no operations.
"""

import argparse
import calendar
import email.utils
import html
import io
import json
import os
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from typing import Any

STATUS_URL = ("https://github.com/{repo}/releases/download/status/status.json")

STATUS_LABELS = {
    "finished": ("Built", "ok"),
    "finished-but-blocked": ("Built, held back", "hold"),
    "failed-to-build": ("Failed", "bad"),
    "waiting-for-build": ("Queued", "wait"),
    "waiting-for-dependencies": ("Waiting for dependencies", "wait"),
    "manual-build-required": ("Manual build", "hold"),
    "unknown": ("Unknown", "wait"),
}

STATUS_HELP = {
    "finished-but-blocked": (
        "The package is built. It is held back because something it links "
        "against, or something that links against it, has not been rebuilt "
        "yet, and publishing it alone would leave the repository "
        "inconsistent."),
    "waiting-for-dependencies": (
        "Nothing is wrong: the packages it links against are still being "
        "built."),
}


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


class StatusNotPublished(Exception):
    """The autobuilder has not published a status file yet.

    Not an error while the autobuilder is being set up: there is simply
    nothing to render, which is different from failing to render it.
    """


def load_status(source: str) -> dict[str, Any]:
    if os.path.exists(source):
        with open(source, encoding="utf-8") as handle:
            return json.load(handle)
    url = source if source.startswith("http") else STATUS_URL.format(repo=source)
    print(f"Fetching {url}", flush=True)
    try:
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed scheme
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise StatusNotPublished(url) from None
        raise SystemExit(f"ERROR: cannot read {url}: HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise SystemExit(f"ERROR: cannot reach {url}: {e.reason}") from None


def ago(seconds: float | None, now: float | None = None) -> str:
    """How long ago something happened, in words."""

    if not seconds:
        return "unknown"
    delta = max(0, int((now if now is not None else time.time()) - seconds))
    if delta < 90:
        return "just now"
    for size, unit in ((60, "minute"), (3600, "hour"), (86400, "day")):
        count = delta // size
        if count < (60 if unit == "minute" else 48 if unit == "hour" else 30):
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "on " + time.strftime("%Y-%m-%d", time.gmtime(seconds))


def parse_iso(iso: str) -> float | None:
    """A GitHub timestamp as seconds since epoch, or None if it will not parse."""

    try:
        return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def dash_if_unknown(phrase: str) -> str:
    """A table says nothing with a dash, the way its other columns do."""

    return "—" if phrase == "unknown" else phrase


def when(iso: str, now: float | None = None) -> str:
    """A GitHub timestamp in the same words as everything else on the site."""

    if not iso:
        return "unknown"
    stamp = parse_iso(iso)
    # Better to show the raw value than to hide a snapshot over a format.
    return ago(stamp, now) if stamp is not None else iso


def absolute(epoch: float | None) -> str:
    """A timestamp written out in full, for the context band."""

    if not epoch:
        return "an unknown time"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch))


CHANNELS_URL = "https://vitasdk.github.io/channels"


def load_channels(base: str) -> dict[str, Any]:
    """The release series and what each of them currently serves.

    Read from where the client reads it, because that is the only thing that
    decides what anybody installs; the catalogue describes what was built,
    which is not the same question.

    Absent is not an error. Until an index is published there are no series to
    describe, and a catalogue that refuses to build for that reason would be
    worse than one that says nothing.
    """

    index = fetch_json(f"{base}/index.json")
    if not index:
        return {}
    series = {}
    for name, entry in sorted(index.get("channels", {}).items()):
        manifest = fetch_json(f"{base}/{name}.json") or {}
        core = manifest.get("core") or {}
        series[name] = {
            "status": entry.get("status", "unknown"),
            "summary": entry.get("summary", ""),
            "sequence": manifest.get("sequence"),
            "core": core.get("release", ""),
            "core_repo": core.get("repository", ""),
            "architectures": core.get("architectures", {}),
            "packages": (manifest.get("packages") or {}).get("release", ""),
            "deprecated": (manifest.get("packages") or {}).get("deprecated", {}),
        }
    return series


def fetch_json(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
            return json.loads(response.read().decode())
    except (urllib.error.URLError, ValueError, TimeoutError):
        return None


def core_build_info(repo: str, tag: str) -> tuple[dict[str, Any] | None, float | None]:
    """A core snapshot's per-host artifact manifest, and when it was published.

    Snapshots published before release.json existed do not have one: that is
    not an error, it is a fact about the snapshot's age. When it is missing
    the caller has no per-host artifacts and no build date, and has to say so
    rather than invent either.
    """

    if not repo or not tag:
        return None, None
    url = f"https://github.com/{repo}/releases/download/{tag}/release.json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
            manifest = json.loads(response.read().decode())
            stamp = response.headers.get("Last-Modified")
    except (urllib.error.URLError, ValueError, TimeoutError):
        return None, None
    built_at = None
    if stamp:
        try:
            built_at = email.utils.parsedate_to_datetime(stamp).timestamp()
        except (TypeError, ValueError, OverflowError):
            built_at = None
    return manifest, built_at


def classify_artifacts(names: list[str]) -> dict[str, str]:
    """Which artifact is the bootstrap archive, the SDK package and the vdpm bundle.

    The manifest declares an opaque file list; the name is the only thing
    that says what each entry is for.
    """

    found: dict[str, str] = {}
    for name in names:
        lower = name.lower()
        if lower.endswith((".db", ".files")):
            continue
        if "bootstrap" in lower:
            found.setdefault("bootstrap", name)
        elif "vdpm" in lower:
            found.setdefault("vdpm", name)
        elif "core" in lower:
            found.setdefault("sdk", name)
    return found


def host_label(triple: str) -> str:
    """A name a person recognises, read out of the triple itself."""

    arch, _, rest = triple.partition("-")
    if "mingw" in rest:
        return f"Windows {arch}"
    if "darwin" in rest:
        return f"macOS {arch}"
    if "freebsd" in rest:
        return f"FreeBSD {arch}"
    if "linux" in rest:
        return f"Linux {arch}" + (" (musl)" if "musl" in rest else "")
    return triple


INSTALLER_URL = ("https://github.com/vitasdk/vdpm/releases/latest/download/"
                 "bootstrap-vitasdk")


def bootstrap_snippet(channel: str | None, windows: bool) -> str:
    """One command to paste, from the release the client publishes.

    Windows downloads the script before running it rather than piping into
    iex: the installer takes parameters, and a piped script cannot.
    """

    if windows:
        set_channel = f'$env:VITASDK_CHANNEL="{esc(channel)}"; ' if channel else ""
        return (f"irm {INSTALLER_URL}.ps1 -OutFile bootstrap-vitasdk.ps1\n"
                f"{set_channel}.\\bootstrap-vitasdk.ps1")
    set_channel = f"VITASDK_CHANNEL={esc(channel)} " if channel else ""
    return f"curl -fsSL {INSTALLER_URL}.sh | {set_channel}bash"


def read_database(data: bytes) -> dict[str, dict[str, str]]:
    """What a published repository contains, from its own pacman database.

    The catalogue describes what is being built now; a snapshot is a
    different question — what was in it — and the only thing that can answer
    it is the snapshot itself.
    """

    entries: dict[str, dict[str, str]] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive:
            if not member.isfile() or os.path.basename(member.name) != "desc":
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            fields: dict[str, str] = {}
            key = ""
            for raw in handle.read().decode("utf-8", "replace").splitlines():
                line = raw.strip()
                if line.startswith("%") and line.endswith("%"):
                    key = line.strip("%")
                elif line and key:
                    fields.setdefault(key, line)
            if "NAME" in fields and "VERSION" in fields:
                entries[fields["NAME"]] = {
                    "version": fields["VERSION"],
                    "description": fields.get("DESC", ""),
                }
    return entries


def fetch_database(repo: str, tag: str, name: str) -> dict[str, dict[str, str]] | None:
    """One snapshot's package list. Missing is not fatal: it is one page."""

    url = f"https://github.com/{repo}/releases/download/{tag}/{name}.db"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
            return read_database(response.read())
    except (urllib.error.URLError, tarfile.TarError, ValueError, TimeoutError):
        print(f"::warning::cannot read the database of {tag}", flush=True)
        return None


def worlds_of(status: dict[str, Any]) -> list[dict[str, Any]]:
    """The targets the catalogue is built for, newest schema or older one."""

    if status.get("worlds"):
        return list(status["worlds"])
    # A status file written before worlds existed describes a single one.
    return [{"arch": "vita", "core": status.get("core_snapshot", ""),
             "repository": "vita", "staging_repository": "vita-staging",
             "description": ""}]


def builds_of(package: dict[str, Any], worlds: list[dict[str, Any]]) -> dict[str, Any]:
    if "builds" in package:
        return package["builds"]
    return {worlds[0]["arch"]: {"status": package.get("status", "unknown"),
                                "details": package.get("details", {})}}


def counts(packages: list[dict[str, Any]], world: str,
           worlds: list[dict[str, Any]] | None = None) -> dict[str, int]:
    worlds = worlds or [{"arch": world}]
    result: dict[str, int] = {}
    for package in packages:
        build = builds_of(package, worlds).get(world)
        if build is None:
            continue
        result[build["status"]] = result.get(build["status"], 0) + 1
    return result


def latest_build_time(builds: dict[str, Any]) -> float | None:
    stamps = [build.get("built_at") for build in builds.values()
              if isinstance(build, dict) and build.get("built_at")]
    return max(stamps) if stamps else None


NAV_TABS = ("Downloads", "Packages", "Build status")
TAB_FILES = {"Downloads": "downloads.html", "Packages": "packages.html",
             "Build status": "status.html"}


def chrome(root: str, series: dict[str, Any], current: str | None, active: str,
          band: str) -> str:
    """The header and context band shared by every page.

    The channel a reader picked follows them across tabs: a pill links to the
    same tab in another channel, not back to Downloads. A world selector
    would sit here too, but there is only one world today, so it stays out
    until a second one exists (see worlds_of).
    """

    filename = TAB_FILES[active]
    pills = ""
    if len(series) > 1:
        items = "".join(
            f'<a class="channel-pill{" current" if name == current else ""}" '
            f'href="{root}channel/{esc(name)}/{filename}">{esc(name)}</a>'
            for name in sorted(series))
        pills = f'<div class="channels">{items}</div>'

    def tab_href(tab: str) -> str:
        if current:
            return f"{root}channel/{esc(current)}/{TAB_FILES[tab]}"
        return f"{root}{TAB_FILES[tab]}"

    tabs = "".join(
        f'<a class="tab{" current" if tab == active else ""}" href="{tab_href(tab)}">'
        f'{esc(tab)}</a>' for tab in NAV_TABS)

    return f"""<header>
  <a class="brand" href="{root}index.html">VitaSDK</a>
  {pills}
  <nav class="tabs">
    {tabs}
    <a class="tab api" href="{root}api/status.json">API</a>
  </nav>
</header>
{band}"""


def core_band(name: str | None, item: dict[str, Any] | None, built_at: float | None) -> str:
    if not name or not item:
        return ('<div class="band">No release channel is published yet; nothing to '
                'download.</div>')
    tag = item.get("core") or "unrecorded"
    # A core published before the manifest carried a build time says nothing
    # rather than saying it was built at an unknown time.
    built = f", built {esc(absolute(built_at))}" if built_at else ""
    return (f'<div class="band">Showing the <strong>{esc(name)}</strong> channel '
            f'&mdash; core <code>{esc(tag)}</code>{built}.</div>')


def packages_band(name: str | None, item: dict[str, Any] | None,
                  snapshots: list[dict[str, Any]]) -> str:
    if not name or not item:
        return ('<div class="band">Showing what the autobuilder is doing right now; no '
                'release channel is published yet.</div>')
    tag = item.get("packages") or "unrecorded"
    published_at = next((entry.get("published_at", "") for entry in snapshots
                         if entry.get("tag") == tag), "")
    stamp = parse_iso(published_at)
    built = f", built {esc(absolute(stamp))}" if stamp else ""
    return (f'<div class="band">Showing the <strong>{esc(name)}</strong> channel '
            f'&mdash; packages <code>{esc(tag)}</code>{built}.</div>')


def status_badge(status: str) -> str:
    label, kind = STATUS_LABELS.get(status, (status, "wait"))
    return f'<span class="badge {kind}">{esc(label)}</span>'


def page(title: str, *, body: str, chrome: str, depth: int = 0,
        generated_at: float | None = None) -> str:
    root = "../" * depth
    if generated_at:
        generated = (f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(generated_at))} "
                     f"({ago(generated_at)})")
    else:
        generated = "an unknown time ago"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="{root}style.css">
</head>
<body>
{chrome}
<main>
{body}
</main>
<footer>Status published {esc(generated)}.</footer>
</body>
</html>
"""


def render_downloads(name: str | None, item: dict[str, Any] | None,
                     manifest: dict[str, Any] | None, built_at: float | None) -> str:
    """Every host a channel's core is published for, and how to install it.

    A host missing from the manifest's architecture list is not yet built for
    this channel; there is no data source that also names hosts nobody has
    built for yet, so this only ever lists what is real.
    """

    architectures = (item or {}).get("architectures") or {}
    if not architectures:
        return """
<h1>Downloads</h1>
<p class="lede">No release channel is published yet. Once one is, this page
lists the systems it builds for and how to install each.</p>
"""

    repo = item.get("core_repo", "")
    tag = item.get("core", "")
    by_host = {entry["name"]: entry.get("artifacts", [])
              for entry in (manifest or {}).get("hosts", [])
              if isinstance(entry, dict) and entry.get("name")}
    built = absolute(built_at) if built_at else ""

    cards = []
    for host in sorted(architectures):
        windows = "mingw" in host
        artifacts = classify_artifacts(by_host.get(host, []))
        if artifacts:
            base = f"https://github.com/{repo}/releases/download/{tag}/"
            links = " ".join(
                f'<a href="{esc(base + artifacts[key])}">{label}</a>'
                for key, label in (("bootstrap", "bootstrap"), ("sdk", "sdk"),
                                   ("vdpm", "vdpm"))
                if key in artifacts)
        elif repo and tag:
            links = (f'<a href="https://github.com/{esc(repo)}/releases/tag/{esc(tag)}">'
                     f'release page</a>')
        else:
            links = ""
        built_html = f'<p class="built">Built {esc(built)}</p>' if built else ""
        cards.append(f"""<div class="host" data-host="{esc(host)}">
  <h3>{esc(host_label(host))}</h3>
  <p class="desc">{esc(host)}</p>
  <p><span class="badge ok">published</span></p>
  {built_html}
  <pre><code>{bootstrap_snippet(name, windows)}</code></pre>
  <p class="links">{links}</p>
</div>""")

    return f"""
<h1>Downloads</h1>
<p class="lede">Pick a system below. Every card runs the same installer; only
the channel differs.</p>
<div class="hosts">{"".join(cards)}</div>
<script>
(function () {{
  var ua = navigator.userAgent;
  var suffix = null;
  if (/Windows/.test(ua)) suffix = 'w64-mingw32';
  else if (/Mac OS X|Macintosh/.test(ua)) suffix = 'apple-darwin';
  else if (/FreeBSD/.test(ua)) suffix = 'freebsd';
  else if (/Linux/.test(ua)) suffix = /musl/i.test(ua) ? 'linux-musl' : 'linux-gnu';
  if (!suffix) return;
  var cards = document.querySelectorAll('.host[data-host]');
  for (var i = 0; i < cards.length; i++) {{
    if (cards[i].dataset.host.endsWith(suffix)) {{ cards[i].classList.add('mine'); break; }}
  }}
}})();
</script>
"""


def view_selector(current: str, snapshots: list[dict[str, Any]], depth: int = 0,
                  series: dict[str, Any] | None = None, home: str = "index.html") -> str:
    """Lets a reader ask the other question: not what is being built, but what
    a published snapshot contains."""

    if not snapshots:
        return ""
    root = "../" * depth
    options = f'<option value="{root}{home}"'
    options += ' selected' if current == "building" else ''
    options += '>Building now</option>'
    for entry in snapshots:
        tag = entry.get("tag", "")
        if not tag:
            continue
        # The tag stays reachable as the title, because it is the thing that
        # makes a build reproducible; it is just not what a person reads.
        options += f'<option value="{root}snapshot/{esc(tag)}.html" title="{esc(tag)}"'
        options += ' selected' if current == tag else ''
        options += f'>{esc(snapshot_label(entry, series))}</option>'
    return (f'<label class="view">Showing '
            f'<select id="view" aria-label="Which repository to show">{options}</select>'
            f'</label>'
            f'<script>document.getElementById("view").addEventListener("change", '
            f'function (event) {{ location.href = event.target.value; }});</script>')


def render_catalogue(status: dict[str, Any], label: str, column: dict[str, str] | None,
                     available: list[dict[str, Any]], series: dict[str, Any] | None,
                     depth: int, home: str) -> str:
    packages = status["packages"]
    worlds = worlds_of(status)
    snapshot_selector = view_selector("building", available, depth=depth, series=series,
                                      home=home)

    summaries = []
    for world in worlds:
        tally = counts(packages, world["arch"], worlds)
        line = " ".join(
            f'<span class="tally">{tally.get(key, 0)} {esc(STATUS_LABELS[key][0].lower())}</span>'
            for key in ("finished", "finished-but-blocked", "waiting-for-build",
                        "waiting-for-dependencies", "failed-to-build")
            if tally.get(key))
        world_label = f'<span class="world">{esc(world["arch"])}</span> ' if len(worlds) > 1 else ""
        summaries.append(f"<p class=\"summary\">{world_label}{line}</p>")

    headers = "".join(f"<th>{esc(w['arch'])}</th>" for w in worlds) if len(worlds) > 1 \
        else "<th>Status</th>"

    rows = []
    for package in packages:
        if column is not None:
            published = f'<td class="version">{esc(column.get(package["name"]) or "—")}</td>'
        else:
            published = f'<td class="version">{esc(package.get("repo_version") or "—")}</td>'
        builds = builds_of(package, worlds)
        cells = ""
        for world in worlds:
            build = builds.get(world["arch"])
            cells += f"<td>{status_badge(build['status']) if build else '<span class=\"absent\">&mdash;</span>'}</td>"
        built = latest_build_time(builds)
        built_cell = f'<td class="k">{esc(ago(built))}</td>' if built else \
            '<td class="absent">&mdash;</td>'
        # Marked where people go looking for something to use, which is the
        # only place a deprecation changes anybody's mind.
        deprecated = package.get("deprecated", "")
        name_cell = f'<a href="package/{esc(package["name"])}.html">{esc(package["name"])}</a>'
        if deprecated:
            name_cell += (f' <span class="badge stale" title="{esc(deprecated)}">'
                          f'deprecated</span>')
        haystack = f'{package["name"]} {package.get("description", "")}'.lower()
        rows.append(
            f'<tr data-name="{esc(package["name"])}" data-search="{esc(haystack)}" '
            f'data-worlds="{esc(" ".join(builds))}">'
            f'<td>{name_cell}</td>'
            f'<td class="version">{esc(package["version"])}</td>'
            f'{published}'
            f'{cells}'
            f'{built_cell}'
            f'<td class="desc">{esc(package.get("description", ""))}</td>'
            f'</tr>')

    if column is not None:
        in_repository = f"<th>{esc(label)}</th>"
    elif label:
        in_repository = f'<th><a href="#snapshots">{esc(label)}</a></th>'
    else:
        in_repository = "<th>Published</th>"

    built_against = ", ".join(
        f'<code>{esc(w["core"])}</code>' + (f' ({esc(w["arch"])})' if len(worlds) > 1 else "")
        for w in worlds)

    if len(worlds) > 1:
        options = "".join(f'<option value="{esc(w["arch"])}">{esc(w["arch"])}</option>'
                          for w in worlds)
        world_filter = (f'<select id="world" aria-label="Target"><option value="">'
                        f'All targets</option>{options}</select>')
    else:
        world_filter = ""

    return f"""
<h2>Catalogue</h2>
<p class="lede">{len(packages)} packages built against {built_against}
from <a href="https://github.com/{esc(status.get("packages_repo", ""))}">{esc(status.get("packages_repo", ""))}</a>.</p>
{"".join(summaries)}
<div class="controls">
  <input id="filter" type="search" placeholder="Filter by name or description" autocomplete="off">
  {world_filter}
  {snapshot_selector}
  <span id="count" class="count"></span>
</div>
<table id="packages">
<thead><tr><th>Package</th><th>Version</th>{in_repository}{headers}<th>Built</th><th>Description</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
<p id="empty" class="empty" hidden>No package matches that.</p>
<script>
const filter = document.getElementById('filter');
const world = document.getElementById('world');
const count = document.getElementById('count');
const empty = document.getElementById('empty');
const rows = Array.from(document.querySelectorAll('#packages tbody tr'));

function apply() {{
  const needle = filter.value.trim().toLowerCase();
  const target = world ? world.value : '';
  let shown = 0;
  for (const row of rows) {{
    const matchesText = needle === '' || row.dataset.search.includes(needle);
    const matchesWorld = target === '' || row.dataset.worlds.split(' ').includes(target);
    row.hidden = !(matchesText && matchesWorld);
    if (!row.hidden) shown++;
  }}
  count.textContent = shown + ' of ' + rows.length;
  empty.hidden = shown !== 0;
}}

filter.addEventListener('input', apply);
if (world) world.addEventListener('change', apply);
apply();
</script>
"""


def render_package(package: dict[str, Any], status: dict[str, Any],
                   series: dict[str, Any] | None = None, default_channel: str | None = None,
                   snapshots: list[dict[str, Any]] | None = None) -> str:
    def links(names: list[str]) -> str:
        if not names:
            return "<p>None.</p>"
        items = " ".join(
            f'<a class="pill" href="{esc(name)}.html">{esc(name)}</a>' for name in names)
        return f"<p>{items}</p>"

    worlds = worlds_of(status)
    builds = builds_of(package, worlds)

    notes = []
    rows = []
    for world in worlds:
        build = builds.get(world["arch"])
        if build is None:
            rows.append(f'<tr><td class="k">{esc(world["arch"])}</td>'
                        f'<td><span class="absent">not built for this target</span></td></tr>')
            continue
        rows.append(f'<tr><td class="k">{esc(world["arch"])}</td>'
                    f'<td>{status_badge(build["status"])} '
                    f'<span class="desc">{esc(world.get("description", ""))}</span></td></tr>')
        details = build.get("details") or {}
        prefix = f'{esc(world["arch"])}: ' if len(worlds) > 1 else ""
        if details.get("desc"):
            notes.append(f"<p class=\"note\">{prefix}{esc(details['desc'])}</p>")
        if build["status"] in STATUS_HELP:
            notes.append(f"<p class=\"note\">{prefix}{esc(STATUS_HELP[build['status']])}</p>")
        for label, url in (details.get("urls") or {}).items():
            notes.append(f'<p class="note">{prefix}build log: '
                         f'<a href="{esc(url)}">{esc(label)}</a></p>')
    world_rows = "".join(rows)

    homepage = ""
    if package.get("url"):
        homepage = f'<p>Upstream: <a href="{esc(package["url"])}">{esc(package["url"])}</a></p>'

    binaries = ", ".join(esc(name) for name in package.get("binaries", []))
    licenses = ", ".join(esc(name) for name in package.get("licenses", [])) or "&mdash;"

    packages_href = (f'../channel/{esc(default_channel)}/packages.html' if default_channel
                     else '../packages.html')

    # Above everything else, because it changes whether the rest is worth
    # reading. It says do not start something new on this, not that it is
    # gone: what already depends on it keeps working.
    # The same rule as everywhere else: say the release, keep the tag within
    # reach for whoever needs to reproduce something.
    published_in = ""
    if package.get("repo_version") and status.get("published_tag"):
        tag = status["published_tag"]
        name = next((n for n, item in sorted((series or {}).items())
                     if item.get("packages") == tag), "")
        published_in = (f' &middot; <a href="{packages_href}#releases" title="{esc(tag)}">'
                        f'{esc(name)}</a>' if name else
                        f' &middot; <a href="{packages_href}#snapshots">{esc(tag)}</a>')

    notice = ""
    if package.get("deprecated"):
        notice = (f'<p class="deprecated"><strong>Deprecated.</strong> '
                  f'{esc(package["deprecated"])}</p>')

    return page(f"{package['name']} - VitaSDK packages", generated_at=status.get("generated_at"),
               depth=1, chrome=chrome("../", series or {}, default_channel, "Packages",
                                      packages_band(default_channel,
                                                    (series or {}).get(default_channel or ""),
                                                    snapshots or [])),
               body=f"""
<h1>{esc(package['name'])}</h1>
<p class="lede">{esc(package.get('description', ''))}</p>
{notice}
<table class="facts">
<tr><th>Recipe version</th><td>{esc(package['version'])}</td></tr>
<tr><th>Published</th><td>{esc(package.get('repo_version') or '—')}{published_in}</td></tr>
<tr><th>Provides</th><td>{binaries}</td></tr>
<tr><th>Licence</th><td>{licenses}</td></tr>
</table>
<h2>Targets</h2>
<table class="facts">{world_rows}</table>
{''.join(notes)}
{homepage}
<h2>Depends on</h2>
{links(package.get('depends', []))}
<h2>Needed by</h2>
{links(package.get('rdepends', []))}
<p><a href="https://github.com/{esc(status.get('packages_repo', ''))}/blob/HEAD/{esc(package['name'])}/VITABUILD">Recipe</a></p>
""")


def recently_built(status: dict[str, Any], limit: int = 60) -> list[dict[str, Any]]:
    """Packages by build time, newest first, across every world."""

    worlds = worlds_of(status)
    entries = []
    for package in status["packages"]:
        for world in worlds:
            build = builds_of(package, worlds).get(world["arch"]) or {}
            if build.get("built_at"):
                entries.append({"package": package, "world": world["arch"],
                                "built_at": build["built_at"],
                                "downloads": build.get("downloads", 0)})
    entries.sort(key=lambda entry: entry["built_at"], reverse=True)
    return entries[:limit]


def render_updates_section(status: dict[str, Any]) -> str:
    entries = recently_built(status)
    if not entries:
        body = "<p>Nothing has been built yet.</p>"
    else:
        rows = "".join(
            f'<tr><td><a href="package/{esc(e["package"]["name"])}.html">'
            f'{esc(e["package"]["name"])}</a></td>'
            f'<td class="version">{esc(e["package"]["version"])}</td>'
            f'<td class="k">{esc(e["world"])}</td>'
            f'<td>{esc(ago(e["built_at"]))}</td>'
            f'<td class="version">{e["downloads"]}</td></tr>' for e in entries)
        body = (f"<table><thead><tr><th>Package</th><th>Version</th><th>Target</th>"
                f"<th>Built</th><th>Downloads</th></tr></thead><tbody>{rows}</tbody></table>")

    return f"""
<h2>Recently built</h2>
<p class="lede">What the autobuilder has produced most recently. A package only
reappears here when its recipe changes, or when something it links against was
rebuilt after it.</p>
{body}
"""


def snapshots_of(status: dict[str, Any],
                 series: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every snapshot worth showing: what was published, plus what a live
    release serves.

    A snapshot cut after the last status file was written is not in that list,
    and neither is one belonging to another series, because the status file is
    written per series. A release pointing at a snapshot the site knows nothing
    about is exactly the thing a reader cannot make sense of.
    """

    snapshots = list(status.get("published_snapshots") or [])
    known = {entry.get("tag") for entry in snapshots}
    for name, item in sorted((series or {}).items()):
        tag = item.get("packages")
        if tag and tag not in known:
            snapshots.insert(0, {"tag": tag, "published_at": "",
                                 "core_snapshot": item.get("core", "")})
            known.add(tag)
    return snapshots


def render_snapshots_section(status: dict[str, Any], series: dict[str, Any] | None = None) -> str:
    """The published snapshots, which are the only history there is.

    Nothing is archived to produce this: the releases themselves are immutable
    and each one carries the core it was built against, so the list is read
    back from what was published.
    """

    snapshots = snapshots_of(status, series)
    repo = status.get("snapshot_repo", "")
    current = status.get("published_tag", "")

    if "published_snapshots" not in status:
        # An older status file cannot say whether anything is published, and
        # claiming nothing is would be a lie the first time it happens.
        body = ("<p>This status file was written before the site listed "
                "snapshots. The list appears after the next build.</p>")
    elif not snapshots:
        body = ("<p>Nothing has been published yet. Packages that are built sit "
                "in the staging repository until a snapshot is cut.</p>")
    else:
        rows = ""
        for entry in snapshots:
            tag = entry.get("tag", "")
            link = (f'<a href="https://github.com/{esc(repo)}/releases/tag/{esc(tag)}">'
                    f'{esc(tag)}</a>') if repo else esc(tag)
            mark = ' <span class="badge ok">current</span>' if tag == current else ""
            # Being the newest snapshot and being the one people install are
            # different facts, and only the second one matters to a reader.
            for name, item in (series or {}).items():
                if item.get("packages") == tag:
                    mark += f' <span class="badge wait">{esc(name)}</span>'
            revision = entry.get("packages_revision", "")
            rows += (f'<tr><td>{link}{mark}</td>'
                     f'<td>{esc(dash_if_unknown(when(entry.get("published_at", ""))))}</td>'
                     f'<td class="k">{esc(entry.get("core_snapshot", "") or "—")}</td>'
                     f'<td class="k">{esc(revision[:7] if revision else "—")}</td></tr>')
        body = (f"<table><thead><tr><th>Snapshot</th><th>Published</th>"
                f"<th>Built against</th><th>Recipes</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>")

    return f"""
<h2 id="snapshots">Published snapshots</h2>
<p class="lede">Each one is an immutable pacman repository. They are what a
channel points at, so an installation can be reproduced exactly by naming one:
nothing in a published snapshot ever changes, and a newer core means a new
snapshot rather than an edit to an old one.</p>
{body}
"""


SERIES_LABELS = {
    "supported": ("Supported", "ok"),
    "development": ("Development", "wait"),
    "deprecated": ("Deprecated", "hold"),
    "end-of-life": ("Ended", "bad"),
}


def snapshot_label(entry: dict[str, Any], series: dict[str, Any] | None = None) -> str:
    """A snapshot named the way somebody would say it out loud.

    Nobody knows what packages-snapshot-20260813.2.1 is. What they know is
    which release they are on, and a release is a toolchain: a snapshot
    belongs to the release whose core its provenance records. One built
    against a toolchain that no release names cannot be attributed, and
    saying so is better than guessing.
    """

    published = when(entry.get("published_at", ""))
    dated = "" if published == "unknown" else f" — {published}"
    tag = entry.get("tag", "")
    for name, item in sorted((series or {}).items()):
        if item.get("packages") == tag:
            return f"{name}{dated} (current)"
    for name, item in sorted((series or {}).items()):
        if item.get("core") and item["core"] == entry.get("core_snapshot"):
            return f"{name}{dated}"
    return f"{published} — earlier toolchain" if dated else "earlier toolchain"


def render_snapshot(entry: dict[str, Any], contents: dict[str, dict[str, str]],
                    status: dict[str, Any], snapshots: list[dict[str, Any]],
                    series: dict[str, Any] | None = None,
                    default_channel: str | None = None) -> str:
    """What one published snapshot contains, read from the snapshot itself."""

    tag = entry.get("tag", "")
    serving = [name for name, item in (series or {}).items()
               if item.get("packages") == tag]
    rows = "".join(
        f'<tr><td>{esc(name)}</td><td class="version">{esc(item["version"])}</td>'
        f'<td class="desc">{esc(item["description"])}</td></tr>'
        for name, item in sorted(contents.items()))
    served_by = ""
    if serving:
        served_by = ("<p>Served by " + ", ".join(f"<code>{esc(n)}</code>" for n in serving)
                     + ". Installing from that release installs exactly this.</p>")

    home = (f"channel/{esc(default_channel)}/packages.html" if default_channel
           else "packages.html")

    return page(f"{snapshot_label(entry, series)} - VitaSDK packages",
                generated_at=status.get("generated_at"),
                depth=1,
                chrome=chrome("../", series or {}, default_channel, "Packages",
                              packages_band(default_channel,
                                            (series or {}).get(default_channel or ""),
                                            snapshots)),
                body=f"""
<h1>{esc(snapshot_label(entry, series))}</h1>
<p class="eyebrow"><code>{esc(tag)}</code></p>
<p class="lede">{len(contents)} packages, built against
<code>{esc(entry.get("core_snapshot", "an unrecorded toolchain"))}</code>. This
list is read from the snapshot's own database, so it is what you get, not what
was intended.</p>
{served_by}
<div class="controls">{view_selector(tag, snapshots, depth=1, series=series, home=home)}</div>
<div class="scroll"><table>
<thead><tr><th>Package</th><th>Version</th><th>Description</th></tr></thead>
<tbody>{rows}</tbody></table></div>
""")


def render_releases_section(status: dict[str, Any],
                            series: dict[str, Any] | None = None) -> str:
    """What a person can actually ask for, which is a release and not a tag."""

    series = series or {}
    if not series:
        body = ("<p>No release series are published yet. Until then the "
                "catalogue describes what has been built rather than what "
                "can be installed.</p>")
    else:
        rows = ""
        for name, entry in series.items():
            label, kind = SERIES_LABELS.get(entry["status"], (entry["status"], "wait"))
            packages = entry["packages"]
            link = (f'<a href="#snapshots">{esc(packages)}</a>'
                    if packages else "&mdash;")
            rows += (f'<tr><td><code>{esc(name)}</code></td>'
                     f'<td><span class="badge {kind}">{esc(label)}</span></td>'
                     f'<td>{link}</td>'
                     f'<td class="k">{esc(entry["core"] or "—")}</td>'
                     f'<td class="desc">{esc(entry["summary"])}</td></tr>')
        body = (f'<div class="scroll"><table><thead><tr><th>Release</th>'
                f'<th>Status</th><th>Packages</th><th>Toolchain</th>'
                f'<th></th></tr></thead><tbody>{rows}</tbody></table></div>')

    first = next(iter(series), "nightly")
    return f"""
<h2 id="releases">Releases</h2>
<p class="lede">A release fixes the toolchain — the compiler, newlib and the
Vita headers — and packages keep improving inside it. It is what you name when
you install, and the only thing that changes it is asking for another one.</p>
{body}
<h3>Installing one</h3>
<pre><code>git clone https://github.com/vitasdk/vdpm &amp;&amp; cd vdpm
VITASDK_CHANNEL={esc(first)} ./bootstrap-vitasdk.sh</code></pre>
<p>Afterwards, <code>vdpm status</code> says which release an installation
follows, and <code>vdpm channels</code> lists these same series from the
client.</p>
"""


def render_status(status: dict[str, Any]) -> str:
    packages = status["packages"]
    worlds = worlds_of(status)

    rows = ""
    for world in worlds:
        tally = counts(packages, world["arch"], worlds)
        if len(worlds) > 1:
            rows += (f'<tr><th colspan="2">{esc(world["arch"])} '
                     f'<span class="desc">{esc(world.get("description", ""))}</span></th></tr>')
        rows += "".join(
            f"<tr><td>{status_badge(key)}</td><td>{tally.get(key, 0)}</td></tr>"
            for key in STATUS_LABELS if tally.get(key))

    jobs = status.get("jobs") or []
    if jobs:
        job_rows = "".join(
            f'<tr><td><a href="{esc(job["html_url"])}">{esc(job["name"])}</a></td>'
            f'<td>{esc(job.get("started_at", ""))}</td></tr>' for job in jobs)
        job_table = (f"<table><thead><tr><th>Worker</th><th>Started</th></tr></thead>"
                     f"<tbody>{job_rows}</tbody></table>")
    else:
        job_table = "<p>No workers are building right now.</p>"

    failed_rows = ""
    for package in packages:
        for world in worlds:
            build = builds_of(package, worlds).get(world["arch"])
            if not build or build["status"] != "failed-to-build":
                continue
            details = build.get("details") or {}
            logs = " ".join(
                f'<a href="{esc(url)}">{esc(label)}</a>'
                for label, url in (details.get("urls") or {}).items()) or "&mdash;"
            failed_rows += (
                f'<tr><td><a href="package/{esc(package["name"])}.html">{esc(package["name"])}</a></td>'
                f'<td class="k">{esc(world["arch"])}</td>'
                f'<td class="version">{esc(package["version"])}</td>'
                f'<td>{logs}</td>'
                f'<td>{esc(details.get("desc") or "")}</td></tr>')
    failed_table = (f"<table><thead><tr><th>Package</th><th>Target</th><th>Version</th>"
                    f"<th>Log</th><th>Detail</th></tr></thead><tbody>{failed_rows}</tbody></table>"
                    ) if failed_rows else "<p>Nothing is failing.</p>"

    raw_cycles = status.get("cycles") or []
    pairs = []
    if isinstance(raw_cycles, dict):
        for arch, entries in raw_cycles.items():
            pairs.extend((f"{a} ({arch})", b) for a, b in entries)
    else:
        pairs.extend(raw_cycles)
    cycle_list = ("<ul>" + "".join(
        f"<li>{esc(a)} &harr; {esc(b)}</li>" for a, b in pairs) + "</ul>") if pairs else ""

    repo = esc(status.get("autobuild_repo", "vitasdk/vitasdk-autobuild"))
    blocks = "".join(
        f"""<pre><code>[{esc(world.get('staging_repository', 'vita-staging'))}]
SigLevel = Never
Server = https://github.com/{repo}/releases/download/staging</code></pre>"""
        for world in worlds)
    staging = f"""
<h2>Staging repository</h2>
<p>Packages marked <em>built</em> and <em>built, held back</em> are also available from a
staging pacman repository. It can contain partial results of a rebuild, so packages
in it may be broken from time to time. To use it, add this above the other repositories
in <code>$VITASDK/etc/pacman.conf</code>:</p>
{blocks}
"""

    return f"""
<h1>Build status</h1>
<p class="lede">{" · ".join(f"{esc(w['arch'])} on <code>{esc(w['core'])}</code>" for w in worlds)},
recipes at <code>{esc((status.get('packages_revision') or '')[:12])}</code>.</p>
<table>{rows}</table>
<h2>Workers</h2>
{job_table}
<h2>Failures</h2>
{failed_table}
{"<h2>Dependency cycles</h2>" + cycle_list if pairs else ""}
{staging}
"""


def render_packages(status: dict[str, Any], name: str | None, item: dict[str, Any] | None,
                    available: list[dict[str, Any]], series: dict[str, Any] | None,
                    contents_by_tag: dict[str, dict[str, dict[str, str]]], depth: int) -> str:
    """The four existing views, now sub-tabs of one page instead of four files."""

    tag = (item or {}).get("packages", "")
    contents = contents_by_tag.get(tag) if tag else None
    # Reduced from the database's name -> {version, description} to name -> version.
    column = ({n: entry["version"] for n, entry in contents.items()}
             if contents is not None else None)
    if name:
        label = f"In {name}"
    elif status.get("published_tag"):
        label = "In the last snapshot"
    else:
        label = "In repository"

    home = f"channel/{name}/packages.html" if name else "packages.html"
    catalogue = render_catalogue(status, label, column, available, series, depth, home)
    updates = render_updates_section(status)
    releases = render_releases_section(status, series)
    snapshots = render_snapshots_section(status, series)

    return f"""
<h1>Packages</h1>
<div class="subtabs">
  <button class="subtab" data-view="catalogue">Catalogue</button>
  <button class="subtab" data-view="updates">Recently built</button>
  <button class="subtab" data-view="releases">Releases</button>
  <button class="subtab" data-view="snapshots">Snapshots</button>
</div>
<section class="subview" data-view="catalogue">{catalogue}</section>
<section class="subview" data-view="updates">{updates}</section>
<section class="subview" data-view="releases">{releases}</section>
<section class="subview" data-view="snapshots">{snapshots}</section>
<script>
const subtabs = document.querySelectorAll('.subtab');
const views = document.querySelectorAll('.subview');
function show(name) {{
  for (const view of views) view.hidden = view.dataset.view !== name;
  for (const tab of subtabs) tab.classList.toggle('current', tab.dataset.view === name);
}}
for (const tab of subtabs) {{
  tab.addEventListener('click', () => {{ location.hash = tab.dataset.view; }});
}}
window.addEventListener('hashchange', () => show((location.hash || '#catalogue').slice(1)));
show((location.hash || '#catalogue').slice(1));
</script>
"""


STYLE = """\
:root { color-scheme: light dark; --fg: #14161a; --bg: #ffffff; --muted: #5c6470;
        --line: #d9dee5; --accent: #1f6feb; --ok: #1a7f37; --bad: #c93c37;
        --hold: #9a6700; --wait: #57606a; --chip: #f2f4f7; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e6edf3; --bg: #0d1117; --muted: #9198a1; --line: #2a313c;
          --accent: #4493f8; --ok: #3fb950; --bad: #f85149; --hold: #d29922;
          --wait: #8b949e; --chip: #161b22; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.55
       ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
header { display: flex; gap: 1.5rem; align-items: center; flex-wrap: wrap;
         padding: 1rem 1.5rem; border-bottom: 1px solid var(--line); }
.brand { font-weight: 700; text-decoration: none; color: var(--fg); }
.channels { display: flex; gap: .35rem; flex-wrap: wrap; }
.channel-pill { padding: .3rem .8rem; border: 1px solid var(--line); border-radius: 6px;
        color: var(--muted); text-decoration: none; font-size: 13px; }
.channel-pill.current { background: var(--accent); border-color: var(--accent); color: #fff;
                font-weight: 600; }
.tabs { display: flex; gap: 1.2rem; align-items: center; margin-left: auto; }
.tab { color: var(--muted); text-decoration: none; padding-bottom: .2rem; }
.tab.current { color: var(--fg); font-weight: 600; border-bottom: 2px solid var(--accent); }
.tab.api { font-size: 13px; margin-left: .4rem; }
nav a:hover, a:hover { color: var(--accent); }
.band { padding: .5rem 1.5rem; background: var(--chip); border-bottom: 1px solid var(--line);
        color: var(--muted); font-size: 13px; }
.band strong { color: var(--fg); }
main { max-width: 68rem; margin: 0 auto; padding: 1.5rem; }
footer { max-width: 68rem; margin: 0 auto; padding: 1.5rem; color: var(--muted);
         border-top: 1px solid var(--line); font-size: 13px; }
h1 { font-size: 1.6rem; margin: 0 0 .4rem; }
h2 { font-size: 1.1rem; margin: 1.8rem 0 .5rem; }
.lede { color: var(--muted); margin: 0 0 1rem; }
a { color: var(--accent); }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
pre { background: var(--chip); padding: .8rem 1rem; border-radius: 6px; overflow-x: auto; }
.controls { display: flex; gap: .6rem; align-items: center; margin: .5rem 0 1rem;
            flex-wrap: wrap; }
input[type=search] { flex: 1 1 16rem; padding: .55rem .7rem;
       border: 1px solid var(--line); border-radius: 6px; background: var(--bg);
       color: var(--fg); font-size: 15px; }
select { padding: .55rem .7rem; border: 1px solid var(--line); border-radius: 6px;
         background: var(--bg); color: var(--fg); font-size: 15px; }
.count { color: var(--muted); font-size: 13px; white-space: nowrap; }
.empty { color: var(--muted); }
.view { color: var(--muted); font-size: 0.9rem; }
.view select { margin-left: 0.35rem; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
table { width: 100%; border-collapse: collapse; margin-bottom: 1rem; display: block;
        overflow-x: auto; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 13px; }
.version { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px;
           white-space: nowrap; }
.desc { color: var(--muted); }
.badge { font-size: 12px; padding: .1rem .5rem; border-radius: 999px;
         background: var(--chip); white-space: nowrap; }
.badge.ok { color: var(--ok); } .badge.bad { color: var(--bad); }
.badge.hold { color: var(--hold); } .badge.wait { color: var(--wait); }
.badge.stale { color: var(--hold); border-color: var(--hold); }
.deprecated { border-left: 3px solid var(--hold); padding: 0.6rem 0.9rem;
  margin: 0 0 1rem; background: color-mix(in srgb, var(--hold) 8%, transparent); }
.tally { margin-right: 1rem; color: var(--muted); font-size: 13px; }
.world { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
         margin-right: .6rem; color: var(--fg); }
.absent { color: var(--muted); font-size: 12px; }
.pill { display: inline-block; padding: .1rem .5rem; margin: 0 .3rem .3rem 0;
        background: var(--chip); border-radius: 999px; text-decoration: none; font-size: 13px; }
.note { background: var(--chip); padding: .6rem .8rem; border-radius: 6px; }
.facts th { width: 12rem; }
.subtabs { display: flex; gap: .5rem; margin-bottom: 1.2rem; flex-wrap: wrap; }
.subtab { padding: .35rem .9rem; border: 1px solid var(--line); border-radius: 6px;
          color: var(--muted); background: var(--bg); font: inherit; cursor: pointer; }
.subtab.current { border-color: var(--accent); color: var(--accent); font-weight: 600; }
.subview[hidden] { display: none; }
.hosts { display: grid; grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr));
         gap: .9rem; margin-top: 1rem; }
.host { border: 1px solid var(--line); border-radius: 8px; padding: .9rem 1.1rem; }
.host.mine { grid-column: 1 / -1; border-color: var(--accent); order: -1;
             background: color-mix(in srgb, var(--accent) 6%, var(--bg)); }
.host h3 { margin: 0 0 .1rem; font-size: 1rem; }
.host .built { color: var(--muted); font-size: 12px; margin: .4rem 0; }
.host .links { display: flex; gap: .9rem; font-size: 13px; margin-top: .6rem; }
"""


def default_channel_of(series: dict[str, Any]) -> str | None:
    """Which channel a bare link (a package page, the site root) points at.

    The newest supported series, which is what the installer itself picks
    when nobody names one: a visitor who copies a command from here has to
    end up with what that command actually installs.
    """

    if not series:
        return None
    supported = [name for name, item in sorted(series.items(), reverse=True)
                 if item.get("status") == "supported"]
    return supported[0] if supported else sorted(series)[0]


def generate(status: dict[str, Any], output_dir: str,
             series: dict[str, Any] | None = None) -> list[str]:
    series = series or {}
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(os.path.join(output_dir, "package"), exist_ok=True)
    os.makedirs(os.path.join(output_dir, "api"), exist_ok=True)

    written = []

    def write(relative: str, content: str) -> None:
        path = os.path.join(output_dir, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        written.append(relative)

    # The same list the snapshots view shows, so a release that serves a
    # snapshot gets its database read and can have a column of its own.
    snapshots = snapshots_of(status, series)
    repository = worlds_of(status)[0].get("repository", "vita")
    store = status.get("snapshot_repo", "")

    # One page per snapshot, read from its own database. Bounded by what the
    # status file lists, and a snapshot that cannot be read costs one page
    # rather than the whole site.
    listed = []
    if store:
        os.makedirs(os.path.join(output_dir, "snapshot"), exist_ok=True)
        for entry in snapshots:
            contents = fetch_database(store, entry.get("tag", ""), repository)
            if contents:
                listed.append((entry, contents))

    available = [entry for entry, _ in listed]
    contents_by_tag = {entry["tag"]: contents for entry, contents in listed}

    write("style.css", STYLE)
    write("api/status.json", json.dumps(status, indent=2) + "\n")

    default_name = default_channel_of(series)
    # Fetched once per channel so every page that shows it shares the result.
    core_info = {name: core_build_info(item.get("core_repo", ""), item.get("core", ""))
                for name, item in series.items()}

    def emit(prefix: str, depth: int, name: str | None, item: dict[str, Any] | None) -> None:
        manifest, built_at = core_info.get(name, (None, None))
        downloads_chrome = chrome("../" * depth, series, name, "Downloads",
                                  core_band(name, item, built_at))
        write(os.path.join(prefix, "downloads.html"),
             page("Downloads - VitaSDK", body=render_downloads(name, item, manifest, built_at),
                  chrome=downloads_chrome, depth=depth,
                  generated_at=status.get("generated_at")))

        packages_chrome = chrome("../" * depth, series, name, "Packages",
                                 packages_band(name, item, snapshots))
        write(os.path.join(prefix, "packages.html"),
             page("Packages - VitaSDK", depth=depth,
                  body=render_packages(status, name, item, available, series,
                                       contents_by_tag, depth),
                  chrome=packages_chrome, generated_at=status.get("generated_at")))

        status_chrome = chrome("../" * depth, series, name, "Build status",
                               packages_band(name, item, snapshots))
        write(os.path.join(prefix, "status.html"),
             page("Build status - VitaSDK", depth=depth, body=render_status(status),
                  chrome=status_chrome, generated_at=status.get("generated_at")))

    if series:
        for name, item in series.items():
            emit(os.path.join("channel", name), 2, name, item)
        default_item = series.get(default_name) if default_name else None
        emit("", 0, default_name, default_item)
    else:
        emit("", 0, None, None)
    # The root copy of Downloads is the site's home page.
    if os.path.exists(os.path.join(output_dir, "downloads.html")):
        shutil.copyfile(os.path.join(output_dir, "downloads.html"),
                        os.path.join(output_dir, "index.html"))
        written.append("index.html")

    for entry, contents in listed:
        write(os.path.join("snapshot", f"{entry['tag']}.html"),
              render_snapshot(entry, contents, status, available, series, default_name))
    for package in status["packages"]:
        write(os.path.join("package", f"{package['name']}.html"),
              render_package(package, status, series, default_name, snapshots))
    return written


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Generate the VitaSDK package catalogue")
    parser.add_argument("--status", default="vitasdk/vitasdk-autobuild",
                        help="path to status.json, a URL, or the autobuild repository")
    parser.add_argument("--output", default="_site", help="directory to write")
    parser.add_argument("--channels", default=CHANNELS_URL,
                        help="where the release series are published")
    args = parser.parse_args(argv[1:])

    try:
        status = load_status(args.status)
    except StatusNotPublished as e:
        # Exit code 2 says "nothing published yet" so that setting the site up
        # before the first build is not reported as a broken deployment.
        print(f"::notice::No status file published yet at {e}, nothing to build")
        return 2

    # Read from where the client reads them; missing simply means no series
    # are published yet, which is not a reason to fail a deployment.
    written = generate(status, args.output, load_channels(args.channels))
    print(f"Wrote {len(written)} files to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
