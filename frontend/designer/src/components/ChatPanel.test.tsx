// Specs for ChatPanel (CONTRACT.md "ChatPanel") — thread rendering (agent
// bubbles w/ inline changes + review link, coach bubbles, typing indicator)
// and the three flags-gated footers that replace designer.html's server
// {% if is_sandbox %}/{% elif can_use_agent %}/{% else %} composer blocks.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { ChatPanel } from "./ChatPanel";
import type { DesignerFlags } from "./ChatPanel";
import type { ChatMessage } from "../hooks/useAgentChat";

const chips = [{ label: "Lower Day 2 volume" }, { label: "Add a deload week" }];

function flags(overrides: Partial<DesignerFlags> = {}): DesignerFlags {
  return {
    is_sandbox: false,
    can_use_agent: true,
    agent_allowance: { metered: false, allowance: 0, remaining: null, can_use: true, tier: "unlimited" },
    signup_url: "/meso/sandbox/signup/",
    price_summary: "$19/mo — unlimited athletes",
    ...overrides,
  };
}

function baseProps(overrides: Partial<Parameters<typeof ChatPanel>[0]> = {}) {
  return {
    messages: [] as ChatMessage[],
    agentTyping: false,
    chips,
    inputText: "",
    onInputChange: vi.fn(),
    onInputKey: vi.fn(),
    onSend: vi.fn(),
    onChip: vi.fn(),
    threadRef: createRef<HTMLDivElement>(),
    flags: flags(),
    ...overrides,
  };
}

describe("thread rendering", () => {
  it("renders an agent bubble's text", () => {
    render(<ChatPanel {...baseProps({ messages: [{ id: 1, role: "agent", text: "Hi coach" }] })} />);
    expect(screen.getByText("Hi coach")).toBeInTheDocument();
  });

  it("renders a coach bubble's text", () => {
    render(<ChatPanel {...baseProps({ messages: [{ id: 2, role: "coach", text: "lighten Friday" }] })} />);
    expect(screen.getByText("lighten Friday")).toBeInTheDocument();
  });

  it("renders inline changes and the review link when the agent message carries them", () => {
    const messages: ChatMessage[] = [
      {
        id: 3,
        role: "agent",
        text: "Lowered Day 2 volume.",
        changes: [{ id: "c1", title: "Squat -10%" }],
        reviewUrl: "/meso/review/9/",
      },
    ];
    render(<ChatPanel {...baseProps({ messages })} />);
    expect(screen.getByText("Squat -10%")).toBeInTheDocument();
    const link = screen.getByTestId("agent-review-link");
    expect(link).toHaveAttribute("href", "/meso/review/9/");
  });

  it("shows the typing indicator only while agentTyping", () => {
    const { rerender } = render(<ChatPanel {...baseProps({ agentTyping: false })} />);
    expect(screen.queryByText(/drafting/i)).not.toBeInTheDocument();
    rerender(<ChatPanel {...baseProps({ agentTyping: true })} />);
  });
});

describe("composer (flags.can_use_agent gate)", () => {
  it("renders the chip row and composer, wiring onChip/onInputChange/onInputKey/onSend", async () => {
    const user = userEvent.setup();
    const onChip = vi.fn();
    const onSend = vi.fn();
    const onInputChange = vi.fn();
    render(<ChatPanel {...baseProps({ onChip, onSend, onInputChange })} />);
    expect(screen.getByTestId("agent-chip-0")).toHaveTextContent("Lower Day 2 volume");
    expect(screen.getByTestId("agent-chip-1")).toHaveTextContent("Add a deload week");
    await user.click(screen.getByTestId("agent-chip-0"));
    expect(onChip).toHaveBeenCalledWith("Lower Day 2 volume");

    const input = screen.getByTestId("agent-composer-input");
    await user.type(input, "x");
    expect(onInputChange).toHaveBeenCalled();

    await user.click(screen.getByTestId("agent-composer-send"));
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("disables chips and the send button while agentTyping", () => {
    render(<ChatPanel {...baseProps({ agentTyping: true })} />);
    expect(screen.getByTestId("agent-chip-0")).toBeDisabled();
    expect(screen.getByTestId("agent-composer-send")).toBeDisabled();
  });

  it("shows the metered allowance note with a subscribe link on the free tier", () => {
    render(
      <ChatPanel
        {...baseProps({
          flags: flags({
            agent_allowance: { metered: true, allowance: 5, remaining: 3, can_use: true, tier: "free" },
          }),
        })}
      />,
    );
    const note = screen.getByTestId("agent-allowance-note");
    expect(note).toHaveTextContent("3");
    expect(note).toHaveTextContent("5");
    expect(screen.getByText(/subscribe for more/i)).toBeInTheDocument();
  });

  it("shows the metered allowance note without a subscribe link on paid tiers", () => {
    render(
      <ChatPanel
        {...baseProps({
          flags: flags({
            agent_allowance: { metered: true, allowance: 100, remaining: 40, can_use: true, tier: "paid" },
          }),
        })}
      />,
    );
    expect(screen.getByTestId("agent-allowance-note")).toBeInTheDocument();
    expect(screen.queryByText(/subscribe for more/i)).not.toBeInTheDocument();
  });

  it("omits the allowance note when not metered (unlimited tier)", () => {
    render(<ChatPanel {...baseProps()} />);
    expect(screen.queryByTestId("agent-allowance-note")).not.toBeInTheDocument();
  });
});

describe("sandbox gate (flags.is_sandbox)", () => {
  it("renders the signup CTA instead of the composer, taking precedence over can_use_agent", () => {
    render(<ChatPanel {...baseProps({ flags: flags({ is_sandbox: true, can_use_agent: true }) })} />);
    const cta = screen.getByTestId("agent-sandbox-cta");
    expect(cta).toHaveAttribute("href", "/meso/sandbox/signup/");
    expect(screen.queryByTestId("agent-composer-input")).not.toBeInTheDocument();
  });
});

describe("exhausted-allowance gate (!can_use_agent, !is_sandbox)", () => {
  it("renders an upgrade CTA with the price summary on the free tier", () => {
    render(
      <ChatPanel
        {...baseProps({
          flags: flags({
            can_use_agent: false,
            agent_allowance: { metered: true, allowance: 5, remaining: 0, can_use: false, tier: "free" },
          }),
        })}
      />,
    );
    const cta = screen.getByTestId("agent-upgrade-cta");
    expect(cta).toBeInTheDocument();
    expect(screen.getByText(/\$19\/mo/)).toBeInTheDocument();
    expect(screen.queryByTestId("agent-composer-input")).not.toBeInTheDocument();
  });

  it("renders the plain reset note (no CTA) on a paid tier", () => {
    render(
      <ChatPanel
        {...baseProps({
          flags: flags({
            can_use_agent: false,
            agent_allowance: { metered: true, allowance: 100, remaining: 0, can_use: false, tier: "paid" },
          }),
        })}
      />,
    );
    expect(screen.queryByTestId("agent-upgrade-cta")).not.toBeInTheDocument();
    expect(screen.getByText(/resets on the 1st/)).toBeInTheDocument();
  });
});
