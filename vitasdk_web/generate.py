"""Generates the package catalogue as plain files.

The catalogue is read-only, so it does not need to be a service. Everything
here turns one status.json, published by vitasdk-autobuild, into a directory
that GitHub Pages can serve: no hosting, no credentials, no operations.
"""

import argparse
import calendar
import html
import json
import os
import shutil
import sys
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


def when(iso: str, now: float | None = None) -> str:
    """A GitHub timestamp in the same words as everything else on the site."""

    if not iso:
        return "unknown"
    try:
        stamp = calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        # Better to show the raw value than to hide a snapshot over a format.
        return iso
    return ago(stamp, now)


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


def page(title: str, *, body: str, depth: int = 0, generated_at: float | None = None) -> str:
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
<header>
  <a class="brand" href="{root}index.html">VitaSDK packages</a>
  <nav>
    <a href="{root}index.html">Catalogue</a>
    <a href="{root}updates.html">Recently built</a>
    <a href="{root}snapshots.html">Snapshots</a>
    <a href="{root}status.html">Build status</a>
    <a href="{root}api/status.json">API</a>
  </nav>
</header>
<main>
{body}
</main>
<footer>Status published {esc(generated)}.</footer>
</body>
</html>
"""


def status_badge(status: str) -> str:
    label, kind = STATUS_LABELS.get(status, (status, "wait"))
    return f'<span class="badge {kind}">{esc(label)}</span>'


def render_index(status: dict[str, Any]) -> str:
    packages = status["packages"]
    worlds = worlds_of(status)

    summaries = []
    for world in worlds:
        tally = counts(packages, world["arch"], worlds)
        line = " ".join(
            f'<span class="tally">{tally.get(key, 0)} {esc(STATUS_LABELS[key][0].lower())}</span>'
            for key in ("finished", "finished-but-blocked", "waiting-for-build",
                        "waiting-for-dependencies", "failed-to-build")
            if tally.get(key))
        label = f'<span class="world">{esc(world["arch"])}</span> ' if len(worlds) > 1 else ""
        summaries.append(f"<p class=\"summary\">{label}{line}</p>")

    headers = "".join(f"<th>{esc(w['arch'])}</th>" for w in worlds) if len(worlds) > 1 \
        else "<th>Status</th>"

    rows = []
    for package in packages:
        repo_version = package.get("repo_version") or "&mdash;"
        builds = builds_of(package, worlds)
        cells = ""
        for world in worlds:
            build = builds.get(world["arch"])
            cells += f"<td>{status_badge(build['status']) if build else '<span class=\"absent\">&mdash;</span>'}</td>"
        haystack = f'{package["name"]} {package.get("description", "")}'.lower()
        rows.append(
            f'<tr data-name="{esc(package["name"])}" data-search="{esc(haystack)}" '
            f'data-worlds="{esc(" ".join(builds))}">'
            f'<td><a href="package/{esc(package["name"])}.html">{esc(package["name"])}</a></td>'
            f'<td class="version">{esc(package["version"])}</td>'
            f'<td class="version">{repo_version}</td>'
            f'{cells}'
            f'<td class="desc">{esc(package.get("description", ""))}</td>'
            f'</tr>')

    published_tag = status.get("published_tag", "")
    in_repository = ('<a href="snapshots.html" title="Versions in '
                     f'{esc(published_tag)}">In {esc(published_tag.replace("packages-snapshot-", ""))}</a>'
                     ) if published_tag else '<a href="snapshots.html">In repository</a>'

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

    return page("VitaSDK packages", generated_at=status.get("generated_at"), body=f"""
<h1>Package catalogue</h1>
<p class="lede">{len(packages)} packages built against {built_against}
from <a href="https://github.com/{esc(status.get("packages_repo", ""))}">{esc(status.get("packages_repo", ""))}</a>.</p>
{"".join(summaries)}
<div class="controls">
  <input id="filter" type="search" placeholder="Filter by name or description" autocomplete="off">
  {world_filter}
  <span id="count" class="count"></span>
</div>
<table id="packages">
<thead><tr><th>Package</th><th>Version</th><th>{in_repository}</th>{headers}<th>Description</th></tr></thead>
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
""")


def render_package(package: dict[str, Any], status: dict[str, Any]) -> str:
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

    return page(f"{package['name']} - VitaSDK packages", generated_at=status.get("generated_at"), depth=1, body=f"""
