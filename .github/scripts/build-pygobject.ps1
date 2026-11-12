# Builds the PyGObject wheel against the GStreamer in $env:GST_ROOT into wheels/
# (needs the MSVC environment and PKG_CONFIG_PATH). Used by windows.yml and caches.yml.
$ErrorActionPreference = 'Stop'
python -m pip install --upgrade pip
python -m pip wheel "pygobject==$env:PYGOBJECT_VERSION" --no-deps --no-binary pygobject `
  --config-settings=setup-args=-Dpycairo=disabled -w wheels
if ($LASTEXITCODE -ne 0) { throw "PyGObject wheel build failed" }
