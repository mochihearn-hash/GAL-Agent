import json
import sqlite3

import pytest

from app.storage.store import Store, empty_memory


def test_sessions_with_same_name_are_isolated_and_rename_survives_restart(tmp_path):
    store = Store(tmp_path)
    a,b = store.create_session("旅程"),store.create_session("旅程")
    assert a["id"] != b["id"]
    memory = empty_memory()
    memory["facts"] = [{"text":"只属于海猫的事实","source_seq":1}]
    store.update_session(a["id"],memory=memory,name="新的名称")
    store.close()
    store = Store(tmp_path)
    assert store.get_session(a["id"])["name"] == "新的名称"
    assert store.get_session(b["id"])["memory"] == empty_memory()
    store.close()


def test_restore_does_not_include_later_plot_and_keeps_original_history(tmp_path):
    store = Store(tmp_path)
    sid = store.create_session("海猫")["id"]
    memory = empty_memory()
    memory["facts"] = [{"text":"最初的事件","source_seq":1}]
    store.update_session(sid,memory=memory)
    old_branch = store.get_session(sid)["branch"]
    point = store.checkpoint(sid,"存档1")
    memory["facts"].append({"text":"未来的真相","source_seq":2})
    store.update_session(sid,memory=memory)
    store.event(sid,"decision",{"dialogue":"未来的真相"})
    restored = store.restore(sid,point["id"])
    assert "未来的真相" not in json.dumps(restored["memory"],ensure_ascii=False)
    assert restored["branch"] != old_branch
    assert restored["needs_reconcile"] == 1
    assert len(store.checkpoints(sid)) == 2
    assert "未来的真相" in json.dumps(store.events(sid),ensure_ascii=False)
    store.close()


@pytest.mark.parametrize("phase",["prepared","sent"])
def test_interrupted_input_is_flagged_and_never_replayed(tmp_path,phase):
    store = Store(tmp_path)
    sid = store.create_session("恢复")["id"]
    store.event(sid,"action",{"phase":phase})
    store.close()
    store = Store(tmp_path)
    assert store.get_session(sid)["needs_reconcile"] == 1
    assert store.events(sid)[0]["payload"]["phase"] == "unconfirmed"
    store.update_session(sid,needs_reconcile=0)
    store.close()
    store = Store(tmp_path)
    assert store.get_session(sid)["needs_reconcile"] == 0
    store.close()


def test_delete_cascades_and_late_writes_cannot_resurrect_session(tmp_path):
    store = Store(tmp_path)
    sid = store.create_session("删除")["id"]
    rid = store.create_run(sid,{"model":"test"})
    event = store.event(sid,"decision",{"dialogue":"旧记录"},rid)
    store.save_image(sid,event["id"],b"image")
    store.checkpoint(sid)
    store.delete_session(sid)
    for table in ["events","runs","checkpoints"]:
        assert store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert not (store.images/sid).exists()
    with pytest.raises(KeyError):
        store.event(sid,"decision",{"dialogue":"迟到响应"})
    assert store.list_sessions() == []
    store.close()


def test_config_and_export_never_store_api_key(tmp_path):
    store = Store(tmp_path)
    config = store.save_config({"name":"模型","api_key":"test-secret-key","credential_ref":"reference","model":"vision"})
    sid = store.create_session("导出")["id"]
    store.create_run(sid,config)
    assert "test-secret-key" not in (tmp_path/"sessions.sqlite3").read_bytes().decode("latin1")
    assert "api_key" not in config
    assert "credential_ref" not in json.dumps(store.export(sid))
    with pytest.raises(ValueError):
        store.image_path("../sessions.sqlite3")
    store.close()


def test_new_sessions_reuse_last_selected_model_across_restart(tmp_path):
    store=Store(tmp_path)
    a=store.save_config({'name':'A','model':'a'})
    b=store.save_config({'name':'B','model':'b'})
    sid=store.create_session('第一段')['id']
    assert store.get_session(sid)['settings']['config_id']==b['id']
    store.update_session(sid,settings={'config_id':a['id'],'profile':{}})
    store.close();store=Store(tmp_path)
    assert store.create_session('第二段')['settings']['config_id']==a['id']
    store.delete_config(a['id'])
    assert store.create_session('第三段')['settings']['config_id']==b['id']
    store.close()
