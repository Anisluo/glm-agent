"""长期记忆库：跨对话保存器件参数、对比结论、用户偏好、经验教训和对话要点。

数据保存在 memory.json（与对话记录同目录）。检索用关键词匹配：英文/数字按词、中文按双字切分，
型号这类关键词在标题和标签里命中权重更高。
"""
import json
import re
import time

TYPES = ["器件参数", "对比结论", "用户偏好", "经验教训", "对话要点"]

MEMORY_TOOLS = [
    {"type": "function", "function": {
        "name": "memory_search",
        "description": "在长期记忆库中检索以往保存的器件参数、对比结论、用户偏好、经验教训和对话要点。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "关键词，如器件型号、参数名，多个词用空格分隔"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "memory_save",
        "description": "新增或更新一条长期记忆。标题相同会覆盖为新内容，所以更新时要给出合并后的完整内容。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "器件参数用『器件参数：型号』，结论用『对比结论：主题』"},
            "content": {"type": "string", "description": "精炼的要点，写清数值、单位、测试条件、来源（规格书版本/页码）"},
            "type": {"type": "string", "enum": TYPES},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "型号、厂商、器件类别等"}},
            "required": ["title", "content", "type"]}}},
    {"type": "function", "function": {
        "name": "memory_delete", "description": "删除一条已经过时或错误的记忆。",
        "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}}},
]
MEMORY_TOOL_NAMES = {t["function"]["name"] for t in MEMORY_TOOLS}
MEMORY_LABELS = {"memory_search": ("🧠", "查记忆"), "memory_save": ("🧠", "存记忆"), "memory_delete": ("🧠", "删记忆")}

MEMORY_PROMPT = """## 长期记忆库
你有一个跨对话保存的记忆库，下方「相关记忆」是根据当前问题自动检索到的条目（可能过时，冲突时以规格书原文为准）。
- 开始对比任务前，先用 memory_search 按器件型号查找以往的参数和结论，能复用就复用，但报告里的数值仍要注明来自规格书；
- 每读完一份 datasheet，立即用 memory_save 保存该器件的关键参数：标题『器件参数：型号』，内容写参数、典型/最小/最大值、单位、测试条件、规格书版本与页码；
- 得出对比结论、发现规格书勘误、总结出可复用的经验，或用户表达了偏好（关注哪些参数、报告格式、风格）时，也用 memory_save 保存；
- 记忆要精炼成要点，不要粘贴原文；同一标题会被覆盖，更新时给出合并后的完整内容；发现错误的记忆用 memory_delete 删除。"""

DISTILL_PROMPT = """你是记忆整理助手。阅读下面的对话记录，提炼以后可能复用的信息，只输出一个 JSON 对象，不要输出其它文字：
{"summary": "本对话的要点（中文条目，含任务目标、关键结论、关键数值、生成或修改的文件、未完成事项，不超过 600 字）",
 "memories": [{"type": "器件参数|对比结论|用户偏好|经验教训", "title": "...", "content": "...", "tags": ["..."]}]}
要求：
- 器件参数的标题统一为『器件参数：型号』，内容写参数名、数值、单位、测试条件、来源（规格书版本/页码）；
- 对比结论标题为『对比结论：主题』；用户偏好标题为『用户偏好：方面』；经验教训标题为『经验教训：主题』；
- 如果「已有相关记忆」里有同标题条目，给出合并后的完整内容（保留旧条目中仍然有效的信息）；
- 只记录对话里明确出现的事实，不要编造；没有值得保存的就返回空数组。"""

COMPRESS_PROMPT = """把下面较早的对话压缩成摘要，供之后继续工作使用。用中文条目，不超过 1500 字。
必须保留：任务目标与用户要求、用户的偏好、已完成的步骤、得到的关键数据（器件型号、参数数值、单位、测试条件）、
生成或修改过的文件路径、尚未解决的问题和下一步计划。删除寒暄、重复内容和大段原文。只输出摘要正文。"""


def _tokens(text):
    """英文/数字按词，中文按单字和双字，用于粗略的关键词匹配。"""
    text = (text or "").lower()
    toks = set(re.findall(r"[a-z0-9][a-z0-9_.+\-]*", text))
    for run in re.findall(r"[一-鿿]+", text):
        toks.update(run[i:i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            toks.add(run)
    return toks


class MemoryStore:
    def __init__(self, path, load_json, save_json):
        self.path, self._save_json = path, save_json
        data = load_json(path, [])
        self.items = [m for m in data if isinstance(m, dict) and m.get("title")] if isinstance(data, list) else []
        for m in self.items:
            m.setdefault("type", "经验教训")
            m.setdefault("tags", [])
            m.setdefault("content", "")
        self._next = max((m.get("id", 0) for m in self.items), default=0) + 1
        for m in self.items:
            if not isinstance(m.get("id"), int):
                m["id"] = self._next
                self._next += 1

    def save(self):
        return self._save_json(self.path, self.items)

    def get(self, mid):
        return next((m for m in self.items if m["id"] == mid), None)

    def upsert(self, title, content, mtype="经验教训", tags=None, source=None):
        """标题相同（忽略大小写和空白）则更新，否则新增。返回 (条目, 是否新增)。"""
        title = " ".join(str(title).split())
        mtype = mtype if mtype in TYPES else "经验教训"
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        key = title.lower()
        now = time.strftime("%Y-%m-%d %H:%M")
        for m in self.items:
            if m["title"].lower() == key:
                m.update(content=str(content).strip(), type=mtype, updated=now)
                m["tags"] = sorted(set(m.get("tags", [])) | set(tags))
                if source:
                    m["source"] = source
                return m, False
        m = {"id": self._next, "type": mtype, "title": title, "content": str(content).strip(), "tags": sorted(set(tags)),
             "source": source or "", "created": now, "updated": now}
        self._next += 1
        self.items.append(m)
        return m, True

    def delete(self, mid):
        m = self.get(mid)
        if m:
            self.items.remove(m)
        return m

    def search(self, query, limit=8, exclude_source=None):
        q = _tokens(query)
        if not q:
            return []
        scored = []
        for m in self.items:
            if exclude_source and m.get("source") == exclude_source and m["type"] == "对话要点":
                continue
            title, tags, body = _tokens(m["title"]), _tokens(" ".join(m.get("tags", []))), _tokens(m["content"])
            score = 3 * len(q & title) + 2 * len(q & tags) + len(q & body)
            if score:
                scored.append((score, m.get("updated", ""), m))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [m for _, _, m in scored[:limit]]

    def digest(self, query, exclude_source=None, limit_chars=3000):
        """注入提示词用的相关记忆：用户偏好总是带上，其余按相关度。"""
        prefs = [m for m in self.items if m["type"] == "用户偏好"]
        found = [m for m in self.search(query, 10, exclude_source) if m["type"] != "用户偏好"]
        lines, total = [], 0
        for m in prefs + found:
            body = m["content"] if len(m["content"]) <= 400 else m["content"][:400] + "…"
            line = f"- [#{m['id']} {m['type']}] {m['title']}（{m.get('updated', '')}）：{body}"
            if total + len(line) > limit_chars:
                break
            lines.append(line)
            total += len(line)
        return "\n".join(lines)

    def run_tool(self, name, args, source=None):
        """执行模型调用的记忆工具，返回 (结果文本, 卡片信息)。"""
        if not isinstance(args, dict):
            raise ValueError("参数不是合法的 JSON 对象")
        if name == "memory_search":
            hits = self.search(args.get("query", ""), 10)
            if not hits:
                return "记忆库里没有相关条目。", {"status": "ok", "preview": "没有相关条目"}
            text = "\n\n".join(f"#{m['id']} [{m['type']}] {m['title']}（更新于 {m.get('updated', '')}）\n{m['content'][:1500]}"
                               for m in hits)
            return text, {"status": "ok", "preview": text[:4000]}
        if name == "memory_save":
            if not args.get("title") or not args.get("content"):
                raise ValueError("title 和 content 不能为空")
            m, new = self.upsert(args["title"], args["content"], args.get("type"), args.get("tags"), source)
            self.save()
            text = f"已{'新增' if new else '更新'}记忆 #{m['id']}：{m['title']}"
            return text, {"status": "ok", "preview": m["content"], "path": m["title"]}
        if name == "memory_delete":
            m = self.delete(int(args.get("id", -1)))
            if not m:
                raise ValueError("没有这条记忆")
            self.save()
            return f"已删除记忆 #{m['id']}：{m['title']}", {"status": "ok"}
        raise ValueError(f"未知的工具 {name}")


def transcript(msgs, limit=40000, tool_limit=600):
    """把对话消息转成给模型压缩/提炼用的纯文本记录（工具输出截短）。"""
    out = []
    for m in msgs:
        role = m.get("role")
        if role == "user":
            files = "".join(f"（附件 {f.get('path')}）" for f in m.get("files", []))
            out.append(f"【用户】{files}{m.get('content', '')}")
        elif role == "assistant":
            if m.get("content") and not m.get("error"):
                out.append(f"【GLM】{m['content']}")
            for c in m.get("tool_calls") or []:
                out.append(f"【GLM 调用工具】{c['function']['name']} {c['function']['arguments'][:300]}")
        elif role == "tool":
            body = (m.get("content") or "")[:tool_limit]
            out.append(f"【工具结果 {m.get('name')} {m.get('path', '')}】{body}")
    text = "\n".join(out)
    if len(text) > limit:  # 保留开头和结尾，中间省略
        text = text[:limit // 3] + "\n…（中间省略）…\n" + text[-(limit * 2 // 3):]
    return text


def parse_json_object(text):
    """从模型输出里取出 JSON 对象（容忍 ```json 代码块和前后多余文字）。"""
    text = (text or "").strip()
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("模型没有返回 JSON")
    return json.loads(text[i:j + 1])
