from lanstreamer.core.config import InputConfig
from lanstreamer.core.store import Store
from lanstreamer.plugins import choice


def setup_ini(path, **plugins):
    path.write_text("[plugins]\n" + "".join(f"{k.replace('_', '-')}={'on' if v else 'off'}\n" for k, v in plugins.items()))
    return path


def test_first_start_without_installer(tmp_path):
    store = Store(tmp_path / "db")
    choice.on_start(store, None)
    assert choice.disabled(store) == {"test-pattern"}
    assert [(i.name, i.plugin) for i in store.output("out1").inputs] == [
        ("Standby", "standby"), ("Short-video submissions", "submissions")]  # fmt: skip
    choice.on_start(store, None)  # a second start adds nothing
    assert len(store.output("out1").inputs) == 2


def test_the_installer_answers_are_taken_over(tmp_path):
    store = Store(tmp_path / "db")
    ini = setup_ini(tmp_path / "setup.ini", submissions=False, playlist=True, test_pattern=True, standby=False)
    choice.on_start(store, ini)
    assert choice.disabled(store) == {"submissions"}  # standby stays on
    assert [i.plugin for i in store.output("out1").inputs] == ["standby"]


def test_installed_again_with_other_answers(tmp_path):
    store = Store(tmp_path / "db")
    ini = setup_ini(tmp_path / "setup.ini", submissions=True, playlist=True, test_pattern=False)
    choice.on_start(store, ini)
    choice.set_enabled(store, "playlist", False)  # changed in the admin page
    choice.on_start(store, ini)  # same installer answers: the admin's change stays
    assert choice.disabled(store) == {"test-pattern", "playlist"}
    setup_ini(ini, submissions=False, playlist=False, test_pattern=True)  # reinstalled
    choice.on_start(store, ini)
    assert choice.disabled(store) == {"playlist"}  # submissions is in use, stays on


def test_in_use_and_always_on(tmp_path):
    import pytest

    store = Store(tmp_path / "db")
    choice.set_enabled(store, "test-pattern", True)
    store.add_input("out1", InputConfig("", "Bars", "test-pattern"))
    with pytest.raises(choice.InUse, match="Bars"):
        choice.set_enabled(store, "test-pattern", False)
    with pytest.raises(ValueError):
        choice.set_enabled(store, "standby", False)
