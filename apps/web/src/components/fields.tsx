/**
 * The controls the product draws itself.
 *
 * A native `<select>` renders as the operating system's list: Chrome's grey
 * box on Windows, a sheet on a phone, a different font on every machine, and
 * no way to show the second line of explanation that half of these options
 * need. In a builder where every other surface is ours, one native dropdown
 * makes the whole panel look unfinished — and it is the control people touch
 * most, because every node's settings are made of them.
 *
 * So: a listbox we own. It keeps the parts a native control gets right and
 * people rely on without thinking — arrow keys and Home/End, Enter and Escape,
 * type a letter to jump, focus returning to the button on close, a real
 * `aria-*` contract for screen readers — and adds the parts it cannot do:
 * a hint under each option, and a tick on the chosen one.
 *
 * It positions itself with `position: fixed` from the button's rectangle
 * rather than absolutely inside it. Every one of these lives inside a dialog
 * or a scrolling panel with `overflow: hidden` somewhere above it, and an
 * absolutely positioned menu is clipped by the first of those it meets.
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { cx } from "../lib/cx";

export type Option = {
  value: string;
  label: string;
  /** A second line, for a choice whose name is not the whole story. */
  hint?: string;
};

const FIELD =
  "w-full rounded-xl border border-ink-700 bg-ink-950/60 px-3 py-2.5 text-sm text-ink-100 outline-none transition-colors focus:border-brand-400";

export function Select({
  value,
  onChange,
  options,
  placeholder = "Choose one",
  disabled = false,
  ariaLabel,
  className = "",
}: {
  value: string;
  onChange: (value: string) => void;
  options: readonly Option[];
  placeholder?: string;
  disabled?: boolean;
  ariaLabel?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [box, setBox] = useState<{ top: number; left: number; width: number } | null>(
    null,
  );
  const button = useRef<HTMLButtonElement | null>(null);
  const list = useRef<HTMLUListElement | null>(null);
  const typed = useRef<{ text: string; at: number }>({ text: "", at: 0 });

  const chosen = options.findIndex((option) => option.value === value);
  const label = chosen >= 0 ? options[chosen].label : "";

  // Measured on open and kept honest while open: a menu that stays put while
  // the panel behind it scrolls is worse than one that closes.
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const rect = button.current?.getBoundingClientRect();
      if (!rect) return;
      setBox({ top: rect.bottom + 4, left: rect.left, width: rect.width });
    };
    place();
    setActive(chosen >= 0 ? chosen : 0);
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open, chosen]);

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      const target = event.target as Node;
      if (button.current?.contains(target) || list.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  useEffect(() => {
    if (!open || !list.current) return;
    list.current
      .querySelectorAll("li")
      [active]?.scrollIntoView({ block: "nearest" });
  }, [open, active]);

  function pick(index: number) {
    const option = options[index];
    if (!option) return;
    onChange(option.value);
    setOpen(false);
    button.current?.focus();
  }

  function onKey(event: React.KeyboardEvent) {
    if (disabled) return;
    if (!open && (event.key === "Enter" || event.key === " " || event.key === "ArrowDown")) {
      event.preventDefault();
      setOpen(true);
      return;
    }
    if (!open) return;

    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      button.current?.focus();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((at) => Math.min(at + 1, options.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((at) => Math.max(at - 1, 0));
    } else if (event.key === "Home") {
      event.preventDefault();
      setActive(0);
    } else if (event.key === "End") {
      event.preventDefault();
      setActive(options.length - 1);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      pick(active);
    } else if (event.key.length === 1 && !event.metaKey && !event.ctrlKey) {
      // Type-ahead: the thing everyone does to a long list without noticing
      // they are doing it.
      const now = Date.now();
      const text =
        now - typed.current.at < 800
          ? typed.current.text + event.key.toLowerCase()
          : event.key.toLowerCase();
      typed.current = { text, at: now };
      const found = options.findIndex((option) =>
        option.label.toLowerCase().startsWith(text),
      );
      if (found >= 0) setActive(found);
    }
  }

  return (
    <>
      <button
        ref={button}
        type="button"
        role="combobox"
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={() => !disabled && setOpen((was) => !was)}
        onKeyDown={onKey}
        className={cx(
          FIELD,
          "flex items-center justify-between gap-2 text-left",
          disabled && "cursor-not-allowed opacity-50",
          open && "border-brand-400",
          className,
        )}
      >
        <span className={cx("truncate", !label && "text-ink-500")}>
          {label || placeholder}
        </span>
        <svg
          viewBox="0 0 24 24"
          aria-hidden="true"
          className={cx(
            "h-4 w-4 shrink-0 text-ink-500 transition-transform",
            open && "rotate-180",
          )}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
        >
          <path d="M6 9l6 6 6-6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && box && (
        <ul
          ref={list}
          role="listbox"
          aria-label={ariaLabel}
          tabIndex={-1}
          onKeyDown={onKey}
          style={{
            position: "fixed",
            top: box.top,
            left: box.left,
            width: box.width,
            maxHeight: "min(20rem, 50vh)",
          }}
          className="z-50 overflow-y-auto rounded-xl border border-[var(--edge-strong)] bg-ink-900 p-1 shadow-xl shadow-black/40"
        >
          {options.map((option, index) => (
            <li
              key={option.value}
              role="option"
              aria-selected={option.value === value}
              onMouseEnter={() => setActive(index)}
              onClick={() => pick(index)}
              className={cx(
                "flex cursor-pointer items-start gap-2 rounded-lg px-2.5 py-2 text-sm",
                index === active ? "bg-ink-800 text-ink-100" : "text-ink-300",
              )}
            >
              <span className="min-w-0 flex-1">
                <span className="block truncate">{option.label}</span>
                {option.hint && (
                  <span className="mt-0.5 block text-xs leading-relaxed text-ink-500">
                    {option.hint}
                  </span>
                )}
              </span>
              {option.value === value && (
                <svg
                  viewBox="0 0 24 24"
                  aria-hidden="true"
                  className="mt-0.5 h-4 w-4 shrink-0 text-brand-400"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                >
                  <path d="M5 12.5l4.5 4.5L19 7" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              )}
            </li>
          ))}
          {options.length === 0 && (
            <li className="px-2.5 py-2 text-sm text-ink-500">Nothing to choose</li>
          )}
        </ul>
      )}
    </>
  );
}

