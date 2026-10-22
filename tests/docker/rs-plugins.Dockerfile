# fallbacksrc and audioloudnorm (gst-plugins-rs) built against Ubuntu 24.04's
# GStreamer with the current Rust – the same build as in .github/workflows/linux.yml.
# Used by tests/local_linux.py; the image is cached, so this runs once per branch.
FROM ubuntu:24.04
ARG BRANCH=0.13
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q && apt-get install -y -q --no-install-recommends \
        build-essential ca-certificates curl git pkg-config \
        libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
RUN curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal
ENV PATH=/root/.cargo/bin:$PATH
RUN git clone --quiet --depth 1 --branch "$BRANCH" https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git /src
WORKDIR /src
# keep in step with linux.yml
RUN RUSTFLAGS="-A dangerous_implicit_autorefs" \
        cargo build --release -p gst-plugin-fallbackswitch -p gst-plugin-audiofx \
    && mkdir /out && cp target/release/libgstfallbackswitch.so target/release/libgstrsaudiofx.so /out/
