/**
 * The "open this properly" button on a cramped field.
 *
 * The inspector is a 356px column, which is the right width for picking a model
 * and the wrong width for writing a system prompt, a page of HTML or twenty
 * lines of Python. Those fields were a five-row textarea in a narrow rail: you
 * could type into them, but not read what you had typed.
 *
 * So the field stays where it is, and an expand button opens the same value in
 * a dialog with room to work. Same state, same onChange — nothing is copied or
 * synchronised, so there is no version of this where the two disagree.
 */

import { useEffect, type ReactNode } from "react";

import { Button } from "../components/ui";

export function ExpandButton({
  onClick,
  label,
}: {
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={`Expand ${label}`}
      aria-label={`Expand ${label}`}
      className="rounded-lg p-1 text-ink-500 transition-colors hover:bg-ink-800 hover:text-ink-200"
    >
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none">
        <path
          d="M9 4H4v5M15 20h5v-5M20 9V4h-5M4 15v5h5"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </button>
  );
}

export function ExpandDialog({
  title,
  hint,
  onClose,
  children,
}: {
  title: string;
  hint?: string;
  onClose: () => void;
  children: ReactNode;
}) {
  // Escape closes. A dialog that can only be dismissed by finding the right
  // button is the kind of thing people describe as the app being stuck.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      // Claimed here so the node dialog underneath (also listening) leaves
      // itself open: one Escape closes one layer.
      event.preventDefault();
      onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      data-expand-dialog
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-label={title}
        className="surface flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-3xl shadow-2xl shadow-black/60"
      >
        <div className="flex items-start justify-between gap-4 border-b border-ink-800/70 px-6 py-4">
          <div className="min-w-0">
            <p className="text-sm font-semibold text-ink-100">{title}</p>
            {hint && (
              <p className="mt-1 text-xs leading-relaxed text-ink-500">
                {hint}
              </p>
            )}
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
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

        <div className="min-h-0 flex-1 overflow-auto px-6 py-5">{children}</div>

        <div className="flex justify-end border-t border-ink-800/70 px-6 py-3.5">
          {/* One button. The value is already saved as it is typed, so "Cancel"
              would be a lie and "Save" would imply it had not been. */}
          <Button onClick={onClose}>Done</Button>
        </div>
      </div>
    </div>
  );
}
