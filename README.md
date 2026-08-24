# vitasdk-web

The public catalogue of the VitaSDK package repository, at build time rather
than at request time.

It reads the `status.json` that
[vitasdk-autobuild](https://github.com/vitasdk/vitasdk-autobuild) publishes to
its `status` release — packages, build queue, published snapshots — and the
channel manifests that [vitasdk.github.io](https://vitasdk.github.io/channels)
publishes — release series, and each one's core snapshot with its per-host
artifacts — and writes a directory of plain HTML that GitHub Pages serves.
Modelled on [msys2-web](https://github.com/msys2/msys2-web), which is
read-only too; being static is not a reduced version of it, it is the same
thing without a server to operate.

```sh
python3 -m vitasdk_web.generate --status vitasdk/vitasdk-autobuild --output _site
python3 -m vitasdk_web.generate --status ./status.json --output _site   # offline
```

A channel picked in the header follows the reader across the site, so most
pages exist once per channel:

```
_site/
  index.html                     the default channel's Downloads page
  channel/<name>/downloads.html  every published host, badge, and install command
  channel/<name>/packages.html   Catalogue / Recently built / Releases / Snapshots, as sub-tabs
  channel/<name>/status.html     queue, workers, failures, cycles, staging repository
  package/<name>.html            one page per package, with both dependency directions
  snapshot/<tag>.html            one page per published snapshot, read from its own database
  api/status.json                the source file, served as an API
  style.css
```

A core snapshot's `release.json` — its per-host artifact list — only exists on
snapshots published after it was introduced; an older one degrades to a plain
link to its GitHub release instead of a crash.

The site updates on a half-hourly schedule and immediately on a
`repository_dispatch` of type `status_updated`, so the autobuilder does not
have to know this repository exists.

No dependencies, no build step. JavaScript is light and layered on top of
static HTML: a filter box, a channel/snapshot selector, sub-tab switching, and
client-side detection of which download card is the reader's own platform.

```sh
python3 -m unittest discover -t . -s tests
```

## Licence

MIT, see [LICENSE](LICENSE).
