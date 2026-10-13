"""mem.parts_store —— 元件参数库（SQLite 持久化）。

与 memory.json（定性/模糊记忆）互补：这里存**结构化**的器件参数（型号→参数值），
用关系表做到精确召回、自动去重、跨型号查询，并避免重复解析同一份 PDF。

- 纯标准库 sqlite3，零第三方依赖。
- EAV 模式：一行一个「型号-参数-值」，适配不同器件参数各不相同的情况。
- 事务写入、不重写整个文件：一次坏写最多影响一行，不会像整文件 JSON 那样一次全丢。
- 连接只在创建它的线程（主线程）使用；相关工具不进 SLOW_TOOLS，一律主线程内联执行。
"""
import json
import sqlite3


_SCHEMA = """
CREATE TABLE IF NOT EXISTS part_param(
    id       INTEGER PRIMARY KEY,
    model    TEXT NOT NULL,
    vendor   TEXT NOT NULL DEFAULT '',
    version  TEXT NOT NULL DEFAULT '',
    param    TEXT NOT NULL,
    value    TEXT NOT NULL DEFAULT '',
    unit     TEXT NOT NULL DEFAULT '',
    pmin     TEXT NOT NULL DEFAULT '',
    ptyp     TEXT NOT NULL DEFAULT '',
    pmax     TEXT NOT NULL DEFAULT '',
    cond     TEXT NOT NULL DEFAULT '',
    source   TEXT NOT NULL DEFAULT '',
    page     INTEGER,
    updated  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(model, version, param, cond)
);
CREATE INDEX IF NOT EXISTS idx_model ON part_param(model);
CREATE INDEX IF NOT EXISTS idx_param ON part_param(param);
"""


def _s(v):
    return "" if v is None else str(v).strip()


def _fmt(r):
    """把一行参数格式化成一句话。"""
    main = _s(r["value"])
    if not main:
        parts = [f"{k} {_s(r[c])}" for k, c in (("min", "pmin"), ("typ", "ptyp"), ("max", "pmax")) if _s(r[c])]
        main = " / ".join(parts)
    unit = (" " + _s(r["unit"])) if _s(r["unit"]) else ""
    cond = f"  @{_s(r['cond'])}" if _s(r["cond"]) else ""
    return f"- {_s(r['param'])}: {main}{unit}{cond}".rstrip()


class PartsStore:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self.db.commit()

    # ---------------------------------------------------------------- 写入
    def remember(self, model, params, vendor="", version="", source=""):
        model = _s(model)
        if not model:
            raise ValueError("model 不能为空")
        if not isinstance(params, list) or not params:
            raise ValueError("params 需要是非空数组")
        n = 0
        for p in params:
            if not isinstance(p, dict) or not _s(p.get("param")):
                continue
            self.db.execute(
                """INSERT INTO part_param(model,vendor,version,param,value,unit,pmin,ptyp,pmax,cond,source,page,updated)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))
                   ON CONFLICT(model,version,param,cond) DO UPDATE SET
                     vendor=excluded.vendor, value=excluded.value, unit=excluded.unit,
                     pmin=excluded.pmin, ptyp=excluded.ptyp, pmax=excluded.pmax,
                     source=excluded.source, page=excluded.page, updated=excluded.updated""",
                (model, _s(vendor), _s(version), _s(p.get("param")), _s(p.get("value")), _s(p.get("unit")),
                 _s(p.get("min")), _s(p.get("typ")), _s(p.get("max")), _s(p.get("cond")), _s(source),
                 p.get("page") if isinstance(p.get("page"), int) else None))
            n += 1
        self.db.commit()
        return n

    # ---------------------------------------------------------------- 读取
    def recall(self, model, version=None):
        model = _s(model)
        if version:
            rows = self.db.execute("SELECT * FROM part_param WHERE model=? AND version=? ORDER BY id",
                                   (model, _s(version))).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM part_param WHERE model=? ORDER BY id", (model,)).fetchall()
        return rows

    def query(self, keyword="", model="", param="", limit=50):
        where, args = [], []
        if _s(model):
            where.append("model LIKE ?"); args.append(f"%{_s(model)}%")
        if _s(param):
            where.append("param LIKE ?"); args.append(f"%{_s(param)}%")
        if _s(keyword):
            kw = f"%{_s(keyword)}%"
            where.append("(model LIKE ? OR param LIKE ? OR value LIKE ? OR cond LIKE ?)")
            args += [kw, kw, kw, kw]
        sql = "SELECT * FROM part_param"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY model, id LIMIT ?"
        args.append(int(limit or 50))
        return self.db.execute(sql, args).fetchall()

    def models(self):
        return self.db.execute(
            "SELECT model, vendor, GROUP_CONCAT(DISTINCT version) AS vers, COUNT(*) AS n "
            "FROM part_param GROUP BY model ORDER BY model").fetchall()

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM part_param").fetchone()[0]

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- 工具分发
    def run_tool(self, name, args):
        """返回 (给模型的结果文本, 卡片信息)，与 run_skill_tool 约定一致。"""
        if not isinstance(args, dict):
            raise ValueError("参数不是合法的 JSON 对象")
        if name == "remember_params":
            n = self.remember(args.get("model"), args.get("params") or [],
                              args.get("vendor", ""), args.get("version", ""), args.get("source", ""))
            ver = f"（{_s(args.get('version'))}）" if _s(args.get("version")) else ""
            text = f"已收录 {_s(args.get('model'))}{ver}：写入/更新 {n} 个参数。库中共 {self.count()} 条。"
        elif name == "recall_params":
            rows = self.recall(args.get("model"), args.get("version"))
            if not rows:
                text = f"参数库中没有 {_s(args.get('model'))} 的记录，需要时请读取 PDF 后用 remember_params 入库。"
            else:
                h = rows[0]
                head = f"{_s(h['model'])}" + (f"（{_s(h['vendor'])}" if _s(h["vendor"]) else "")
                head += (f" {_s(h['version'])}）" if _s(h["version"]) else ("）" if _s(h["vendor"]) else ""))
                text = f"{head} — {len(rows)} 个参数：\n" + "\n".join(_fmt(r) for r in rows)
        elif name == "query_params":
            rows = self.query(args.get("keyword", ""), args.get("model", ""),
                              args.get("param", ""), args.get("limit", 50))
            if not rows:
                text = "没有匹配的参数。"
            else:
                text = f"命中 {len(rows)} 条：\n" + "\n".join(
                    f"- [{_s(r['model'])}{(' ' + _s(r['version'])) if _s(r['version']) else ''}] "
                    f"{_s(r['param'])}: {_s(r['value']) or _s(r['ptyp'])}{(' ' + _s(r['unit'])) if _s(r['unit']) else ''}"
                    for r in rows)
        elif name == "list_models":
            rows = self.models()
            if not rows:
                text = "参数库为空，还没有收录任何型号。"
            else:
                text = f"已收录 {len(rows)} 个型号：\n" + "\n".join(
                    f"- {_s(r['model'])}" + (f"（{_s(r['vendor'])}）" if _s(r["vendor"]) else "")
                    + (f" 版本 {_s(r['vers'])}" if _s(r["vers"]) else "") + f"：{r['n']} 个参数"
                    for r in rows)
        else:
            raise ValueError(f"未知的工具 {name}")
        return text, {"status": "ok", "preview": text[:4000]}


