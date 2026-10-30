# lanstreamer-sdk

Write input plugins for [lan-streamer](https://github.com/zGLados/lan-streamer) without installing lan-streamer itself.

```
python -m lanstreamer_sdk new my-plugin     # a working plugin in a new folder
python -m lanstreamer_sdk check my-plugin   # loads it like lan-streamer, shows the forms
python -m pytest my-plugin                  # its tests
python -m lanstreamer_sdk pack my-plugin    # the zip for the admin page (Plugins → Upload)
```

Read the [plugin guide](https://github.com/zGLados/lan-streamer/blob/main/docs/plugin-guide.md). The major version of this package is the plugin API version.