/**
 * A tick box that looks like the rest of the product.
 *
 * The native one cannot be styled past its border in any browser worth
 * supporting, and it is the control that carries the most consequential
 * settings here — "this video needs a voice", "show what the flow is doing".
 * Rendered as a button with `role="checkbox"`, which is the same contract to
 * a screen reader and the same behaviour to a keyboard.
 */
export function CheckBox({
  checked,
  onChange,
  label,
  hint,
  disabled = false,
  className = "",
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: React.ReactNode;
  hint?: React.ReactNode;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => !disabled && onChange(!checked)}
      className={cx(
        "flex w-full items-start gap-2.5 rounded-xl px-1 py-1 text-left transition-colors",
        disabled ? "cursor-not-allowed opacity-50" : "hover:bg-ink-800/40",
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cx(
          "mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded-[0.3rem] border transition-colors",
          checked
            ? "border-brand-400 bg-brand-500 text-white"
            : "border-ink-600 bg-ink-950/60",
        )}
      >
        {checked && (
          <svg viewBox="0 0 24 24" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="3">
            <path d="M5 12.5l4.5 4.5L19 7" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        )}
      </span>
      <span className="min-w-0">
        <span className="block text-sm text-ink-200">{label}</span>
        {hint && (
          <span className="mt-0.5 block text-xs leading-relaxed text-ink-500">
            {hint}
          </span>
        )}
      </span>
    </button>
  );
}