<h1>{esc(package['name'])}</h1>
<p class="lede">{esc(package.get('description', ''))}</p>
<table class="facts">
<tr><th>Recipe version</th><td>{esc(package['version'])}</td></tr>
<tr><th>In the repository</th><td>{esc(package.get('repo_version') or '—')}{f" &middot; <a href=\"../snapshots.html\">{esc(status.get('published_tag', ''))}</a>" if package.get('repo_version') and status.get('published_tag') else ""}</td></tr>
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


def render_updates(status: dict[str, Any]) -> str:
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

    return page("Recently built - VitaSDK packages", generated_at=status.get("generated_at"), body=f"""
<h1>Recently built</h1>
<p class="lede">What the autobuilder has produced most recently. A package only
reappears here when its recipe changes, or when something it links against was
rebuilt after it.</p>
{body}
""")


def render_snapshots(status: dict[str, Any]) -> str:
    """The published snapshots, which are the only history there is.

    Nothing is archived to produce this: the releases themselves are immutable
    and each one carries the core it was built against, so the list is read
    back from what was published.
    """

    snapshots = status.get("published_snapshots") or []
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
            revision = entry.get("packages_revision", "")
            rows += (f'<tr><td>{link}{mark}</td>'
                     f'<td>{esc(when(entry.get("published_at", "")))}</td>'
                     f'<td class="k">{esc(entry.get("core_snapshot", "") or "—")}</td>'
                     f'<td class="k">{esc(revision[:7] if revision else "—")}</td></tr>')
        body = (f"<table><thead><tr><th>Snapshot</th><th>Published</th>"
                f"<th>Built against</th><th>Recipes</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>")

    return page("Snapshots - VitaSDK packages", generated_at=status.get("generated_at"), body=f"""
<h1>Published snapshots</h1>
<p class="lede">Each one is an immutable pacman repository. They are what a
channel points at, so an installation can be reproduced exactly by naming one:
nothing in a published snapshot ever changes, and a newer core means a new
snapshot rather than an edit to an old one.</p>
{body}
""")


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

    return page("Build status - VitaSDK packages", generated_at=status.get("generated_at"), body=f"""
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
""")


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
header { display: flex; gap: 1.5rem; align-items: baseline; flex-wrap: wrap;
         padding: 1rem 1.5rem; border-bottom: 1px solid var(--line); }
.brand { font-weight: 700; text-decoration: none; color: var(--fg); }
nav a { margin-right: 1rem; color: var(--muted); text-decoration: none; }
nav a:hover, a:hover { color: var(--accent); }
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
.tally { margin-right: 1rem; color: var(--muted); font-size: 13px; }
.world { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
         margin-right: .6rem; color: var(--fg); }
.absent { color: var(--muted); font-size: 12px; }
.pill { display: inline-block; padding: .1rem .5rem; margin: 0 .3rem .3rem 0;
        background: var(--chip); border-radius: 999px; text-decoration: none; font-size: 13px; }
.note { background: var(--chip); padding: .6rem .8rem; border-radius: 6px; }
.facts th { width: 12rem; }
"""


def generate(status: dict[str, Any], output_dir: str) -> list[str]:
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(os.path.join(output_dir, "package"), exist_ok=True)
    os.makedirs(os.path.join(output_dir, "api"), exist_ok=True)

    written = []

    def write(relative: str, content: str) -> None:
        path = os.path.join(output_dir, relative)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        written.append(relative)

    write("style.css", STYLE)
    write("index.html", render_index(status))
    write("status.html", render_status(status))
    write("updates.html", render_updates(status))
    write("snapshots.html", render_snapshots(status))
    write("api/status.json", json.dumps(status, indent=2) + "\n")
    for package in status["packages"]:
        write(os.path.join("package", f"{package['name']}.html"),
              render_package(package, status))
    return written


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Generate the VitaSDK package catalogue")
    parser.add_argument("--status", default="vitasdk/vitasdk-autobuild",
                        help="path to status.json, a URL, or the autobuild repository")
    parser.add_argument("--output", default="_site", help="directory to write")
    args = parser.parse_args(argv[1:])

    try:
        status = load_status(args.status)
    except StatusNotPublished as e:
        # Exit code 2 says "nothing published yet" so that setting the site up
        # before the first build is not reported as a broken deployment.
        print(f"::notice::No status file published yet at {e}, nothing to build")
        return 2

    written = generate(status, args.output)
    print(f"Wrote {len(written)} files to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
