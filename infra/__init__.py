"""infra —— 基础设施层：纯数据常量 + 文件/JSON IO，不依赖 core/ui。"""
from infra.config import (BASE_URLS, DEFAULT_CONFIG, HINT_TEXT, LOAD_PROBLEMS, MAX_FILE_BYTES,
                          SKIP_DIRS, TOOL_LABELS, TOOL_PROMPT, TOOLS)
from infra.jsonio import SAVER, load_json, save_json
from infra.fileio import (api_content, dir_overview, diff_stats, lang_of, open_in_explorer,
                          read_text_file, unified_diff, write_text_file)
