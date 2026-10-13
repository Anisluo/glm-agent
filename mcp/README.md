# mcp/ —— 自建 MCP agent（服务端）

这个目录放**本项目自己实现的 MCP 服务端**（agent），每个 agent 是一个可独立启动的
stdio 进程，对外提供若干工具供 GLM 调用。

> 区别于根目录的 `mcp_client.py`：那是**客户端/传输层**，负责连接并驱动这里（或第三方）
> 的 MCP 服务端。本目录只写**服务端**。

## 构成

| 文件 | 作用 |
|---|---|
| `framework.py` | 轻量 MCP 服务端框架（stdio 上的 JSON-RPC，零第三方依赖）。所有 agent 共用。 |
| `datetime_agent.py` | 示例 agent：当前时间、日期相减、日期加减天数。 |
| `text_agent.py` | 示例 agent：文本字数/行数统计、高频词统计。 |

## 协议约定（必须遵守）

框架已帮你处理好，但了解一下有助于排错：

- 每条消息一行 JSON，`\n` 结尾；从 **stdin 读、stdout 写**。
- **stdout 只能出现 JSON-RPC 报文**。任何日志、`print` 调试都要走 **stderr**
  （用 `framework.log(...)`），否则会污染协议流，客户端会丢弃该行。
- Windows 上 stdin/stdout 已被框架强制为 **UTF-8**（客户端按 UTF-8 解析），
  别自己再改回去，否则中文会乱码导致解析失败。

## 新增一个 agent（三步）

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from framework import MCPServer

srv = MCPServer("myname")                      # 服务器名，决定工具函数名前缀 mcp__myname__

@srv.tool("tool_name", "给模型看的工具说明",
          {"type": "object",
           "properties": {"x": {"type": "string", "description": "参数说明"}},
           "required": ["x"]},
          read_only=True)                      # 只读工具会被客户端允许并行执行
def tool_name(x):
    return "结果文本"                          # 返回 str；返回 dict/list 会自动转 JSON

if __name__ == "__main__":
    srv.run()
```

工具函数抛异常不会让服务器崩溃，框架会把它转成 `isError` 结果回给模型。

## 注册到应用

在 `config/mcp_servers.json` 的 `mcpServers` 下加一项（服务器名用**纯英文**，
否则会被客户端编码成短码）：

```json
"myname": {
  "command": "python",
  "args": ["mcp/my_agent.py"],
  "disabled": false
}
```

- 路径相对于程序运行目录（`启动.bat` 会 `cd` 到本项目根目录，所以 `mcp/xxx.py` 可用）。
- 改完配置后，在应用里打开 MCP 面板**刷新/重连**即可看到新工具。

## 本地自测（不启动 GUI）

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"now","arguments":{}}}' \
  | python mcp/datetime_agent.py
```

## 打包成 exe 的注意

这些 agent 用 `command: python` 以**独立进程**启动，不会被 PyInstaller 自动打进主程序。
若要随 exe 分发，需要目标机器有 `python`，或改用绝对路径指向解释器，或把 agent 也
单独打包——开发/源码运行模式下开箱即用，无需额外处理。
