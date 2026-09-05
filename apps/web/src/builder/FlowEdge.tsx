/**
 * The line between two nodes, with a way to remove it.
 *
 * A connection you can draw but not undo is a trap: the only exits were
 * selecting the hairline and pressing Delete, or deleting a node. A small
 * cross sits at the midpoint of every edge; it appears when the pointer is
 * over the edge or the edge is selected, and one click disconnects. Removal
 * goes through `deleteElements`, so it lands in `onEdgesChange` and marks
 * the flow dirty like any other edit.
 */

import {
  BaseEdge,
  EdgeLabelRenderer,
  getSmoothStepPath,
  useReactFlow,
  type EdgeProps,
} from "@xyflow/react";

export function FlowEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  style,
  markerEnd,
  selected,
  sourceHandleId,
}: EdgeProps) {
  const { deleteElements } = useReactFlow();
  const [path, labelX, labelY] = getSmoothStepPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });

  // A handover line carries the conversation, not a result. Said on the line
  // itself, so the two kinds of connection leaving an agent never look alike.
  const handover = sourceHandleId === "handover";

  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        style={handover ? { ...style, stroke: "var(--series)" } : style}
        markerEnd={markerEnd}
      />
      <EdgeLabelRenderer>
        {handover && (
          <span
            className="pointer-events-none absolute -translate-x-1/2 rounded-full border border-ink-700/70 bg-ink-900 px-2 py-0.5 text-[0.7rem] whitespace-nowrap"
            style={{ left: labelX, top: labelY - 24, color: "var(--series)" }}
          >
            hands the conversation over
          </span>
        )}
        <button
          type="button"
          aria-label="Disconnect"
          title="Disconnect"
          onClick={(event) => {
            event.stopPropagation();
            void deleteElements({ edges: [{ id }] });
          }}
          // Always visible, quietly: the label layer renders outside the edge
          // element, so a hover rule on the edge cannot reach this button, and
          // a control that only appears when you already know to select the
          // line is a control nobody finds. Quiet means a dim glyph, not a
          // translucent button: at 60% opacity the dashed line showed through
          // the disc and the cross looked broken. `nodrag nopan` keeps the
          // click from starting a pan.
          className={
            "nodrag nopan pointer-events-auto absolute grid h-5 w-5 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full border text-[0.7rem] leading-none shadow transition-colors " +
            "border-ink-600 bg-ink-850 hover:border-[var(--status-bad)] hover:text-[var(--status-bad)] " +
            (selected ? "text-ink-100" : "text-ink-400")
          }
          style={{ left: labelX, top: labelY }}
        >
          ×
        </button>
      </EdgeLabelRenderer>
    </>
  );
}
