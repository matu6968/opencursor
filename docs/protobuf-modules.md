# Bundled protobuf modules (index)

The ESM bundle `dist/esm/index.js` inlines webpack modules. Line ranges below match [`discoveries.md`](../../discoveries.md) in this repo (file: `index-full.js` when unpacked for analysis).

## `agent/v1` — agent runtime, tools, exec

| Source (bundle path) | Approx. lines | Role |
|----------------------|----------------|------|
| `agent/v1/agent_pb.js` | 74131–80039 | Core agent messages, `AgentClientMessage`, `AgentServerMessage`, run request, interaction updates |
| `agent/v1/agent_skills_pb.js` | 80040–80106 | Skills descriptors |
| `agent/v1/ai_attribution_tool_pb.js` | 80107–80241 | Tool protos |
| `agent/v1/apply_agent_diff_tool_pb.js` | 80242–80409 | Tool protos |
| `agent/v1/ask_question_tool_pb.js` | 80410–80661 | Tool protos |
| `agent/v1/background_shell_exec_pb.js` | 80662–80892 | Exec protos |
| `agent/v1/computer_use_tool_pb.js` | 80893–81397 | Tool protos |
| `agent/v1/create_plan_tool_pb.js` | 81398–81598 | Tool protos |
| `agent/v1/cursor_rules_pb.js` | 81599–81768 | Rules / context |
| `agent/v1/delete_exec_pb.js` | 81769–82009 | Exec |
| `agent/v1/delete_tool_pb.js` | 82010–82039 | Tool |
| `agent/v1/diagnostics_exec_pb.js` | 82040–82273 | Exec |
| `agent/v1/exec_pb.js` | 82274–82633 | Exec client/server messages |
| `agent/v1/fetch_exec_pb.js` | 82634–82740 | Exec |
| `agent/v1/generate_image_tool_pb.js` | 82741–82959 | Tool |
| `agent/v1/grep_exec_pb.js` | 82960–83313 | Exec |
| `agent/v1/grep_tool_pb.js` | 83314–83343 | Tool |
| `agent/v1/kv_pb.js` | 83344–83515 | KV client/server |
| `agent/v1/ls_exec_pb.js` | 83516–83799 | Exec |
| `agent/v1/ls_tool_pb.js` | 83800–83829 | Tool |
| `agent/v1/mcp_auth_tool_pb.js` | 83830–84059 | Tool |
| `agent/v1/mcp_exec_pb.js` | 84060–84823 | Exec |
| `agent/v1/mcp_pb.js` | 84824–85028 | MCP descriptors |
| `agent/v1/pr_management_tool_pb.js` | 85029–85332 | Tool |
| `agent/v1/read_exec_pb.js` | 85333–85557 | Exec |
| `agent/v1/read_tool_pb.js` | 85558–85732 | Tool |
| `agent/v1/record_screen_exec_pb.js` | 85733–85918 | Exec |
| `agent/v1/replace_env_tool_pb.js` | 85919–86063 | Tool |
| `agent/v1/report_bugfix_results_tool_pb.js` | 86064–86231 | Tool |
| `agent/v1/request_context_exec_pb.js` | 86232–86948 | Exec |
| `agent/v1/sandbox_pb.js` | 86949–87082 | Sandbox policy |
| `agent/v1/selected_context_pb.js` | 87083–88494 | IDE / context selection |
| `agent/v1/setup_vm_environment_tool_pb.js` | 88495–88591 | Tool |
| `agent/v1/shell_exec_pb.js` | 88592–89357 | Exec |
| `agent/v1/shell_tool_pb.js` | 89358–89455 | Tool |
| `agent/v1/subagent_exec_pb.js` | 89456–89847 | Exec |
| `agent/v1/subagents_pb.js` | 89848–90172 | Subagents |
| `agent/v1/switch_mode_tool_pb.js` | 90173–90406 | Tool |
| `agent/v1/todo_tool_pb.js` | 90407–90700 | Tool |
| `agent/v1/utils_pb.js` | 90701–90781 | Shared utils messages |
| `agent/v1/web_fetch_tool_pb.js` | 90782–91024 | Tool |
| `agent/v1/web_search_tool_pb.js` | 91025–91281 | Tool |
| `agent/v1/write_exec_pb.js` | 91282–91476 | Exec |

## `aiserver/v1` — dashboard, chat, repository, privacy

| Source | Approx. lines | Role |
|--------|----------------|------|
| `aiserver/v1/chat_pb.js` | 91477–104333 | Chat-related messages |
| `aiserver/v1/dashboard_connect.js` | 104334–105901 | **Connect service** `aiserver.v1.DashboardService` |
| `aiserver/v1/dashboard_pb.js` | 105902–133355 | Dashboard RPC payloads |
| `aiserver/v1/privacy_mode_pb.js` | 133356–133374 | Privacy enum for `x-ghost-mode` derivation |
| `aiserver/v1/repository_pb.js` | 133375–136207 | Repository messages |
| `aiserver/v1/utils_pb.js` | 136208–138617 | Shared aiserver utils |

## Chunk `642.index-full.js`

| Source | Lines | Role |
|--------|-------|------|
| `./src/agent/cloud-agent.ts` | 4–936 | Cloud `Agent` / `Run`, SSE loop |

## Other inlined workspace packages (non-proto)

See `discoveries.md` for `./src/agent/*.ts`, `agent-kv`, `context`, `cursor-sdk-local-runtime`, `cursor-sdk-shared`, `metrics`, `utils`.
