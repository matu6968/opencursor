# `aiserver.v1.AnalyticsService` (Connect-RPC)

**Type name:** `aiserver.v1.AnalyticsService`

Embedded in `dist/esm/index-full.js` (search `typeName: "aiserver.v1.AnalyticsService"`).

All methods are **unary**.

| RPC | Request | Response |
|-----|---------|----------|
| `TrackEvents` | `aiserver.v1.TrackEventsRequest` | `aiserver.v1.TrackEventsResponse` |
| `Batch` | `aiserver.v1.BatchRequest` | `aiserver.v1.BatchResponse` |
| `BootstrapStatsig` | `aiserver.v1.BootstrapStatsigRequest` | `aiserver.v1.BootstrapStatsigResponse` |
| `SubmitLogs` | `aiserver.v1.SubmitLogsRequest` | `aiserver.v1.SubmitLogsResponse` |
| `IngestConversation` | `aiserver.v1.IngestConversationRequest` | `aiserver.v1.IngestConversationResponse` |
| `UploadIssueTrace` | `aiserver.v1.UploadIssueTraceRequest` | `aiserver.v1.UploadIssueTraceResponse` |
| `DownloadIssueTraces` | `aiserver.v1.DownloadIssueTracesRequest` | `aiserver.v1.DownloadIssueTracesResponse` |

Supporting message types in the same bundle module include `aiserver.v1.AnalyticsEvent`, `aiserver.v1.BatchEvent`, `aiserver.v1.AnalyticsContext`, `aiserver.v1.ClientLogEntry`, etc.

The official SDK wires this service for internal telemetry (e.g. `sdk.run.created` / `sdk.run.completed`); the stage-1 Python port does not call it.
