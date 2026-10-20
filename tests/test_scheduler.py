from lanstreamer.scheduler import Debounce, InputState, OnAir, decide


def inp(id, priority, active=True, **kw):
    return InputState(id=id, priority=priority, active=active, **kw)


# --- F-SCH-01: highest priority wins ---------------------------------------------


def test_highest_active_priority_wins():
    inputs = [inp("low", 1), inp("high", 5), inp("mid", 3)]
    assert decide(inputs, None, now=0) == "high"


def test_inactive_inputs_are_skipped():
    inputs = [inp("high", 5, active=False), inp("mid", 3)]
    assert decide(inputs, None, now=0) == "mid"


# --- F-SCH-02: fallback ---------------------------------------------------------------


def test_fallback_plays_when_nothing_is_active():
    inputs = [inp("twitch", 5, active=False), inp("playlist", 0, active=False, fallback=True)]
    assert decide(inputs, None, now=0) == "playlist"


def test_nothing_on_air_without_any_active_input():
    assert decide([inp("twitch", 5, active=False)], None, now=0) is None


def test_active_input_replaces_fallback_at_once():
    inputs = [inp("twitch", 5), inp("playlist", 0, fallback=True, min_duration_s=600)]
    assert decide(inputs, OnAir("playlist", since=0), now=1) == "twitch"


# --- F-SCH-04: interruption and handover -------------------------------------------


def test_higher_priority_interrupts_even_before_min_duration():
    inputs = [inp("ingest", 9), inp("tiktok", 5, min_duration_s=60)]
    assert decide(inputs, OnAir("tiktok", since=0), now=5) == "ingest"


def test_lower_priority_never_interrupts():
    inputs = [inp("ingest", 9, releasable=True), inp("tiktok", 5)]
    assert decide(inputs, OnAir("ingest", since=0), now=1000) == "ingest"


def test_current_input_going_inactive_hands_over_immediately():
    inputs = [inp("ingest", 9, active=False, min_duration_s=600), inp("tiktok", 5)]
    assert decide(inputs, OnAir("ingest", since=0), now=1) == "tiktok"


def test_equal_priority_waits_for_release():
    inputs = [inp("a", 5, releasable=False), inp("b", 5)]
    assert decide(inputs, OnAir("a", since=0), now=100) == "a"


def test_equal_priority_hands_over_when_released():
    inputs = [inp("a", 5, releasable=True), inp("b", 5)]
    assert decide(inputs, OnAir("a", since=0), now=1) == "b"


def test_equal_priority_respects_min_duration():
    inputs = [inp("a", 5, releasable=True, min_duration_s=30), inp("b", 5)]
    assert decide(inputs, OnAir("a", since=0), now=29) == "a"
    assert decide(inputs, OnAir("a", since=0), now=30) == "b"


def test_max_duration_forces_handover_without_release():
    inputs = [inp("a", 5, releasable=False, max_duration_s=120), inp("b", 5)]
    assert decide(inputs, OnAir("a", since=0), now=119) == "a"
    assert decide(inputs, OnAir("a", since=0), now=120) == "b"


def test_single_input_of_top_priority_keeps_playing():
    inputs = [inp("a", 5, releasable=True, max_duration_s=1), inp("low", 1)]
    assert decide(inputs, OnAir("a", since=0), now=100) == "a"


def test_equal_priority_rotates_in_list_order():
    inputs = [inp("a", 5), inp("b", 5), inp("c", 5)]
    assert decide(inputs, OnAir("b", since=0), now=1) == "c"
    assert decide(inputs, OnAir("c", since=0), now=1) == "a"


# --- F-AKT-05: overrides --------------------------------------------------------------


def test_blocked_input_is_ignored():
    inputs = [inp("twitch", 5, override="block"), inp("tiktok", 3)]
    assert decide(inputs, OnAir("twitch", since=0), now=1) == "tiktok"


def test_forced_input_wins_even_when_inactive_and_low():
    inputs = [inp("ingest", 9), inp("playlist", 0, active=False, override="force")]
    assert decide(inputs, OnAir("ingest", since=0), now=1) == "playlist"


def test_forced_input_stays_on_air():
    inputs = [inp("a", 1, override="force"), inp("b", 9, override="force")]
    assert decide(inputs, OnAir("a", since=0), now=1) == "a"
    assert decide(inputs, None, now=1) == "b"


# --- Debounce -------------------------------------------------------------------------


def test_debounce_ignores_short_flaps():
    d = Debounce(hold_s=3)
    assert d.update(True, now=0) is False
    assert d.update(False, now=1) is False
    assert d.update(True, now=2) is False
    assert d.update(True, now=4) is False
    assert d.update(True, now=5) is True


def test_debounce_resets_when_state_returns():
    d = Debounce(hold_s=3, initial=True)
    d.update(False, now=0)
    d.update(True, now=2)
    assert d.update(False, now=4) is True  # new pending period started at 4
    assert d.update(False, now=7) is False
