"""fallbacksrc's state for output.log (media/output.py), without GStreamer."""

from lanstreamer.media.output import source_state

STATS = (
    "application/x-fallbacksrc-stats, num-retry=(guint64){retries}, num-fallback-retry=(guint64)0,"
    " last-retry-reason=(GstFallbackSourceRetryReason){reason}, last-fallback-retry-reason=(GstFallbackSourceRetryReason)none,"
    " buffering-percent=(int){percent}, fallback-buffering-percent=(int)100;"
)


def test_a_running_source_shows_its_buffer():
    assert source_state("running", STATS.format(retries=0, reason="none", percent=100)) == "running, buffered 100 %"


def test_retries_show_how_often_and_why():
    text = STATS.format(retries=3, reason="timeout", percent=40)
    assert source_state("buffering", text) == "buffering, buffered 40 %, 3 retries (last: timeout)"
    one = STATS.format(retries=1, reason="error", percent=100)
    assert source_state("running", one) == "running, buffered 100 %, 1 retry (last: error)"


def test_unknown_text_does_not_raise():
    assert source_state("retrying", "") == "retrying, buffered ? %"
