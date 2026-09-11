/**
 * Enough markdown for what a model actually writes back, shared by the two
 * screens that show a model's prose: the published chat window, and the App
 * Builder where the agent says what it changed. Without it both print
 * `**Sunrise Bakery**` at a person, asterisks and all.
 */

import type { ReactNode } from "react";

/**
 * Models answer in markdown whether or not anyone asked, and a window that
 * prints it raw shows a person `**Nemotron**`, a wall of asterisks and, worst
 * of all, a table as a screenful of pipes and dashes. This handles what turns
 * up in practice: headings, bold, inline code, links, dash and numbered lists,
 * quotes, fenced code, and tables.
 *
 * It builds React elements rather than HTML. The text comes from a model,
 * which got it from whatever the flow read, so it is never trusted enough to
 * be set as markup — and a link's href is checked before it is used, because
 * `javascript:` is a link too.
 */
export function Formatted({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  const lines = text.split("\n");
  let index = 0;

  const flushList = (items: string[], ordered: boolean) => {
    if (items.length === 0) return;
    const Tag = ordered ? "ol" : "ul";
    blocks.push(
      <Tag
        key={`list-${blocks.length}`}
        className={`space-y-1 pl-5 ${ordered ? "list-decimal" : "list-disc"}`}
      >
        {items.map((item, at) => (
          <li key={at}>{inline(item)}</li>
        ))}
      </Tag>,
    );
  };

  while (index < lines.length) {
    const line = lines[index];

    // A fenced block: kept verbatim, because that is the point of a fence.
    if (line.trim().startsWith("```")) {
      const body: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith("```")) {
        body.push(lines[index]);
        index += 1;
      }
      index += 1;
      blocks.push(
        <pre
          key={`code-${blocks.length}`}
          className="overflow-x-auto rounded-xl border border-[var(--edge)] bg-ink-950/60 p-3 font-mono text-xs"
        >
          {body.join("\n")}
        </pre>,
      );
      continue;
    }

    // A table: header, a row of dashes, then rows. Anything else that starts
    // with a pipe is treated as text rather than guessed at.
    if (isRow(line) && isDivider(lines[index + 1] ?? "")) {
      const header = cells(line);
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && isRow(lines[index])) {
        rows.push(cells(lines[index]));
        index += 1;
      }
      blocks.push(
        <div
          key={`table-${blocks.length}`}
          className="overflow-x-auto rounded-xl border border-[var(--edge)]"
        >
          <table className="w-full border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-[var(--edge)]">
                {header.map((cell, at) => (
                  <th
                    key={at}
                    className="px-2.5 py-2 font-semibold text-ink-200"
                  >
                    {inline(cell)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, at) => (
                <tr
                  key={at}
                  className="border-b border-[var(--edge)] last:border-0"
                >
                  {row.map((cell, cellAt) => (
                    <td
                      key={cellAt}
                      className="px-2.5 py-2 align-top text-ink-300"
                    >
                      {inline(cell)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      const ordered = Boolean(numbered);
      const items: string[] = [];
      while (index < lines.length) {
        const next = lines[index].match(
          ordered ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*]\s+(.*)$/,
        );
        if (!next) break;
        items.push(next[1]);
        index += 1;
      }
      flushList(items, ordered);
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      blocks.push(
        <p
          key={`h-${blocks.length}`}
          className="pt-1 text-sm font-semibold text-ink-100"
        >
          {inline(heading[2])}
        </p>,
      );
      index += 1;
      continue;
    }

    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      blocks.push(
        <p
          key={`q-${blocks.length}`}
          className="border-l-2 border-[var(--edge-strong)] pl-3 text-ink-400"
        >
          {inline(quote[1])}
        </p>,
      );
      index += 1;
      continue;
    }

    if (line.trim()) {
      blocks.push(
        <p key={`p-${blocks.length}`} className="whitespace-pre-wrap">
          {inline(line)}
        </p>,
      );
    }
    index += 1;
  }

  return <>{blocks}</>;
}

const isRow = (line: string): boolean => line.trim().startsWith("|");
const isDivider = (line: string): boolean =>
  /^\s*\|[\s:|-]+\|?\s*$/.test(line) && line.includes("-");

function cells(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

/** A link we are willing to open. Anything else stays as its own text. */
function safeHref(href: string): string | null {
  const trimmed = href.trim();
  return /^https?:\/\//i.test(trimmed) ? trimmed : null;
}

/** Bold, inline code and links within one line. */
function inline(line: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const pattern =
    /\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s)]+)/g;
  let last = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(line))) {
    if (match.index > last) parts.push(line.slice(last, match.index));
    if (match[1] !== undefined) {
      parts.push(
        <strong key={parts.length} className="font-semibold">
          {match[1]}
        </strong>,
      );
    } else if (match[2] !== undefined) {
      parts.push(
        <code
          key={parts.length}
          className="rounded bg-ink-800/60 px-1 py-0.5 font-mono text-[0.85em]"
        >
          {match[2]}
        </code>,
      );
    } else if (match[3] !== undefined) {
      const href = safeHref(match[4]);
      parts.push(
        href ? (
          <a
            key={parts.length}
            href={href}
            target="_blank"
            rel="noreferrer noopener"
            className="text-brand-400 underline decoration-dotted underline-offset-2"
          >
            {match[3]}
          </a>
        ) : (
          match[3]
        ),
      );
    } else if (match[5] !== undefined) {
      const href = safeHref(match[5]);
      parts.push(
        href ? (
          <a
            key={parts.length}
            href={href}
            target="_blank"
            rel="noreferrer noopener"
            className="break-all text-brand-400 underline decoration-dotted underline-offset-2"
          >
            {match[5]}
          </a>
        ) : (
          match[5]
        ),
      );
    }
    last = match.index + match[0].length;
  }
  if (last < line.length) parts.push(line.slice(last));
  return parts;
}
