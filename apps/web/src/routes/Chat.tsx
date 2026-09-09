/**
 * The page behind a published chat link.
 *
 * No sign-in, no chrome, no navigation back into the product: whoever opens
 * this is a customer of the person who built the flow, not a user of ours. It
 * is also the page that gets put in an iframe, so it fills whatever box it is
 * given and carries its own background.
 */

import { useParams } from "react-router-dom";

import { ChatWindow } from "../components/chat/ChatWindow";

export function Chat() {
  const { flowId = "", token = "" } = useParams();

  return (
    <main className="grid min-h-dvh place-items-center bg-ink-950 p-3 sm:p-6">
      <ChatWindow
        flowId={flowId}
        token={token}
        renameDocument
        className="h-[min(92dvh,44rem)] w-full max-w-2xl"
      />
    </main>
  );
}
