"""Generates the package catalogue as plain files.

The catalogue is read-only, so it does not need to be a service. Everything
here turns one status.json, published by vitasdk-autobuild, into a directory
that GitHub Pages can serve: no hosting, no credentials, no operations.
"""

import argparse
import html
import json
import os
import shutil
import sys
import time
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


def load_status(source: str) -> dict[str, Any]:
    if os.path.exists(source):
        with open(source, encoding="utf-8") as handle:
            return json.load(handle)
    url = source if source.startswith("http") else STATUS_URL.format(repo=source)
    print(f"Fetching {url}", flush=True)
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed scheme
        return json.loads(response.read().decode())


def counts(packages: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for package in packages:
        result[package["status"]] = result.get(package["status"], 0) + 1
    return result


def page(title: str, body: str, depth: int = 0) -> str:
    root = "../" * depth
    generated = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
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
    <a href="{root}status.html">Build status</a>
    <a href="{root}api/status.json">API</a>
  </nav>
</header>
<main>
{body}
</main>
<footer>Generated {esc(generated)} from the autobuilder's status file.</footer>
</body>
</html>
"""


def status_badge(status: str) -> str:
    label, kind = STATUS_LABELS.get(status, (status, "wait"))
    return f'<span class="badge {kind}">{esc(label)}</span>'


def render_index(status: dict[str, Any]) -> str:
    packages = status["packages"]
    tally = counts(packages)
    summary = " ".join(
        f'<span class="tally">{tally.get(key, 0)} {esc(STATUS_LABELS[key][0].lower())}</span>'
        for key in ("finished", "finished-but-blocked", "waiting-for-build",
                    "waiting-for-dependencies", "failed-to-build")
        if tally.get(key))

    rows = []
    for package in packages:
        repo_version = package.get("repo_version") or "&mdash;"
        rows.append(
            f'<tr data-name="{esc(package["name"])}">'
            f'<td><a href="package/{esc(package["name"])}.html">{esc(package["name"])}</a></td>'
            f'<td class="version">{esc(package["version"])}</td>'
            f'<td class="version">{repo_version}</td>'
            f'<td>{status_badge(package["status"])}</td>'
            f'<td class="desc">{esc(package.get("description", ""))}</td>'
            f'</tr>')

    return page("VitaSDK packages", f"""
<h1>Package catalogue</h1>
<p class="lede">{len(packages)} packages built against
<code>{esc(status.get("core_snapshot", "unknown core"))}</code>
from <a href="https://github.com/{esc(status.get("packages_repo", ""))}">{esc(status.get("packages_repo", ""))}</a>.</p>
<p class="summary">{summary}</p>
<input id="filter" type="search" placeholder="Filter packages" autocomplete="off">
<table id="packages">
<thead><tr><th>Package</th><th>Version</th><th>In repository</th><th>Status</th><th>Description</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
<script>
const filter = document.getElementById('filter');
const rows = Array.from(document.querySelectorAll('#packages tbody tr'));
filter.addEventListener('input', () => {{
  const needle = filter.value.trim().toLowerCase();
  for (const row of rows) {{
    row.hidden = needle !== '' && !row.dataset.name.toLowerCase().includes(needle);
  }}
}});
</script>
""")


def render_package(package: dict[str, Any], status: dict[str, Any]) -> str:
    def links(names: list[str]) -> str:
        if not names:
            return "<p>None.</p>"
        items = " ".join(
            f'<a class="pill" href="{esc(name)}.html">{esc(name)}</a>' for name in names)
        return f"<p>{items}</p>"

    details = package.get("details") or {}
    notes = []
    if details.get("desc"):
        notes.append(f"<p class=\"note\">{esc(details['desc'])}</p>")
    if package["status"] in STATUS_HELP:
        notes.append(f"<p class=\"note\">{esc(STATUS_HELP[package['status']])}</p>")
    for label, url in (details.get("urls") or {}).items():
        notes.append(f'<p class="note">Build log: <a href="{esc(url)}">{esc(label)}</a></p>')

    homepage = ""
    if package.get("url"):
        homepage = f'<p>Upstream: <a href="{esc(package["url"])}">{esc(package["url"])}</a></p>'

    binaries = ", ".join(esc(name) for name in package.get("binaries", []))
    licenses = ", ".join(esc(name) for name in package.get("licenses", [])) or "&mdash;"

    return page(f"{package['name']} - VitaSDK packages", f"""
<h1>{esc(package['name'])}</h1>
<p class="lede">{esc(package.get('description', ''))}</p>
<table class="facts">
<tr><th>Recipe version</th><td>{esc(package['version'])}</td></tr>
<tr><th>In the repository</th><td>{esc(package.get('repo_version') or '—')}</td></tr>
<tr><th>Status</th><td>{status_badge(package['status'])}</td></tr>
<tr><th>Provides</th><td>{binaries}</td></tr>
<tr><th>Licence</th><td>{licenses}</td></tr>
<tr><th>Built against</th><td><code>{esc(status.get('core_snapshot', ''))}</code></td></tr>
</table>
{''.join(notes)}
{homepage}
<h2>Depends on</h2>
{links(package.get('depends', []))}
<h2>Needed by</h2>
{links(package.get('rdepends', []))}
<p><a href="https://github.com/{esc(status.get('packages_repo', ''))}/blob/HEAD/{esc(package['name'])}/VITABUILD">Recipe</a></p>
""", depth=1)


def render_status(status: dict[str, Any]) -> str:
    packages = status["packages"]
    tally = counts(packages)
    rows = "".join(
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

    failed = [p for p in packages if p["status"] == "failed-to-build"]
    if failed:
        failed_rows = "".join(
            f'<tr><td><a href="package/{esc(p["name"])}.html">{esc(p["name"])}</a></td>'
            f'<td class="version">{esc(p["version"])}</td>'
            f'<td>{esc((p.get("details") or {}).get("desc") or "")}</td></tr>' for p in failed)
        failed_table = (f"<table><thead><tr><th>Package</th><th>Version</th><th>Detail</th>"
                        f"</tr></thead><tbody>{failed_rows}</tbody></table>")
    else:
        failed_table = "<p>Nothing is failing.</p>"

    cycles = status.get("cycles") or []
    cycle_list = ("<ul>" + "".join(
        f"<li>{esc(a)} &harr; {esc(b)}</li>" for a, b in cycles) + "</ul>") if cycles else ""

    staging = f"""
<h2>Staging repository</h2>
<p>Packages marked <em>built</em> and <em>built, held back</em> are also available from a
staging pacman repository. It can contain partial results of a rebuild, so packages
in it may be broken from time to time. To use it, add this above the other repositories
in <code>$VITASDK/etc/pacman.conf</code>:</p>
<pre><code>[vita-staging]
SigLevel = Never
Server = https://github.com/{esc(status.get('autobuild_repo', 'vitasdk/vitasdk-autobuild'))}/releases/download/staging</code></pre>
"""

    return page("Build status - VitaSDK packages", f"""
<h1>Build status</h1>
<p class="lede">Core <code>{esc(status.get('core_snapshot', ''))}</code>,
recipes at <code>{esc((status.get('packages_revision') or '')[:12])}</code>.</p>
<table>{rows}</table>
<h2>Workers</h2>
{job_table}
<h2>Failures</h2>
{failed_table}
{"<h2>Dependency cycles</h2>" + cycle_list if cycles else ""}
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
input[type=search] { width: 100%; padding: .55rem .7rem; margin: .5rem 0 1rem;
       border: 1px solid var(--line); border-radius: 6px; background: var(--bg);
       color: var(--fg); font-size: 15px; }
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

    status = load_status(args.status)
    written = generate(status, args.output)
    print(f"Wrote {len(written)} files to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