# ---------------------------------------------------------------- 工具定义
PARTS_TOOLS = [
    {"type": "function", "function": {
        "name": "list_models",
        "description": "列出元件参数库里已收录的全部型号及参数条数。开始对比前先查这里，判断哪些型号已收录、避免重复解析 PDF。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "recall_params",
        "description": "按型号精确取回已收录的器件参数。若某型号已在库中，优先用它而不是重新读取 PDF。",
        "parameters": {"type": "object", "properties": {
            "model": {"type": "string", "description": "器件型号，如 AD8421"},
            "version": {"type": "string", "description": "可选，版本/Rev"}}, "required": ["model"]}}},
    {"type": "function", "function": {
        "name": "query_params",
        "description": "在参数库里跨型号检索（关键词匹配型号/参数名/值/条件）。例如查“所有型号的温漂”。",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "关键词，匹配型号/参数名/值/条件"},
            "model": {"type": "string", "description": "可选，限定型号"},
            "param": {"type": "string", "description": "可选，限定参数名"},
            "limit": {"type": "integer", "description": "最多返回条数，默认 50"}}}}},
    {"type": "function", "function": {
        "name": "remember_params",
        "description": ("把从 datasheet 提取到的器件参数存入参数库（跨对话复用，按 型号+版本+参数+条件 自动去重更新）。"
                        "读完一份 PDF、确认参数后就调用它入库，方便以后精确召回。"),
        "parameters": {"type": "object", "properties": {
            "model": {"type": "string", "description": "器件型号"},
            "vendor": {"type": "string", "description": "厂商，可选"},
            "version": {"type": "string", "description": "版本/Rev/日期，可选"},
            "source": {"type": "string", "description": "来源文件名，可选"},
            "params": {"type": "array", "description": "参数列表", "items": {"type": "object", "properties": {
                "param": {"type": "string", "description": "参数名，如 工作电压"},
                "value": {"type": "string", "description": "数值或范围，如 2.2--5.5；范围也可用 min/typ/max 分列"},
                "unit": {"type": "string"},
                "min": {"type": "string"}, "typ": {"type": "string"}, "max": {"type": "string"},
                "cond": {"type": "string", "description": "测试条件，如 25°C,Vs=5V"},
                "page": {"type": "integer", "description": "来源页码，可选"}}}}},
            "required": ["model", "params"]}}},
]
PARTS_TOOL_NAMES = {t["function"]["name"] for t in PARTS_TOOLS}
PARTS_LABELS = {"list_models": ("🗂", "型号清单"), "recall_params": ("📇", "取回参数"),
                "query_params": ("🔎", "查参数库"), "remember_params": ("💾", "入库参数")}
PARTS_PROMPT = ("## 元件参数库（持久化，跨对话）\n"
                "你有一个 SQLite 元件参数库。对比 datasheet 时：先 list_models / recall_params 查已收录的型号，"
                "命中就直接复用、不要重复读 PDF；读完新 PDF 确认参数后用 remember_params 入库。"
                "跨型号找某个参数用 query_params。")
