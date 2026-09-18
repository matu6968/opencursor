# `agent.v1.AgentService` (Connect-RPC)

**Type name:** `agent.v1.AgentService`

Defined in the generated Connect schema embedded in `dist/esm/index-full.js` (search for `typeName: "agent.v1.AgentService"`).

| RPC | Kind | Request message | Response message |
|-----|------|-----------------|-------------------|
| `Run` | **BiDi streaming** | `agent.v1.AgentClientMessage` | `agent.v1.AgentServerMessage` |
| `RunSSE` | Server streaming | `aiserver.v1.BidiRequestId` | `agent.v1.AgentServerMessage` |
| `RunPoll` | Server streaming | `agent.v1.BidiPollRequest` | `agent.v1.BidiPollResponse` |
| `NameAgent` | Unary | `agent.v1.NameAgentRequest` | `agent.v1.NameAgentResponse` |
| `UpdateConversationMetadata` | Unary | `agent.v1.UpdateConversationMetadataRequest` | `agent.v1.UpdateConversationMetadataResponse` |
| `CreateTranscriptOverview` | Unary | `agent.v1.CreateTranscriptOverviewRequest` | `agent.v1.CreateTranscriptOverviewResponse` |
| `GetUsableModels` | Unary | `agent.v1.GetUsableModelsRequest` | `agent.v1.GetUsableModelsResponse` |
| `GetDefaultModelForCli` | Unary | `agent.v1.GetDefaultModelForCliRequest` | `agent.v1.GetDefaultModelForCliResponse` |
| `GetAllowedModelIntents` | Unary | `agent.v1.GetAllowedModelIntentsRequest` | `agent.v1.GetAllowedModelIntentsResponse` |
| `UploadConversationBlobs` | Unary | `agent.v1.UploadConversationBlobsRequest` | `agent.v1.UploadConversationBlobsResponse` |
| `NotifyConversationClone` | Unary | `agent.v1.NotifyConversationCloneRequest` | `agent.v1.NotifyConversationCloneResponse` |
| `GetNewChatNudgeLegacyModelPicker` | Unary | `agent.v1.GetNewChatNudgeLegacyModelPickerRequest` | `agent.v1.GetNewChatNudgeLegacyModelPickerResponse` |
| `GetNewChatNudgeParameterizedModelPicker` | Unary | `agent.v1.GetNewChatNudgeParameterizedModelPickerRequest` | `agent.v1.GetNewChatNudgeParameterizedModelPickerResponse` |

## `AgentClientMessage` / `AgentServerMessage` (envelope)

`AgentClientMessage` `oneof message` (field numbers from bundle):

| Field | Message |
|-------|---------|
| `run_request` | `agent.v1.AgentRunRequest` |
| `exec_client_message` | from `exec_pb` |
| `exec_client_control_message` | from `exec_pb` |
| `kv_client_message` | from `kv_pb` |
| `conversation_action` | from `agent_pb` |
| `interaction_response` | from `agent_pb` |
| `client_heartbeat` | `agent.v1.ClientHeartbeat` |
| `prewarm_request` | `agent.v1.PrewarmRequest` |

`AgentServerMessage` `oneof message`:

| Field | Message |
|-------|---------|
| `interaction_update` | `agent.v1.InteractionUpdate` |
| `exec_server_message` | from `exec_pb` |
| `exec_server_control_message` | `agent.v1.ExecServerControlMessage` |
| `conversation_checkpoint_update` | from `agent_pb` |
| `kv_server_message` | from `kv_pb` |
| `interaction_query` | from `agent_pb` |

These envelopes are what the **local** agent runtime drives over Connect; the stage-1 Python cloud client uses REST+SSE instead.

`Run` is the HTTP/2 bidi path (`application/connect+proto`). When HTTP/2 is unavailable (plain `http:` URL, `local.useHttp1ForAgent`, `CURSOR_USE_HTTP1`, `GetServerConfig.http2_config` force-disable, missing `h2`, or a failed negotiation), the client uses `RunSSE` plus unary `BidiAppend` instead. Classic gRPC (`application/grpc`) is not used.

## Local `Run` flow

The local executor writes an initial `AgentClientMessage` before reading server messages:

1. `run_request` with `AgentRunRequest`, including a present `ConversationStateStructure` (`mode = AGENT_MODE_AGENT` on the first turn; later the last checkpoint), a `ConversationAction`, requested model details, MCP tools, conversation IDs, selected context, and optional pre-fetched blobs.
2. `client_heartbeat` every ~5 seconds while the stream is active.
3. `interaction_response` when the server sends an interaction query that needs a local answer.
4. `exec_client_message` / `exec_client_control_message` and `kv_client_message` from local exec and KV controllers.
5. Mid-turn `conversation_action.inject_context_action` for `Run.steer(text)`: `injection_id`, `expected_run_id` (the in-flight generation UUID / Connect `x-request-id`), and `user_context.user_message`. Empty or whitespace text, or a turn that is not live, does not send; the call returns `revert_to_followup`.

Server messages are split into independent handlers:

| Server branch | Local handling |
|---------------|----------------|
| `interaction_update` | Converted into `InteractionUpdate` JSON and then into public `SDKMessage` values when applicable. `context_injection_state` acks a pending `Run.steer`: `queued` keeps waiting (clears the 15s timer; never returned as a public outcome), `delivered` → `complete_delivered`, `queued_for_next_turn` / `cancelled` / `rejected` / timeout / turn end → `revert_to_followup`. |
| `exec_server_message` / `exec_server_control_message` | Drives local shell, grep, file, MCP, subagent, and related tool executors. |
| `conversation_checkpoint_update` | Persists conversation checkpoints through the blob/checkpoint store. |
| `kv_server_message` | Reads/writes local agent KV blobs. |
| `interaction_query` | Produces `interaction_response` messages. |

The public stream is not the raw protobuf stream. It is the decoded `SDKMessage` stream documented in [../messages.md](../messages.md) and persisted through the local run-event envelope documented in [../local-runtime.md](../local-runtime.md).
