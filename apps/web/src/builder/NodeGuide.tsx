/**
 * What a node is for, in the words a person asked for: when to use it, what
 * has to exist before it works, and a chain it usually sits in. The text comes
 * from the engine's registry (`when`, `needs`, `example` on each node class),
 * so it cannot drift from what the node actually does.
 */

import type { NodeSpec } from "./specs";

export function NodeGuide({ spec }: { spec: NodeSpec }) {
  return (
    <div className="space-y-4 text-xs leading-relaxed text-ink-300">
      <p className="text-[0.8rem] text-ink-200">{spec.description}</p>

      <section>
        <h4 className="mb-1 text-[0.66rem] font-medium tracking-[0.12em] text-ink-500 uppercase">
          When to use it
        </h4>
        <p>{spec.when}</p>
      </section>

      <section>
        <h4 className="mb-1 text-[0.66rem] font-medium tracking-[0.12em] text-ink-500 uppercase">
          {spec.is_trigger ? "How it starts" : "What it needs"}
        </h4>
        <ul className="list-disc space-y-1 pl-4">
          {spec.needs.map((need) => (
            <li key={need}>{need}</li>
          ))}
          {!spec.is_trigger && (
            <li>
              Every flow starts with exactly one trigger. This node cannot
              start a flow on its own.
            </li>
          )}
        </ul>
      </section>

      <section>
        <h4 className="mb-1 text-[0.66rem] font-medium tracking-[0.12em] text-ink-500 uppercase">
          A typical flow
        </h4>
        <ol className="flex flex-wrap items-center gap-1.5">
          {spec.example.split("->").map((step, index) => (
            <li key={index} className="flex items-center gap-1.5">
              {index > 0 && <span className="text-ink-600">then</span>}
              <span className="rounded-md border border-ink-700/70 bg-ink-850/70 px-1.5 py-0.5 text-ink-200">
                {step.trim()}
              </span>
            </li>
          ))}
        </ol>
      </section>

      <p className="font-mono text-[0.62rem] text-ink-600">{spec.type}</p>
    </div>
  );
}
