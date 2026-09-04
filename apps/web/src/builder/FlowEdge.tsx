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

  return (
    <>
      <BaseEdge id={id} path={path} style={style} markerEnd={markerEnd} />
      <EdgeLabelRenderer>
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
          // line is a control nobody finds. Full strength on hover or select.
          // `nodrag nopan` keeps the click from starting a pan.
          className={
            "nodrag nopan pointer-events-auto absolute grid h-5 w-5 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full border text-[0.7rem] leading-none shadow transition-opacity " +
            "border-ink-600 bg-ink-900 text-ink-300 hover:border-[var(--status-bad)] hover:text-[var(--status-bad)] hover:opacity-100 " +
            (selected ? "opacity-100" : "opacity-60")
          }
          style={{ left: labelX, top: labelY }}
        >
          ×
        </button>
      </EdgeLabelRenderer>
    </>
  );
}
