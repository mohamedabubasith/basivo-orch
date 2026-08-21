/**
 * The flow builder.
 *
 * Three commitments shape this screen.
 *
 * **Saving is explicit.** An autosaving canvas sounds friendlier and is worse
 * here: a flow is a thing other systems call, and a half-dragged graph written
 * to the server is a production pipeline in a state nobody chose. The draft
 * lives in the browser until saved, and the header says so.
 *
 * **Validation belongs to the engine.** `POST …/validate` runs the same
 * `validate_graph` the executor runs, and its problems are pinned to the nodes
 * they name. Re-implementing those rules in TypeScript would produce a second
 * opinion, and the second opinion is always the wrong one.
 *
 * **A test run reports what happened, not that it was requested.** Node results
 * come back from the run itself and are painted onto the canvas, and the event
 * timeline underneath is the real sequence with real durations and retries.
 */

import { motion } from "motion/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Link,
  Link as RouterLink,
  useNavigate,
  useParams,
} from "react-router-dom";
import {
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  addEdge,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type NodeChange,
  MarkerType,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { ApiError, api } from "../../lib/api";
import { cx } from "../../lib/cx";
import { loadConfig } from "../../lib/config";
import { WorkspaceProvider, useWorkspace } from "../../lib/workspace";
import { ThemeToggle } from "../../components/ThemeToggle";
import { Alert, Button, PageLoader, Spinner } from "../../components/ui";
import { FlowNodeCard } from "../../builder/FlowNodeCard";
import { NodeIconChip, nodeAccent } from "../../builder/nodeIcons";
import { Inspector } from "../../builder/Inspector";
import { TestRunPanel } from "../../builder/TestRunPanel";
import {
  attachProblems,
  edgeId,
  makeNodeId,
  toCanvas,
  toGraph,
  type FlowNode,
  type Graph,
} from "../../builder/graph";
import { buildSuggestions } from "../../builder/suggestions";
import {
  groupSpecs,
  initialConfig,
  loadSpecs,
  type NodeSpec,
} from "../../builder/specs";
import { duration } from "./bits";

const NODE_TYPES = { basivo: FlowNodeCard };

/** Kept in one place: the layout maths and the components must agree. */
const INSPECTOR_WIDTH = 340;
const NODE_WIDTH = 248;

interface FlowDetail {
  id: string;
  name: string;
  slug: string;
  description: string | null;
  published_version_id: string | null;
  graph: Graph;
  version: number;
  /** When the scheduler fires this next. Null unless it is scheduled and published. */
  next_run_at: string | null;
}

interface NodeExecution {
  node_id: string;
  node_type: string;
  status: "pending" | "running" | "succeeded" | "failed" | "skipped";
  attempt: number;
  duration_ms: number | null;
  error: string | null;
  started_at: string | null;
}

interface RunDetail {
  id: string;
  status: string;
  error: string | null;
  duration_ms: number | null;
  nodes: NodeExecution[];
}

/** What `POST /run?mode=async` answers with: the run exists, watch it here. */
interface RunAccepted {
  run_id: string;
  status: string;
}

const TERMINAL_RUN_STATUSES = new Set(["succeeded", "failed", "cancelled"]);

/**
 * Follow a detached run to its end, reporting every snapshot on the way.
 *
 * Polling rather than SSE: this goes through the api client, so an access
 * token that expires during a long agent run is refreshed like any other
 * request. An EventSource cannot carry that refresh, and the run it was
 * watching would silently stop updating — the exact failure this replaced.
 */
async function pollRun(
  url: string,
  onSnapshot: (run: RunDetail) => void,
  onStuckInQueue: () => void,
): Promise<RunDetail> {
  const INTERVAL_MS = 1200;
  // A run stays QUEUED until a worker claims it, which normally takes under a
  // second. Much longer than that means no worker is listening, and the
  // honest thing is to say so rather than spin: "nothing is happening" is
  // otherwise indistinguishable from "your flow is slow".
  const QUEUE_PATIENCE_MS = 8000;
  const startedAt = Date.now();
  let warned = false;

  for (;;) {
    const snapshot = await api.get<RunDetail>(url);
    onSnapshot(snapshot);
    if (TERMINAL_RUN_STATUSES.has(snapshot.status)) return snapshot;
    if (
      !warned &&
      snapshot.status === "queued" &&
      Date.now() - startedAt > QUEUE_PATIENCE_MS
    ) {
      warned = true;
      onStuckInQueue();
    }
    await new Promise((resolve) => setTimeout(resolve, INTERVAL_MS));
  }
}

/**
 * The builder owns the whole viewport.
 *
 * It was inside the app shell, which meant a canvas boxed into the same
 * `max-w-6xl` column as a settings form, with a sidebar eating 264px of the
 * one axis a graph needs most. A builder is a workspace, not a page: it gets
 * the screen, and its own compact bar carries the way back.
 */
export function Builder() {
  return (
    <WorkspaceProvider>
      <ReactFlowProvider>
        <BuilderInner />
      </ReactFlowProvider>
    </WorkspaceProvider>
  );
}

function BuilderInner() {
  const { flowId } = useParams<{ flowId: string }>();
  const navigate = useNavigate();
  const { orgId } = useWorkspace();
  const base = `/api/v1/orgs/${orgId}/flows/${flowId}`;

  const [flow, setFlow] = useState<FlowDetail | null>(null);
  const [specs, setSpecs] = useState<NodeSpec[] | null>(null);
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<
    null | "save" | "validate" | "publish" | "run"
  >(null);
  const [banner, setBanner] = useState<{
    tone: "ok" | "bad";
    text: string;
  } | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [testPanelOpen, setTestPanelOpen] = useState(false);
  const [testInput, setTestInput] = useState<string>("");
  const [endpointsOpen, setEndpointsOpen] = useState(false);
  const [publicBase, setPublicBase] = useState<string>("");
  //: Seconds since the current run started, so a multi-minute agent flow shows
  //: movement instead of an unexplained spinner.
  const [elapsed, setElapsed] = useState(0);

  /** Paint each canvas node with its status from a run snapshot. */
  const paintNodes = useCallback(
    (snapshot: RunDetail) => {
      const byId = new Map(
        snapshot.nodes.map((execution) => [execution.node_id, execution]),
      );
      setNodes((current) =>
        current.map((node) => {
          const execution = byId.get(node.id);
          return {
            ...node,
            data: {
              ...node.data,
              runStatus:
                execution && execution.status !== "pending"
                  ? (execution.status as FlowNode["data"]["runStatus"])
                  : undefined,
              runDetail: execution ? detailFor(execution) : undefined,
            },
          };
        }),
      );
    },
    [setNodes],
  );

  useEffect(() => {
    void loadConfig().then((config) =>
      setPublicBase(config.public_base_url ?? ""),
    );
  }, []);

  // The payload someone crafts to exercise a flow is worth keeping — retyping
  // it on every visit is how test inputs degenerate to {}.
  useEffect(() => {
    if (!flowId) return;
    try {
      setTestInput(localStorage.getItem(`basivo.testinput.${flowId}`) ?? "{}");
    } catch {
      setTestInput("{}");
    }
  }, [flowId]);

  const canvasRef = useRef<HTMLDivElement>(null);
  const { screenToFlowPosition } = useReactFlow();

  const specMap = useMemo(
    () => new Map((specs ?? []).map((spec) => [spec.type, spec])),
    [specs],
  );

  useEffect(() => {
    if (!orgId || !flowId) return;
    let cancelled = false;
    void Promise.all([api.get<FlowDetail>(base), loadSpecs()])
      .then(([detail, specList]) => {
        if (cancelled) return;
        setFlow(detail);
        setSpecs(specList);
        const map = new Map(specList.map((spec) => [spec.type, spec]));
        const canvas = toCanvas(detail.graph, map);
        setNodes(canvas.nodes);
        setEdges(canvas.edges);
      })
      .catch(() => !cancelled && setLoadError("Could not open this flow."));
    return () => {
      cancelled = true;
    };
  }, [base, orgId, flowId, setNodes, setEdges]);

  // Leaving with unsaved work should cost a keystroke, not a flow.
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const markDirty = useCallback(() => {
    setDirty(true);
    setBanner(null);
  }, []);

  const handleNodesChange = useCallback(
    (changes: NodeChange<FlowNode>[]) => {
      onNodesChange(changes);
      // Selection and measurement are not edits. Without this filter the flow
      // is "unsaved" the moment it is opened and clicked once.
      if (
        changes.some(
          (change) => change.type !== "select" && change.type !== "dimensions",
        )
      ) {
        markDirty();
      }
    },
    [onNodesChange, markDirty],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      setEdges((current) =>
        addEdge(
          {
            ...connection,
            id: edgeId(
              connection.source,
              connection.target,
              connection.sourceHandle,
            ),
            type: "smoothstep",
            animated: true,
            style: { stroke: "var(--series)", strokeWidth: 2, opacity: 0.55 },
          },
          current,
        ),
      );
      markDirty();
    },
    [setEdges, markDirty],
  );

  /**
   * Where a click-added node lands.
   *
   * Three versions, each wrong in its own way. A fixed point stacked every
   * node at identical coordinates — the second hid the first and their handles
   * overlapped, and you cannot drag a connection between two nodes occupying
   * the same pixels. Fixed flow coordinates fixed the overlap but not the aim:
   * the canvas has been panned and zoomed and the inspector covers its right
   * third, so nodes landed off-screen or under a panel. A small stagger from
   * the viewport centre kept them on screen but still visibly overlapping.
   *
   * What a flow actually wants is a chain: each new node to the right of the
   * furthest-right one, at its height, a clear gap away. The first node goes
   * in the middle of the *visible* canvas, with the inspector's width taken
   * out of the usable area.
   */
  function nextSlot(): { x: number; y: number } {
    if (nodes.length > 0) {
      const rightmost = nodes.reduce((far, node) =>
        node.position.x > far.position.x ? node : far,
      );
      return {
        x: rightmost.position.x + NODE_WIDTH + 72,
        y: rightmost.position.y,
      };
    }

    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return { x: 140, y: 160 };
    const usableWidth = rect.width - (selected ? INSPECTOR_WIDTH : 0);
    return screenToFlowPosition({
      x: rect.left + usableWidth / 2 - NODE_WIDTH / 2,
      y: rect.top + rect.height / 2 - 40,
    });
  }

  const hasTrigger = nodes.some((node) => node.data.isTrigger);

  function addNode(spec: NodeSpec, position: { x: number; y: number }) {
    // The engine rejects a graph with more than one trigger — see
    // `validate_graph` in graph.py — because a flow needs exactly one thing
    // that decides when it runs. Refusing here means the author finds out
    // when they try to add the second one, not several clicks later when
    // Validate rejects a graph they thought was finished.
    if (spec.is_trigger && hasTrigger) {
      setBanner({
        tone: "bad",
        text: "A flow can only have one trigger. Delete the existing one first.",
      });
      return;
    }
    const id = makeNodeId(spec.type, new Set(nodes.map((node) => node.id)));
    setNodes((current) => [
      ...current,
      {
        id,
        type: "basivo",
        position,
        data: {
          label: spec.label,
          nodeType: spec.type,
          config: initialConfig(spec),
          ports: spec.ports,
          isTrigger: spec.is_trigger,
        },
      },
    ]);
    setSelected(id);
    markDirty();
  }

  function updateSelected(patch: (node: FlowNode) => FlowNode) {
    setNodes((current) =>
      current.map((node) => (node.id === selected ? patch(node) : node)),
    );
    markDirty();
  }

  function deleteSelected() {
    setNodes((current) => current.filter((node) => node.id !== selected));
    setEdges((current) =>
      current.filter(
        (edge) => edge.source !== selected && edge.target !== selected,
      ),
    );
    setSelected(null);
    markDirty();
  }

  async function save(): Promise<boolean> {
    setBusy("save");
    setBanner(null);
    try {
      const saved = await api.patch<FlowDetail>(base, {
        graph: toGraph(nodes, edges),
      });
      setFlow(saved);
      setDirty(false);
      setBanner({ tone: "ok", text: `Saved as version ${saved.version}.` });
      return true;
    } catch (err) {
      setBanner({
        tone: "bad",
        text:
          err instanceof ApiError ? err.message : "Could not save this flow.",
      });
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function validate() {
    // Validation runs server-side against the *saved* graph, so saving first is
    // not a convenience — it is what makes the answer be about what you see.
    if (dirty && !(await save())) return;
    setBusy("validate");
    try {
      // This endpoint answers 200 with `{valid, problems}` rather than raising:
      // a rejected graph is a normal answer to "is this valid?", not a failed
      // request. Treating any non-throw as success was exactly the bug this
      // had — an HTTP node with no URL reported "This flow will run", which is
      // the one thing a validate button must never get wrong.
      const result = await api.post<{ valid: boolean; problems: string[] }>(
        `${base}/validate`,
      );
      const list = result.valid ? [] : (result.problems ?? []);
      setProblems(list);
      setNodes((current) => attachProblems(current, list));
      setBanner(
        list.length === 0
          ? { tone: "ok", text: "This flow will run." }
          : {
              tone: "bad",
              text: `${list.length} problem${list.length === 1 ? "" : "s"}.`,
            },
      );
    } catch (err) {
      // Publish and run *do* raise 422 carrying the same payload, so the
      // fallback path stays.
      const list = extractProblems(err);
      setProblems(list);
      setNodes((current) => attachProblems(current, list));
      setBanner({
        tone: "bad",
        text: list.length
          ? `${list.length} problem${list.length === 1 ? "" : "s"}.`
          : err instanceof ApiError
            ? err.message
            : "Could not validate this flow.",
      });
    } finally {
      setBusy(null);
    }
  }

  async function publish() {
    if (dirty && !(await save())) return;
    setBusy("publish");
    try {
      await api.post(`${base}/publish`);
      const detail = await api.get<FlowDetail>(base);
      setFlow(detail);
      setProblems([]);
      setNodes((current) => attachProblems(current, []));
      setBanner({ tone: "ok", text: `Published version ${detail.version}.` });
    } catch (err) {
      const list = extractProblems(err);
      setProblems(list);
      setNodes((current) => attachProblems(current, list));
      setBanner({
        tone: "bad",
        text: list.length
          ? `Cannot publish: ${list.length} problem${list.length === 1 ? "" : "s"}.`
          : err instanceof ApiError
            ? err.message
            : "Could not publish.",
      });
    } finally {
      setBusy(null);
    }
  }

  async function deleteFlow() {
    if (
      !confirm(
        `Delete "${flow?.name ?? "this flow"}"? Its run history goes with it, and anything calling its published URL starts getting 404s. This cannot be undone.`,
      )
    )
      return;
    try {
      await api.del(base);
      navigate("/app/flows");
    } catch (err) {
      setBanner({
        tone: "bad",
        text:
          err instanceof ApiError ? err.message : "Could not delete this flow.",
      });
    }
  }

  async function testRun() {
    // What gets sent is exactly what the panel shows — no hidden default. A
    // run button that silently posts {} is how "for test I didn't know what
    // you were doing" happens.
    let parsed: unknown = {};
    if (testInput.trim()) {
      try {
        parsed = JSON.parse(testInput);
      } catch {
        setBanner({ tone: "bad", text: "The test input is not valid JSON." });
        return;
      }
    }
    try {
      localStorage.setItem(`basivo.testinput.${flowId}`, testInput);
    } catch {
      // Persistence is a convenience; running matters more.
    }

    if (dirty && !(await save())) return;
    setTestPanelOpen(false);
    setBusy("run");
    setRun(null);
    setElapsed(0);
    setNodes((current) =>
      current.map((node) => ({
        ...node,
        data: { ...node.data, runStatus: undefined, runDetail: undefined },
      })),
    );
    try {
      // Started detached, then polled. An agent flow runs for minutes, and a
      // request held open for all of them is a spinner that says nothing,
      // sitting under whatever proxy timeout is shortest. Polling the run
      // detail also goes through the api client, so a token refresh mid-run
      // is handled — an EventSource could not do that.
      const accepted = await api.post<RunAccepted>(`${base}/run?mode=async`, {
        input: parsed,
      });
      const startedAt = Date.now();
      const tick = window.setInterval(
        () => setElapsed(Math.round((Date.now() - startedAt) / 1000)),
        1000,
      );
      let result: RunDetail;
      try {
        result = await pollRun(
          `/api/v1/orgs/${orgId}/runs/${accepted.run_id}`,
          (snapshot) => {
            setRun(snapshot);
            paintNodes(snapshot);
          },
          () =>
            setBanner({
              tone: "bad",
              text:
                "This run is queued but nothing has picked it up: the run worker " +
                "does not look like it is running. Start it with `make worker`.",
            }),
        );
      } finally {
        window.clearInterval(tick);
      }
      setRun(result);
      paintNodes(result);
      setBanner(
        result.status === "succeeded"
          ? {
              tone: "ok",
              text: `Run succeeded in ${duration(result.duration_ms)}.`,
            }
          : { tone: "bad", text: result.error ?? `Run ${result.status}.` },
      );
    } catch (err) {
      const list = extractProblems(err);
      if (list.length) {
        setProblems(list);
        setNodes((current) => attachProblems(current, list));
      }
      setBanner({
        tone: "bad",
        text: list.length
          ? "This flow cannot run yet. See the flagged nodes."
          : err instanceof ApiError
            ? err.message
            : "Could not start the run.",
      });
    } finally {
      setBusy(null);
    }
  }

  if (loadError) return <Alert>{loadError}</Alert>;
  if (!flow || !specs) return <PageLoader label="Opening flow" />;

  const selectedNode = nodes.find((node) => node.id === selected) ?? null;
  const selectedSpec = selectedNode
    ? specMap.get(selectedNode.data.nodeType)
    : undefined;
  const selectedSuggestions = selectedNode
    ? buildSuggestions(selectedNode.id, nodes, edges, specMap)
    : [];

  return (
    <div className="flex h-dvh flex-col bg-ink-950">
      <header className="flex flex-none flex-wrap items-center gap-3 border-b border-ink-800/70 px-4 py-2.5">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2.5">
            <Link
              to="/app/flows"
              className="text-ink-500 transition-colors hover:text-ink-200"
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none">
                <path
                  d="M14 7l-5 5 5 5"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </Link>
            <h1 className="truncate text-lg font-semibold text-ink-100">
              {flow.name}
            </h1>
            <span className="rounded-md border border-ink-700 px-1.5 py-0.5 text-[0.66rem] text-ink-400">
              v{flow.version}
            </span>
            {dirty ? (
              <span className="text-xs" style={{ color: "var(--status-warn)" }}>
                Unsaved changes
              </span>
            ) : flow.published_version_id ? (
              <span className="text-xs" style={{ color: "var(--status-good)" }}>
                Published
              </span>
            ) : (
              <span className="text-xs text-ink-500">Draft</span>
            )}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <ThemeToggle compact />
          <Button
            variant="ghost"
            onClick={() => void validate()}
            loading={busy === "validate"}
          >
            Validate
          </Button>
          <div className="relative">
            {/* Not the Button's `loading` prop: it hides the label under the
                spinner to keep the width steady, which is right for a submit
                and wrong here — on a run that lasts minutes, the ticking
                count is the whole difference between "working" and "hung".
                Tabular numerals keep it from jittering instead. */}
            <Button
              variant="secondary"
              onClick={() => setTestPanelOpen((open) => !open)}
              disabled={busy === "run"}
            >
              {busy === "run" ? (
                <>
                  <Spinner className="h-3.5 w-3.5" />
                  <span className="tabular-nums">Running {elapsed}s</span>
                </>
              ) : (
                "Test run"
              )}
            </Button>
            {testPanelOpen && (
              <TestRunPanel
                value={testInput}
                onChange={setTestInput}
                onRun={() => void testRun()}
                onClose={() => setTestPanelOpen(false)}
                triggerType={
                  nodes.find((node) =>
                    node.data.nodeType.startsWith("trigger."),
                  )?.data.nodeType
                }
                running={busy === "run"}
              />
            )}
          </div>
          <Button
            variant="secondary"
            onClick={() => void save()}
            loading={busy === "save"}
            disabled={!dirty}
          >
            Save
          </Button>
          <Button onClick={() => void publish()} loading={busy === "publish"}>
            Publish
          </Button>
          {flow.published_version_id && (
            <div className="relative">
              <Button
                variant="secondary"
                onClick={() => setEndpointsOpen((open) => !open)}
              >
                Endpoints
              </Button>
              {endpointsOpen && (
                <EndpointsPanel
                  base={publicBase}
                  flowId={flow.id}
                  hasWebhookTrigger={nodes.some(
                    (node) => node.data.nodeType === "trigger.webhook",
                  )}
                  onClose={() => setEndpointsOpen(false)}
                />
              )}
            </div>
          )}
          <button
            onClick={() => void deleteFlow()}
            aria-label="Delete this flow"
            title="Delete this flow"
            className="rounded-lg border border-ink-700 p-2.5 text-ink-500 transition-colors hover:border-[var(--status-bad)] hover:text-[var(--status-bad)]"
          >
            <svg
              viewBox="0 0 24 24"
              className="h-4 w-4"
              fill="none"
              aria-hidden="true"
            >
              <path
                d="M4.5 6.5h15M9.5 6V4.8c0-.7.6-1.3 1.3-1.3h2.4c.7 0 1.3.6 1.3 1.3V6M7 6.5l.8 12a1.6 1.6 0 0 0 1.6 1.5h5.2a1.6 1.6 0 0 0 1.6-1.5l.8-12M10 10.5v6M14 10.5v6"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </div>
      </header>

      {banner && (
        <motion.div
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          className="flex-none px-4 pt-3"
        >
          <Alert tone={banner.tone === "ok" ? "success" : undefined}>
            {banner.text}
            {problems.length > 0 && (
              <ul className="mt-2 list-disc space-y-1 pl-4 text-xs">
                {problems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            )}
          </Alert>
        </motion.div>
      )}

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <Palette
          specs={specs}
          hasTrigger={hasTrigger}
          onAdd={(spec) => addNode(spec, nextSlot())}
        />

        <div
          ref={canvasRef}
          className="relative flex min-w-0 flex-1 flex-col"
          onDragOver={(event) => {
            event.preventDefault();
            event.dataTransfer.dropEffect = "move";
          }}
          onDrop={(event) => {
            event.preventDefault();
            const type = event.dataTransfer.getData("application/basivo-node");
            const spec = specMap.get(type);
            if (!spec) return;
            // addNode itself refuses a second trigger, so a drag that gets
            // this far still cannot land one — this only stops the drop from
            // silently doing nothing when the palette already blocked the drag.
            addNode(
              spec,
              screenToFlowPosition({ x: event.clientX, y: event.clientY }),
            );
          }}
        >
          <div className="relative min-h-0 flex-1">
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={NODE_TYPES}
              onNodesChange={handleNodesChange}
              onEdgesChange={(changes) => {
                onEdgesChange(changes);
                markDirty();
              }}
              onConnect={onConnect}
              onNodeClick={(_, node) => setSelected(node.id)}
              onPaneClick={() => setSelected(null)}
              fitView
              // Without a cap, opening a flow with one node zooms that node to
              // fill the viewport — the graph looks broken and the text renders
              // at poster size. `fitView` optimises for filling space; a builder
              // wants a readable, familiar scale.
              fitViewOptions={{ maxZoom: 1, padding: 0.35 }}
              minZoom={0.25}
              maxZoom={1.75}
              defaultEdgeOptions={{
                type: "smoothstep",
                animated: true,
                // The series colour rather than a neutral grey: this line *is*
                // the thing carrying data from one node to the next, so it gets
                // the hue reserved for identity rather than chrome.
                // 2px at 55% opacity was a hairline nobody could see against
                // the grid, let alone grab. An edge is the data path; it should
                // be the second most visible thing on the canvas after a node.
                style: {
                  stroke: "var(--series)",
                  strokeWidth: 2.5,
                  opacity: 0.9,
                },
                markerEnd: {
                  type: MarkerType.ArrowClosed,
                  color: "var(--series)",
                  width: 18,
                  height: 18,
                },
              }}
              // Not a `bg-*` utility: @xyflow/react's own stylesheet puts an
              // explicit `background-color: var(--xy-background-color, ...)`
              // directly on `.react-flow` — the exact same class a Tailwind
              // utility here would target, so it was a specificity tie, and
              // that library's CSS is injected (as part of this route's lazy
              // chunk) after Tailwind's, winning ties by source order. Their
              // own rule already reads `--xy-background-color` first, which is
              // the supported override point, so setting that custom property
              // wins without a specificity fight — confirmed by the fact that a
              // competing class here rendered pure white in light mode with no
              // visible error.
              // Same lesson as the background, applied to the rest of the
              // chrome: @xyflow/react styles its minimap and controls from its
              // own injected stylesheet, which loads after Tailwind and wins
              // ties by source order. Classes on those components lost silently
              // and rendered white boxes on a white canvas. These custom
              // properties are the library's supported override point.
              style={
                {
                  // The names end in `-default` — that is the whole reason the
                  // first attempt at this changed nothing and the minimap stayed
                  // a white box on a grey canvas. `base.css` declares
                  // `--xy-<thing>-default` and each rule reads
                  // `var(--xy-<thing>-props, var(--xy-<thing>, var(--xy-<thing>-default)))`.
                  "--xy-background-color-default": "var(--canvas-bg)",
                  // Not ink-850: that is near-white in light mode, so the map
                  // was a white box on a pale canvas. The canvas colour plus a
                  // border reads as a recessed inset in both themes.
                  "--xy-minimap-background-color-default": "var(--canvas-bg)",
                  "--xy-minimap-mask-background-color-default":
                    "color-mix(in oklab, var(--canvas-bg) 62%, transparent)",
                  "--xy-minimap-node-background-color-default":
                    "var(--color-ink-500)",
                  "--xy-edge-stroke-default": "var(--series)",
                  "--xy-edge-stroke-width-default": "2.5",
                  "--xy-handle-background-color-default":
                    "var(--color-ink-400)",
                  "--xy-connectionline-stroke-default":
                    "var(--color-brand-400)",
                  "--xy-controls-button-background-color-default":
                    "var(--color-ink-850)",
                  "--xy-controls-button-background-color-hover-default":
                    "var(--color-ink-800)",
                  "--xy-controls-button-color-default": "var(--color-ink-300)",
                  "--xy-controls-button-color-hover-default":
                    "var(--color-brand-300)",
                  "--xy-controls-button-border-color-default": "var(--edge)",
                  // The grid, painted here rather than with <Background>.
                  // That component renders inside `.react-flow__viewport`, which
                  // is the element the pan/zoom transform is applied to — so its
                  // squares scaled with the zoom. Drawn on the outer element the
                  // grid is a fixed surface the graph moves over, which is what
                  // a drafting surface behaves like.
                  backgroundImage: `
                  linear-gradient(to right,
                    color-mix(in oklab, var(--canvas-line) 55%, transparent) 1px,
                    transparent 1px),
                  linear-gradient(to bottom,
                    color-mix(in oklab, var(--canvas-line) 55%, transparent) 1px,
                    transparent 1px)
                `,
                  backgroundSize: "26px 26px",
                } as React.CSSProperties
              }
              className="[&_.react-flow__attribution]:!bg-transparent [&_.react-flow__attribution]:!text-ink-600 [&_.react-flow__attribution_a]:!text-ink-600"
            >
              {/* Lines, not dots — a boxed grid reads as a drafting surface,
                and it is what every peer tool trains people to expect. */}
              <Controls
                showInteractive={false}
                className="!overflow-hidden !rounded-xl !border !border-ink-700/70 !bg-ink-850/90 !shadow-lg !backdrop-blur [&_button]:!h-8 [&_button]:!w-8 [&_button]:!border-0 [&_button]:!border-b [&_button]:!border-ink-700/60 [&_button]:!bg-transparent [&_button]:!fill-ink-300 [&_button:hover]:!bg-ink-800 [&_button:hover]:!fill-brand-300 [&_button:last-child]:!border-b-0"
              />
              {/* Only once there is a graph worth navigating. On a three-node
                flow the map is an empty box with two dashes in it — chrome
                that has not earned its corner of the canvas. */}
              {nodes.length >= 6 && (
                <MiniMap
                  pannable
                  zoomable
                  ariaLabel="Flow overview"
                  nodeStrokeWidth={0}
                  nodeBorderRadius={4}
                  nodeColor={(node) =>
                    nodeAccent(String(node.data?.nodeType ?? ""))
                  }
                  maskColor="color-mix(in oklab, var(--canvas-bg) 72%, transparent)"
                  className="!bottom-4 !rounded-xl !border !border-ink-700/70 !bg-ink-850/85 !shadow-lg !backdrop-blur"
                  style={{ width: 168, height: 108 }}
                />
              )}
            </ReactFlow>

            {nodes.length === 0 && (
              <div className="pointer-events-none absolute inset-0 grid place-items-center">
                <div className="text-center">
                  <p className="text-ink-300">
                    Drag a trigger onto the canvas to begin.
                  </p>
                  <p className="mx-auto mt-1.5 max-w-xs text-xs leading-relaxed text-ink-500">
                    Every flow needs exactly one trigger (the thing that decides
                    when it runs), and at least one node after it.
                  </p>
                </div>
              </div>
            )}
          </div>

          {run && (
            <RunSummary
              run={run}
              elapsed={elapsed}
              onClose={() => setRun(null)}
            />
          )}
        </div>

        {selectedNode && selectedSpec && (
          <Inspector
            spec={selectedSpec}
            name={selectedNode.data.label}
            config={selectedNode.data.config}
            problem={selectedNode.data.problem}
            orgId={orgId}
            flowId={flow.id}
            publicBase={publicBase}
            isPublished={Boolean(flow.published_version_id)}
            nextRunAt={flow.next_run_at}
            suggestions={selectedSuggestions}
            onRename={(name) =>
              updateSelected((node) => ({
                ...node,
                data: { ...node.data, label: name },
              }))
            }
            onChange={(config) =>
              updateSelected((node) => ({
                ...node,
                data: { ...node.data, config },
              }))
            }
            onDelete={deleteSelected}
            onClose={() => setSelected(null)}
          />
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------- endpoints --- */

/**
 * The production URLs a published flow answers on — printed, not implied.
 *
 * Publishing used to end with "Published version N." and nothing else: the
 * whole point of publishing is that some other system can now call this flow,
 * and the UI never said where or how. This is that answer, copyable: the
 * blocking endpoint, the SSE stream, the auth header, and a curl that works
 * once a real key is pasted in.
 */
function EndpointsPanel({
  base,
  flowId,
  hasWebhookTrigger,
  onClose,
}: {
  base: string;
  flowId: string;
  hasWebhookTrigger: boolean;
  onClose: () => void;
}) {
  const runUrl = `${base}/flows/${flowId}/run`;
  const streamUrl = `${base}/flows/${flowId}/run/stream`;
  const hookUrl = `${base}/hooks/${flowId}`;
  const curl = [
    `curl -X POST ${runUrl} \\`,
    `  -H "Authorization: Bearer bsv_YOUR_API_KEY" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '{"input": {"name": "Ada"}}'`,
  ].join("\n");

  return (
    <div className="surface absolute top-full right-0 z-30 mt-2 w-[440px] rounded-2xl p-4 shadow-xl shadow-black/40">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-ink-100">Call this flow</p>
          <p className="mt-1 text-xs leading-relaxed text-ink-500">
            Runs the <em>published</em> version. Authenticate with an{" "}
            <RouterLink
              to="/app/api-keys"
              className="text-brand-400 underline decoration-dotted underline-offset-2"
            >
              API key
            </RouterLink>{" "}
            in <code className="text-ink-400">Authorization: Bearer</code> or{" "}
            <code className="text-ink-400">X-API-Key</code>.
          </p>
        </div>
        <button
          onClick={onClose}
          aria-label="Close endpoints"
          className="rounded-lg p-1.5 text-ink-500 transition-colors hover:bg-ink-800 hover:text-ink-200"
        >
          <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none">
            <path
              d="M6 6l12 12M18 6L6 18"
              stroke="currentColor"
              strokeWidth="1.8"
            />
          </svg>
        </button>
      </div>

      <div className="mt-4 space-y-3">
        <CopyRow label="Run (blocking)" value={runUrl} />
        <CopyRow label="Run (SSE stream)" value={streamUrl} />
        {hasWebhookTrigger && (
          <div>
            <CopyRow label="Inbound hook (no API key)" value={hookUrl} />
            <p className="mt-1 text-[0.68rem] leading-relaxed text-ink-500">
              For senders that can't add headers of their own. Paste it into
              GitHub or GitLab webhook settings with the trigger's secret. The
              secret authenticates each delivery, so it only answers when the
              trigger has <em>Require signature</em> on.
            </p>
          </div>
        )}
        <div>
          <p className="mb-1 text-[0.68rem] font-medium text-ink-400">
            Example
          </p>
          <div className="relative">
            <pre className="overflow-x-auto rounded-lg border border-ink-700/70 bg-ink-950/60 p-3 font-mono text-[0.7rem] leading-relaxed text-ink-300">
              {curl}
            </pre>
            <CopyButton value={curl} className="absolute top-2 right-2" />
          </div>
        </div>
      </div>
    </div>
  );
}

function CopyRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="mb-1 text-[0.68rem] font-medium text-ink-400">{label}</p>
      <div className="flex items-center gap-1.5">
        <code className="min-w-0 flex-1 truncate rounded-lg border border-ink-700/70 bg-ink-950/60 px-2.5 py-2 font-mono text-[0.72rem] text-ink-200">
          {value}
        </code>
        <CopyButton value={value} />
      </div>
    </div>
  );
}

function CopyButton({
  value,
  className = "",
}: {
  value: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      aria-label="Copy"
      title="Copy"
      onClick={() => {
        void navigator.clipboard?.writeText(value);
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      }}
      className={cx(
        "flex-none rounded-lg border border-ink-700 p-2 text-ink-400 transition-colors hover:border-brand-400 hover:text-ink-100",
        className,
      )}
    >
      {copied ? (
        <svg
          viewBox="0 0 24 24"
          className="h-3.5 w-3.5"
          fill="none"
          aria-hidden="true"
        >
          <path
            d="M5 12.5 10 17.5 19 7"
            stroke="var(--status-good)"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      ) : (
        <svg
          viewBox="0 0 24 24"
          className="h-3.5 w-3.5"
          fill="none"
          aria-hidden="true"
        >
          <rect
            x="9"
            y="9"
            width="11"
            height="11"
            rx="2"
            stroke="currentColor"
            strokeWidth="1.6"
          />
          <path
            d="M5 15V6a2 2 0 0 1 2-2h9"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
          />
        </svg>
      )}
    </button>
  );
}

/* --------------------------------------------------------------- palette --- */

function Palette({
  specs,
  hasTrigger,
  onAdd,
}: {
  specs: NodeSpec[];
  hasTrigger: boolean;
  onAdd: (spec: NodeSpec) => void;
}) {
  const [query, setQuery] = useState("");
  // Fifteen node types is past the point where scanning works. Matching on
  // label AND type means both "video" and "social.post" find the right thing,
  // which are the two ways people actually search a palette.
  const matching = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return specs;
    return specs.filter(
      (spec) =>
        spec.label.toLowerCase().includes(needle) ||
        spec.type.toLowerCase().includes(needle) ||
        spec.description.toLowerCase().includes(needle),
    );
  }, [specs, query]);
  const groups = useMemo(() => groupSpecs(matching), [matching]);

  return (
    <div className="flex w-[228px] flex-none flex-col border-r border-ink-800/70 bg-ink-900/50">
      <div className="flex-none p-3 pb-2">
        <div className="relative">
          <svg
            viewBox="0 0 24 24"
            className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-ink-500"
            fill="none"
            aria-hidden="true"
          >
            <circle
              cx="10.5"
              cy="10.5"
              r="6"
              stroke="currentColor"
              strokeWidth="1.8"
            />
            <path
              d="M15 15l4 4"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
            />
          </svg>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search nodes"
            aria-label="Search nodes"
            className="w-full rounded-lg border border-ink-700 bg-ink-950/60 py-1.5 pr-2 pl-8 text-xs text-ink-100 outline-none placeholder:text-ink-500 focus:border-brand-400"
          />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        {groups.length === 0 && (
          <p className="px-1 py-6 text-center text-xs text-ink-500">
            Nothing matches “{query}”.
          </p>
        )}
        {groups.map((group) => (
          <div key={group.heading} className="mb-4">
            <p className="mb-1.5 px-1 text-[0.66rem] font-medium tracking-[0.12em] text-ink-500 uppercase">
              {group.heading}
            </p>
            <ul className="space-y-1">
              {group.specs.map((spec) => {
                // A flow needs exactly one trigger, so once one exists the rest
                // of the palette's triggers are shown but not offered — visible
                // for context (this is what started the flow), disabled so a
                // click or drag cannot produce a rejected graph.
                const blocked = spec.is_trigger && hasTrigger;
                return (
                  <li key={spec.type}>
                    <button
                      draggable={!blocked}
                      onDragStart={(event) =>
                        event.dataTransfer.setData(
                          "application/basivo-node",
                          spec.type,
                        )
                      }
                      onClick={() => !blocked && onAdd(spec)}
                      disabled={blocked}
                      title={
                        blocked
                          ? "A flow can only have one trigger."
                          : spec.description
                      }
                      className={cx(
                        "flex w-full items-center gap-2.5 rounded-xl border border-ink-700/60 bg-ink-850/60 px-2.5 py-2 text-left transition-all",
                        blocked
                          ? "cursor-not-allowed opacity-40"
                          : "cursor-grab hover:-translate-y-px hover:border-ink-500 hover:shadow-md active:cursor-grabbing",
                      )}
                    >
                      <NodeIconChip type={spec.type} size={7} />
                      <span className="min-w-0">
                        <span className="block truncate text-xs font-medium text-ink-200">
                          {spec.label}
                        </span>
                        <span className="block truncate font-mono text-[0.62rem] text-ink-500">
                          {spec.type}
                        </span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>

      {/* Pinned below the scroll area rather than trailing the list: a hint
          you have to scroll fifteen nodes to reach is a hint nobody reads. */}
      <p className="flex-none border-t border-ink-800/70 px-4 py-2.5 text-[0.66rem] leading-relaxed text-ink-600">
        Drag onto the canvas, or click to drop one at the top left.
      </p>
    </div>
  );
}

/* ------------------------------------------------------------ run result --- */

function RunSummary({
  run,
  elapsed,
  onClose,
}: {
  run: RunDetail;
  elapsed: number;
  onClose: () => void;
}) {
  const live = !TERMINAL_RUN_STATUSES.has(run.status);
  const ordered = [...run.nodes].sort((a, b) =>
    (a.started_at ?? "").localeCompare(b.started_at ?? ""),
  );

  return (
    <motion.div
      initial={{ height: 0, opacity: 0 }}
      animate={{ height: "auto", opacity: 1 }}
      className="max-h-56 flex-none overflow-y-auto border-t border-ink-800/70 bg-ink-900/60"
    >
      <div className="p-4">
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <p className="text-sm font-medium text-ink-100">
            {live ? "Running now" : "Last test run"}
          </p>
          {live && (
            <span
              className="h-1.5 w-1.5 flex-none animate-pulse rounded-full"
              style={{ backgroundColor: "var(--status-warn)" }}
              aria-hidden="true"
            />
          )}
          <span className="font-mono text-xs text-ink-500">
            {run.id.slice(0, 8)}
          </span>
          <Link
            to={`/app/runs/${run.id}`}
            className="ml-1 text-xs text-brand-400 underline decoration-dotted underline-offset-2 hover:text-brand-300"
          >
            Open full log →
          </Link>
          <span className="ml-auto font-mono text-xs text-ink-300">
            {live ? `${elapsed}s` : duration(run.duration_ms)}
          </span>
          <button
            onClick={onClose}
            aria-label="Hide run report"
            className="rounded-lg p-1 text-ink-500 transition-colors hover:bg-ink-800 hover:text-ink-200"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none">
              <path
                d="M6 6l12 12M18 6L6 18"
                stroke="currentColor"
                strokeWidth="1.8"
              />
            </svg>
          </button>
        </div>

        <ul className="space-y-1.5">
          {ordered.map((execution) => (
            <li
              key={`${execution.node_id}-${execution.attempt}`}
              className="flex items-center gap-3 text-xs"
            >
              <span
                className="h-1.5 w-1.5 flex-none rounded-full"
                style={{ backgroundColor: toneOf(execution.status) }}
              />
              <span className="w-40 truncate font-mono text-ink-300">
                {execution.node_id}
              </span>
              <span
                className="w-20 flex-none"
                style={{ color: toneOf(execution.status) }}
              >
                {execution.status}
              </span>
              {execution.attempt > 1 && (
                <span style={{ color: "var(--status-warn)" }}>
                  attempt {execution.attempt}
                </span>
              )}
              <span className="ml-auto flex-none font-mono text-ink-400">
                {duration(execution.duration_ms)}
              </span>
              {execution.error && (
                <span
                  className="w-full truncate font-mono"
                  style={{ color: "var(--status-bad)" }}
                >
                  {execution.error}
                </span>
              )}
            </li>
          ))}
        </ul>
      </div>
    </motion.div>
  );
}

function toneOf(status: string): string {
  if (status === "succeeded") return "var(--status-good)";
  if (status === "failed") return "var(--status-bad)";
  if (status === "skipped") return "var(--color-ink-400)";
  return "var(--series)";
}

function detailFor(execution: NodeExecution): string | undefined {
  if (execution.status === "failed") return execution.error?.slice(0, 40);
  if (execution.duration_ms !== null) return duration(execution.duration_ms);
  return undefined;
}

/**
 * Pull the problem list out of a 422.
 *
 * `validate_graph` collects every problem rather than raising on the first, so
 * that the editor can flag them all in one pass. Losing the list here and
 * showing "invalid" would throw away the part that makes it useful.
 */
function extractProblems(error: unknown): string[] {
  if (!(error instanceof ApiError)) return [];
  // `error.body` is the whole response JSON. FastAPI wraps whatever an
  // HTTPException's `detail=` carries inside its own top-level `detail` key,
  // so a rejected graph's problem list is one level deeper than it looks:
  // `{"detail": {"message": "...", "problems": [...]}}` — see `_graph_error`
  // in `basivo_orch/flows/router.py`.
  const body = error.body as { detail?: { problems?: unknown } } | undefined;
  const problems = body?.detail?.problems;
  return Array.isArray(problems) ? problems.map(String) : [];
}
