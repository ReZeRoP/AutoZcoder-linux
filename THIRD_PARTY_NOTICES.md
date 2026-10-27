# Third-party notices

AutoZcoder-linux is an unofficial, independent Linux fork of
[B3hnamR/AutoZcoder](https://github.com/B3hnamR/AutoZcoder), based on v1.2.2.
It is not affiliated with ZCode, z.ai, or the Fedora Project. Upstream project
names, artwork and other retained notices identify their respective owners;
they do not imply endorsement.

## Main application

The root [LICENSE](LICENSE) is the upstream MIT license, copyright B3hnamR.
Linux packaging and integration changes are provided under that license except
where an existing component has its own license. The root license does **not**
relicense third-party components or make the whole binary bundle MIT-only.
The original upstream README is preserved in `docs/UPSTREAM-README.md`; its
Windows build instructions and final MIT shorthand describe upstream, not the
licensing or runtime support of this Linux distribution.

## Vendored CAPTCHA solver source — CC BY-NC-SA 4.0

`tools/captcha-mint/captcha-*.ts` are vendored from
[TriDefender/zcode-api](https://github.com/TriDefender/zcode-api), with attribution
to TriDefender and that project's contributors. Its full license is preserved
in [tools/captcha-mint/LICENSE.vendor](tools/captcha-mint/LICENSE.vendor), fetched
from <https://raw.githubusercontent.com/TriDefender/zcode-api/master/LICENSE>.
The license is **Creative Commons Attribution–NonCommercial–ShareAlike 4.0
International (CC BY-NC-SA 4.0)**:
<https://creativecommons.org/licenses/by-nc-sa/4.0/>.

AutoZcoder's integration wraps this solver in a stdio sidecar; see the upstream
integration notes in `tools/captcha-mint/README.md`. This Linux port retains
those sources and notices without porting or building the sidecar. The retained
solver source is subject to attribution, noncommercial, and ShareAlike terms;
consult the full license before reuse or redistribution. It is not MIT-licensed.
No Windows `captcha-mint.exe`, Linux sidecar executable, or Bun runtime is shipped.
A system containing the retained source is not an all-permissively-licensed
Fedora package, and this RPM is not an official Fedora repository submission.

## Private binary dependencies

The RPM also bundles locked Python packages, a private Python 3.12 runtime,
Playwright's Chromium/FFmpeg downloads, and pinned Wrangler/Node modules. These
have their own licenses, including permissive licenses, Python's license,
browser codec/component notices, and copyleft terms in some components. Node.js,
GTK, WebKit, PyGObject and the desktop Python interpreter are system dependencies,
not copied into the package.

- Python desktop and optional package versions are recorded in
  `packaging/fedora/requirements.lock` and `generator-requirements.lock`.
- Private runtime/browser versions are in `packaging/fedora/runtime-versions.json`.
- Wrangler and transitive Node dependencies are integrity-pinned in
  `packaging/fedora/node-tools/package-lock.json`.
- The mail-worker template's `package.json` declares ISC; retained source notices
  and the upstream root license should be consulted together.
- Original license/notice files remain inside the bundled distributions.
  The staging script collects available package license metadata and license
  texts in `BUNDLED-LICENSES.txt`, installed with the RPM's license documents.
  It is an inventory, not a new license grant or a legal compliance certification.

Review upstream redistribution and corresponding-source obligations before
commercial redistribution. Do not describe this entire source tree or binary
bundle as MIT-only or claim service-side permission for account automation.
