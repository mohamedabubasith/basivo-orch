/**
 * The theme control: three states, not a switch.
 *
 * A two-state toggle cannot express "follow my system", which is the setting
 * most people actually want and the only one that keeps working when their OS
 * changes at sunset. Three segments make the current state readable without
 * pressing anything.
 */

import { motion } from "motion/react";

import { cx } from "../lib/cx";
import { useTheme, type ThemeChoice } from "../lib/theme";

const OPTIONS: { value: ThemeChoice; label: string; icon: ReactIcon }[] = [
  {
    value: "light",
    label: "Light",
    icon: (
      <>
        <circle cx="12" cy="12" r="4" />
        <path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4" />
      </>
    ),
  },
  {
    value: "system",
    label: "System",
    icon: (
      <>
        <rect x="3" y="4.5" width="18" height="12" rx="2" />
        <path d="M9 20h6" />
      </>
    ),
  },
  {
    value: "dark",
    label: "Dark",
    icon: <path d="M20 13.5A8.5 8.5 0 1 1 10.5 4a6.8 6.8 0 0 0 9.5 9.5Z" />,
  },
];

type ReactIcon = React.ReactNode;

export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const { choice, setChoice } = useTheme();

  return (
    <div
      role="radiogroup"
      aria-label="Colour theme"
      className={cx(
        "flex items-center gap-0.5 rounded-xl border border-ink-700/60 bg-ink-900/50 p-0.5",
        compact ? "w-fit" : "w-full",
      )}
    >
      {OPTIONS.map((option) => (
        <button
          key={option.value}
          role="radio"
          aria-checked={choice === option.value}
          aria-label={option.label}
          title={option.label}
          onClick={() => setChoice(option.value)}
          className={cx(
            "relative flex flex-1 items-center justify-center rounded-lg py-1.5 transition-colors",
            choice === option.value ? "text-ink-100" : "text-ink-500 hover:text-ink-300",
          )}
        >
          {choice === option.value && (
            <motion.span
              layoutId="theme-pill"
              className="absolute inset-0 rounded-lg bg-ink-800"
              transition={{ type: "spring", stiffness: 400, damping: 32 }}
            />
          )}
          <svg
            viewBox="0 0 24 24"
            className="relative h-4 w-4"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.7"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            {option.icon}
          </svg>
        </button>
      ))}
    </div>
  );
}
