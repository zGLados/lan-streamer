from lanstreamer.media.encoders import candidates


def vendors(specs):
    return list(dict.fromkeys(s.vendor for s in specs))


def test_auto_tries_gpus_before_cpu():
    assert vendors(candidates("auto", "win32")) == ["nvidia", "amd", "intel", "cpu"]
    assert vendors(candidates("auto", "linux")) == ["nvidia", "amd", "intel", "cpu"]


def test_forced_vendor_falls_back_to_cpu_only():
    assert vendors(candidates("amd", "win32")) == ["amd", "cpu"]
    assert vendors(candidates("nvidia", "linux")) == ["nvidia", "cpu"]


def test_forced_cpu_has_no_duplicates():
    assert [s.element for s in candidates("cpu", "win32")] == ["x264enc"]


def test_platform_specific_elements():
    assert "amfh264enc" in [s.element for s in candidates("amd", "win32")]
    assert "vah264enc" in [s.element for s in candidates("amd", "linux")]
