import sqlite3

import pytest

from lanstreamer.core.config import InputConfig, OutputConfig
from lanstreamer.core.store import MIGRATIONS, NotFound, Store, new_id


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "lanstreamer.db")
    yield s
    s.close()


def test_first_start_seeds_one_output_with_two_test_inputs_and_standby(store):
    [out] = store.outputs()
    assert (out.id, out.fps) == ("out1", 60)
    assert [i.name for i in out.inputs] == ["Test input A", "Test input B", "Standby"]
    standby = out.inputs[2]
    assert (standby.plugin, standby.fallback, standby.priority) == ("standby", True, 0)


def test_schema_version_is_set(tmp_path):
    Store(tmp_path / "x.db").close()
    db = sqlite3.connect(tmp_path / "x.db")
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_reopening_keeps_the_data(tmp_path):
    s = Store(tmp_path / "db")
    s.add_input("out1", InputConfig("", "Twitch Just Chatting", "twitch-top", {"category": "Just Chatting"}, priority=5))
    s.close()
    out = Store(tmp_path / "db").output("out1")
    twitch = out.inputs[-1]
    assert (twitch.id, twitch.settings, twitch.priority) == ("twitch-just-chatting", {"category": "Just Chatting"}, 5)


def test_update_input_fields(store):
    store.update_input("test-a", priority=20, override="force", fallback=True, settings={"pattern": "ball"})
    a = store.output("out1").inputs[0]
    assert (a.priority, a.override, a.fallback, a.settings) == (20, "force", True, {"pattern": "ball"})


def test_unknown_fields_and_ids_are_rejected(store):
    with pytest.raises(ValueError, match="cannot change"):
        store.update_input("test-a", output_id="other")
    with pytest.raises(NotFound):
        store.update_input("nope", priority=1)
    with pytest.raises(NotFound):
        store.add_input("nope", InputConfig("", "x", "test-pattern"))


def test_delete_output_removes_its_inputs(store):
    out = store.add_output(OutputConfig(id="", name="Second screen", inputs=[InputConfig("", "Clock", "test-pattern")]))
    assert out.id == "second-screen" and out.inputs[0].id == "clock"
    store.delete_output(out.id)
    assert [o.id for o in store.outputs()] == ["out1"]
    store.add_input("out1", InputConfig("", "Clock", "test-pattern"))  # the id is free again


def test_reorder_inputs(store):
    store.reorder_inputs("out1", ["test-b", "test-a", "standby"])
    assert [i.id for i in store.output("out1").inputs] == ["test-b", "test-a", "standby"]


def test_new_id_is_unique_and_readable():
    assert new_id("Twitch – Just Chatting!", set()) == "twitch-just-chatting"
    assert new_id("A", {"a"}).startswith("a-")
    assert new_id("???", set()) == "item"


def test_upgrade_from_schema_1_keeps_the_data(tmp_path):
    db = sqlite3.connect(tmp_path / "old.db")
    db.executescript(f"BEGIN; {MIGRATIONS[0]}; PRAGMA user_version = 1; COMMIT;")
    db.execute("INSERT INTO outputs VALUES ('out1', 'Output 1', 1920, 1080, 60, 0, 'auto', 0)")
    db.execute("INSERT INTO inputs VALUES ('mine', 'out1', 'Mine', 'test-pattern', '{}', 7, 0, 0, NULL, 'none', 0)")
    db.commit()
    db.close()
    store = Store(tmp_path / "old.db")
    # no reseeding; the upgrade adds the Standby fallback
    assert [i.id for i in store.output("out1").inputs] == ["mine", "standby-out1"]
    assert store.output("out1").inputs[1].fallback is True
    store.set_setting("admin_password", "x")
    assert store.get_setting("admin_password") == "x"
    store.close()
