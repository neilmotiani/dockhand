import type { Metadata } from "next";

import { Assistant } from "@/components/Assistant";

export const metadata: Metadata = { title: "Assistant · Dockhand" };

export default function AssistantPage() {
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Assistant</h1>
          <p>
            A simulated Alexa+ voice experience: ask out loud, hear the answer, see the evidence. It
            answers only from the Dockhand MCP server, and asks before it changes anything.
          </p>
        </div>
      </div>
      <Assistant />
    </>
  );
}
