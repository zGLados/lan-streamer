# Contributing to lan-streamer

Thanks for helping! Bug reports, fixes, docs and plugins are all welcome. For anything bigger than a small fix, please open an issue first, so that nobody builds something that will not fit.

## Do you need to change this repository?

**Probably not for a new plugin.** A plugin works as a project of your own: `pip install lanstreamer-sdk`, write it, and upload the zip in the admin page. See the [plugin guide](docs/plugin-guide.md). You keep control of its releases, and it needs no review here.

A plugin only moves into this repository (built-in, selectable in the installer) if the maintainer agrees to look after it, because it then ships to everyone who installs lan-streamer. Please ask in an issue first. [Adding a built-in plugin](#adding-a-built-in-plugin) lists what it takes.

## Setting up

You need Python 3.12+. GStreamer is not needed for the unit tests.

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"        # Windows: .venv\Scripts\pip ...
```

Details on all the tests: [docs/development.md](docs/development.md).

## Making a change

1. Fork the repository and create a branch from `main`. Pull requests are the only way in.
2. Make the change, with tests. Keep a pull request to one topic.
3. Run the checks:

   ```
   python -m ruff check .
   python -m pytest
   python tests/e2e/run_local.py      # if web pages or the API changed; needs `pip install playwright` and Edge or Chrome
   ```

4. Add a line under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) if users or plugin authors will notice the change.
5. Open the pull request: what changed, why, and how you tested it.

A pull request automatically runs ruff and the unit tests on Linux and Windows. The full test (installers, real video pipeline, browser playback) is expensive and is started by the maintainer before merging.

## Conventions

- Code, comments, docs and commit messages are in **English**.
- Write code that reads like the code around it. Comments explain why, not what.
- Commit messages: a short summary line in plain words, then the reason if it is not obvious.
- Everything has to run on **Windows and Linux**. Windows is the main platform, so watch out for file locks, paths and process handling.

## Things that need extra care

| If you change … | Then also … |
| --- | --- |
| `lanstreamer_sdk` | It is a public API, see [Compatibility](docs/plugin-guide.md#compatibility): additions only, new things with defaults. `tests/test_plugin_contract.py` must keep passing without edits. Raise the version in `packaging/sdk/pyproject.toml` (additions: minor, fixes: patch). |
| an HTTP endpoint | Update [docs/interfaces.md](docs/interfaces.md); `tests/test_docs.py` checks it. |
| anything that needs another GStreamer element | Add its plugin to `packaging/windows/gstreamer-plugins.txt` and the element to the self-test. The installer ships only what is on that list, so it would work for you and fail for users. |
| a Python dependency | Add it to `pyproject.toml`, and say why it is needed in the pull request. |

## Adding a built-in plugin

After the maintainer agreed:

1. Put the plugin folder in `src/lanstreamer/plugins/builtin/<id>/`, with tests in `tests/`.
2. Add the id to `BUILTIN_IDS` in `src/lanstreamer_sdk/manifest.py`, and to the list of built-in ids in the [plugin guide](docs/plugin-guide.md#the-plugin-folder).
3. Make it selectable in the Windows installer, in `packaging/windows/lan-streamer.iss`:
   - a line in `[Tasks]`, with `Flags: unchecked` (new plugins are off by default),
   - two lines in `[INI]` for `setup.ini` (`on` / `off`),
   - the description in English and German.
4. Add the plugin to `OFF_BY_DEFAULT` in `src/lanstreamer/plugins/choice.py`, so that it is also off on Linux and in a checkout.
5. Python packages go into `pyproject.toml`: the `requirements` of `plugin.toml` are only installed for uploaded plugins.
6. See the table above for GStreamer elements.

## Using AI tools

Welcome, with two rules: you are responsible for what you submit, so read and understand it and run the checks yourself; and say in the pull request if a large part was generated. Instructions for the tools themselves are in [AGENTS.md](AGENTS.md).

## Security problems

Please do not describe a security problem in a public issue. Open an issue that only asks for a private way to reach the maintainer.

Keep in mind that plugins run with lan-streamer's rights by design and that lan-streamer is meant for a LAN or VPN (see the [plugin guide](docs/plugin-guide.md#packing-and-uploading)).

## License

By contributing you agree that your contribution is published under the [MIT license](LICENSE).
