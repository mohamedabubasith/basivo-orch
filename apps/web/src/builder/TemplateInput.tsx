/**
 * A text field that completes `{{ references }}`.
 *
 * Typing `{{` opens a dropdown of everything this node can actually refer to
 * (see `suggestions.ts` for what that means and why only upstream counts);
 * typing narrows it, ↑/↓ move, Enter or a click inserts the reference and
 * closes the braces. The point is n8n's and Flowise's point: nobody should
 * have to remember that a webhook's body lives at `input.body` or what they
 * named a variable three nodes ago — the editor knows.
 */

import { useEffect, useRef, useState } from "react";

import { cx } from "../lib/cx";
import type { Suggestion } from "./suggestions";

const INPUT =
  "w-full rounded-lg border border-ink-700 bg-ink-950/60 px-2.5 py-2 text-sm text-ink-100 outline-none focus:border-brand-400";

/** The unclosed `{{` nearest before the caret, if any, and the partial after it. */
function openReference(value: string, caret: number): { start: number; partial: string } | null {
  const before = value.slice(0, caret);
  const open = before.lastIndexOf("{{");
  if (open === -1) return null;
  const between = before.slice(open + 2);
  if (between.includes("}}")) return null;
  return { start: open + 2, partial: between.trimStart() };
}

export function TemplateInput({
  value,
  onChange,
  suggestions,
  multiline = false,
  rows = 4,
  placeholder,
  mono = false,
}: {
  value: string;
  onChange: (value: string) => void;
  suggestions: Suggestion[];
  multiline?: boolean;
  rows?: number;
  placeholder?: string;
  mono?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [partial, setPartial] = useState("");
  const [active, setActive] = useState(0);
  const fieldRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);

  const matches = suggestions
    .filter((s) => s.token.toLowerCase().includes(partial.toLowerCase()))
    .slice(0, 12);

  // Keep the highlighted row in range as typing narrows the list.
  useEffect(() => {
    if (active >= matches.length) setActive(0);
  }, [matches.length, active]);

  function refreshDropdown() {
    const field = fieldRef.current;
    if (!field) return;
    const reference = openReference(field.value, field.selectionStart ?? 0);
    if (reference && suggestions.length > 0) {
      setPartial(reference.partial);
      setOpen(true);
    } else {
      setOpen(false);
    }
  }

  function insert(suggestion: Suggestion) {
    const field = fieldRef.current;
    if (!field) return;
    const caret = field.selectionStart ?? value.length;
    const reference = openReference(value, caret);
    if (!reference) return;

    const after = value.slice(caret);
    // Close the braces unless the author already typed them.
    const closing = after.trimStart().startsWith("}}") ? "" : " }}";
    const next = `${value.slice(0, reference.start)} ${suggestion.token}${closing}${after}`;
    onChange(next);
    setOpen(false);

    const position = reference.start + 1 + suggestion.token.length + closing.length;
    requestAnimationFrame(() => {
      field.focus();
      field.selectionStart = field.selectionEnd = position;
    });
  }

  const shared = {
    value,
    placeholder,
    spellCheck: false as const,
    className: cx(INPUT, mono && "font-mono text-[0.8rem]", multiline && "resize-y"),
    onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      onChange(event.target.value);
      requestAnimationFrame(refreshDropdown);
    },
    onKeyUp: (event: React.KeyboardEvent) => {
      // Caret moves (arrows, clicks) can enter or leave a {{ … }} region.
      if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) refreshDropdown();
    },
    onKeyDown: (event: React.KeyboardEvent) => {
      if (!open || matches.length === 0) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActive((a) => (a + 1) % matches.length);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        setActive((a) => (a - 1 + matches.length) % matches.length);
      } else if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        insert(matches[active]);
      } else if (event.key === "Escape") {
        setOpen(false);
      }
    },
    // Delayed so a click on a suggestion lands before the dropdown unmounts.
    onBlur: () => setTimeout(() => setOpen(false), 150),
  };

  return (
    <div className="relative">
      {multiline ? (
        <textarea
          ref={(el) => {
            fieldRef.current = el;
          }}
          rows={rows}
          {...shared}
        />
      ) : (
        <input
          ref={(el) => {
            fieldRef.current = el;
          }}
          {...shared}
        />
      )}

      {open && matches.length > 0 && (
        <ul
          role="listbox"
          className="surface absolute inset-x-0 top-full z-40 mt-1 max-h-56 overflow-y-auto rounded-xl p-1 shadow-xl shadow-black/40"
        >
          {matches.map((suggestion, index) => (
            <li key={suggestion.token}>
              <button
                type="button"
                role="option"
                aria-selected={index === active}
                // mousedown, not click: click fires after blur has closed the
                // dropdown and the press would hit whatever moved underneath.
                onMouseDown={(event) => {
                  event.preventDefault();
                  insert(suggestion);
                }}
                onMouseEnter={() => setActive(index)}
                className={cx(
                  "flex w-full items-baseline gap-2 rounded-lg px-2.5 py-1.5 text-left",
                  index === active ? "bg-ink-800" : "",
                )}
              >
                <code className="min-w-0 flex-1 truncate font-mono text-[0.72rem] text-ink-100">
                  {"{{ "}
                  {suggestion.token}
                  {" }}"}
                </code>
                <span className="flex-none truncate text-[0.66rem] text-ink-500">
                  {suggestion.hint}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
