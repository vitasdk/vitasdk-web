# vitasdk-web

The public catalogue of the VitaSDK package repository, at build time rather
than at request time.

It reads one file — the `status.json` that
[vitasdk-autobuild](https://github.com/vitasdk/vitasdk-autobuild) publishes to
its `status` release — and writes a directory of plain HTML that GitHub Pages
serves. Modelled on [msys2-web](https://github.com/msys2/msys2-web), which is
read-only too; being static is not a reduced version of it, it is the same
thing without a server to operate.

```sh
python3 -m vitasdk_web.generate --status vitasdk/vitasdk-autobuild --output _site
python3 -m vitasdk_web.generate --status ./status.json --output _site   # offline
```

Output:

```
_site/
  index.html            the catalogue, filterable client side
  status.html           queue, workers, failures, cycles, staging repository
  package/<name>.html   one page per package, with both dependency directions
  api/status.json       the source file, served as an API
  style.css
```

The site updates on a half-hourly schedule and immediately on a
`repository_dispatch` of type `status_updated`, so the autobuilder does not
have to know this repository exists.

No dependencies, no build step, no JavaScript beyond a filter box.

```sh
python3 -m unittest discover -t . -s tests
```

## Licence

MIT, see [LICENSE](LICENSE).
