# GLM-MCP-文档对比agent

基于智谱 GLM（默认 `glm-5.3`）的 Windows 桌面 AI agent，PyQt6 界面，可打包成单个 exe。
内置 datasheet 对比 skill：读取目录中的多份器件规格书 PDF，整理参数，生成 LaTeX 源文件并用 XeLaTeX 编译成 PDF 对比报告。

## 功能

- **对话**：流式输出、Markdown 与代码高亮、思考过程折叠、思考强度 low / high / max；多个对话可同时运行。
- **文件与命令**：关联工作目录后，GLM 可以查看、搜索、新建、修改、移动、复制、删除（移到回收站）文件，并运行 PowerShell 命令；修改可在卡片上一键撤销。
- **datasheet 对比 skill**（`skills/datasheet-compare`）：PDF 概况 / 按页读取 / 关键词搜索 / 版本对比、报告模板、XeLaTeX 编译与错误检查。
- **长期记忆库**：跨对话保存器件参数、对比结论、用户偏好、经验教训；对话结束后自动提炼要点，新问题自动检索相关记忆；长对话自动压缩成摘要。
- **MCP 客户端**：配置格式与常见 MCP 客户端的 `mcpServers` 相同，支持 stdio、Streamable HTTP 和旧版 SSE 服务器。
- **稳定性**：全局异常不闪退、对话出错自动恢复、网络与限流自动重试、并发排队、数据文件自动备份、单实例运行；日志在 `logs/app.log`。
- **速度**：PyMuPDF 解析 PDF 并缓存到磁盘，只读工具并行执行，复用 HTTPS 连接，界面显示每步用时与进度。

## 运行

需要 Python 3.10+（Windows）。

```bash
pip install -r requirements.txt
python glm_client.py
```

首次启动在「设置」里填写 API Key。使用 GLM Coding Plan 时，接口地址选「GLM Coding Plan（国内）」：
`https://open.bigmodel.cn/api/coding/paas/v4`。

生成 PDF 报告需要 XeLaTeX（推荐 MiKTeX）：

```bash
winget install --id MiKTeX.MiKTeX -e
```

## 打包成 exe

双击 `build.bat`，输出 `dist/GLM-MCP-文档对比agent.exe`。打包后可自检：

```bash
"dist/GLM-MCP-文档对比agent.exe" --selftest some.pdf
```

结果写入 `dist/logs/selftest.txt`。

## 数据文件

以下数据都在程序（exe）所在目录，包含个人数据，已在 `.gitignore` 中排除。
所有 `*.json` 统一放在 `config/` 子目录下，保持根目录整洁：

| 文件 | 内容 |
|---|---|
| `config/config.json` | API Key、接口地址、模型等设置 |
| `config/conversations.json` | 对话记录（及 `.bak` 备份） |
| `config/memory.json` | 长期记忆库 |
| `config/mcp_servers.json` | MCP 服务器配置（可能含令牌） |
| `logs/`、`cache/` | 日志、PDF 解析缓存 |

## MCP 配置示例

在程序左侧「MCP 服务」→「编辑配置…」中填写：

```json
{
  "mcpServers": {
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/docs"]},
    "remote": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer <token>"}}
  }
}
```

## 代码结构

| 文件 | 说明 |
|---|---|
| `glm_client.py` | 界面、对话流程（Session）、模型请求、文件工具 |
| `skill_tools.py` | datasheet 对比 skill 的 PDF / LaTeX 工具 |
| `shell_tools.py` | 命令执行、文件查找与管理 |
| `memory_store.py` | 长期记忆库、对话提炼与压缩的提示词 |
| `mcp_client.py` | MCP 客户端（stdio / Streamable HTTP / SSE） |
| `skills/datasheet-compare/` | skill 说明与报告模板 |
