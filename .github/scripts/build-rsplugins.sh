#!/usr/bin/env bash
# Builds fallbacksrc and audioloudnorm (gst-plugins-rs branch $GST_PLUGINS_RS) against
# Ubuntu's GStreamer into rsplugins/. Used by linux.yml and caches.yml.
set -euo pipefail
sudo apt-get update -q
sudo apt-get install -y -q libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
git clone --quiet --depth 1 --branch "$GST_PLUGINS_RS" https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git
cd gst-plugins-rs
# The 0.13 branch predates Rust 1.89, which made this lint an error (audioloudnorm)
RUSTFLAGS="-A dangerous_implicit_autorefs" \
  cargo build --release --quiet -p gst-plugin-fallbackswitch -p gst-plugin-audiofx
mkdir -p ../rsplugins
cp target/release/libgstfallbackswitch.so target/release/libgstrsaudiofx.so ../rsplugins/
ls -la ../rsplugins
