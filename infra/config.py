"""infra.config —— 全局纯数据常量（无 __file__、无 Qt 依赖），供各层共享。"""

BASE_URLS = {  # Coding Plan（编程套餐）的额度只能走 /api/coding/ 地址，按量计费的余额走普通地址
    "GLM Coding Plan（国内）": "https://open.bigmodel.cn/api/coding/paas/v4",
    "智谱开放平台 按量计费（国内）": "https://open.bigmodel.cn/api/paas/v4",
    "GLM Coding Plan（国际 Z.ai）": "https://api.z.ai/api/coding/paas/v4",
    "Z.ai 按量计费（国际）": "https://api.z.ai/api/paas/v4",
}
DEFAULT_CONFIG = {
    "api_key": "",
    "base_url": BASE_URLS["GLM Coding Plan（国内）"],
    "model": "glm-5.3",
    "temperature": 0.7,
    "reasoning_effort": "max",  # GLM-5.3 始终思考，只能选强度：low / high / max
    "auto_memory": True,        # 对话结束后自动把要点提炼到记忆库
    "system_prompt": "",
}
MAX_FILE_BYTES = 300 * 1024
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "dist", "build"}
HINT_TEXT = "GLM 生成的内容可能有误，请核实重要信息"
LOAD_PROBLEMS = []  # 启动时读取数据遇到的问题，窗口打开后提示用户

# ---------------------------------------------------------------- 文件工具（仅限工作目录内）
TOOLS = [
    {"type": "function", "function": {
        "name": "list_dir", "description": "列出工作目录中某个目录的内容（不递归）。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对于工作目录的路径，默认为 ."}}}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "读取一个文本文件的完整内容。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对于工作目录的文件路径"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "新建文件，或用给定内容整体覆盖已有文件。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对于工作目录的文件路径"},
            "content": {"type": "string", "description": "文件的完整新内容"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "把文件中的 old_string 替换为 new_string。old_string 必须与原文完全一致且在文件中唯一（除非 replace_all 为 true）。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对于工作目录的文件路径"},
            "old_string": {"type": "string", "description": "要被替换的原文"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
            "replace_all": {"type": "boolean", "description": "是否替换所有匹配，默认 false"}},
            "required": ["path", "old_string", "new_string"]}}},
]
TOOL_PROMPT = """你可以通过工具直接操作工作目录中的文件并运行命令：
- 查看：list_dir、read_file、find_files（按文件名通配）、grep_files（按内容正则搜索）；
- 修改文件前先用 read_file 读取最新内容；
- 小范围修改用 edit_file（old_string 必须与文件原文完全一致，并带足够上下文保证唯一）；新建或整体重写用 write_file；
- 路径一律使用相对于工作目录的路径，不能访问工作目录以外的文件；
- 文件管理：make_dir、move_path、copy_path、delete_path（移到回收站）；
- 命令：run_command 在工作目录用 Windows PowerShell 执行（可运行 python、pip、git、编译等），不要运行需要交互输入或不会自动退出的命令；
- 这些操作会直接执行、不再询问用户，所以动手前想清楚：删除只用于确实不需要的文件，命令避免影响工作目录以外的系统设置。
- 提速：互不依赖的读取类操作（读多份文件/PDF、多个页段、搜索、查记忆）请在同一次回复里同时发起多个工具调用，程序会并行执行，能明显减少等待；不要一次只读一点再等下一轮。
完成后用简短的中文说明改了哪些地方。"""
TOOL_LABELS = {"list_dir": ("📂", "查看目录"), "read_file": ("📄", "读取"),
               "write_file": ("📝", "写入"), "edit_file": ("✏️", "修改")}
