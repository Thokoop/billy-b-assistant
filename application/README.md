# Application code and stable service paths

Billy keeps service entry points separate from the code loaded by a release.
The service paths used by 2.6.0 and earlier installations must remain usable;
this layout does not require changing their systemd units.

| Location | Responsibility |
| --- | --- |
| `main.py` | Stable assistant service entry point |
| `webconfig/server.py` | Stable web service entry point |
| `billy_runtime/` | Supervision, configuration parsing, updates and independent recovery |
| `application/core/` | Assistant, audio, Bluetooth and provider code |
| `application/webconfig/` | Web application, templates, scripts, styles and its build tools |
| `setup/` | Existing Wi-Fi service entry points and installation assets |
| `dependencies/` | Release contract, dependency locks and system-component manifest |

The small service entry points start the supervisor before importing application
code. Normal workers validate the selected release's configuration, then add its
`application/` directory to the Python import path. Recovery does not import that
directory and remains available if it is missing or broken. Old application
files left in the installation root must not override the selected release.

`billy_runtime.paths.CODE_ROOT` is the selected source tree; `INSTALL_ROOT` is
the original installation containing shared settings and user data. Keep `.env`,
profiles, personas, custom songs, models and state on their existing paths.
Shipped sounds and persona presets remain at the source-tree root. The worker's
working directory remains that source root, not `application/`.

Managed updates still launch `<source>/main.py` and
`<source>/webconfig/server.py` with the selected environment. The original root
application remains the baseline fallback and must not be deleted. This is a
source-layout change, not an on-device data migration. Existing release records,
the `current-source` alias and the worker protocol remain unchanged.

For local development, start the same service entry points from the repository
root. Run Python tests from the root (`python -m pytest`); their configuration
includes `application/` on the import path. For an ad-hoc Python command
importing `core`, set `PYTHONPATH=application` from the repository root.

Install frontend build dependencies in their current location after cloning,
moving the checkout or changing the lockfile:

```sh
cd application/webconfig
npm ci --include=dev
npm run build:css
```

The build uses the locally installed, locked Tailwind CLI and CSS package.
An installation in the old root-level `webconfig/` directory is not used.
Edit `tailwind/src/input.css`, then rebuild the tracked `static/css/styles.css`.

On a development unit that already runs an unreleased 2.7.0 bootstrap, this
change also needs the new root launchers and `billy_runtime/entrypoints.py`.
Update the complete root checkout and selected release consistently, then
restart both services; no service-file changes are needed. Sync complete source
trees, not just the old `core/` or `webconfig/` directories. Never use a blanket
delete against the installation root, which also contains persistent user data.
