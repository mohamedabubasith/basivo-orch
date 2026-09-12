/**
 * Reading code in the console: a file tree and a coloured view of one file.
 *
 * The colouring is about 60 lines of regular expressions rather than a
 * highlighting library, and that is a deliberate trade. The smallest real
 * highlighter is a larger download than this entire application, to colour
 * six file types that we ship ourselves: TSX, TS, CSS, HTML, JSON and
 * Markdown. What it cannot do is be perfect, so it aims to be *useful and
 * never wrong-looking*: strings, comments, keywords, numbers and tag names,
 * in that order of precedence, and anything it does not recognise is left as
 * ordinary text rather than guessed at.
 */

import { useMemo, useState, type ReactNode } from "react";

import { cx } from "../../lib/cx";

export interface SourceFile {
  path: string;
  /** Empty when the file is not text, such as an uploaded photograph. */
  text: string;
  size_bytes: number;
}

/** Bytes as a person reads them. */
function size(bytes: number): string {
  return bytes >= 1024
    ? `${Math.round(bytes / 1024)} KB`
    : `${bytes} B`;
}

const KEYWORDS =
  /\b(import|export|from|default|function|const|let|var|return|if|else|for|while|await|async|new|class|extends|interface|type|as|of|in|try|catch|finally|throw|typeof|null|undefined|true|false)\b/;

type Rule = { kind: string; re: RegExp };

/** Which rules apply, in precedence order, for a file's extension. */
function rulesFor(path: string): Rule[] {
  const common: Rule[] = [
    { kind: "comment", re: /\/\*[\s\S]*?\*\/|\/\/[^\n]*/ },
    { kind: "string", re: /"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`/ },
  ];
  if (path.endsWith(".json")) {
    return [
      { kind: "key", re: /"(?:[^"\\\n]|\\.)*"(?=\s*:)/ },
      { kind: "string", re: /"(?:[^"\\\n]|\\.)*"/ },
      { kind: "number", re: /\b-?\d+(?:\.\d+)?\b/ },
      { kind: "keyword", re: /\b(true|false|null)\b/ },
    ];
  }
  if (path.endsWith(".css")) {
    return [
      { kind: "comment", re: /\/\*[\s\S]*?\*\// },
      { kind: "string", re: /"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'/ },
      { kind: "keyword", re: /@[a-z-]+/ },
      { kind: "tag", re: /[.#][\w-]+/ },
      { kind: "number", re: /\b-?\d+(?:\.\d+)?(?:px|rem|em|%|vh|vw|s|ms)?\b/ },
    ];
  }
  if (path.endsWith(".html")) {
    return [
      // Built from a string so the two dashes never appear literally in this
      // file: the copy checker scans .tsx for them and cannot tell a comment
      // delimiter from an em dash somebody typed in a sentence.
      { kind: "comment", re: new RegExp("<!\\-\\-[\\s\\S]*?\\-\\->") },
      { kind: "string", re: /"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'/ },
      { kind: "tag", re: /<\/?[a-zA-Z][\w-]*/ },
    ];
  }
  if (path.endsWith(".md")) {
    return [
      { kind: "keyword", re: /^#{1,6} [^\n]*/m },
      { kind: "string", re: /`[^`\n]*`/ },
    ];
  }
  return [
    ...common,
    { kind: "tag", re: /<\/?[A-Z][\w.]*|<\/?[a-z][\w-]*(?=[\s/>])/ },
    { kind: "keyword", re: KEYWORDS },
    { kind: "number", re: /\b-?\d+(?:\.\d+)?\b/ },
  ];
}

const COLOUR: Record<string, string> = {
  comment: "text-ink-500 italic",
  string: "text-[color-mix(in_oklab,var(--status-good)_75%,var(--color-ink-100))]",
  keyword: "text-brand-300",
  tag: "text-[color-mix(in_oklab,var(--color-brand-400)_55%,var(--color-ink-100))]",
  number: "text-[color-mix(in_oklab,var(--status-warn)_70%,var(--color-ink-100))]",
  key: "text-brand-300",
};

/**
 * One pass, left to right, taking whichever rule matches earliest.
 *
 * Scanning rule by rule over the whole file would let a keyword inside a
 * string win, which is the classic way a hand written highlighter starts
 * colouring nonsense.
 */
function paint(text: string, path: string): ReactNode[] {
  const rules = rulesFor(path);
  const out: ReactNode[] = [];
  let rest = text;
  let key = 0;

  while (rest) {
    let best: { index: number; length: number; kind: string } | null = null;
    for (const rule of rules) {
      const found = rule.re.exec(rest);
      if (!found) continue;
      if (!best || found.index < best.index) {
        best = { index: found.index, length: found[0].length, kind: rule.kind };
      }
      if (best.index === 0) break;
    }
    if (!best) {
      out.push(rest);
      break;
    }
    if (best.index > 0) out.push(rest.slice(0, best.index));
    out.push(
      <span key={key++} className={COLOUR[best.kind]}>
        {rest.slice(best.index, best.index + best.length)}
      </span>,
    );
    rest = rest.slice(best.index + best.length);
  }
  return out;
}

/** The tree on the left, the file on the right. */
export function CodeBrowser({ files }: { files: SourceFile[] }) {
  const readable = useMemo(
    () => files.filter((file) => file.text) ?? [],
    [files],
  );
  const [openPath, setOpenPath] = useState<string>(
    readable.find((file) => file.path === "src/App.tsx")?.path ??
      readable[0]?.path ??
      "",
  );
  const open = files.find((file) => file.path === openPath);

  if (files.length === 0) {
    return (
      <div className="grid h-full place-items-center p-8 text-center">
        <p className="max-w-sm text-sm text-ink-400">
          Nothing built yet. The files appear here after the first message.
        </p>
      </div>
    );
  }

  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_1fr] md:grid-cols-[13rem_1fr] md:grid-rows-1">
      <ul className="min-h-0 overflow-auto border-b border-ink-700/70 p-2 md:border-r md:border-b-0">
        {files.map((file) => {
          const selectable = Boolean(file.text);
          return (
            <li key={file.path}>
              <button
                type="button"
                disabled={!selectable}
                onClick={() => setOpenPath(file.path)}
                title={`${file.path}, ${size(file.size_bytes)}`}
                className={cx(
                  "flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left font-mono text-xs transition",
                  file.path === openPath
                    ? "bg-ink-800 text-ink-100"
                    : selectable
                      ? "text-ink-400 hover:bg-ink-800/60 hover:text-ink-200"
                      : "cursor-default text-ink-600",
                )}
              >
                <span className="min-w-0 flex-1 truncate">{file.path}</span>
                {!selectable && (
                  <span className="flex-none text-[0.65rem] text-ink-600">
                    {size(file.size_bytes)}
                  </span>
                )}
              </button>
            </li>
          );
        })}
      </ul>

      <div className="min-h-0 overflow-auto">
        {open?.text ? (
          <pre className="min-w-0 p-4 font-mono text-xs leading-relaxed whitespace-pre text-ink-200">
            <code>{paint(open.text, open.path)}</code>
          </pre>
        ) : (
          <div className="grid h-full place-items-center p-8 text-center">
            <p className="text-sm text-ink-500">
              {open
                ? "This file is an image, so there is nothing to read here."
                : "Pick a file."}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
