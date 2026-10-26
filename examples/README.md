# Example plugins

Three input plugins to learn from and to copy. Each is a folder with a `plugin.toml` and one Python module; the bigger two have their own tests (`pytest examples/<folder>`).

| Example | The usual case for | Shows |
| --- | --- | --- |
| [`hello-plugin`](hello-plugin) | a first try | The smallest plugin: settings, polling `is_active()`, a test picture as source, metadata. |
| [`web-stream`](web-stream) | a camera, a streaming service, another server | An external live stream that is on air while it is online: settings with validation, network checks off the event loop, a JSON status URL for title and channel, taking turns with inputs of the same priority. Standard library only. |
| [`stream-queue`](stream-queue) | anything viewers send in | A queue of stream links from viewers: a form on the player page, limits per device, the queue under the form and in the admin page, kept across restarts, pushed activity, next item after the end. |

Start your own:

```
cp -r examples/web-stream my-plugin           # change id, name and entry in plugin.toml
python -m lanstreamer_sdk check my-plugin     # loads it like lan-streamer, shows the forms
python -m pytest my-plugin                    # its tests
python -m lanstreamer_sdk pack my-plugin      # → web-stream-1.0.0.zip, upload in the admin page
```

Read next: [docs/plugin-guide.md](../docs/plugin-guide.md) (how to write a plugin) and [docs/interfaces.md](../docs/interfaces.md) (every interface of lan-streamer).
