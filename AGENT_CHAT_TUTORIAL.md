# 平台 ↔ Agent 双向对话教程

## 先说结论

平台已经具备双向的**数据通道**，但 MCP 本身不是聊天推送协议：

```text
Hermes / Agent（MCP 客户端） ──调用工具──▶ 光器件平台（MCP 服务器）
平台画布 ──写入消息──▶ 会话收件箱 ──Agent 主动读取──▶ Hermes / Agent
```

MCP 服务器不能反向调用或唤醒已经停止生成的 Hermes 会话。因此，仅配置 `/mcp` 后：

- Hermes 中发命令、调用平台：可以；
- 平台聊天框把命令存入同一 session：可以；
- Hermes 正在运行并调用 `poll_messages` / `wait_for_messages` 时接收：可以；
- Hermes 页面闲置时由平台主动把它叫醒：不可以，除非增加一个**常驻 Agent gateway/worker**。

## A. 人在 Hermes 中交互（无需 gateway）

1. 启动统一服务：`python -m optplat.api`。
2. Hermes MCP URL 填 `http://平台地址:8003/mcp`；若设置了 `OPTPLAT_TOKEN`，同时配置 Bearer token。
3. 打开平台画布，在“实时会话”填 `default` 并点连接。
4. Hermes 和所有工具调用都使用 `session="default"`。
5. 在 Hermes 发送下面的系统要求：

   > 每轮开始先调用 poll_messages(default)，处理平台用户消息；修改后用
   > push_to_canvas(session=default, note=中文回复) 回传。

6. 平台聊天框发消息后，在 Hermes 再发送一句“继续/检查平台消息”，触发下一轮即可。

这是真双向状态同步，但对话轮次仍由 Hermes 用户触发。

## B. 平台聊天框直接唤起 Agent（需要常驻 gateway）

gateway 是一个一直运行的 Agent host，而不是 MCP server 的一部分。它循环：

```python
while True:
    inbox = wait_for_messages(session="default", timeout_seconds=25)
    if not inbox["messages"]:
        continue
    # 1. 把消息交给 Hermes/GLM/Claude 的模型 API
    # 2. 模型按需调用平台 MCP 工具
    # 3. 用 push_to_canvas(..., note="回复") 将结果推回画布
```

若 Agent 框架不方便调用阻塞式 MCP 工具，也可长轮询 REST：

```http
GET /workspace/default/messages/wait?timeout=25&mark_read=true
```

收到消息后，gateway 必须把同一个 `session` 传给 `get_canvas`、`run_workflow`、
`autotune` 和 `push_to_canvas`。平台 SSE 会把回复、画布和运行结果实时显示出来。

## 为什么平台不直接内置 Hermes

Hermes 的 UI/MCP 客户端与模型推理服务是两个角色。除非 Hermes 提供受支持的“创建消息/运行 Agent”API，
平台无法安全地伪造一次 Hermes 对话。将 gateway 独立出来还能避免平台保存模型 API Key，并允许替换 GLM、
Claude 或内部 Agent。若你能提供 Hermes 的 Agent API 地址、鉴权方式和请求/流式响应格式，可以再实现一个具体 adapter。

## 排错

| 症状 | 检查 |
|---|---|
| Hermes 能调用工具，平台发消息无响应 | 没有常驻 worker；在 Hermes 发一轮消息，或部署 gateway |
| Agent 收不到消息 | 画布与工具的 `session` 是否完全一致 |
| 消息重复 | 使用 `mark_read=true`，不要同时运行两个消费者 |
| 画布不显示 Agent 回复 | gateway 是否调用了 `push_to_canvas(note=...)`；画布 SSE 是否已连接 |
| 401 | `/mcp`、`/workspace/*` 使用相同 Bearer token |
