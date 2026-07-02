// Agent chat helpers ported from createMeso() (meso.js: agentErrorText,
// batchMessage, pollBatch). pollBatch was previously a `this`-bound method
// (this.pushAgent, this.sleep, this.pollIntervalMs/pollMaxAttempts) — it is
// refactored here into a dependency-injected function: no `this`, every
// side effect (network, sleep, message delivery) comes in via `options` so
// the hook (useAgentChat) supplies the real fetch/timers and the specs can
// inject fakes without a component instance.

/** One change chip in a resolved agent batch (inert until applied at review). */
export interface ChatChange {
  id?: string | number;
  kind?: string;
  title?: string;
  member?: string;
  before?: string;
  after?: string;
}

/** One rendered chat turn. `error` styles the bubble as a failure. */
export interface AgentMessage {
  text: string;
  changes?: ChatChange[];
  reviewUrl?: string | null;
  error?: boolean;
}

/** The batch status endpoint's response shape while polling. */
export interface BatchStatusData {
  status?: "drafting" | "pending" | "applied" | "dismissed" | "failed" | string;
  error?: string;
  summary?: string;
  changes?: ChatChange[];
  review_url?: string;
}

/** Maps a failed agent request's HTTP status to friendly copy. */
export function agentErrorText(
  status: number,
  data: { error?: string } | null | undefined,
): string {
  if (status === 503) return "The agent isn't configured in this environment yet.";
  if (status === 502) return "The agent had trouble responding. Give it another try.";
  if (status === 400) return "That message couldn't be sent — try a shorter instruction.";
  return (data && data.error) || "The agent couldn't process that request.";
}

/**
 * Shapes a resolved batch status reply into a chat message. Changes are
 * inert here — the review link is the only way to act on them.
 */
export function batchMessage(data: BatchStatusData): AgentMessage {
  const changes = data.changes || [];
  let text = data.summary || "";
  if (!changes.length) {
    text =
      text ||
      "I couldn't find any safe changes to propose for that. Try rephrasing or adjusting the plan directly.";
  }
  return {
    text,
    changes,
    reviewUrl: changes.length ? data.review_url ?? null : null,
  };
}

export interface PollBatchOptions {
  /** Defaults to the global `fetch`. */
  fetchImpl?: typeof fetch;
  /** Defaults to a real `setTimeout`-backed sleep. */
  sleep?: (ms: number) => Promise<void>;
  intervalMs?: number;
  maxAttempts?: number;
  /** Called exactly once with the terminal message (resolved, failed, error, or timeout). */
  onMessage: (message: AgentMessage) => void;
}

const defaultSleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Polls a batch's status endpoint while the background agent job runs.
 * Resolves after delivering exactly one message via `onMessage` — rendering
 * the batch, an error, or a timeout hint. Every branch here is pinned by the
 * ported spec: no status url, drafting → keep polling, failed, HTTP error,
 * network error, and the attempt cap.
 */
export async function pollBatch(
  statusUrl: string | null | undefined,
  options: PollBatchOptions,
): Promise<void> {
  const {
    fetchImpl = fetch,
    sleep = defaultSleep,
    intervalMs = 1500,
    maxAttempts = 40,
    onMessage,
  } = options;

  if (!statusUrl) {
    onMessage({ text: "The agent couldn't process that request.", error: true });
    return;
  }

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    let data: BatchStatusData;
    try {
      const res = await fetchImpl(statusUrl);
      data = await res.json().catch(() => ({}));
      if (!res.ok) {
        onMessage({ text: agentErrorText(res.status, data), error: true });
        return;
      }
    } catch (err) {
      console.error("Agent status poll failed", err);
      onMessage({
        text: "Something went wrong reaching the agent. Please try again.",
        error: true,
      });
      return;
    }

    if (data.status === "drafting") {
      await sleep(intervalMs);
      continue;
    }
    if (data.status === "failed") {
      onMessage({
        text: data.error || "The agent had trouble responding. Give it another try.",
        error: true,
      });
      return;
    }
    // pending / applied / dismissed — a resolved batch.
    onMessage(batchMessage(data));
    return;
  }

  onMessage({
    text: "The agent is taking longer than expected. Check the review screen in a moment.",
    error: true,
  });
}
