# Statement of Work (SOW)
## Basivo — AI Orchestrator Agent Platform

**Document purpose:** This SOW is written to be used as grounding context for an AI coding agent (e.g. Claude Code) building this product. It defines scope, architecture direction, node taxonomy, and API contracts precisely enough to generate implementation plans from.

---

## 1. Product Summary

Basivo is an AI workflow orchestrator positioned against n8n, Flowise, and similar tools. Unlike competitors, which expose many low-level primitive nodes (HTTP request, parse JSON, set variable, loop, etc.), Basivo exposes a small set of **high-level "outcome" nodes** that each internally handle what would otherwise require chaining 10+ primitive nodes. Complexity is hidden inside well-engineered nodes, not offloaded onto the user as wiring work.

### Core differentiators
1. **End-to-end pipeline observability** — every workflow run is logged at the node level with enough structure to produce analysis and improvement suggestions (e.g. failure clustering, latency hotspots, cost breakdown per node).
2. **Capability-level nodes, not operation-level nodes** — a Code Agent node that takes a ticket and produces a verified, committed PR; a Voice node that runs a full LiveKit session; an Agent node with built-in tool use, memory, and retries — each as a single configurable block.
3. **Dual-mode workflow consumption** — every workflow is callable externally both as a streaming (SSE) endpoint and a standard HTTP request/response endpoint, so it can be embedded in a frontend or called from any backend.

---

## 2. Node Architecture

### Tier 1 — Utility / Infrastructure Nodes (minimal, familiar, not a selling point)
Keep this set small (6-8 nodes). These should follow conventions users already know from n8n/Zapier — no need to innovate here.

| Node | Purpose |
|---|---|
| Webhook Trigger | Start a workflow via inbound HTTP call |
| Scheduler Trigger | Cron/interval-based trigger |
| Manual Trigger | Run on demand from UI or API |
| HTTP Request | Generic outbound call for cases not covered by a capability node |
| Condition / Router | Basic branching logic |
| Variable / Set | Pass and transform data between nodes |

### Tier 2 — Capability Nodes (the product's differentiation)
Each of these is a single node in the UI but is backed internally by a multi-step, opinionated pipeline that the team maintains and instruments.

| Node | What it does internally |
|---|---|
| **Agent Node** | Full reasoning agent: LLM call → tool selection → tool execution → retry/error handling → memory update, exposed as one configurable block |
| **Code Agent Node** | Ingests a ticket/spec → generates code → runs tests/lint → (optional) human approval gate → commits/opens PR on the target repo |
| **Voice Node** | Manages a full LiveKit session (connect, stream, transcribe, respond) as one block |
| **Data/Integration Node** | Handles auth + fetch + transform for a connected external service via configuration, not manual chaining |
| **Observer Node** | Attaches to any flow to capture structured logs and feed the analysis/insights layer natively |
| **Memory Node** | Persistent, queryable memory (short-term/session + long-term/vector store) that any node in the flow can read from or write to — shared context across runs, not just within a single agent call |

**Escape hatch:** Capability nodes should support "expand" — power users can drill into the underlying steps of a node to override a sub-step when the default doesn't fit, without forcing that complexity on everyone by default.

---

## 3. Observability & Analysis Layer

- Every node execution emits structured logs: `{ node_id, node_type, status, input_summary, output_summary, duration_ms, error, timestamp }`.
- Logs roll up per workflow run and across runs, enabling:
  - Failure clustering (e.g. "Code Agent node fails 30% of the time on tickets mentioning migrations")
  - Latency/cost hotspot detection per node type
  - Suggested pipeline improvements surfaced to the user, not just raw logs
- This layer is a first-class product feature (the "Observer Node" above), not an afterthought bolted onto execution.

---

## 4. External Workflow Access — API Contract

Every workflow, once published, is callable externally via two modes:

### Option 1 — Streaming (for frontends)
- `POST /flows/{id}/run/stream`
- Transport: **Server-Sent Events (SSE)** (preferred over WebSockets for one-directional status push; broadly supported by frontend frameworks)
- Emits one event per node lifecycle transition:
  ```json
  { "node": "Code Agent", "status": "running", "progress": "Generating patch...", "timestamp": "..." }
  ```
- Final event carries the completed result payload.

### Option 2 — Standard request/response (for backend-to-backend)
- `POST /flows/{id}/run` — blocking call, returns final result once the workflow completes.
- `POST /flows/{id}/run` (async variant) — returns `202 Accepted` + `run_id` immediately.
- `GET /flows/{id}/runs/{run_id}` — polling endpoint for callers that can't hold a long-lived connection (serverless functions, cron-triggered jobs, etc.).

### Cross-mode requirement
A single execution (`run_id`) must be attachable from either mode — a caller can kick off a run via Option 2 and later attach to its live event stream via Option 1 to check in on an in-progress run.

---

## 5. Risk Areas / Guardrails to Design For

- **Auto-commit risk:** The Code Agent Node's commit step is the highest-risk feature in the product. It must not ship without: passing test-suite gate, lint gate, and a configurable human-approval step before merge, plus rollback capability.
- **Scope creep risk:** Agent Node, Code Agent Node, Voice Node, and Observability are each substantial standalone products in the market. The build should sequence these rather than attempt all four simultaneously — see Section 6.
- **Positioning risk:** Avoid claiming competitors "can't" do these things — n8n/Flowise can approximate parts of this via custom/community nodes. The differentiation claim should be "first-class, reliable, pre-built capability" vs. "DIY assembly required," not "impossible elsewhere."

---

## 6. Suggested Build Sequencing (for scoping, not final)

1. **Foundation:** Tier 1 utility nodes + workflow engine + Observer Node/logging layer + dual-mode API (Sections 2 Tier 1, 3, 4)
2. **Wedge feature:** Pick one Tier 2 capability node as the initial differentiator (Agent Node or Code Agent Node) and ship it with guardrails
3. **Expansion:** Add remaining capability nodes (Voice, additional Data/Integration nodes) once core loop is validated

*(This sequencing is a starting hypothesis — confirm against actual go-to-market priorities before finalizing.)*

---

## 7. Open Questions to Resolve Before Implementation

- Exact list and priority order of Data/Integration nodes to support at launch
- Approval-gate UX for Code Agent Node auto-commits (who approves, where, timeout behavior)
- Pricing/tiering model and how it maps to node usage (e.g. does Code Agent Node consume more credits than Agent Node?)
- Multi-tenancy and auth model for externally-exposed workflow endpoints
- Memory Node scope: per-workflow vs. per-user vs. per-organization memory namespaces, retention/expiry policy, and choice of vector store backend
