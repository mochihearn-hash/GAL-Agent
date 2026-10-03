# SQLite 保存会话状态、事件和记忆检查点；PNG 截图单独保存在会话专属目录。
# 本类由事件循环统一写入，事务覆盖单次状态更新；会话名称可重名，真正身份使用 UUID。
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def empty_memory():
    return {"recent": [], "chapters": [], "facts": [], "hypotheses": [], "questions": [], "summarized_through": 0}


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    """Single event-loop writer; SQLite transactions persist each transition."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.images = self.root / "screenshots"
        self.images.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / "sessions.sqlite3",check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO metadata VALUES('schema_version','1');
            CREATE TABLE IF NOT EXISTS configs(id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                settings TEXT NOT NULL, memory TEXT NOT NULL, guidance TEXT NOT NULL,
                branch TEXT NOT NULL, last_image TEXT, last_window TEXT, needs_reconcile INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                branch TEXT NOT NULL, config TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                run_id TEXT REFERENCES runs(id) ON DELETE CASCADE, branch TEXT NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_session ON events(session_id, seq);
            CREATE TABLE IF NOT EXISTS checkpoints(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                snapshot TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        # WAL 支持写入时读取，FULL 要求更严格的同步；schema_version 是数据结构版本，非应用版本。
        if self.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] != "1":
            raise RuntimeError("数据库版本与当前程序不兼容，请保留数据并使用对应版本")
        # 重启后不能判断游戏是否载入了同一存档；存在截图的会话需要重新核对进度。
        # prepared/sent 可能已经影响游戏，标记未确认，不自动重放这些动作。
        # Never replay transitions which may already have reached the operating system.
        with self.db:
            self.db.execute("UPDATE sessions SET needs_reconcile=1 WHERE last_image IS NOT NULL")
            for row in self.db.execute("SELECT id,session_id,payload FROM events WHERE kind='action'").fetchall():
                if json.loads(row["payload"]).get("phase") in {"prepared", "sent"}:
                    self.db.execute("UPDATE sessions SET needs_reconcile=1 WHERE id=?", (row["session_id"],))
                    payload = json.loads(row["payload"])
                    payload["phase"] = "unconfirmed"
                    self.db.execute("UPDATE events SET payload=? WHERE id=?",(dump(payload),row["id"]))

    def close(self):
        self.db.close()

    def get_session(self, sid: str):
        # 将 JSON 列还原成对象；先确认会话存在，避免迟到任务写回已删除会话。
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            raise KeyError("会话不存在")
        result = dict(row)
        for key in ("settings", "memory", "guidance", "last_window"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def list_sessions(self):
        return [dict(r) for r in self.db.execute("SELECT id,name,created_at,updated_at,needs_reconcile FROM sessions ORDER BY updated_at DESC").fetchall()]

    def create_session(self, name: str):
        # 新会话始终使用空白剧情记忆，只继承默认模型配置，不继承其他会话的游戏窗口或剧情。
        sid = uid()
        timestamp = now()
        with self.db:
            self.db.execute("INSERT INTO sessions(id,name,created_at,updated_at,settings,memory,guidance,branch) VALUES(?,?,?,?,?,?,?,?)", (sid,name,timestamp,timestamp,dump({"config_id":self.default_config_id(),"profile":{}}),dump(empty_memory()),"[]",uid()))
        return self.get_session(sid)

    def default_config_id(self):
        # 优先使用最近明确选择的配置；引用已删除时回退到最近创建/更新的可用配置。
        row = self.db.execute("SELECT c.id FROM configs c JOIN metadata m ON m.value=c.id WHERE m.key='default_config'").fetchone()
        if not row:
            row = self.db.execute("SELECT id FROM configs ORDER BY created_at DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def update_session(self, sid: str, **values):
        # SQL列名只来自白名单，值使用参数绑定；模型配置选中后记为新会话的默认选择。
        self.get_session(sid)
        allowed = {"name", "settings", "memory", "guidance", "last_image", "last_window", "needs_reconcile", "branch"}
        if not values.keys() <= allowed:
            raise ValueError("不支持的会话字段")
        values["updated_at"] = now()
        pairs = [(key, dump(value) if key in {"settings","memory","guidance","last_window"} else value) for key,value in values.items()]
        with self.db:
            self.db.execute(f"UPDATE sessions SET {','.join(key+'=?' for key,_ in pairs)} WHERE id=?", [v for _,v in pairs]+[sid])
            if values.get("settings",{}).get("config_id"):
                self.db.execute("INSERT INTO metadata VALUES('default_config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(values["settings"]["config_id"],))
        return self.get_session(sid)

    def delete_session(self, sid: str):
        # 外键级联删除事件、运行和检查点；截图只删除该会话目录，不触及游戏自己的存档。
        self.get_session(sid)
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE id=?", (sid,))
        target = (self.images / sid).resolve()
        if target.parent != self.images.resolve():
            raise ValueError("无效的截图目录")
        if target.exists():
            shutil.rmtree(target)

    def save_config(self, payload, cid=None):
        # 防御性移除 api_key，即便调用者传入也不写进数据库；只允许保留系统凭据引用。
        cid = cid or uid()
        clean = copy.deepcopy(payload)
        clean.pop("api_key", None)
        with self.db:
            self.db.execute("INSERT INTO configs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (cid,dump(clean),now()))
            self.db.execute("INSERT INTO metadata VALUES('default_config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(cid,))
        return {"id":cid, **clean}

    def get_config(self, cid):
        row = self.db.execute("SELECT payload FROM configs WHERE id=?", (cid,)).fetchone()
        if not row:
            raise KeyError("模型配置不存在")
        return {"id":cid, **json.loads(row[0])}

    def configs(self):
        return [{"id":r["id"],**json.loads(r["payload"])} for r in self.db.execute("SELECT * FROM configs ORDER BY created_at").fetchall()]

    def delete_config(self, cid):
        # 删除配置后解除所有会话对它的引用；实际密钥删除由接口层调用 Credentials 完成。
        self.get_config(cid)
        with self.db:
            self.db.execute("DELETE FROM configs WHERE id=?", (cid,))
        for item in self.list_sessions():
            session = self.get_session(item["id"])
            if session["settings"].get("config_id") == cid:
                settings = session["settings"]
                settings["config_id"] = None
                self.update_session(session["id"], settings=settings)

    def create_run(self, sid, config):
        # 每次开始生成独立运行 ID，保存所用配置快照，但剔除密钥与凭据引用供安全导出。
        session = self.get_session(sid)
        rid = uid()
        clean = {k:v for k,v in config.items() if k not in {"api_key","credential_ref"}}
        with self.db:
            self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?)", (rid,sid,session["branch"],dump(clean),now()))
        return rid

    def event(self, sid, kind, payload, run_id=None):
        # 自增 seq 用于时间线排序和分页；branch 区分恢复检查点后产生的新剧情分支。
        session = self.get_session(sid)
        eid = uid()
        timestamp = now()
        with self.db:
            cur = self.db.execute("INSERT INTO events(id,session_id,run_id,branch,kind,payload,created_at) VALUES(?,?,?,?,?,?,?)", (eid,sid,run_id,session["branch"],kind,dump(payload),timestamp))
            self.db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (timestamp,sid))
        return {"id":eid,"seq":cur.lastrowid,"kind":kind,"payload":payload,"created_at":timestamp,"branch":session["branch"]}

    def replace_event_payload(self, sid, eid, payload):
        # 动作阶段在同一事件上更新，防止日志把一个点击误展示为多个独立决策。
        self.get_session(sid)
        with self.db:
            self.db.execute("UPDATE events SET payload=? WHERE session_id=? AND id=?", (dump(payload),sid,eid))

    def events(self, sid, before=None, limit=100):
        # 数据库倒序取最近一页，再翻回正序供界面展示；before 是排他的序号上界。
        self.get_session(sid)
        rows = self.db.execute("SELECT * FROM events WHERE session_id=? AND seq<? ORDER BY seq DESC LIMIT ?", (sid,before or 2**63-1,limit)).fetchall()
        return [{**dict(row),"payload":json.loads(row["payload"])} for row in reversed(rows)]

    def save_image(self, sid, eid, data):
        # 先写临时文件再替换目标，避免正常写入中断留下半张 PNG；数据库只保存相对路径。
        self.get_session(sid)
        target = self.images / sid / f"{eid}.png"
        target.parent.mkdir(exist_ok=True)
        temp = target.with_suffix(".tmp")
        temp.write_bytes(data)
        temp.replace(target)
        relative = f"{sid}/{eid}.png"
        self.update_session(sid, last_image=relative)
        return relative

    def image_path(self, relative):
        # 解析后的真实路径必须仍在截图根目录内，禁止相对路径穿越以及读取任意非PNG文件。
        path = (self.images / relative).resolve()
        if not path.is_relative_to(self.images.resolve()) or path.suffix != ".png":
            raise ValueError("无效的截图路径")
        if not path.is_file():
            raise KeyError("截图不存在")
        return path

    def checkpoint(self, sid, note=""):
        # 检查点保存 Agent 记忆、设置和对应截图位置，不包含游戏内部变量，也不会替游戏存档。
        session = self.get_session(sid)
        snapshot = {k:session[k] for k in ("memory","guidance","last_image","last_window","settings","branch","needs_reconcile")}
        snapshot["through_seq"] = self.db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE session_id=?",(sid,)).fetchone()[0]
        cid = uid()
        with self.db:
            self.db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?)",(cid,sid,dump(snapshot),note,now()))
        return {"id":cid,"note":note,"created_at":now(),"snapshot":snapshot}

    def checkpoints(self,sid):
        self.get_session(sid)
        return [{**dict(r),"snapshot":json.loads(r["snapshot"])} for r in self.db.execute("SELECT * FROM checkpoints WHERE session_id=? ORDER BY created_at DESC",(sid,)).fetchall()]

    def restore(self,sid,cid):
        # 恢复前保留当前检查点，恢复后创建新分支；旧历史不删除，也不混入恢复后的记忆。
        # 用户仍需要在游戏内载入对应存档，再重新绑定并确认对齐。
        self.get_session(sid)
        row = self.db.execute("SELECT snapshot FROM checkpoints WHERE session_id=? AND id=?",(sid,cid)).fetchone()
        if not row:
            raise KeyError("检查点不存在")
        snapshot = json.loads(row[0])
        self.checkpoint(sid,"恢复前自动保存")
        values = {k:v for k,v in snapshot.items() if k not in {"through_seq","branch"}}
        values["branch"] = uid()
        values["needs_reconcile"] = 1
        self.update_session(sid,**values)
        self.event(sid,"restore",{"checkpoint_id":cid,"through_seq":snapshot["through_seq"],"message":"已恢复 Agent 记忆，请载入对应游戏存档并重新绑定窗口"})
        return self.get_session(sid)

    def export(self,sid):
        # 导出剧情、操作和检查点用于回查；不导出完整连接配置或系统凭据内容。
        session = self.get_session(sid)
        session["settings"] = {"profile":session["settings"].get("profile",{})}
        events = self.db.execute("SELECT * FROM events WHERE session_id=? ORDER BY seq",(sid,)).fetchall()
        runs = self.db.execute("SELECT * FROM runs WHERE session_id=? ORDER BY created_at",(sid,)).fetchall()
        return {"format_version":1,"session":session,"events":[{**dict(r),"payload":json.loads(r["payload"])} for r in events],"runs":[{**dict(r),"config":json.loads(r["config"])} for r in runs],"checkpoints":self.checkpoints(sid)}
