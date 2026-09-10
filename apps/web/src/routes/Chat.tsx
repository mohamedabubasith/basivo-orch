/**
 * The page behind a published chat link.
 *
 * No sign-in and no navigation into the product: whoever opens this is a
 * customer of the person who built the flow, not a user of ours. It is also
 * the page that gets put in an iframe, so it fills whatever box it is given.
 *
 * The theme switch is here rather than inherited silently. This link gets sent
 * to strangers on unknown machines, and a chat that is white on a dark laptop
 * at midnight is the sort of thing people close rather than complain about.
 */

import { useParams } from "react-router-dom";

import { ChatWindow } from "../components/chat/ChatWindow";
import { ThemeToggle } from "../components/ThemeToggle";
import { Logo } from "../components/ui";

export function Chat() {
  const { flowId = "", token = "" } = useParams();

  return (
    <div className="flex min-h-dvh flex-col bg-ink-950">
      <header className="flex items-center justify-between gap-3 px-4 py-3 sm:px-6">
        <Logo />
        <ThemeToggle compact />
      </header>

      <main className="flex flex-1 justify-center px-3 pb-4 sm:px-6 sm:pb-6">
        <ChatWindow
          flowId={flowId}
          token={token}
          renameDocument
          className="h-[calc(100dvh-5.5rem)] w-full max-w-3xl"
        />
      </main>
    </div>
  );
}
