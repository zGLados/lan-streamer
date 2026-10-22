# A clean Ubuntu 24.04 with the .deb installed by apt (tests/local_linux.py).
# Stage "deps": the distribution packages only (cached), also used for install.sh.
FROM ubuntu:24.04 AS deps
ENV DEBIAN_FRONTEND=noninteractive
COPY depends.txt /tmp/depends.txt
RUN apt-get update -q \
    && apt-get install -y -q $(grep -v '^#' /tmp/depends.txt | sed 's/ *(.*)//') gstreamer1.0-tools curl \
    && python3 -m venv /e2e && /e2e/bin/pip install --quiet httpx websockets

FROM deps
COPY lan-streamer_*.deb /tmp/
RUN apt-get install -y -q /tmp/lan-streamer_*.deb
COPY start.sh /start.sh
CMD ["/start.sh"]
