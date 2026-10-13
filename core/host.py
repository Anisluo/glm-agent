"""core.host —— Session 需要宿主实现的回调/取数接口契约。"""


class SessionHost:
    """Session 需要宿主实现的接口（回调 + 取数据），由 glm_client.MainWindow 实现。

    —— UI 回调（通知界面，无返回值）——
      on_session_state(session)                某对话开始/结束运行
      show_message(conv, msg, streaming=False) 新增一条消息到界面
      refresh_message(conv, msg, streaming=False) 刷新某条消息
      on_compressed(conv)                      历史被压缩，界面插入摘要分隔
      on_turn_finished(conv)                   一轮结束，安排记忆提炼
    —— 领域 / 持久化（有返回值）——
      cfg                                      配置字典
      mcp                                      MCP 管理器
      build_messages(conv, upto=None, summary=None) -> list
      tools_for(conv) -> list
      compression_job(conv) -> (text, cut_at) | None
      run_tool(session, name, args) -> (result, info)
      save_convs(force=False)
    """
