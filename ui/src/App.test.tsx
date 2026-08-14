import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import {
  AccountHttpError,
  changePassword,
  loadAdminAccess,
  loadProfile,
  parseAdminAccess,
  parseAssignmentMutation,
  parsePasswordMutation,
  parseProfile,
  parseProfileMutation,
  saveDisplayName,
  saveProjectAssignment,
} from "./account";
import i18n, { type SupportedLocale } from "./i18n";
import {
  composeMessage,
  inboxPageSize,
  isCanonicalInlineRasterImageSource,
  isSafeMarkdownLinkTarget,
  loadDeliveryStatus,
  loadInbox,
  loadMessage,
  loadProjectAgents,
  loadProjects,
  loadSearch,
  loadThread,
  MailHttpError,
  mailRouteHash,
  mailThreadRouteHash,
  markdownUrlTransform,
  parseInboxPage,
  parseDeliveryResult,
  parseMailRoute,
  parseMessageDetail,
  parseProjectAgents,
  parseProjects,
  parseSearchPage,
  parseThreadPage,
  replyIdempotencyKeyFor,
  replyToMessage,
  retryDelivery,
  threadPageSize,
} from "./mail";
import {
  loadPreferences,
  parsePreferences,
  saveCorrespondenceLocale,
} from "./preferences";
import {
  adminAccessResponse,
  adminProfile,
  adminProjects,
  adminUser,
  disabledUser,
  deliveryResponse,
  inboxResponse,
  memberProfile,
  memberUser,
  messageDetail,
  messageOne,
  messageTwo,
  preferencesResponse,
  projectAgentsResponse,
  projectOne,
  projectsResponse,
  projectTwo,
  searchResponse,
  server,
  threadReply,
  threadResponse,
} from "./test/server";

const preferencesUrl = "*/mail/api/v1/me/preferences";

describe("Markdown resource boundaries", () => {
  const inline = (mime: string, raw: string) =>
    `data:image/${mime};base64,${window.btoa(raw)}`;

  it.each([
    ["png", "\x89PNG\r\n\x1a\nrest"],
    ["jpeg", "\xff\xd8\xffrest"],
    ["gif", "GIF87arest"],
    ["gif", "GIF89arest"],
    ["webp", "RIFFxxxxWEBPrest"],
  ])("accepts canonical bounded %s raster bytes", (mime, raw) => {
    expect(isCanonicalInlineRasterImageSource(inline(mime, raw))).toBe(true);
  });

  it.each([
    ["local route", "/mail/logout"],
    ["relative route", "image.png"],
    ["blob URL", "blob:http://test/id"],
    ["remote URL", "https://tracker.invalid/pixel.png"],
    ["uppercase MIME", inline("PNG", "\x89PNG\r\n\x1a\nrest")],
    ["surrounding whitespace", ` ${inline("png", "\x89PNG\r\n\x1a\nrest")}`],
    ["empty payload", "data:image/png;base64,"],
    ["invalid Base64", "data:image/png;base64,%%%%"],
    ["MIME mismatch", inline("png", "GIF89arest")],
    [
      "noncanonical Base64",
      "data:image/gif;base64,R0lGODlheB==",
    ],
    [
      "oversized payload",
      `data:image/png;base64,${"A".repeat(Math.ceil((2 * 1024 * 1024) / 3) * 4 + 1)}`,
    ],
    ["short WebP", inline("webp", "RIFFWEBP")],
    ["wrong WebP marker", inline("webp", "RIFFxxxxNOPErest")],
  ])("rejects %s", (_label, source) => {
    expect(isCanonicalInlineRasterImageSource(source)).toBe(false);
  });

  it.each([
    ["HTTP", "http://example.test/path"],
    ["HTTPS", "HTTPS://example.test/path"],
    ["mailto", "mailto:operator@example.test"],
    ["root-relative", "/mail/inbox"],
    ["path-relative", "../messages/101"],
    ["query-relative", "?project=11"],
    ["percent-encoded Polish", "https://example.test/Wroc%C5%82aw"],
    ["percent-encoded CJK", "https://example.test/%E8%B7%AF%E5%BE%84"],
    ["percent-encoded emoji", "https://example.test/%F0%9F%94%90"],
    ["encoded percent sign", "https://example.test/100%25"],
    ["double-encoded Polish", "https://example.test/Wroc%25C5%2582aw"],
  ])("accepts a safe %s link target", (_label, target) => {
    expect(isSafeMarkdownLinkTarget(target)).toBe(true);
  });

  it.each([
    ["empty target", ""],
    ["surrounding whitespace", " https://example.test"],
    ["literal control character", "https://example.test/\u0001probe"],
    ["encoded control character", "mailto:test@example.test?subject=%0aBcc"],
    ["encoded C1 control character", "https://example.test/%C2%85probe"],
    ["double-encoded control character", "https://example.test/%250aprobe"],
    ["double-encoded C1 control character", "https://example.test/%25C2%2585probe"],
    ["malformed percent escape", "https://example.test/%probe"],
    ["fragment target", "#checkpoint"],
    ["relative target with fragment", "/mail/#account"],
    ["protocol-relative target", "//tracker.invalid/path"],
    ["backslash target", "\\tracker.invalid/path"],
    ["JavaScript", "javascript:alert(1)"],
    ["data", "data:text/html,unsafe"],
    ["blob", "blob:https://example.test/id"],
  ])("rejects a %s link target", (_label, target) => {
    expect(isSafeMarkdownLinkTarget(target)).toBe(false);
  });

  it("fails closed for every transformed URL attribute", () => {
    const png = inline("png", "\x89PNG\r\n\x1a\nrest");

    expect(markdownUrlTransform(png, "src")).toBe(png);
    expect(markdownUrlTransform("https://tracker.invalid/pixel", "src"))
      .toBeUndefined();
    expect(markdownUrlTransform("https://example.test", "href")).toBe(
      "https://example.test",
    );
    expect(markdownUrlTransform("javascript:alert(1)", "href"))
      .toBeUndefined();
    expect(markdownUrlTransform("#checkpoint", "href")).toBeUndefined();
    expect(markdownUrlTransform("https://example.test", "cite"))
      .toBeUndefined();
  });
});

describe("Durable mail client", () => {
  it("bounds remembered reply attempts with deterministic oldest-first eviction", () => {
    const attempts = new Map<string, string>();
    for (let index = 0; index < 64; index += 1) {
      attempts.set(`fingerprint-${index}`, `key-${index}`);
    }

    expect(replyIdempotencyKeyFor(attempts, "fingerprint-10")).toBe("key-10");
    const added = replyIdempotencyKeyFor(attempts, "fingerprint-new");

    expect(added).toMatch(/^human-ui:/u);
    expect(attempts.size).toBe(64);
    expect(attempts.has("fingerprint-0")).toBe(false);
    expect(attempts.get("fingerprint-new")).toBe(added);
  });

  it("parses and sends the exact compose, reply, and status contracts", async () => {
    const requests: Array<{ method: string; path: string; body: unknown }> = [];
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/agents", ({ request }) => {
        requests.push({
          method: request.method,
          path: new URL(request.url).pathname,
          body: null,
        });
        return HttpResponse.json(projectAgentsResponse);
      }),
      http.post("*/mail/api/v1/projects/:projectId/messages", async ({ request }) => {
        requests.push({
          method: request.method,
          path: new URL(request.url).pathname,
          body: await request.json(),
        });
        return HttpResponse.json(deliveryResponse);
      }),
      http.post(
        "*/mail/api/v1/projects/:projectId/messages/:messageId/replies",
        async ({ request }) => {
          requests.push({
            method: request.method,
            path: new URL(request.url).pathname,
            body: await request.json(),
          });
          return HttpResponse.json(deliveryResponse);
        },
      ),
      http.get("*/mail/api/v1/deliveries/:deliveryId", ({ request }) => {
        requests.push({
          method: request.method,
          path: new URL(request.url).pathname,
          body: null,
        });
        return HttpResponse.json({ ...deliveryResponse, reused: true });
      }),
      http.post("*/mail/api/v1/deliveries/:deliveryId/retry", ({ request }) => {
        requests.push({
          method: request.method,
          path: new URL(request.url).pathname,
          body: {},
        });
        return HttpResponse.json({ ...deliveryResponse, reused: true });
      }),
    );

    await expect(loadProjectAgents(projectOne.id)).resolves.toEqual(
      projectAgentsResponse,
    );
    await expect(
      composeMessage(projectOne.id, {
        idempotency_key: "compose-key",
        expected_project_generation: projectAgentsResponse.project_generation,
        recipients: [{
          agent_id: projectAgentsResponse.items[1]!.agent_id,
          expected_agent_generation: projectAgentsResponse.items[1]!.agent_generation,
        }],
        subject: "Subject",
        body_md: "Body",
        thread_id: null,
      }),
    ).resolves.toEqual(deliveryResponse);
    await expect(
      replyToMessage(projectOne.id, messageOne.id, {
        idempotency_key: "reply-key",
        expected_sender_agent_id: messageDetail.reply_target.agent_id,
        expected_sender_agent_generation:
          messageDetail.reply_target.agent_generation,
        expected_sender_project_id: messageDetail.reply_target.project_id,
        expected_sender_project_generation:
          messageDetail.reply_target.project_generation,
        body_md: "Reply",
      }),
    ).resolves.toEqual(deliveryResponse);
    await expect(loadDeliveryStatus(deliveryResponse.id)).resolves.toEqual({
      ...deliveryResponse,
      reused: true,
    });
    await expect(retryDelivery(deliveryResponse.id)).resolves.toEqual({
      ...deliveryResponse,
      reused: true,
    });
    expect(requests).toEqual([
      {
        method: "GET",
        path: `/mail/api/v1/projects/${projectOne.id}/agents`,
        body: null,
      },
      {
        method: "POST",
        path: `/mail/api/v1/projects/${projectOne.id}/messages`,
        body: {
          idempotency_key: "compose-key",
          expected_project_generation: projectAgentsResponse.project_generation,
          recipients: [{
            agent_id: projectAgentsResponse.items[1]!.agent_id,
            expected_agent_generation: projectAgentsResponse.items[1]!.agent_generation,
          }],
          subject: "Subject",
          body_md: "Body",
          thread_id: null,
        },
      },
      {
        method: "POST",
        path: `/mail/api/v1/projects/${projectOne.id}/messages/${messageOne.id}/replies`,
        body: {
          idempotency_key: "reply-key",
          expected_sender_agent_id: messageDetail.reply_target.agent_id,
          expected_sender_agent_generation:
            messageDetail.reply_target.agent_generation,
          expected_sender_project_id: messageDetail.reply_target.project_id,
          expected_sender_project_generation:
            messageDetail.reply_target.project_generation,
          body_md: "Reply",
        },
      },
      {
        method: "GET",
        path: `/mail/api/v1/deliveries/${deliveryResponse.id}`,
        body: null,
      },
      {
        method: "POST",
        path: `/mail/api/v1/deliveries/${deliveryResponse.id}/retry`,
        body: {},
      },
    ]);
  });

  it.each([
    [{ ...deliveryResponse, id: "bad" }],
    [{ ...deliveryResponse, status: "lost" }],
    [{ ...deliveryResponse, reused: "yes" }],
    [{ ...deliveryResponse, message_id: 0 }],
    [{ ...deliveryResponse, commit_sha: "bad" }],
    [{ ...deliveryResponse, next_attempt_ts: "bad" }],
    [{ ...deliveryResponse, debug: true }],
  ])("rejects a malformed durable response %#", (payload) => {
    expect(() => parseDeliveryResult(payload)).toThrow(TypeError);
  });

  it("rejects invalid route identities before sending", async () => {
    await expect(loadProjectAgents(0)).rejects.toThrow(TypeError);
    expect(() =>
      composeMessage(0, {
        idempotency_key: "key",
        expected_project_generation: "d".repeat(64),
        recipients: [{
          agent_id: 1,
          expected_agent_generation: "1".repeat(64),
        }],
        subject: "Subject",
        body_md: "Body",
        thread_id: null,
      }),
    ).toThrow(TypeError);
    expect(() =>
      replyToMessage(projectOne.id, 0, {
        idempotency_key: "key",
        expected_sender_agent_id: messageDetail.reply_target.agent_id,
        expected_sender_agent_generation:
          messageDetail.reply_target.agent_generation,
        expected_sender_project_id: messageDetail.reply_target.project_id,
        expected_sender_project_generation:
          messageDetail.reply_target.project_generation,
        body_md: "Body",
      }),
    ).toThrow(TypeError);
    expect(() => loadDeliveryStatus("not-a-delivery")).toThrow(TypeError);
    expect(() => retryDelivery("not-a-delivery")).toThrow(TypeError);
  });

  it("validates the exact project-agent contract and requested identity", async () => {
    expect(parseProjectAgents(projectAgentsResponse)).toEqual(projectAgentsResponse);
    const invalidPayloads: unknown[] = [
      null,
      { ...projectAgentsResponse, items: {} },
      { ...projectAgentsResponse, project_id: 0 },
      { ...projectAgentsResponse, project_generation: "INVALID" },
      {
        ...projectAgentsResponse,
        items: [{ ...projectAgentsResponse.items[0], agent_id: 0 }],
      },
      {
        ...projectAgentsResponse,
        items: [{ ...projectAgentsResponse.items[0], agent_generation: "INVALID" }],
      },
      { ...projectAgentsResponse, items: [null], total: 1 },
      {
        ...projectAgentsResponse,
        items: [{ ...projectAgentsResponse.items[0], name: 7 }],
        total: 1,
      },
      {
        ...projectAgentsResponse,
        items: [{ ...projectAgentsResponse.items[0], display_name: 7 }],
        total: 1,
      },
      {
        ...projectAgentsResponse,
        items: [{ ...projectAgentsResponse.items[0], human: true }],
        total: 1,
      },
      { ...projectAgentsResponse, items: [], total: -1 },
      { ...projectAgentsResponse, items: [], total: 0, debug: true },
    ];
    for (const payload of invalidPayloads) {
      expect(() => parseProjectAgents(payload)).toThrow(TypeError);
    }

    server.use(
      http.get("*/mail/api/v1/projects/:projectId/agents", () =>
        HttpResponse.json({ ...projectAgentsResponse, project_id: projectTwo.id }),
      ),
    );
    await expect(loadProjectAgents(projectOne.id)).rejects.toThrow(TypeError);
  });

  it("keeps typed error codes without trusting an invalid proxy body", async () => {
    server.use(
      http.post("*/mail/api/v1/projects/:projectId/messages", () =>
        HttpResponse.json(
          { detail: { code: "idempotency_conflict" } },
          { status: 409 },
        ),
      ),
      http.post(
        "*/mail/api/v1/projects/:projectId/messages/:messageId/replies",
        () => new HttpResponse("upstream unavailable", { status: 503 }),
      ),
    );
    const composeFailure = await composeMessage(projectOne.id, {
      idempotency_key: "key",
      expected_project_generation: projectAgentsResponse.project_generation,
      recipients: [{
        agent_id: projectAgentsResponse.items[1]!.agent_id,
        expected_agent_generation: projectAgentsResponse.items[1]!.agent_generation,
      }],
      subject: "Subject",
      body_md: "Body",
      thread_id: null,
    }).catch((error: unknown) => error);
    const replyFailure = await replyToMessage(projectOne.id, messageOne.id, {
      idempotency_key: "key",
      expected_sender_agent_id: messageDetail.reply_target.agent_id,
      expected_sender_agent_generation:
        messageDetail.reply_target.agent_generation,
      expected_sender_project_id: messageDetail.reply_target.project_id,
      expected_sender_project_generation:
        messageDetail.reply_target.project_generation,
      body_md: "Body",
    }).catch((error: unknown) => error);

    expect(composeFailure).toBeInstanceOf(MailHttpError);
    expect(composeFailure).toMatchObject({
      status: 409,
      code: "idempotency_conflict",
    });
    expect(replyFailure).toBeInstanceOf(MailHttpError);
    expect(replyFailure).toMatchObject({ status: 503, code: null });
  });
});

async function waitForEnglishPreferences() {
  expect(await screen.findByText("Language saved for your account")).toBeVisible();
}

function localePickerTrigger(): HTMLButtonElement {
  const trigger = document.querySelector<HTMLButtonElement>(
    ".locale-picker-trigger",
  );
  expect(trigger).not.toBeNull();
  return trigger as HTMLButtonElement;
}

async function selectUiLocale(
  user: ReturnType<typeof userEvent.setup>,
  nativeName: string,
): Promise<void> {
  await user.click(localePickerTrigger());
  const listbox = screen.getByRole("listbox");
  const option = within(listbox)
    .getAllByRole("option")
    .find((candidate) => candidate.textContent?.includes(nativeName));
  expect(option).toBeDefined();
  await user.click(option as HTMLElement);
}

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  readonly close = vi.fn();
  onerror: EventSource["onerror"] = null;
  onmessage: EventSource["onmessage"] = null;
  onopen: EventSource["onopen"] = null;

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  emitOpen() {
    this.onopen?.call(this as unknown as EventSource, new Event("open"));
  }

  emitError() {
    this.onerror?.call(this as unknown as EventSource, new Event("error"));
  }

  emitMessage() {
    this.onmessage?.call(
      this as unknown as EventSource,
      new MessageEvent("message", { data: '{"kind":"changed"}' }),
    );
  }
}

describe("Iris landing shell", () => {
  beforeEach(async () => {
    window.history.replaceState({}, "", "/mail/");
    document.documentElement.lang = "en";
    await i18n.changeLanguage("en");
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("arms and mutes the notification sound from the topbar", async () => {
    // The control is the only way a reader can turn tones on: browsers refuse
    // audio until a gesture inside the page, and arriving from the login form
    // does not count.
    const user = userEvent.setup();
    // Node 26 ships its own `localStorage` global that stays unavailable
    // without `--localstorage-file`, and it shadows the one jsdom would
    // provide — so the preference store has to be supplied here or every
    // persistence path silently no-ops.
    const stored = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => stored.get(key) ?? null,
      setItem: (key: string, value: string) => {
        stored.set(key, value);
      },
      removeItem: (key: string) => {
        stored.delete(key);
      },
    });
    const contexts: unknown[] = [];
    vi.stubGlobal(
      "AudioContext",
      vi.fn(function AudioContextStub(this: unknown) {
        const ctx = {
          createOscillator: () => ({
            connect: vi.fn(),
            frequency: { value: 0 },
            type: "sine" as OscillatorType,
            start: vi.fn(),
            stop: vi.fn(),
            onended: null,
          }),
          createGain: () => ({
            connect: vi.fn(),
            gain: {
              setValueAtTime: vi.fn(),
              exponentialRampToValueAtTime: vi.fn(),
            },
          }),
          currentTime: 0,
          destination: {},
          close: vi.fn().mockResolvedValue(undefined),
        };
        contexts.push(ctx);
        return ctx;
      }),
    );

    render(<App />);
    await waitForEnglishPreferences();

    const toggle = await screen.findByRole("button", {
      name: "Notification sound off. Click to unmute.",
    });
    expect(toggle).toHaveAttribute("aria-pressed", "false");

    await user.click(toggle);

    // Enabling plays the default tone: it doubles as the gesture the browser
    // wants and as proof to the reader that audio works here.
    expect(stored.get("agentMailSound")).toBe("on");
    expect(contexts).toHaveLength(1);
    const armed = await screen.findByRole("button", {
      name: "Notification sound on. Click to mute.",
    });
    expect(armed).toHaveAttribute("aria-pressed", "true");

    await user.click(armed);

    // Muting must stay silent — no second context.
    expect(contexts).toHaveLength(1);
    expect(stored.get("agentMailSound")).toBe("off");
    expect(
      await screen.findByRole("button", {
        name: "Notification sound off. Click to unmute.",
      }),
    ).toHaveAttribute("aria-pressed", "false");

    vi.unstubAllGlobals();
  });

  it("renders the real inbox without demo controls or invented unread data", async () => {
    render(<App />);
    await waitForEnglishPreferences();

    expect(await screen.findByRole("heading", { name: "Inbox" })).toBeVisible();
    expect(await screen.findByText(messageOne.subject)).toBeVisible();
    expect(screen.getByLabelText("Iris")).toHaveTextContent("🌈Iris");
    expect(document.querySelector(".brand-mark")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("Durable delivery")).toBeVisible();
    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    const links = within(navigation).getAllByRole("link");
    expect(links).toHaveLength(6);
    expect(links.map((link) => link.textContent)).toEqual([
      "Projects",
      "Inbox",
      "Search",
      "Compose",
      "Account",
      "Administration",
    ]);
    expect(screen.getByRole("link", { name: "Projects" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "Inbox" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByText("2 messages")).toBeVisible();
    expect(screen.getByText(/From Gospodarz/)).toBeVisible();
    expect(screen.getByText("From archive-agent")).toBeVisible();
    expect(screen.queryByText("From archive-agent · archive-agent")).not.toBeInTheDocument();
    expect(screen.getByText("Acknowledgement requested")).toBeVisible();
    expect(screen.queryByText("Demo role")).not.toBeInTheDocument();
    expect(screen.queryByText("Unread message")).not.toBeInTheDocument();
    expect(screen.queryByText("Open inbox")).not.toBeInTheDocument();
    expect(screen.queryByText("Production deployment verified")).not.toBeInTheDocument();
    expect(localePickerTrigger()).toHaveAttribute("name", "ui-language");
    expect(localePickerTrigger()).toHaveAttribute("data-locale", "en");
    const signOut = screen.getByRole("button", { name: "Sign out" });
    const logoutForm = signOut.closest("form");
    expect(signOut).toHaveAttribute("type", "submit");
    expect(logoutForm).toHaveAttribute("action", "/mail/logout");
    expect(logoutForm).toHaveAttribute("method", "post");
    expect(screen.getByLabelText("Filter by project")).toHaveAttribute(
      "name",
      "project-filter",
    );
  });

  it("opens a real project inbox from the projects view", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#projects");
    render(<App />);
    await waitForEnglishPreferences();

    expect(await screen.findByRole("heading", { name: "Projects" })).toBeVisible();
    expect(screen.getByText("2 projects")).toBeVisible();
    expect(screen.getByText("Administrator")).toBeVisible();
    expect(screen.getByText("Archived")).toBeVisible();
    await user.click(
      screen.getByRole("link", {
        name: `Open inbox for ${projectOne.human_key}`,
      }),
    );
    expect(await screen.findByRole("heading", { name: "Inbox" })).toBeVisible();
    expect(screen.getByLabelText("Filter by project")).toHaveValue(
      String(projectOne.id),
    );
  });

  it("composes an idempotent durable message from the administrator route", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    let requestBody: unknown;
    server.use(
      http.post("*/mail/api/v1/projects/:projectId/messages", async ({ request }) => {
        requestBody = await request.json();
        return HttpResponse.json(deliveryResponse);
      }),
    );
    render(<App />);
    await waitForEnglishPreferences();

    expect(await screen.findByRole("heading", { name: "Compose" })).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    await user.click(await screen.findByDisplayValue("GreenDog"));
    await user.click(screen.getByDisplayValue("BlueLake"));
    await user.type(screen.getByLabelText("Subject"), "Review the release");
    await user.type(screen.getByLabelText("Thread ID (optional)"), "release-2026");
    await user.type(screen.getByLabelText("Message in Markdown"), "**Proceed** after UAT.");
    await act(async () => {
      fireEvent.keyDown(screen.getByLabelText("Message in Markdown"), {
        key: "Enter",
        ctrlKey: true,
      });
    });

    const confirmation = await screen.findByRole("region", {
      name: "Review message before delivery",
    });
    expect(confirmation).toHaveFocus();
    expect(requestBody).toBeUndefined();
    expect(within(confirmation).getByText(projectOne.human_key)).toBeVisible();
    expect(within(confirmation).getByText("Review the release")).toBeVisible();
    expect(within(confirmation).getByText("release-2026")).toBeVisible();
    expect(within(confirmation).getByText("High")).toBeVisible();
    expect(within(confirmation).getByText("Release operator")).toBeVisible();
    expect(within(confirmation).getByText("GreenDog", { selector: "code" })).toBeVisible();
    expect(within(confirmation).getByText(/delivered separately to 2 recipients/)).toBeVisible();
    expect(within(confirmation).getByText("MESSAGE FROM HUMAN OVERSEER")).toBeVisible();
    expect(within(confirmation).getByText(/prefers replies in English \(en\)/)).toBeVisible();
    expect(within(confirmation).getByText("Proceed", { selector: "strong" })).toBeVisible();
    await user.click(within(confirmation).getByRole("button", { name: "Confirm and send" }));

    expect(await screen.findByText("Published exactly once.")).toBeVisible();
    expect(screen.getByText(`Delivery reference: ${deliveryResponse.id}`)).toBeVisible();
    expect(requestBody).toEqual({
      idempotency_key: expect.stringMatching(/^human-ui:/u),
      expected_project_generation: projectAgentsResponse.project_generation,
      recipients: [
        {
          agent_id: projectAgentsResponse.items[0]!.agent_id,
          expected_agent_generation: projectAgentsResponse.items[0]!.agent_generation,
        },
        {
          agent_id: projectAgentsResponse.items[1]!.agent_id,
          expected_agent_generation: projectAgentsResponse.items[1]!.agent_generation,
        },
      ],
      subject: "Review the release",
      body_md: "**Proceed** after UAT.",
      thread_id: "release-2026",
    });
    expect(screen.getByDisplayValue("GreenDog")).not.toBeChecked();
    expect(screen.getByDisplayValue("BlueLake")).not.toBeChecked();
    expect(screen.getByLabelText("Subject")).toHaveValue("");
    expect(screen.getByLabelText("Message in Markdown")).toHaveValue("");
  });

  it("returns to the preserved draft when a locale change invalidates its final preview", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    server.use(
      http.patch(preferencesUrl, () =>
        HttpResponse.json(preferencesResponse("pl")),
      ),
    );
    render(<App />);
    await waitForEnglishPreferences();

    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    await user.click(await screen.findByDisplayValue("GreenDog"));
    await user.type(screen.getByLabelText("Subject"), "Preserve this subject");
    await user.type(screen.getByLabelText("Message in Markdown"), "Preserve this body");
    await user.click(screen.getByRole("button", { name: "Review message" }));
    expect(
      screen.getByRole("region", { name: "Review message before delivery" }),
    ).toBeVisible();

    await selectUiLocale(user, "Polski");

    expect(
      screen.queryByRole("region", { name: "Review message before delivery" }),
    ).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Nowa wiadomość" })).toBeVisible();
    expect(screen.getByDisplayValue("Preserve this subject")).toBeVisible();
    expect(screen.getByDisplayValue("Preserve this body")).toBeVisible();
    expect(screen.getByDisplayValue("GreenDog")).toBeChecked();
  });

  it("replies with only human text while Iris derives routing", async () => {
    const user = userEvent.setup();
    let requestBody: unknown;
    server.use(
      http.post(
        "*/mail/api/v1/projects/:projectId/messages/:messageId/replies",
        async ({ request }) => {
          requestBody = await request.json();
          return HttpResponse.json(deliveryResponse);
        },
      ),
    );
    render(<App />);
    await user.click(
      await screen.findByRole("link", {
        name: new RegExp(`Open message.*${messageOne.subject}`),
      }),
    );
    await user.type(await screen.findByLabelText("Reply in Markdown"), "Approved.");
    await act(async () => {
      fireEvent.keyDown(screen.getByLabelText("Reply in Markdown"), {
        key: "Enter",
        metaKey: true,
      });
    });

    const confirmation = await screen.findByRole("region", {
      name: "Review reply before delivery",
    });
    expect(requestBody).toBeUndefined();
    expect(within(confirmation).getByText("Delivery target")).toBeVisible();
    expect(
      within(confirmation).getAllByText("claude-linux-holzera-1"),
    ).toHaveLength(2);
    expect(within(confirmation).getByText("Re: Production rollout verified")).toBeVisible();
    expect(within(confirmation).getByText("release-101")).toBeVisible();
    expect(within(confirmation).getByText("Gospodarz")).toBeVisible();
    expect(
      within(confirmation).getByText("claude-linux-holzera-1", { selector: "code" }),
    ).toBeVisible();
    expect(within(confirmation).queryByText(/delivered separately/)).not.toBeInTheDocument();
    expect(within(confirmation).getByText("MESSAGE FROM HUMAN OVERSEER")).toBeVisible();
    await user.click(within(confirmation).getByRole("button", { name: "Confirm and send" }));

    expect(await screen.findByText("Published exactly once.")).toBeVisible();
    expect(requestBody).toEqual({
      idempotency_key: expect.stringMatching(/^human-ui:/u),
      expected_sender_agent_id: messageDetail.reply_target.agent_id,
      expected_sender_agent_generation:
        messageDetail.reply_target.agent_generation,
      expected_sender_project_id: messageDetail.reply_target.project_id,
      expected_sender_project_generation:
        messageDetail.reply_target.project_generation,
      body_md: "Approved.",
    });
    expect(screen.getByLabelText("Reply in Markdown")).toHaveValue("");
  });

  it("shows the qualified delivery target for a cross-project reply", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(
        "*/mail/api/v1/projects/:projectId/messages/:messageId",
        () => HttpResponse.json({
          ...messageDetail,
          sender: "RemoteAgent@remote-project",
          sender_name: "RemoteAgent",
          sender_display_name: "Remote operator",
          reply_target: {
            agent_id: 73,
            agent_generation: "7".repeat(64),
            project_id: 37,
            project_generation: "8".repeat(64),
            canonical_name: "RemoteAgent@remote-project",
          },
        }),
      ),
    );
    render(<App />);
    await user.click(
      await screen.findByRole("link", {
        name: new RegExp(`Open message.*${messageOne.subject}`),
      }),
    );
    await user.type(await screen.findByLabelText("Reply in Markdown"), "Route safely.");
    await user.click(screen.getByRole("button", { name: "Review reply" }));

    const confirmation = screen.getByRole("region", {
      name: "Review reply before delivery",
    });
    expect(within(confirmation).getByText("Delivery target")).toBeVisible();
    expect(
      within(confirmation).getAllByText("RemoteAgent@remote-project"),
    ).toHaveLength(2);
    expect(within(confirmation).getByText("Remote operator")).toBeVisible();
    expect(within(confirmation).queryByText(projectOne.human_key))
      .not.toBeInTheDocument();
  });

  it("renders a typed lifetime conflict without exposing server detail", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(
        "*/mail/api/v1/projects/:projectId/messages/:messageId/replies",
        () => HttpResponse.json(
          { detail: { code: "reply_target_unavailable" } },
          { status: 409 },
        ),
      ),
    );
    render(<App />);
    await user.click(
      await screen.findByRole("link", {
        name: new RegExp(`Open message.*${messageOne.subject}`),
      }),
    );
    await user.type(await screen.findByLabelText("Reply in Markdown"), "Stale route.");
    await user.click(screen.getByRole("button", { name: "Review reply" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));

    expect(await screen.findByText(/conflicts with an earlier request/)).toBeVisible();
    expect(screen.queryByText("reply_target_unavailable")).not.toBeInTheDocument();
  });

  it("previews the exact Polish correspondence preamble and supports returning to compose", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json(preferencesResponse("en", "pl")),
      ),
    );
    render(<App />);
    await waitForEnglishPreferences();
    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    await user.click(await screen.findByDisplayValue("GreenDog"));
    await user.type(screen.getByLabelText("Subject"), "Polish correspondence");
    await user.type(screen.getByLabelText("Message in Markdown"), "**Dzień dobry**");
    await user.click(screen.getByRole("button", { name: "Review message" }));

    const confirmation = screen.getByRole("region", {
      name: "Review message before delivery",
    });
    expect(within(confirmation).getByText("New thread")).toBeVisible();
    expect(within(confirmation).getByText(/prefers replies in Polish \(pl\)/)).toBeVisible();
    expect(within(confirmation).getByText("Dzień dobry", { selector: "strong" })).toBeVisible();
    await user.click(within(confirmation).getByRole("button", { name: "Back to editing" }));
    expect(screen.queryByRole("region", { name: "Review message before delivery" }))
      .not.toBeInTheDocument();
    expect(screen.getByLabelText("Message in Markdown")).toHaveValue("**Dzień dobry**");
    await user.click(screen.getByRole("button", { name: "Review message" }));
    await user.type(screen.getByLabelText("Subject"), " updated");
    expect(screen.queryByRole("region", { name: "Review message before delivery" }))
      .not.toBeInTheDocument();
  });

  it("discloses when the exact server preamble is unavailable in compose", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json({ detail: "private preference failure" }, { status: 503 }),
      ),
    );
    render(<App />);
    expect(await screen.findByText(/Could not load your saved language/)).toBeVisible();
    const projectSelect = screen.getByLabelText<HTMLSelectElement>("Project");
    const staleProjectOption = document.createElement("option");
    staleProjectOption.value = "999";
    staleProjectOption.textContent = "Stale project";
    projectSelect.append(staleProjectOption);
    fireEvent.change(projectSelect, { target: { value: "999" } });
    await user.click(await screen.findByDisplayValue("GreenDog"));
    await user.type(screen.getByLabelText("Subject"), "Fallback preview");
    await user.type(screen.getByLabelText("Message in Markdown"), "Visible body");
    await user.click(screen.getByRole("button", { name: "Review message" }));

    const confirmation = screen.getByRole("region", {
      name: "Review message before delivery",
    });
    expect(within(confirmation).getByText("999")).toBeVisible();
    expect(within(confirmation).getByText(/cannot show the exact language advisory/)).toBeVisible();
    expect(within(confirmation).queryByText("MESSAGE FROM HUMAN OVERSEER"))
      .not.toBeInTheDocument();
    expect(within(confirmation).getByText("Visible body")).toBeVisible();
  });

  it("previews fallback reply routing and returns to an unchanged draft", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json({ detail: "private preference failure" }, { status: 503 }),
      ),
      http.get(
        "*/mail/api/v1/projects/:projectId/messages/:messageId",
        () => HttpResponse.json({
          ...messageDetail,
          subject: "Re: Already routed",
          thread_id: null,
          sender_display_name: null,
        }),
      ),
      http.get("*/mail/api/v1/projects", () =>
        HttpResponse.json({ items: [projectTwo], total: 1 }),
      ),
    );
    render(<App />);
    await user.click(
      await screen.findByRole("link", {
        name: new RegExp(`Open message.*${messageOne.subject}`),
      }),
    );
    await user.type(await screen.findByLabelText("Reply in Markdown"), "Unchanged reply.");
    await user.click(screen.getByRole("button", { name: "Review reply" }));

    const confirmation = screen.getByRole("region", {
      name: "Review reply before delivery",
    });
    expect(within(confirmation).getByText("Delivery target")).toBeVisible();
    expect(
      within(confirmation).getAllByText("claude-linux-holzera-1"),
    ).toHaveLength(2);
    expect(within(confirmation).getByText("Re: Already routed")).toBeVisible();
    expect(within(confirmation).getByText(String(messageOne.id))).toBeVisible();
    expect(within(confirmation).queryByText("claude-linux-holzera-1", { selector: "code" }))
      .not.toBeInTheDocument();
    expect(within(confirmation).getByText(/cannot show the exact language advisory/)).toBeVisible();
    await user.click(within(confirmation).getByRole("button", { name: "Back to editing" }));
    expect(screen.getByLabelText("Reply in Markdown")).toHaveValue("Unchanged reply.");
  });

  it("previews compose Markdown through the same fail-closed React renderer", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    render(<App />);
    await waitForEnglishPreferences();

    await user.click(screen.getByRole("button", { name: "Preview" }));
    const emptyPreview = screen.getByRole("region", { name: "Message preview" });
    expect(within(emptyPreview).getByText("Nothing to preview yet.")).toBeVisible();
    expect(emptyPreview).not.toHaveAttribute("aria-live");
    expect(document.querySelector('label[for="compose-body"]')).toBeNull();
    expect(screen.getByRole("button", { name: "Review message" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const textarea = screen.getByLabelText<HTMLTextAreaElement>("Message in Markdown");
    expect(screen.getByRole("group", { name: "Markdown formatting" })).toBeVisible();
    await user.type(textarea, "selected");
    textarea.setSelectionRange(0, 8);
    await user.click(screen.getByRole("button", { name: "Bold" }));
    expect(textarea).toHaveValue("**selected**");
    expect(textarea).toHaveFocus();
    expect(textarea.selectionStart).toBe(2);
    expect(textarea.selectionEnd).toBe(10);
    await user.keyboard("replacement");
    expect(textarea).toHaveValue("**replacement**");

    await user.clear(textarea);
    await user.type(textarea, "Title");
    textarea.setSelectionRange(0, 5);
    await user.click(screen.getByRole("button", { name: "Heading" }));
    expect(textarea).toHaveValue("## Title");
    expect(textarea.selectionStart).toBe(3);
    expect(textarea.selectionEnd).toBe(8);
    await user.clear(textarea);
    await user.type(textarea, "const answer = 42;");
    textarea.setSelectionRange(0, textarea.value.length);
    await user.click(screen.getByRole("button", { name: "Code block" }));
    expect(textarea).toHaveValue("```\nconst answer = 42;\n```");
    expect(textarea.selectionStart).toBe(4);
    expect(textarea.selectionEnd).toBe(22);

    await user.clear(textarea);
    await user.type(textarea, "x");
    textarea.setSelectionRange(1, 1);
    await user.click(screen.getByRole("button", { name: "Inline code" }));
    expect(textarea).toHaveValue("x``");
    expect(textarea.selectionStart).toBe(2);
    expect(textarea.selectionEnd).toBe(2);

    fireEvent.change(textarea, { target: { value: "x".repeat(49_999) } });
    textarea.setSelectionRange(0, 1);
    await user.click(screen.getByRole("button", { name: "Bold" }));
    expect(textarea).toHaveValue("x".repeat(49_999));

    await user.clear(textarea);
    fireEvent.change(textarea, {
      target: {
        value: "**Visible** [blocked](#fragment) ![remote](https://tracker.invalid/pixel.png)\n\n<script>unsafe()</script>",
      },
    });
    await user.click(screen.getByRole("button", { name: "Split" }));
    const splitPreview = screen.getByRole("region", { name: "Message preview" });
    expect(screen.getByLabelText("Message in Markdown")).toBeVisible();
    expect(within(splitPreview).getByText("Visible", { selector: "strong" })).toBeVisible();
    expect(within(splitPreview).getByText("blocked").closest("a")).toBeNull();
    expect(within(splitPreview).getByText("remote")).toHaveClass("markdown-image-alt");
    expect(splitPreview.querySelector("script")).not.toBeInTheDocument();
    expect(screen.getByText(/\/ 50,000 characters$/u)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(screen.queryByLabelText("Message in Markdown")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.queryByRole("region", { name: "Message preview" }))
      .not.toBeInTheDocument();
  });

  it("offers the same empty, split, and safe Markdown preview while replying", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(
      await screen.findByRole("link", {
        name: new RegExp(`Open message.*${messageOne.subject}`),
      }),
    );

    await user.click(await screen.findByRole("button", { name: "Preview" }));
    expect(
      within(screen.getByRole("region", { name: "Reply preview" })).getByText(
        "Nothing to preview yet.",
      ),
    ).toBeVisible();
    const reviewReply = screen.getByRole("button", { name: "Review reply" });
    expect(reviewReply).toBeDisabled();
    fireEvent.submit(reviewReply);
    expect(screen.getByText(/could not be submitted/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByLabelText("Reply in Markdown"), "~~Retired~~\nnext");
    await user.click(screen.getByRole("button", { name: "Split" }));
    const preview = screen.getByRole("region", { name: "Reply preview" });
    expect(within(preview).getByText("Retired", { selector: "del" })).toBeVisible();
    expect(preview.querySelector("br")).toBeInTheDocument();
    expect(screen.getByLabelText("Reply in Markdown")).toBeVisible();
  });

  it("searches aliases and canonical agent names and manages a bounded selection", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    render(<App />);
    await waitForEnglishPreferences();
    expect(screen.getByText("Choose a project to load its active agents.")).toBeVisible();

    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    const search = await screen.findByLabelText("Find recipients");
    expect(screen.getByText("Release operator")).toBeVisible();
    expect(screen.getByText("GreenDog", { selector: "code" })).toBeVisible();
    expect(screen.getByText("0 of 100 recipients selected")).toBeVisible();

    await user.click(screen.getByDisplayValue("GreenDog"));
    await user.type(search, "schema");
    expect(screen.getByDisplayValue("IndigoBridge")).toBeVisible();
    expect(screen.queryByDisplayValue("GreenDog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Select all" }));
    expect(screen.getByText("2 of 100 recipients selected")).toBeVisible();
    await user.clear(search);
    expect(screen.getByDisplayValue("GreenDog")).toBeChecked();
    expect(screen.getByDisplayValue("IndigoBridge")).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Clear selection" }));
    await user.type(search, "missing person");
    expect(screen.getByText("No active agents match this search.")).toBeVisible();
    await user.clear(search);
    await user.type(search, "release");
    await user.click(screen.getByRole("button", { name: "Select all" }));
    expect(screen.getByText("1 of 100 recipients selected")).toBeVisible();
    expect(screen.getByDisplayValue("GreenDog")).toBeChecked();
    expect(screen.queryByDisplayValue("BlueLake")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear selection" }));
    await user.clear(search);

    const greenDog = screen.getByDisplayValue("GreenDog");
    await user.click(greenDog);
    expect(screen.getByText("1 of 100 recipients selected")).toBeVisible();
    await user.click(greenDog);
    expect(screen.getByText("0 of 100 recipients selected")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Select all" }));
    expect(screen.getByText("3 of 100 recipients selected")).toBeVisible();
    for (const agent of projectAgentsResponse.items) {
      expect(screen.getByDisplayValue(agent.name)).toBeChecked();
    }
    await user.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.getByText("0 of 100 recipients selected")).toBeVisible();
  });

  it("renders recipient loading failures, empty projects, and session expiry honestly", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/agents", () =>
        HttpResponse.json({ detail: "private agent failure" }, { status: 500 }),
      ),
    );
    const failed = render(<App />);
    await waitForEnglishPreferences();
    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    expect(await screen.findByText(/Active agents could not be loaded/)).toBeVisible();
    expect(screen.queryByText("private agent failure")).not.toBeInTheDocument();
    failed.unmount();

    server.use(
      http.get("*/mail/api/v1/projects/:projectId/agents", ({ params }) =>
        HttpResponse.json({
          project_id: Number(params.projectId),
          project_generation: "d".repeat(64),
          items: [],
          total: 0,
        }),
      ),
    );
    const empty = render(<App />);
    await waitForEnglishPreferences();
    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    expect(await screen.findByText("No active agents are available in this project."))
      .toBeVisible();
    empty.unmount();

    const onUnauthorized = vi.fn();
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/agents", () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );
    render(<App onUnauthorized={onUnauthorized} />);
    await waitForEnglishPreferences();
    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledOnce());
    expect(screen.getByText(/Active agents could not be loaded/)).toBeVisible();
  });

  it("caps Select all at 100 canonical recipients", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    const agents = Array.from({ length: 101 }, (_, index) => ({
      agent_id: index + 1,
      agent_generation: (index % 16).toString(16).repeat(64),
      name: `Agent${String(index + 1).padStart(3, "0")}`,
      display_name: index === 100 ? "Overflow agent" : null,
      notify_sound: null,
    }));
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/agents", ({ params }) =>
        HttpResponse.json({
          project_id: Number(params.projectId),
          project_generation: "d".repeat(64),
          items: agents,
          total: agents.length,
        }),
      ),
    );
    render(<App />);
    await waitForEnglishPreferences();
    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    await screen.findByDisplayValue("Agent101");
    expect(
      screen.getByText(/101 active agents are available.*select up to 100/u),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Select all" }));
    expect(screen.getByText("100 of 100 recipients selected")).toBeVisible();
    expect(screen.getByDisplayValue("Agent100")).toBeChecked();
    expect(screen.getByDisplayValue("Agent101")).not.toBeChecked();
    expect(screen.getByDisplayValue("Agent101")).toBeDisabled();
  });

  it.each(["resolve", "reject"] as const)(
    "ignores a stale project-agent directory %s after switching projects",
    async (outcome) => {
      const user = userEvent.setup();
      window.history.replaceState({}, "", "/mail/#compose");
      const activeProjectTwo = {
        ...projectTwo,
        archived_at: null,
        role: "admin" as const,
        can_reply: true,
      };
      let oldRequests = 0;
      let resolveOld: (response: Response) => void = () => undefined;
      let rejectOld: (reason: unknown) => void = () => undefined;
      const oldDirectory = new Promise<Response>((resolve, reject) => {
        resolveOld = resolve;
        rejectOld = reject;
      });
      const originalFetch = globalThis.fetch.bind(globalThis);
      vi.stubGlobal(
        "fetch",
        (input: RequestInfo | URL, init?: RequestInit) => {
          const requestUrl =
            typeof input === "string"
              ? input
              : input instanceof URL
                ? input.href
                : input.url;
          if (requestUrl.endsWith(`/mail/api/v1/projects/${projectOne.id}/agents`)) {
            oldRequests += 1;
            return oldDirectory;
          }
          return originalFetch(input, init);
        },
      );
      server.use(
        http.get("*/mail/api/v1/projects", () =>
          HttpResponse.json({ items: [projectOne, activeProjectTwo], total: 2 }),
        ),
      );
      render(<App />);
      await waitForEnglishPreferences();
      await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
      await waitFor(() => expect(oldRequests).toBe(1));
      await user.selectOptions(
        screen.getByLabelText("Project"),
        String(activeProjectTwo.id),
      );
      expect(await screen.findByDisplayValue("GreenDog")).toBeVisible();

      await act(async () => {
        if (outcome === "resolve") {
          resolveOld(
            new Response(
              JSON.stringify({
                project_id: projectOne.id,
                project_generation: "a".repeat(64),
                items: [{
                  agent_id: 999,
                  agent_generation: "b".repeat(64),
                  name: "OldOnly",
                  display_name: null,
                  // Required by the parser. Without it the response fails to
                  // parse and the late handler never runs at all — which the
                  // assertions below cannot tell apart from the stale-guard
                  // doing its job, so the test would keep passing while
                  // silently stopping testing the guard.
                  notify_sound: null,
                }],
                total: 1,
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        } else {
          rejectOld(new TypeError("late old-directory failure"));
        }
        await Promise.resolve();
      });
      expect(screen.queryByDisplayValue("OldOnly")).not.toBeInTheDocument();
      expect(screen.getByDisplayValue("GreenDog")).toBeVisible();
    },
  );

  it("ignores an aborted project-agent request without replacing the picker", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    let agentRequests = 0;
    const originalFetch = globalThis.fetch.bind(globalThis);
    vi.stubGlobal(
      "fetch",
      (input: RequestInfo | URL, init?: RequestInit) => {
        const requestUrl =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        if (requestUrl.includes("/mail/api/v1/projects/11/agents")) {
          agentRequests += 1;
          return Promise.reject(new DOMException("aborted", "AbortError"));
        }
        return originalFetch(input, init);
      },
    );
    render(<App />);
    await waitForEnglishPreferences();
    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    await waitFor(() => expect(agentRequests).toBe(1));
    expect(screen.getByText("Loading active agents…")).toBeVisible();
    expect(screen.queryByText(/Active agents could not be loaded/)).not.toBeInTheDocument();
  });

  it("refreshes and reconciles lifetime-bound recipients after a typed 409 without retrying", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    let directoryReads = 0;
    let writes = 0;
    let releaseRefresh: () => void = () => undefined;
    const refreshGate = new Promise<void>((resolve) => {
      releaseRefresh = resolve;
    });
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/agents", async () => {
        directoryReads += 1;
        if (directoryReads === 1) {
          return HttpResponse.json(projectAgentsResponse);
        }
        await refreshGate;
        return HttpResponse.json({
          ...projectAgentsResponse,
          items: projectAgentsResponse.items.map((agent) =>
            agent.name === "BlueLake"
              ? { ...agent, agent_generation: "4".repeat(64) }
              : agent,
          ),
        });
      }),
      http.post("*/mail/api/v1/projects/:projectId/messages", () => {
        writes += 1;
        return HttpResponse.json(
          { detail: { code: "recipient_unavailable" } },
          { status: 409 },
        );
      }),
    );
    render(<App />);
    await waitForEnglishPreferences();
    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    await user.click(await screen.findByDisplayValue("GreenDog"));
    await user.click(screen.getByDisplayValue("BlueLake"));
    await user.type(screen.getByLabelText("Subject"), "Keep the draft");
    await user.type(screen.getByLabelText("Message in Markdown"), "Still valid text.");
    await user.click(screen.getByRole("button", { name: "Review message" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));

    expect(await screen.findByText(/Refreshing it before you can review again/)).toBeVisible();
    expect(writes).toBe(1);
    expect(screen.queryByRole("region", { name: "Review message before delivery" }))
      .not.toBeInTheDocument();
    await act(async () => {
      releaseRefresh();
      await refreshGate;
    });
    expect(await screen.findByText(/directory was refreshed/)).toBeVisible();
    expect(await screen.findByDisplayValue("GreenDog")).toBeChecked();
    expect(screen.getByDisplayValue("BlueLake")).not.toBeChecked();
    expect(screen.getByText("1 of 100 recipients selected")).toBeVisible();
    expect(screen.getByLabelText("Subject")).toHaveValue("Keep the draft");
    expect(screen.getByLabelText("Message in Markdown")).toHaveValue("Still valid text.");
    expect(directoryReads).toBe(2);
    expect(writes).toBe(1);
  });

  it("clears recipient selections when a typed 409 reveals a recreated project", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    let directoryReads = 0;
    let writes = 0;
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/agents", () => {
        directoryReads += 1;
        return HttpResponse.json(
          directoryReads === 1
            ? projectAgentsResponse
            : { ...projectAgentsResponse, project_generation: "e".repeat(64) },
        );
      }),
      http.post("*/mail/api/v1/projects/:projectId/messages", () => {
        writes += 1;
        return HttpResponse.json(
          { detail: { code: "project_recreated" } },
          { status: 409 },
        );
      }),
    );
    render(<App />);
    await waitForEnglishPreferences();
    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    await user.click(await screen.findByDisplayValue("GreenDog"));
    await user.type(screen.getByLabelText("Subject"), "Project lifetime");
    await user.type(screen.getByLabelText("Message in Markdown"), "Preserve this draft.");
    await user.click(screen.getByRole("button", { name: "Review message" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));

    expect(await screen.findByText(/directory was refreshed/)).toBeVisible();
    expect(screen.getByText("0 of 100 recipients selected")).toBeVisible();
    expect(screen.getByDisplayValue("GreenDog")).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Review message" })).toBeDisabled();
    expect(screen.getByLabelText("Subject")).toHaveValue("Project lifetime");
    expect(screen.getByLabelText("Message in Markdown")).toHaveValue("Preserve this draft.");
    expect(directoryReads).toBe(2);
    expect(writes).toBe(1);
  });

  it("reports a failed directory refresh for lifetime-invalid delivery without retrying", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    let directoryReads = 0;
    let writes = 0;
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/agents", () => {
        directoryReads += 1;
        return directoryReads === 1
          ? HttpResponse.json(projectAgentsResponse)
          : HttpResponse.json({ detail: "private refresh failure" }, { status: 503 });
      }),
      http.post("*/mail/api/v1/projects/:projectId/messages", () => {
        writes += 1;
        return HttpResponse.json(
          { detail: { code: "agent_lifetime_invalid" } },
          { status: 409 },
        );
      }),
    );
    render(<App />);
    await waitForEnglishPreferences();
    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    await user.click(await screen.findByDisplayValue("GreenDog"));
    await user.type(screen.getByLabelText("Subject"), "Refresh failure");
    await user.type(screen.getByLabelText("Message in Markdown"), "Draft survives.");
    await user.click(screen.getByRole("button", { name: "Review message" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));

    expect(await screen.findByText(/directory could not be refreshed/)).toBeVisible();
    expect(screen.queryByText("private refresh failure")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Message in Markdown")).toHaveValue("Draft survives.");
    expect(directoryReads).toBe(2);
    expect(writes).toBe(1);
  });

  it("reuses the same compose key through conflict and failed or pending polls", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    const keys: string[] = [];
    let writes = 0;
    let polls = 0;
    server.use(
      http.post("*/mail/api/v1/projects/:projectId/messages", async ({ request }) => {
        writes += 1;
        const body = (await request.json()) as { idempotency_key: string };
        keys.push(body.idempotency_key);
        return writes === 1
          ? HttpResponse.json(
              { detail: { code: "idempotency_conflict" } },
              { status: 409 },
            )
          : HttpResponse.json({
              ...deliveryResponse,
              status: "pending",
              message_id: null,
              commit_sha: null,
              next_attempt_ts: "2026-08-12T20:30:00Z",
            });
      }),
      http.post("*/mail/api/v1/deliveries/:deliveryId/retry", () => {
        polls += 1;
        if (polls === 1) {
          return HttpResponse.json(
            { detail: "private delivery failure" },
            { status: 503 },
          );
        }
        if (polls === 2) {
          return HttpResponse.json({
            ...deliveryResponse,
            status: "pending",
            message_id: null,
            commit_sha: null,
            next_attempt_ts: "2026-08-12T20:31:00Z",
            reused: true,
          });
        }
        return HttpResponse.json({ ...deliveryResponse, reused: true });
      }),
    );
    render(<App />);
    await waitForEnglishPreferences();
    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    await user.click(await screen.findByDisplayValue("GreenDog"));
    await user.type(screen.getByLabelText("Subject"), "Retry safely");
    await user.type(screen.getByLabelText("Message in Markdown"), "Keep this text.");

    await user.click(screen.getByRole("button", { name: "Review message" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));
    expect(await screen.findByText(/conflicts with an earlier request/)).toBeVisible();
    expect(screen.getByLabelText("Message in Markdown")).toHaveValue("Keep this text.");
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));
    expect(await screen.findByText("Accepted and waiting to publish.")).toBeVisible();
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);

    await user.click(screen.getByRole("button", { name: "Check status" }));
    expect(await screen.findByText(/could not be submitted/)).toBeVisible();
    expect(screen.queryByText("private delivery failure")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Message in Markdown")).toHaveValue("Keep this text.");

    await user.click(screen.getByRole("button", { name: "Review message" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));
    expect(await screen.findByText("Accepted and waiting to publish.")).toBeVisible();
    expect(keys).toHaveLength(3);
    expect(new Set(keys)).toEqual(new Set([keys[0]]));

    await user.click(screen.getByRole("button", { name: "Check status" }));
    await waitFor(() => expect(polls).toBe(2));
    expect(screen.getByText("Accepted and waiting to publish.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Check status" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Check status" }));
    expect(await screen.findByText("Published exactly once.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Check status" })).not.toBeInTheDocument();
  });

  it("reuses the same compose key after an ambiguous failure and navigation", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    const keys: string[] = [];
    server.use(
      http.post("*/mail/api/v1/projects/:projectId/messages", async ({ request }) => {
        const body = (await request.json()) as { idempotency_key: string };
        keys.push(body.idempotency_key);
        return keys.length === 1
          ? HttpResponse.json({ detail: "ambiguous upstream failure" }, { status: 503 })
          : HttpResponse.json(deliveryResponse);
      }),
    );
    render(<App />);
    await waitForEnglishPreferences();
    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    await user.click(await screen.findByDisplayValue("GreenDog"));
    await user.type(screen.getByLabelText("Subject"), "Do not duplicate");
    await user.type(screen.getByLabelText("Message in Markdown"), "One durable intent.");
    await user.click(screen.getByRole("button", { name: "Review message" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));
    expect(await screen.findByText(/could not be submitted/)).toBeVisible();

    await user.click(screen.getByRole("link", { name: "Inbox" }));
    expect(await screen.findByRole("heading", { name: "Inbox" })).toBeVisible();
    await user.click(screen.getByRole("link", { name: "Compose" }));
    expect(await screen.findByRole("heading", { name: "Compose" })).toBeVisible();
    expect(screen.getByLabelText("Project")).toHaveValue(String(projectOne.id));
    expect(screen.getByLabelText("Subject")).toHaveValue("Do not duplicate");
    expect(screen.getByLabelText("Message in Markdown")).toHaveValue("One durable intent.");
    await user.click(await screen.findByDisplayValue("GreenDog"));
    await user.click(screen.getByRole("button", { name: "Review message" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));

    expect(await screen.findByText("Published exactly once.")).toBeVisible();
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
  });

  it("keeps a reply idempotent through submission and status failures", async () => {
    const user = userEvent.setup();
    const keys: string[] = [];
    let writes = 0;
    let polls = 0;
    server.use(
      http.post(
        "*/mail/api/v1/projects/:projectId/messages/:messageId/replies",
        async ({ request }) => {
          writes += 1;
          const body = (await request.json()) as { idempotency_key: string };
          keys.push(body.idempotency_key);
          if (writes === 1) {
            return HttpResponse.json(
              { detail: "private reply failure" },
              { status: 503 },
            );
          }
          return HttpResponse.json({
            ...deliveryResponse,
            status: "pending",
            message_id: null,
            commit_sha: null,
            next_attempt_ts: "2026-08-12T20:32:00Z",
          });
        },
      ),
      http.post("*/mail/api/v1/deliveries/:deliveryId/retry", () => {
        polls += 1;
        if (polls === 1) {
          return HttpResponse.json(
            { detail: "private status failure" },
            { status: 503 },
          );
        }
        if (polls === 2) {
          return HttpResponse.json({
            ...deliveryResponse,
            status: "pending",
            message_id: null,
            commit_sha: null,
            next_attempt_ts: "2026-08-12T20:33:00Z",
            reused: true,
          });
        }
        return HttpResponse.json({ ...deliveryResponse, reused: true });
      }),
    );
    render(<App />);
    await user.click(
      await screen.findByRole("link", {
        name: new RegExp(`Open message.*${messageOne.subject}`),
      }),
    );
    await user.type(await screen.findByLabelText("Reply in Markdown"), "Keep this reply.");

    await user.click(screen.getByRole("button", { name: "Review reply" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));
    expect(await screen.findByText(/could not be submitted/)).toBeVisible();
    expect(screen.queryByText("private reply failure")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Reply in Markdown")).toHaveValue("Keep this reply.");

    await user.click(screen.getByRole("button", { name: "Confirm and send" }));
    expect(await screen.findByText("Accepted and waiting to publish.")).toBeVisible();
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);

    await user.click(screen.getByRole("button", { name: "Check status" }));
    expect(await screen.findByText(/could not be submitted/)).toBeVisible();
    expect(screen.queryByText("private status failure")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Review reply" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));
    expect(await screen.findByText("Accepted and waiting to publish.")).toBeVisible();
    expect(new Set(keys)).toEqual(new Set([keys[0]]));

    await user.click(screen.getByRole("button", { name: "Check status" }));
    await waitFor(() => expect(polls).toBe(2));
    expect(screen.getByText("Accepted and waiting to publish.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Check status" }));
    expect(await screen.findByText("Published exactly once.")).toBeVisible();
    expect(screen.getByLabelText("Reply in Markdown")).toHaveValue("Keep this reply.");
  });

  it("reuses the same reply key after a pending response and navigation", async () => {
    const user = userEvent.setup();
    const keys: string[] = [];
    server.use(
      http.post(
        "*/mail/api/v1/projects/:projectId/messages/:messageId/replies",
        async ({ request }) => {
          const body = (await request.json()) as { idempotency_key: string };
          keys.push(body.idempotency_key);
          return keys.length === 1
            ? HttpResponse.json({
                ...deliveryResponse,
                status: "pending",
                message_id: null,
                commit_sha: null,
                next_attempt_ts: "2026-08-12T20:34:00Z",
              })
            : HttpResponse.json({ ...deliveryResponse, reused: true });
        },
      ),
    );
    render(<App />);
    await user.click(
      await screen.findByRole("link", {
        name: new RegExp(`Open message.*${messageOne.subject}`),
      }),
    );
    await user.type(await screen.findByLabelText("Reply in Markdown"), "One reply only.");
    await user.click(screen.getByRole("button", { name: "Review reply" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));
    expect(await screen.findByText("Accepted and waiting to publish.")).toBeVisible();

    await user.click(screen.getByRole("link", { name: "Inbox" }));
    await user.click(
      await screen.findByRole("link", {
        name: new RegExp(`Open message.*${messageOne.subject}`),
      }),
    );
    await user.type(await screen.findByLabelText("Reply in Markdown"), "One reply only.");
    await user.click(screen.getByRole("button", { name: "Review reply" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));

    expect(await screen.findByText("Published exactly once.")).toBeVisible();
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
  });

  it("multiplexes pending reply attempts across A to B to A navigation", async () => {
    const user = userEvent.setup();
    const secondMessage = {
      ...messageOne,
      id: 202,
      subject: "Second immutable reply target",
      sender: "SecondAgent",
      sender_name: "SecondAgent",
      sender_display_name: "Second operator",
      thread_id: "release-202",
    };
    const secondDetail = {
      ...messageDetail,
      ...secondMessage,
      reply_target: {
        agent_id: 42,
        agent_generation: "5".repeat(64),
        project_id: projectOne.id,
        project_generation: projectAgentsResponse.project_generation,
        canonical_name: "SecondAgent",
      },
    };
    const attempts: Array<{ messageId: string; key: string }> = [];
    server.use(
      http.get("*/mail/api/v1/inbox", () =>
        HttpResponse.json({
          items: [messageOne, secondMessage],
          total: 2,
          next_cursor: null,
        }),
      ),
      http.get(
        "*/mail/api/v1/projects/:projectId/messages/:messageId",
        ({ params }) =>
          params.messageId === String(messageOne.id)
            ? HttpResponse.json(messageDetail)
            : HttpResponse.json(secondDetail),
      ),
      http.post(
        "*/mail/api/v1/projects/:projectId/messages/:messageId/replies",
        async ({ params, request }) => {
          const body = (await request.json()) as { idempotency_key: string };
          attempts.push({
            messageId: String(params.messageId),
            key: body.idempotency_key,
          });
          return HttpResponse.json({
            ...deliveryResponse,
            status: "pending",
            message_id: null,
            commit_sha: null,
            next_attempt_ts: "2026-08-12T20:35:00Z",
          });
        },
      ),
    );
    render(<App />);

    const sendPendingReply = async (subject: string, body: string) => {
      await user.click(
        await screen.findByRole("link", {
          name: new RegExp(`Open message.*${subject}`),
        }),
      );
      await user.type(await screen.findByLabelText("Reply in Markdown"), body);
      await user.click(screen.getByRole("button", { name: "Review reply" }));
      await user.click(screen.getByRole("button", { name: "Confirm and send" }));
      expect(await screen.findByText("Accepted and waiting to publish.")).toBeVisible();
      await user.click(screen.getByRole("link", { name: "Inbox" }));
    };

    await sendPendingReply(messageOne.subject, "First reply body.");
    await sendPendingReply(secondMessage.subject, "Second reply body.");
    await sendPendingReply(messageOne.subject, "First reply body.");

    expect(attempts).toHaveLength(3);
    expect(attempts[2]!.key).toBe(attempts[0]!.key);
    expect(attempts[1]!.key).not.toBe(attempts[0]!.key);
  });

  it("keeps the reply key when only the sender display alias changes", async () => {
    const user = userEvent.setup();
    const keys: string[] = [];
    let senderAlias = "Original alias";
    server.use(
      http.get(
        "*/mail/api/v1/projects/:projectId/messages/:messageId",
        () => HttpResponse.json({
          ...messageDetail,
          sender_display_name: senderAlias,
        }),
      ),
      http.post(
        "*/mail/api/v1/projects/:projectId/messages/:messageId/replies",
        async ({ request }) => {
          const body = (await request.json()) as { idempotency_key: string };
          keys.push(body.idempotency_key);
          return HttpResponse.json({
            ...deliveryResponse,
            status: "pending",
            message_id: null,
            commit_sha: null,
            next_attempt_ts: "2026-08-12T20:36:00Z",
          });
        },
      ),
    );
    render(<App />);

    await user.click(
      await screen.findByRole("link", {
        name: new RegExp(`Open message.*${messageOne.subject}`),
      }),
    );
    await user.type(await screen.findByLabelText("Reply in Markdown"), "Alias-safe reply.");
    await user.click(screen.getByRole("button", { name: "Review reply" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));
    expect(await screen.findByText("Accepted and waiting to publish.")).toBeVisible();

    senderAlias = "Renamed alias";
    await user.click(screen.getByRole("link", { name: "Inbox" }));
    await user.click(
      await screen.findByRole("link", {
        name: new RegExp(`Open message.*${messageOne.subject}`),
      }),
    );
    expect(await screen.findByText(/Renamed alias/)).toBeVisible();
    await user.type(screen.getByLabelText("Reply in Markdown"), "Alias-safe reply.");
    await user.click(screen.getByRole("button", { name: "Review reply" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));

    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
  });

  it("renders compose initialization failures and an empty active-project set", async () => {
    window.history.replaceState({}, "", "/mail/#compose");
    server.use(
      http.get("*/mail/api/v1/me/profile", () =>
        HttpResponse.json({ detail: "private profile failure" }, { status: 500 }),
      ),
    );
    const failed = render(<App />);
    expect(await screen.findByText(/could not be loaded/)).toBeVisible();
    expect(screen.queryByText("private profile failure")).not.toBeInTheDocument();
    failed.unmount();

    server.use(
      http.get("*/mail/api/v1/me/profile", () => HttpResponse.json(adminProfile)),
      http.get("*/mail/api/v1/projects", () =>
        HttpResponse.json({
          items: [projectTwo],
          total: 1,
        }),
      ),
    );
    render(<App />);
    expect(
      await screen.findByText("There are no active projects available for compose."),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Review message" })).not.toBeInTheDocument();
  });

  it("fails compose validation and preserves text on server or session errors", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    const onUnauthorized = vi.fn();
    server.use(
      http.post("*/mail/api/v1/projects/:projectId/messages", () =>
        HttpResponse.json({ detail: { code: "actor_forbidden" } }, { status: 401 }),
      ),
    );
    render(<App onUnauthorized={onUnauthorized} />);
    await waitForEnglishPreferences();
    fireEvent.submit(screen.getByRole("button", { name: "Review message" }));
    expect(await screen.findByText(/could not be submitted/)).toBeVisible();

    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    await user.click(await screen.findByDisplayValue("GreenDog"));
    await user.type(screen.getByLabelText("Subject"), "Stay put");
    await user.type(screen.getByLabelText("Message in Markdown"), "Unsaved text");
    await user.click(screen.getByRole("button", { name: "Review message" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledOnce());
    expect(screen.getByLabelText("Message in Markdown")).toHaveValue("Unsaved text");
  });

  it("renders quarantine and hides compose from a non-administrator", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#compose");
    server.use(
      http.post("*/mail/api/v1/projects/:projectId/messages", () =>
        HttpResponse.json({
          ...deliveryResponse,
          status: "quarantined",
          message_id: null,
          commit_sha: null,
        }),
      ),
    );
    const adminView = render(<App />);
    await waitForEnglishPreferences();
    await user.selectOptions(screen.getByLabelText("Project"), String(projectOne.id));
    await user.click(await screen.findByDisplayValue("GreenDog"));
    await user.type(screen.getByLabelText("Subject"), "Quarantine");
    await user.type(screen.getByLabelText("Message in Markdown"), "Review me");
    await user.click(screen.getByRole("button", { name: "Review message" }));
    await user.click(screen.getByRole("button", { name: "Confirm and send" }));
    expect(await screen.findByText(/quarantined for manual review/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Check status" })).not.toBeInTheDocument();
    adminView.unmount();

    server.use(
      http.get("*/mail/api/v1/me/profile", () => HttpResponse.json(memberProfile)),
    );
    render(<App />);
    expect(await screen.findByText(/Administrator access is required/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Review message" })).not.toBeInTheDocument();
  });

  it("loads a stored Polish UI locale while keeping correspondence independent", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json(preferencesResponse("pl", "en")),
      ),
    );
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Skrzynka" })).toBeVisible();
    expect(document.documentElement).toHaveAttribute("lang", "pl");
    expect(screen.getByRole("navigation", { name: "Główna nawigacja" })).toBeVisible();
    expect(screen.getByText(messageOne.subject)).toBeVisible();
    expect(localePickerTrigger()).toHaveAttribute("data-locale", "pl");
    expect(localePickerTrigger()).toHaveAccessibleDescription(
      "Język zapisany na Twoim koncie",
    );
    expect(screen.getByRole("button", { name: "Wyloguj się" })).toBeVisible();

    await user.click(screen.getByRole("link", { name: "Napisz" }));
    expect(await screen.findByRole("heading", { name: "Nowa wiadomość" })).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Projekt"), String(projectOne.id));
    await user.click(await screen.findByDisplayValue("GreenDog"));
    await user.type(screen.getByLabelText("Temat"), "Polski interfejs");
    await user.type(screen.getByLabelText("Wiadomość w Markdown"), "Treść");
    await user.click(screen.getByRole("button", { name: "Sprawdź wiadomość" }));
    const confirmation = screen.getByRole("region", {
      name: "Sprawdź wiadomość przed dostawą",
    });
    expect(within(confirmation).getByText(/polecenie Human Overseer o wysokim priorytecie/))
      .toBeVisible();
    expect(within(confirmation).getByText(/prefers replies in English \(en\)/)).toBeVisible();
    expect(within(confirmation).getByRole("button", { name: "Potwierdź i wyślij" }))
      .toBeVisible();
  });

  it("uses correct English and Polish plural forms", async () => {
    await i18n.changeLanguage("en");
    expect(i18n.t("inbox.count", { count: 1 })).toBe("1 message");
    expect(i18n.t("inbox.count", { count: 2 })).toBe("2 messages");
    await i18n.changeLanguage("pl");
    expect(i18n.t("projects.count", { count: 1 })).toBe("1 projekt");
    expect(i18n.t("projects.count", { count: 2 })).toBe("2 projekty");
    expect(i18n.t("projects.count", { count: 5 })).toBe("5 projektów");
  });

  it("persists only the UI locale with same-origin no-store requests", async () => {
    const user = userEvent.setup();
    let preferenceReads = 0;
    let capturedRequest: Request | undefined;
    let capturedBody: unknown;
    let finishRequest: () => void = () => undefined;
    const requestGate = new Promise<void>((resolve) => {
      finishRequest = resolve;
    });

    server.use(
      http.get(preferencesUrl, () => {
        preferenceReads += 1;
        return HttpResponse.json(preferencesResponse("en"));
      }),
      http.patch(preferencesUrl, async ({ request }) => {
        capturedRequest = request;
        capturedBody = await request.json();
        await requestGate;
        return HttpResponse.json(preferencesResponse("pl"));
      }),
    );

    render(<App />);
    await waitForEnglishPreferences();

    await selectUiLocale(user, "Polski");
    expect(await screen.findByText("Saving language…")).toBeVisible();
    expect(localePickerTrigger()).toBeDisabled();
    expect(screen.getByRole("heading", { name: "Inbox" })).toBeVisible();

    expect(capturedRequest?.method).toBe("PATCH");
    expect(capturedRequest?.credentials).toBe("same-origin");
    expect(capturedRequest?.cache).toBe("no-store");
    expect(capturedRequest?.headers.get("accept")).toBe("application/json");
    expect(capturedRequest?.headers.get("content-type")).toBe("application/json");
    expect(capturedBody).toEqual({ preferred_ui_locale: "pl" });
    expect(preferenceReads).toBe(1);

    act(() => finishRequest());

    expect(await screen.findByRole("heading", { name: "Skrzynka" })).toBeVisible();
    expect(preferenceReads).toBe(1);
    expect(localePickerTrigger()).toHaveAttribute("data-locale", "pl");
    expect(screen.getByRole("status")).toHaveTextContent("Język zapisany na Twoim koncie");
  });

  it("ignores the current locale and overlapping choices while a catalog is loading", async () => {
    const user = userEvent.setup();
    let finishCatalogLoad: () => void = () => undefined;
    const catalogGate = new Promise<void>((resolve) => {
      finishCatalogLoad = resolve;
    });
    const prepareLocaleCatalog = vi.fn(async (locale: SupportedLocale) => {
      if (locale === "pl") {
        await catalogGate;
      }
    });
    let patchRequests = 0;
    server.use(
      http.patch(preferencesUrl, () => {
        patchRequests += 1;
        return HttpResponse.json(preferencesResponse("pl"));
      }),
    );

    render(<App prepareLocaleCatalog={prepareLocaleCatalog} />);
    await waitForEnglishPreferences();

    await user.click(localePickerTrigger());
    await user.click(screen.getByRole("option", { name: /current language: english/i }));
    expect(prepareLocaleCatalog).not.toHaveBeenCalled();

    await user.click(localePickerTrigger());
    const polish = screen.getByRole("option", { name: /use polski/i });
    const french = screen.getByRole("option", { name: /use français/i });
    act(() => {
      fireEvent.click(polish);
      fireEvent.click(french);
    });

    expect(prepareLocaleCatalog).toHaveBeenCalledTimes(1);
    expect(prepareLocaleCatalog).toHaveBeenCalledWith("pl");
    expect(patchRequests).toBe(0);
    act(() => finishCatalogLoad());
    expect(await screen.findByRole("heading", { name: "Skrzynka" })).toBeVisible();
    expect(patchRequests).toBe(1);
  });

  it("keeps the future API boundary testable through MSW", async () => {
    const response = await fetch("http://localhost/mail/api/v1/health");

    await expect(response.json()).resolves.toEqual({ status: "ok" });
  });

  it("calls the injected login callback on an unauthorized preference read", async () => {
    window.history.replaceState({}, "", "/mail/?project=mail#inbox");
    const onUnauthorized = vi.fn();
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );

    render(<App onUnauthorized={onUnauthorized} />);

    await waitFor(() =>
      expect(onUnauthorized).toHaveBeenCalledWith(
        "/mail/login?next=%2Fmail%2F%3Fproject%3Dmail%23inbox",
      ),
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Your session expired. Redirecting to sign in.",
    );
    expect(localePickerTrigger()).toBeDisabled();
  });

  it("uses same-tab login navigation when no unauthorized callback is supplied", async () => {
    window.history.replaceState({}, "", "/mail/#projects");
    const navigateTo = vi.fn();
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );

    render(<App navigateTo={navigateTo} />);

    await waitFor(() =>
      expect(navigateTo).toHaveBeenCalledWith(
        "/mail/login?next=%2Fmail%2F%23projects",
      ),
    );
  });

  it("fails loud and falls back to an English preview for invalid responses", async () => {
    document.documentElement.lang = "pl";
    await i18n.changeLanguage("pl");
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json({
          ...preferencesResponse("pl"),
          stored: {
            preferred_ui_locale: "not-a-locale",
            preferred_correspondence_locale: null,
          },
        }),
      ),
    );

    render(<App />);

    expect(
      await screen.findByText(
        "Could not load your saved language. Changes apply only for this visit.",
      ),
    ).toBeVisible();
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
    expect(document.documentElement).toHaveAttribute("lang", "en");
    expect(screen.getByRole("heading", { name: "Inbox" })).toBeVisible();
    expect(localePickerTrigger()).toBeEnabled();
  });

  it("keeps language changes local when the preference read fails", async () => {
    const user = userEvent.setup();
    let patchRequests = 0;
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json({ detail: "unavailable" }, { status: 503 }),
      ),
      http.patch(preferencesUrl, () => {
        patchRequests += 1;
        return HttpResponse.json({ detail: "must not be called" }, { status: 500 });
      }),
    );

    render(<App />);

    expect(
      await screen.findByText(
        "Could not load your saved language. Changes apply only for this visit.",
      ),
    ).toBeVisible();
    expect(localePickerTrigger()).toHaveAttribute("data-locale", "en");

    await selectUiLocale(user, "Polski");

    expect(patchRequests).toBe(0);
    expect(document.documentElement).toHaveAttribute("lang", "pl");
    expect(screen.getByRole("heading", { name: "Skrzynka" })).toBeVisible();
    expect(localePickerTrigger()).toHaveAttribute("data-locale", "pl");
    expect(screen.getByRole("status")).toHaveTextContent(
      "Nie udało się wczytać zapisanego języka. Zmiany obowiązują tylko podczas tej wizyty.",
    );
  });

  it("keeps the previous locale when saving fails", async () => {
    const user = userEvent.setup();
    server.use(
      http.patch(preferencesUrl, () =>
        HttpResponse.json({ detail: "unavailable" }, { status: 503 }),
      ),
    );
    render(<App />);
    await waitForEnglishPreferences();

    await selectUiLocale(user, "Polski");

    expect(
      await screen.findByText(
        "Could not save your language. Your previous language is still active.",
      ),
    ).toBeVisible();
    expect(localePickerTrigger()).toHaveAttribute("data-locale", "en");
    expect(document.documentElement).toHaveAttribute("lang", "en");
    expect(screen.getByRole("heading", { name: "Inbox" })).toBeVisible();
  });

  it("loads a locale catalog before persisting the preference", async () => {
    const user = userEvent.setup();
    let patchRequests = 0;
    server.use(
      http.patch(preferencesUrl, () => {
        patchRequests += 1;
        return HttpResponse.json(preferencesResponse("fr"));
      }),
    );
    const prepareLocaleCatalog = vi.fn(async (locale: SupportedLocale) => {
      if (locale === "fr") {
        throw new Error("locale chunk unavailable");
      }
    });

    render(<App prepareLocaleCatalog={prepareLocaleCatalog} />);
    await waitForEnglishPreferences();

    await selectUiLocale(user, "Français");

    await waitFor(() =>
      expect(prepareLocaleCatalog).toHaveBeenCalledWith("fr"),
    );
    expect(patchRequests).toBe(0);
    expect(document.documentElement).toHaveAttribute("lang", "en");
    expect(screen.getByRole("heading", { name: "Inbox" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Could not save your language. Your previous language is still active.",
    );
  });

  it("rolls back and redirects when the preference write loses authorization", async () => {
    const user = userEvent.setup();
    const onUnauthorized = vi.fn();
    server.use(
      http.patch(preferencesUrl, () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );
    render(<App onUnauthorized={onUnauthorized} />);
    await waitForEnglishPreferences();

    await selectUiLocale(user, "Polski");

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledOnce());
    expect(localePickerTrigger()).toHaveAttribute("data-locale", "en");
    expect(localePickerTrigger()).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Your session expired. Redirecting to sign in.",
    );
  });

  it.each([
    [
      "preferred UI locale",
      {
        ...preferencesResponse("en"),
        stored: {
          preferred_ui_locale: "not-a-locale",
          preferred_correspondence_locale: null,
        },
      },
    ],
    [
      "preferred correspondence locale",
      {
        ...preferencesResponse("en"),
        stored: {
          preferred_ui_locale: "en",
          preferred_correspondence_locale: "not-a-locale",
        },
      },
    ],
    [
      "effective UI locale",
      {
        ...preferencesResponse("en"),
        effective: { ui_locale: "not-a-locale", correspondence_locale: "en" },
      },
    ],
    [
      "effective correspondence locale",
      {
        ...preferencesResponse("en"),
        effective: { ui_locale: "en", correspondence_locale: "not-a-locale" },
      },
    ],
  ])("rejects an invalid %s at runtime", (_label, payload) => {
    expect(() => parsePreferences(payload)).toThrow(TypeError);
  });

  it("opens a real message detail and renders Markdown without raw HTML", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(
      await screen.findByRole("link", {
        name: new RegExp(`Open message.*${messageOne.subject}`),
      }),
    );

    expect(
      await screen.findByRole("heading", { name: messageOne.subject, level: 1 }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "Release", level: 2 })).toBeVisible();
    expect(screen.getByText("<script>", { selector: "code" })).toBeVisible();
    expect(document.querySelector(".message-body script")).not.toBeInTheDocument();
    expect(screen.getByText("codex-wsl-home-1")).toBeVisible();
    expect(screen.getByText("2 attachments")).toBeVisible();
    expect(
      screen.getByText("artifact, application/json, 1,280 B"),
    ).toBeVisible();
    expect(
      screen.getByText("attachment, unknown media type, unknown size"),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: /Back to inbox/ })).toHaveAttribute(
      "href",
      `#inbox?project=${projectOne.id}`,
    );
  });

  it("renders message detail without linking a malformed persisted thread", async () => {
    const malformedThreadId = "\ud800";
    window.history.replaceState(
      {},
      "",
      `/mail/#message/${projectOne.id}/${messageOne.id}`,
    );
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/messages/:messageId", () =>
        HttpResponse.json({ ...messageDetail, thread_id: malformedThreadId }),
      ),
    );
    render(<App />);

    expect(
      await screen.findByRole("heading", { name: messageOne.subject, level: 1 }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "Release", level: 2 })).toBeVisible();
    expect(document.querySelector(".thread-inline-link")).not.toBeInTheDocument();
  });

  it("omits malformed inbox thread links but keeps the numeric fallback", async () => {
    server.use(
      http.get("*/mail/api/v1/inbox", () =>
        HttpResponse.json({
          ...inboxResponse,
          items: [{ ...messageOne, thread_id: "" }, messageTwo],
        }),
      ),
    );
    render(<App />);

    expect(await screen.findByText(messageOne.subject)).toBeVisible();
    expect(screen.getByText(messageTwo.subject)).toBeVisible();
    expect(
      screen.queryByRole("link", { name: "Thread:" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: `Thread: ${messageTwo.id}` }),
    ).toHaveAttribute("href", `#thread/${projectTwo.id}/${messageTwo.id}`);
  });

  it("opens a linked thread as an accessible oldest-to-newest conversation", async () => {
    const user = userEvent.setup();
    const unsafeThreadResponse = {
      ...threadResponse,
      items: [
        threadReply,
        {
          ...messageDetail,
          body_md: [
            "# Release",
            "![remote tracker](https://tracker.invalid/pixel.png)",
            "<script>window.__threadExecuted = true</script>",
          ].join("\n\n"),
          cc: ["release-observer"],
          created_ts: threadReply.created_ts,
          sender_display_name: null,
          sender_name: messageDetail.sender,
          to: [],
        },
      ],
    };
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/threads", () =>
        HttpResponse.json(unsafeThreadResponse),
      ),
    );
    render(<App />);

    const threadLink = await screen.findByRole("link", {
      name: `Thread: ${messageOne.thread_id}`,
    });
    expect(threadLink).toHaveAttribute(
      "href",
      `#thread/${projectOne.id}/${messageOne.thread_id}`,
    );
    expect(
      screen.getByRole("link", { name: `Thread: ${messageTwo.id}` }),
    ).toHaveAttribute(
      "href",
      `#thread/${projectTwo.id}/${messageTwo.id}`,
    );
    await user.click(threadLink);

    expect(
      await screen.findByRole("heading", { name: messageOne.subject, level: 1 }),
    ).toBeVisible();
    expect(
      screen.getByText(messageOne.thread_id, { selector: "code" }),
    ).toBeVisible();
    expect(screen.getByText("2 messages")).toBeVisible();
    const cards = document.querySelectorAll<HTMLDetailsElement>(".thread-message");
    expect(cards).toHaveLength(2);
    expect(cards[0]).not.toHaveAttribute("open");
    expect(cards[1]).toHaveAttribute("open");
    expect(screen.getByRole("heading", { name: "Follow-up", level: 3 }))
      .toBeVisible();
    expect(screen.queryByRole("heading", { name: "Release" }))
      .not.toBeInTheDocument();

    await user.click(within(cards[0]!).getByText(messageOne.subject));
    expect(cards[0]).toHaveAttribute("open");
    expect(await screen.findByRole("heading", { name: "Release", level: 2 }))
      .toBeVisible();
    expect(screen.getByText("remote tracker", { exact: true })).toHaveClass(
      "markdown-image-alt",
    );
    expect(document.querySelector(".thread-message img")).not.toBeInTheDocument();
    expect(document.querySelector(".thread-message script")).not.toBeInTheDocument();
    expect(
      within(cards[0]!).getByRole("link", {
        name: `Open message: ${messageOne.subject}`,
      }),
    ).toHaveAttribute(
      "href",
      `#message/${projectOne.id}/${messageOne.id}`,
    );
    expect(screen.getByRole("link", { name: /Back to inbox/ })).toHaveAttribute(
      "href",
      `#inbox?project=${projectOne.id}`,
    );
  });

  it("orders canonical UTC thread timestamps by microsecond and then exact id", async () => {
    const earlierMicrosecond = {
      ...messageDetail,
      id: 902,
      subject: "Earlier microsecond",
      created_ts: "2026-08-11T11:15:00.000001Z",
    };
    const laterMicrosecond = {
      ...threadReply,
      id: 101,
      subject: "Later microsecond",
      created_ts: "2026-08-11T11:15:00.000002Z",
    };
    const equalTimestampLowerId = {
      ...messageDetail,
      id: 303,
      subject: "Equal instant lower id",
      created_ts: "2026-08-11T11:15:00.000003Z",
    };
    const equalTimestampHigherId = {
      ...threadReply,
      id: 404,
      subject: "Equal instant higher id",
      created_ts: equalTimestampLowerId.created_ts,
    };
    window.history.replaceState(
      {},
      "",
      `/mail/#thread/${projectOne.id}/${messageOne.thread_id}`,
    );
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/threads", () =>
        HttpResponse.json({
          items: [
            earlierMicrosecond,
            laterMicrosecond,
            equalTimestampHigherId,
            equalTimestampLowerId,
          ],
          total: 4,
          next_cursor: null,
          subject: messageOne.subject,
        }),
      ),
    );
    render(<App />);

    expect(
      await screen.findByRole("heading", { name: messageOne.subject, level: 1 }),
    ).toBeVisible();
    const summaries = [
      ...document.querySelectorAll(".thread-message > summary strong"),
    ].map((element) => element.textContent);
    expect(summaries).toEqual([
      "Earlier microsecond",
      "Later microsecond",
      "Equal instant lower id",
      "Equal instant higher id",
    ]);
  });

  it(
    "paginates a newest-first thread without duplicates and displays it oldest-first",
    async () => {
      const user = userEvent.setup();
      const cursors: Array<string | null> = [];
      window.history.replaceState(
        {},
        "",
        `/mail/#thread/${projectOne.id}/${messageOne.thread_id}`,
      );
      server.use(
        http.get(
          "*/mail/api/v1/projects/:projectId/threads",
          ({ request }) => {
            const cursor = new URL(request.url).searchParams.get("cursor");
            cursors.push(cursor);
            return cursor === null
              ? HttpResponse.json({
                  items: [threadReply],
                  total: 2,
                  next_cursor: "older-page",
                  subject: messageOne.subject,
                })
              : HttpResponse.json({
                  items: [messageDetail, threadReply],
                  total: 2,
                  next_cursor: null,
                  subject: messageOne.subject,
                });
          },
        ),
      );
      render(<App />);

      expect(
        await screen.findByRole("heading", {
          name: messageOne.subject,
          level: 1,
        }),
      ).toBeVisible();
      await user.click(screen.getByRole("button", { name: "Load more" }));
      expect(
        await screen.findByRole("heading", {
          name: messageOne.subject,
          level: 1,
        }),
      ).toBeVisible();
      const summaries = [
        ...document.querySelectorAll(".thread-message > summary strong"),
      ].map((element) => element.textContent);
      expect(summaries).toEqual([messageOne.subject, threadReply.subject]);
      expect(document.querySelectorAll(".thread-message")).toHaveLength(2);
      expect(screen.queryByRole("button", { name: "Load more" }))
        .not.toBeInTheDocument();
      expect(cursors).toEqual([null, "older-page"]);
    },
  );

  it.each([
    [
      "empty",
      200,
      { items: [], total: 0, next_cursor: null, subject: "" },
      "There are no messages in this view.",
    ],
    ["error", 500, { detail: "private failure" }, "This message could not be loaded."],
  ] as const)("renders the thread %s state without reflecting private errors", async (
    _state,
    status,
    payload,
    expected,
  ) => {
    window.history.replaceState(
      {},
      "",
      `/mail/#thread/${projectOne.id}/${messageOne.thread_id}`,
    );
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/threads", () =>
        HttpResponse.json(payload, { status }),
      ),
    );
    render(<App />);

    expect(await screen.findByText(expected)).toBeVisible();
    expect(screen.queryByText("private failure")).not.toBeInTheDocument();
    if (_state === "empty") {
      expect(
        screen.getByRole("heading", {
          name: messageOne.thread_id,
          level: 1,
        }),
      ).toBeVisible();
    }
  });

  it("keeps loaded thread messages when an older-page request fails", async () => {
    const user = userEvent.setup();
    window.history.replaceState(
      {},
      "",
      `/mail/#thread/${projectOne.id}/${messageOne.thread_id}`,
    );
    server.use(
      http.get(
        "*/mail/api/v1/projects/:projectId/threads",
        ({ request }) =>
          new URL(request.url).searchParams.has("cursor")
            ? HttpResponse.json({ detail: "private page failure" }, { status: 500 })
            : HttpResponse.json({
                items: [threadReply],
                total: 2,
                next_cursor: "failure-cursor",
                subject: messageOne.subject,
              }),
      ),
    );
    render(<App />);

    expect(
      await screen.findByRole("heading", { name: messageOne.subject, level: 1 }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(
      await screen.findByText("More messages could not be loaded. Try again."),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", { name: messageOne.subject, level: 1 }),
    ).toBeVisible();
    expect(screen.queryByText("private page failure")).not.toBeInTheDocument();
  });

  it.each(["resolve", "reject"] as const)(
    "ignores a stale initial thread %s after navigating to another thread",
    async (outcome) => {
      const oldThreadId = "old-thread";
      const newThreadId = "new-thread";
      let oldRequests = 0;
      let resolveOld: (response: Response) => void = () => undefined;
      let rejectOld: (reason: unknown) => void = () => undefined;
      const oldThread = new Promise<Response>((resolve, reject) => {
        resolveOld = resolve;
        rejectOld = reject;
      });
      const originalFetch = globalThis.fetch.bind(globalThis);
      vi.stubGlobal(
        "fetch",
        (input: RequestInfo | URL, init?: RequestInit) => {
          const url = new URL(
            input instanceof Request ? input.url : String(input),
            window.location.origin,
          );
          if (
            url.pathname.endsWith("/threads") &&
            url.searchParams.get("thread_id") === oldThreadId
          ) {
            oldRequests += 1;
            return oldThread;
          }
          return originalFetch(input, init);
        },
      );
      window.history.replaceState(
        {},
        "",
        `/mail/#thread/${projectOne.id}/${oldThreadId}`,
      );
      server.use(
        http.get(
          "*/mail/api/v1/projects/:projectId/threads",
          ({ request }) =>
            new URL(request.url).searchParams.get("thread_id") === newThreadId
              ? HttpResponse.json({
                  items: [{
                    ...messageDetail,
                    subject: "Current thread subject",
                    thread_id: newThreadId,
                  }],
                  total: 1,
                  next_cursor: null,
                  subject: "Current thread subject",
                })
              : HttpResponse.json({ detail: "not found" }, { status: 404 }),
        ),
      );
      render(<App />);
      await waitFor(() => expect(oldRequests).toBe(1));

      window.history.pushState(
        {},
        "",
        mailRouteHash({
          view: "thread",
          projectId: projectOne.id,
          threadId: newThreadId,
        }),
      );
      window.dispatchEvent(new HashChangeEvent("hashchange"));
      expect(
        await screen.findByRole("heading", {
          name: "Current thread subject",
          level: 1,
        }),
      ).toBeVisible();

      await act(async () => {
        if (outcome === "resolve") {
          resolveOld(
            new Response(
              JSON.stringify({
                items: [{
                  ...messageDetail,
                  subject: "Stale thread subject",
                  thread_id: oldThreadId,
                }],
                total: 1,
                next_cursor: null,
                subject: "Stale thread subject",
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        } else {
          rejectOld(new Error("private stale thread failure"));
        }
        await Promise.resolve();
      });

      expect(screen.queryByText("Stale thread subject")).not.toBeInTheDocument();
      expect(screen.queryByText("private stale thread failure"))
        .not.toBeInTheDocument();
      expect(
        screen.getByRole("heading", { name: "Current thread subject", level: 1 }),
      ).toBeVisible();
    },
  );

  it.each(["resolve", "reject"] as const)(
    "aborts and ignores a stale thread page %s after a project switch",
    async (outcome) => {
      const user = userEvent.setup();
      const currentThreadId = "current-project-thread";
      let pageSignal: AbortSignal | null = null;
      let resolvePage: (response: Response) => void = () => undefined;
      let rejectPage: (reason: unknown) => void = () => undefined;
      const oldPage = new Promise<Response>((resolve, reject) => {
        resolvePage = resolve;
        rejectPage = reject;
      });
      const originalFetch = globalThis.fetch.bind(globalThis);
      vi.stubGlobal(
        "fetch",
        (input: RequestInfo | URL, init?: RequestInit) => {
          const url = new URL(
            input instanceof Request ? input.url : String(input),
            window.location.origin,
          );
          if (
            url.pathname.endsWith("/threads") &&
            url.searchParams.get("thread_id") === messageOne.thread_id &&
            url.searchParams.get("cursor") === "delayed-page"
          ) {
            pageSignal = init?.signal ?? null;
            return oldPage;
          }
          return originalFetch(input, init);
        },
      );
      window.history.replaceState(
        {},
        "",
        `/mail/#thread/${projectOne.id}/${messageOne.thread_id}`,
      );
      server.use(
        http.get(
          "*/mail/api/v1/projects/:projectId/threads",
          ({ params, request }) =>
            params.projectId === String(projectTwo.id) &&
            new URL(request.url).searchParams.get("thread_id") === currentThreadId
              ? HttpResponse.json({
                  items: [{
                    ...messageDetail,
                    id: messageTwo.id,
                    project_id: projectTwo.id,
                    project_slug: projectTwo.slug,
                    subject: "Current project thread",
                    thread_id: currentThreadId,
                  }],
                  total: 1,
                  next_cursor: null,
                  subject: "Current project thread",
                })
              : HttpResponse.json({
                  items: [threadReply],
                  total: 2,
                  next_cursor: "delayed-page",
                  subject: messageOne.subject,
                }),
        ),
      );
      render(<App />);
      expect(await screen.findByRole("button", { name: "Load more" }))
        .toBeVisible();
      await user.click(screen.getByRole("button", { name: "Load more" }));
      await waitFor(() => expect(pageSignal).not.toBeNull());

      window.history.pushState(
        {},
        "",
        mailRouteHash({
          view: "thread",
          projectId: projectTwo.id,
          threadId: currentThreadId,
        }),
      );
      window.dispatchEvent(new HashChangeEvent("hashchange"));
      expect(
        await screen.findByRole("heading", {
          name: "Current project thread",
          level: 1,
        }),
      ).toBeVisible();
      expect((pageSignal as AbortSignal | null)?.aborted).toBe(true);

      await act(async () => {
        if (outcome === "resolve") {
          resolvePage(
            new Response(
              JSON.stringify({
                items: [{
                  ...messageDetail,
                  id: 99,
                  subject: "Stale older thread page",
                }],
                total: 2,
                next_cursor: null,
                subject: messageOne.subject,
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        } else {
          rejectPage(new Error("private stale page failure"));
        }
        await Promise.resolve();
      });

      expect(screen.queryByText("Stale older thread page"))
        .not.toBeInTheDocument();
      expect(screen.queryByText("More messages could not be loaded. Try again."))
        .not.toBeInTheDocument();
      expect(
        screen.getByRole("heading", { name: "Current project thread", level: 1 }),
      ).toBeVisible();
    },
  );

  it("aborts a pending older thread page on unmount", async () => {
    const user = userEvent.setup();
    let pageSignal: AbortSignal | null = null;
    const originalFetch = globalThis.fetch.bind(globalThis);
    window.history.replaceState(
      {},
      "",
      `/mail/#thread/${projectOne.id}/${messageOne.thread_id}`,
    );
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/threads", () =>
        HttpResponse.json({
          items: [threadReply],
          total: 2,
          next_cursor: "pending-page",
          subject: messageOne.subject,
        }),
      ),
    );
    const view = render(<App />);
    expect(await screen.findByRole("button", { name: "Load more" }))
      .toBeVisible();
    vi.stubGlobal(
      "fetch",
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(
          input instanceof Request ? input.url : String(input),
          window.location.origin,
        );
        if (
          url.pathname.endsWith("/threads") &&
          url.searchParams.get("thread_id") === messageOne.thread_id &&
          url.searchParams.get("cursor") === "pending-page"
        ) {
          pageSignal = init?.signal ?? null;
          return new Promise<Response>((_resolve, reject) => {
            pageSignal?.addEventListener("abort", () =>
              reject(new DOMException("cancelled", "AbortError")),
            );
          });
        }
        return originalFetch(input, init);
      },
    );

    await user.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(pageSignal).not.toBeNull());
    expect((pageSignal as AbortSignal | null)?.aborted).toBe(false);
    view.unmount();

    expect((pageSignal as AbortSignal | null)?.aborted).toBe(true);
    await act(async () => Promise.resolve());
  });

  it("redirects exactly once when a thread read loses authorization", async () => {
    const onUnauthorized = vi.fn();
    window.history.replaceState(
      {},
      "",
      `/mail/#thread/${projectOne.id}/${messageOne.thread_id}`,
    );
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/threads", () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );
    render(<App onUnauthorized={onUnauthorized} />);

    expect(
      await screen.findByText("Your session expired. Redirecting to sign in."),
    ).toBeVisible();
    expect(onUnauthorized).toHaveBeenCalledOnce();
    expect(onUnauthorized).toHaveBeenCalledWith(
      `/mail/login?next=%2Fmail%2F%23thread%2F${projectOne.id}%2F${messageOne.thread_id}`,
    );
    expect(
      within(screen.getByRole("navigation", { name: "Primary navigation" }))
        .getByRole("link", { name: "Inbox" }),
    ).toHaveAttribute("aria-current", "page");
  });

  it("ignores an aborted initial thread read without exposing an error", async () => {
    let threadRequests = 0;
    const originalFetch = globalThis.fetch.bind(globalThis);
    vi.stubGlobal(
      "fetch",
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(
          input instanceof Request ? input.url : String(input),
          window.location.origin,
        );
        if (
          url.pathname.endsWith("/threads") &&
          url.searchParams.get("thread_id") === messageOne.thread_id
        ) {
          threadRequests += 1;
          return Promise.reject(new DOMException("cancelled", "AbortError"));
        }
        return originalFetch(input, init);
      },
    );
    window.history.replaceState(
      {},
      "",
      `/mail/#thread/${projectOne.id}/${messageOne.thread_id}`,
    );
    render(<App />);

    await waitFor(() => expect(threadRequests).toBe(1));
    expect(screen.getByText("Loading message…")).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each(["unauthorized", "aborted"] as const)(
    "handles an %s older-page thread request without losing loaded messages",
    async (outcome) => {
      const user = userEvent.setup();
      const onUnauthorized = vi.fn();
      window.history.replaceState(
        {},
        "",
        `/mail/#thread/${projectOne.id}/${messageOne.thread_id}`,
      );
      server.use(
        http.get(
          "*/mail/api/v1/projects/:projectId/threads",
          ({ request }) => {
            const cursor = new URL(request.url).searchParams.get("cursor");
            if (cursor !== null) {
              return outcome === "unauthorized"
                ? HttpResponse.json({ detail: "expired" }, { status: 401 })
                : HttpResponse.error();
            }
            return HttpResponse.json({
              items: [threadReply],
              total: 2,
              next_cursor: "next-page",
              subject: messageOne.subject,
            });
          },
        ),
      );
      if (outcome === "aborted") {
        const originalFetch = globalThis.fetch.bind(globalThis);
        vi.stubGlobal(
          "fetch",
          (input: RequestInfo | URL, init?: RequestInit) => {
            const url = new URL(
              input instanceof Request ? input.url : String(input),
              window.location.origin,
            );
            if (url.searchParams.get("cursor") === "next-page") {
              return Promise.reject(new DOMException("cancelled", "AbortError"));
            }
            return originalFetch(input, init);
          },
        );
      }
      render(<App onUnauthorized={onUnauthorized} />);

      expect(
        await screen.findByRole("heading", { name: messageOne.subject, level: 1 }),
      ).toBeVisible();
      await user.click(screen.getByRole("button", { name: "Load more" }));
      if (outcome === "unauthorized") {
        await waitFor(() => expect(onUnauthorized).toHaveBeenCalledOnce());
        expect(
          screen.getByText("Your session expired. Redirecting to sign in."),
        ).toBeVisible();
      } else {
        await waitFor(() =>
          expect(screen.getByRole("button", { name: "Loading more…" }))
            .toBeDisabled(),
        );
        expect(onUnauthorized).not.toHaveBeenCalled();
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      }
      expect(document.querySelectorAll(".thread-message")).toHaveLength(
        outcome === "unauthorized" ? 0 : 1,
      );
    },
  );

  it("renders GFM as accessible React elements behind fail-closed URLs", async () => {
    const user = userEvent.setup();
    const png = `data:image/png;base64,${window.btoa("\x89PNG\r\n\x1a\nrest")}`;
    const body = [
      "# Delivery report",
      "## Nested evidence",
      "### Validation details",
      "#### Parser outcome",
      "##### Boundary note",
      "###### Terminal note",
      "**Strong result** and ~~retired path~~.",
      "- [x] Reviewed\n- [ ] Pending",
      "First line\nSecond line",
      "> Audited quote",
      "| Identifier | Result |\n| --- | --- |\n| IRIS-101 | Passed |",
      "```ts\nconst delivered = true;\n```",
      "[Safe HTTPS](https://example.test/report) [safe mail](mailto:ops@example.test) [safe Polish](https://example.test/Wrocław) [safe relative](../messages/101)",
      "[blocked fragment](#delivery-report) [blocked routed fragment](/mail/#account) [blocked JavaScript](javascript:alert%281%29)",
      `![inline proof](${png})`,
      "![remote tracker](https://tracker.invalid/pixel.png)",
      "<script>window.__markdownExecuted = true</script>",
      '<img src="/mail/logout" alt="raw image" onerror="window.__markdownExecuted = true">',
    ].join("\n\n");
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/messages/:messageId", () =>
        HttpResponse.json({ ...messageDetail, body_md: body }),
      ),
    );
    render(<App />);

    await user.click(
      await screen.findByRole("link", {
        name: new RegExp(`Open message.*${messageOne.subject}`),
      }),
    );

    expect(
      await screen.findByRole("heading", { name: "Delivery report", level: 2 }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "Nested evidence", level: 3 }))
      .toBeVisible();
    expect(screen.getByRole("heading", { name: "Validation details", level: 4 }))
      .toBeVisible();
    expect(screen.getByRole("heading", { name: "Parser outcome", level: 5 }))
      .toBeVisible();
    expect(screen.getByRole("heading", { name: "Boundary note", level: 6 }))
      .toBeVisible();
    expect(screen.getByRole("heading", { name: "Terminal note", level: 6 }))
      .toBeVisible();
    expect(screen.getByText("Strong result", { selector: "strong" })).toBeVisible();
    expect(screen.getByText("retired path", { selector: "del" })).toBeVisible();
    const completedTask = screen.getByRole("checkbox", { name: "Completed task" });
    const incompleteTask = screen.getByRole("checkbox", { name: "Incomplete task" });
    expect(completedTask).toBeChecked();
    expect(completedTask).toBeDisabled();
    expect(completedTask.closest("li")).toHaveTextContent("Reviewed");
    expect(incompleteTask).not.toBeChecked();
    expect(incompleteTask).toBeDisabled();
    expect(incompleteTask.closest("li")).toHaveTextContent("Pending");
    expect(document.querySelector(".message-body p br")).toBeInTheDocument();
    expect(screen.getByText("Audited quote").closest("blockquote")).not.toBeNull();
    expect(screen.getByRole("region", { name: "Markdown table" })).toHaveAttribute(
      "tabindex",
      "0",
    );
    expect(screen.getByRole("cell", { name: "IRIS-101" })).toBeVisible();
    expect(screen.getByRole("cell", { name: "Passed" })).toBeVisible();
    expect(screen.getByLabelText("Code block")).toHaveAttribute("tabindex", "0");
    expect(screen.getByText("const delivered = true;", { selector: "code" }))
      .toBeVisible();
    expect(screen.getByRole("link", { name: "Safe HTTPS" })).toHaveAttribute(
      "href",
      "https://example.test/report",
    );
    expect(screen.getByRole("link", { name: "safe mail" })).toHaveAttribute(
      "href",
      "mailto:ops@example.test",
    );
    expect(screen.getByRole("link", { name: "safe Polish" })).toHaveAttribute(
      "href",
      "https://example.test/Wroc%C5%82aw",
    );
    expect(screen.getByRole("link", { name: "safe relative" })).toHaveAttribute(
      "href",
      "../messages/101",
    );
    expect(screen.getByText("blocked fragment").closest("a")).toBeNull();
    expect(screen.getByText("blocked routed fragment").closest("a")).toBeNull();
    expect(screen.getByText("blocked JavaScript").closest("a")).toBeNull();
    expect(screen.getByRole("img", { name: "inline proof" })).toHaveAttribute(
      "src",
      png,
    );
    expect(screen.getByText("remote tracker", { exact: true })).toHaveClass(
      "markdown-image-alt",
    );
    expect(document.querySelectorAll(".message-body img")).toHaveLength(1);
    expect(document.querySelector(".message-body script")).not.toBeInTheDocument();
    expect(document.querySelector(".message-body [onerror]")).not.toBeInTheDocument();
    expect(document.querySelector(".message-body [onclick]")).not.toBeInTheDocument();
    expect(screen.queryByText("raw image")).not.toBeInTheDocument();
  });

  it("localizes Markdown accessibility labels with the saved Polish UI locale", async () => {
    window.history.replaceState(
      {},
      "",
      `/mail/#message/${projectOne.id}/${messageOne.id}`,
    );
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json(preferencesResponse("pl")),
      ),
      http.get("*/mail/api/v1/projects/:projectId/messages/:messageId", () =>
        HttpResponse.json({
          ...messageDetail,
          body_md: [
            "- [x] Gotowe",
            "- [ ] Oczekuje",
            "| Kontrola | Wynik |\n| --- | --- |\n| Nazwy | Poprawne |",
            "```text\nlokalizacja\n```",
          ].join("\n\n"),
        }),
      ),
    );
    render(<App />);

    expect(
      await screen.findByRole("checkbox", { name: "Ukończone zadanie" }),
    ).toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: "Nieukończone zadanie" }),
    ).not.toBeChecked();
    expect(screen.getByRole("region", { name: "Tabela Markdown" })).toBeVisible();
    expect(screen.getByLabelText("Blok kodu")).toBeVisible();
    expect(screen.queryByLabelText("Markdown table")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Code block")).not.toBeInTheDocument();
  });

  it.each([
    [
      "en" as const,
      /Open message.*Production rollout verified.*From Gospodarz.*Project: \/mateusz-klatt\/mcp_agent_mail.*Acknowledgement requested.*High/,
    ],
    [
      "pl" as const,
      /Otwórz wiadomość.*Production rollout verified.*Od: Gospodarz.*Projekt: \/mateusz-klatt\/mcp_agent_mail.*Wymagane potwierdzenie.*Wysoki/,
    ],
  ])("keeps all visible message-row text in the %s accessible name", async (language, accessibleName) => {
    server.use(
      http.get("*/mail/api/v1/me/preferences", () =>
        HttpResponse.json(preferencesResponse(language)),
      ),
    );
    render(<App />);

    expect(
      await screen.findByRole("link", { name: accessibleName }),
    ).toBeVisible();
  });

  it("renders a viewer detail from a deep link", async () => {
    window.history.replaceState(
      {},
      "",
      `/mail/#message/${projectTwo.id}/${messageTwo.id}`,
    );
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/messages/:messageId", () =>
        HttpResponse.json({
          ...messageDetail,
          ...messageTwo,
          body_md: "Viewer-visible message",
          to: ["viewer"],
          cc: [],
          attachments: [],
          can_reply: false,
        }),
      ),
    );
    render(<App />);

    expect(
      await screen.findByRole("heading", { name: messageTwo.subject }),
    ).toBeVisible();
    expect(screen.getByText("Viewer-visible message")).toBeVisible();
  });

  it("rejects a mismatched detail response without rendering it", async () => {
    window.history.replaceState(
      {},
      "",
      `/mail/#message/${projectTwo.id}/${messageTwo.id}`,
    );
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/messages/:messageId", () =>
        HttpResponse.json(messageDetail),
      ),
    );
    render(<App />);

    expect(
      await screen.findByText("This message could not be loaded."),
    ).toBeVisible();
    expect(screen.queryByRole("heading", { name: messageOne.subject })).not.toBeInTheDocument();
  });

  it.each(["resolve", "reject"] as const)(
    "ignores a late message detail %s after navigating to another message",
    async (outcome) => {
      let oldRequests = 0;
      let resolveOld: (response: Response) => void = () => undefined;
      let rejectOld: (reason: unknown) => void = () => undefined;
      const oldDetail = new Promise<Response>((resolve, reject) => {
        resolveOld = resolve;
        rejectOld = reject;
      });
      const originalFetch = globalThis.fetch.bind(globalThis);
      vi.stubGlobal(
        "fetch",
        (input: RequestInfo | URL, init?: RequestInit) => {
          const requestUrl =
            typeof input === "string"
              ? input
              : input instanceof URL
                ? input.href
                : input.url;
          if (
            requestUrl.includes(
              `/mail/api/v1/projects/${projectOne.id}/messages/${messageOne.id}`,
            )
          ) {
            oldRequests += 1;
            return oldDetail;
          }
          return originalFetch(input, init);
        },
      );
      window.history.replaceState(
        {},
        "",
        `/mail/#message/${projectOne.id}/${messageOne.id}`,
      );
      server.use(
        http.get(
          "*/mail/api/v1/projects/:projectId/messages/:messageId",
          ({ params }) =>
            params.projectId === String(projectTwo.id) &&
            params.messageId === String(messageTwo.id)
              ? HttpResponse.json({
                  ...messageDetail,
                  ...messageTwo,
                  body_md: "Viewer route remains current",
                  to: ["viewer"],
                  cc: [],
                  attachments: [],
                  can_reply: false,
                })
              : HttpResponse.json({ detail: "not found" }, { status: 404 }),
        ),
      );
      render(<App />);
      await waitFor(() => expect(oldRequests).toBe(1));

      act(() => {
        window.history.replaceState(
          {},
          "",
          `/mail/#message/${projectTwo.id}/${messageTwo.id}`,
        );
        window.dispatchEvent(new HashChangeEvent("hashchange"));
      });
      expect(
        await screen.findByRole("heading", { name: messageTwo.subject }),
      ).toBeVisible();

      await act(async () => {
        if (outcome === "resolve") {
          resolveOld(
            new Response(JSON.stringify(messageDetail), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          );
        } else {
          rejectOld(new TypeError("late old-route failure"));
        }
        await Promise.resolve();
      });

      expect(
        screen.getByRole("heading", { name: messageTwo.subject }),
      ).toBeVisible();
      expect(
        screen.queryByRole("heading", { name: messageOne.subject }),
      ).not.toBeInTheDocument();
    },
  );

  it("honours deep links and popstate navigation", async () => {
    const inboxQueries: URL[] = [];
    window.history.replaceState(
      {},
      "",
      `/mail/#message/${projectOne.id}/${messageOne.id}`,
    );
    server.use(
      http.get("*/mail/api/v1/inbox", ({ request }) => {
        inboxQueries.push(new URL(request.url));
        return HttpResponse.json(inboxResponse);
      }),
    );
    render(<App />);

    expect(
      await screen.findByRole("heading", { name: messageOne.subject }),
    ).toBeVisible();
    expect(inboxQueries.at(-1)?.searchParams.get("project_id")).toBe(
      String(projectOne.id),
    );

    act(() => {
      window.history.pushState({}, "", "#projects");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(await screen.findByRole("heading", { name: "Projects" })).toBeVisible();

    act(() => {
      window.history.pushState({}, "", `#inbox?project=${projectTwo.id}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(await screen.findByLabelText("Filter by project")).toHaveValue(
      String(projectTwo.id),
    );
    await waitFor(() =>
      expect(inboxQueries.at(-1)?.searchParams.get("project_id")).toBe(
        String(projectTwo.id),
      ),
    );
  });

  it("changes the project filter and sends only typed pagination parameters", async () => {
    const user = userEvent.setup();
    const requests: Request[] = [];
    server.use(
      http.get("*/mail/api/v1/inbox", ({ request }) => {
        requests.push(request);
        return HttpResponse.json(inboxResponse);
      }),
    );
    render(<App />);
    await screen.findByText(messageOne.subject);

    await user.selectOptions(
      screen.getByLabelText("Filter by project"),
      String(projectTwo.id),
    );

    await waitFor(() => expect(requests).toHaveLength(2));
    const url = new URL(requests[1]?.url ?? "");
    expect(url.searchParams.get("limit")).toBe(String(inboxPageSize));
    expect(url.searchParams.get("project_id")).toBe(String(projectTwo.id));
    expect(url.searchParams.has("cursor")).toBe(false);
    expect(requests[1]?.credentials).toBe("same-origin");
    expect(requests[1]?.cache).toBe("no-store");
    expect(window.location.hash).toBe(`#inbox?project=${projectTwo.id}`);
  });

  it("loads the next cursor page and de-duplicates updated messages", async () => {
    const user = userEvent.setup();
    const cursors: Array<string | null> = [];
    server.use(
      http.get("*/mail/api/v1/inbox", ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("cursor");
        cursors.push(cursor);
        return cursor === null
          ? HttpResponse.json({
              items: [messageOne],
              total: 2,
              next_cursor: "opaque-page-2",
            })
          : HttpResponse.json({
              items: [
                { ...messageOne, subject: "Rollout status refreshed" },
                messageTwo,
              ],
              total: 2,
              next_cursor: null,
            });
      }),
    );
    render(<App />);
    await screen.findByText(messageOne.subject);

    await user.click(screen.getByRole("button", { name: "Load more" }));

    expect(await screen.findByText("Rollout status refreshed")).toBeVisible();
    expect(screen.getByText(messageTwo.subject)).toBeVisible();
    expect(screen.queryByText(messageOne.subject)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    expect(cursors).toEqual([null, "opaque-page-2"]);
  });

  it("discards an old project's delayed page when the inbox route changes", async () => {
    const user = userEvent.setup();
    let releaseOldPage: (() => void) | undefined;
    const oldPageCanFinish = new Promise<void>((resolve) => {
      releaseOldPage = resolve;
    });
    const originalFetch = globalThis.fetch.bind(globalThis);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const url = new URL(
          input instanceof Request ? input.url : String(input),
          window.location.origin,
        );
        if (url.searchParams.get("cursor") === "old-project-page") {
          await oldPageCanFinish;
          return new Response(
            JSON.stringify({
              items: [{ ...messageTwo, subject: "Stale project page" }],
              total: 2,
              next_cursor: null,
            }),
            { headers: { "content-type": "application/json" } },
          );
        }
        return originalFetch(input, init);
      },
    );
    server.use(
      http.get("*/mail/api/v1/inbox", async ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("project_id") === String(projectTwo.id)) {
          return HttpResponse.json({
            items: [{ ...messageTwo, project_id: projectTwo.id }],
            total: 1,
            next_cursor: null,
          });
        }
        return HttpResponse.json({
          items: [messageOne],
          total: 2,
          next_cursor: "old-project-page",
        });
      }),
    );
    render(<App />);
    await screen.findByText(messageOne.subject);

    await user.click(screen.getByRole("button", { name: "Load more" }));
    await user.selectOptions(
      screen.getByLabelText("Filter by project"),
      String(projectTwo.id),
    );
    expect(await screen.findByText(messageTwo.subject)).toBeVisible();

    releaseOldPage?.();
    await act(async () => oldPageCanFinish);
    await waitFor(() =>
      expect(screen.queryByText("Stale project page")).not.toBeInTheDocument(),
    );
    expect(screen.getByText(messageTwo.subject)).toBeVisible();
    expect(screen.queryByText(messageOne.subject)).not.toBeInTheDocument();
    fetchSpy.mockRestore();
  });

  it("ignores an old project's delayed page error when the inbox route changes", async () => {
    const user = userEvent.setup();
    let rejectOldPage: (() => void) | undefined;
    const oldPageCanFail = new Promise<void>((resolve) => {
      rejectOldPage = resolve;
    });
    const originalFetch = globalThis.fetch.bind(globalThis);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const url = new URL(
          input instanceof Request ? input.url : String(input),
          window.location.origin,
        );
        if (url.searchParams.get("cursor") === "old-project-error") {
          await oldPageCanFail;
          throw new Error("stale page failure");
        }
        return originalFetch(input, init);
      },
    );
    server.use(
      http.get("*/mail/api/v1/inbox", ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("project_id") === String(projectTwo.id)) {
          return HttpResponse.json({
            items: [{ ...messageTwo, project_id: projectTwo.id }],
            total: 1,
            next_cursor: null,
          });
        }
        return HttpResponse.json({
          items: [messageOne],
          total: 2,
          next_cursor: "old-project-error",
        });
      }),
    );
    render(<App />);
    await screen.findByText(messageOne.subject);

    await user.click(screen.getByRole("button", { name: "Load more" }));
    await user.selectOptions(
      screen.getByLabelText("Filter by project"),
      String(projectTwo.id),
    );
    expect(await screen.findByText(messageTwo.subject)).toBeVisible();

    rejectOldPage?.();
    await act(async () => oldPageCanFail);
    await waitFor(() =>
      expect(screen.queryByText("More messages could not be loaded. Try again."))
        .not.toBeInTheDocument(),
    );
    expect(screen.getByText(messageTwo.subject)).toBeVisible();
    fetchSpy.mockRestore();
  });

  it("preserves the current inbox behind a generic load-more error", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("*/mail/api/v1/inbox", ({ request }) =>
        new URL(request.url).searchParams.has("cursor")
          ? HttpResponse.json(
              { detail: "private database error" },
              { status: 503 },
            )
          : HttpResponse.json({
              items: [messageOne],
              total: 2,
              next_cursor: "failure-cursor",
            }),
      ),
    );
    render(<App />);
    await screen.findByText(messageOne.subject);

    await user.click(screen.getByRole("button", { name: "Load more" }));

    expect(
      await screen.findByText("More messages could not be loaded. Try again."),
    ).toBeVisible();
    expect(screen.getByText(messageOne.subject)).toBeVisible();
    expect(screen.queryByText("private database error")).not.toBeInTheDocument();
  });

  it("shows generic initial and detail errors without leaking response bodies", async () => {
    server.use(
      http.get("*/mail/api/v1/inbox", () =>
        HttpResponse.json({ detail: "sensitive SQL text" }, { status: 500 }),
      ),
    );
    const first = render(<App />);
    expect(
      await screen.findByText("Messages could not be loaded. Try again later."),
    ).toBeVisible();
    expect(screen.queryByText("sensitive SQL text")).not.toBeInTheDocument();
    first.unmount();

    window.history.replaceState(
      {},
      "",
      `/mail/#message/${projectOne.id}/9999`,
    );
    server.use(
      http.get("*/mail/api/v1/inbox", () => HttpResponse.json(inboxResponse)),
      http.get("*/mail/api/v1/projects/:projectId/messages/:messageId", () =>
        HttpResponse.json({ detail: "hidden project" }, { status: 404 }),
      ),
    );
    render(<App />);
    expect(await screen.findByText("This message could not be loaded.")).toBeVisible();
    expect(screen.queryByText("hidden project")).not.toBeInTheDocument();
  });

  it("renders honest empty project and inbox states", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#projects");
    server.use(
      http.get("*/mail/api/v1/projects", () =>
        HttpResponse.json({ items: [], total: 0 }),
      ),
      http.get("*/mail/api/v1/inbox", () =>
        HttpResponse.json({ items: [], total: 0, next_cursor: null }),
      ),
    );
    render(<App />);

    expect(
      await screen.findByText("No projects are assigned to your account."),
    ).toBeVisible();
    await user.click(screen.getByRole("link", { name: "Inbox" }));
    expect(
      await screen.findByText("There are no messages in this view."),
    ).toBeVisible();
  });

  it("opens SSE before fetching, debounces refreshes, reconnects, and cleans up", async () => {
    const order: string[] = [];
    let inboxReads = 0;
    const source = new FakeEventSource("/mail/events");
    const factory = vi.fn(() => {
      order.push("events");
      return source;
    });
    server.use(
      http.get("*/mail/api/v1/projects", () => {
        order.push("projects");
        return HttpResponse.json(projectsResponse);
      }),
      http.get("*/mail/api/v1/inbox", () => {
        order.push("inbox");
        inboxReads += 1;
        return HttpResponse.json(inboxResponse);
      }),
    );
    const view = render(<App createEventSource={factory} />);
    await screen.findByText(messageOne.subject);

    expect(order[0]).toBe("events");
    expect(factory).toHaveBeenCalledWith("/mail/events");
    act(() => source.emitOpen());
    expect(screen.getByText("Live updates connected")).toBeVisible();
    act(() => source.emitError());
    expect(screen.getByText("Reconnecting live updates…")).toBeVisible();

    vi.useFakeTimers();
    act(() => {
      source.emitMessage();
      source.emitMessage();
      vi.advanceTimersByTime(249);
    });
    expect(inboxReads).toBe(1);
    act(() => vi.advanceTimersByTime(1));
    vi.useRealTimers();
    await waitFor(() => expect(inboxReads).toBe(2));

    vi.useFakeTimers();
    act(() => source.emitMessage());
    view.unmount();
    act(() => vi.advanceTimersByTime(250));
    expect(source.close).toHaveBeenCalledOnce();
    expect(source.onopen).toBeNull();
    expect(source.onerror).toBeNull();
    expect(source.onmessage).toBeNull();
    expect(inboxReads).toBe(2);
  });

  it("redirects exactly once when viewer data loses authorization", async () => {
    const onUnauthorized = vi.fn();
    window.history.replaceState(
      {},
      "",
      `/mail/#inbox?project=${projectOne.id}`,
    );
    server.use(
      http.get("*/mail/api/v1/projects", () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
      http.get("*/mail/api/v1/inbox", () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );
    render(<App onUnauthorized={onUnauthorized} />);

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledOnce());
    expect(onUnauthorized).toHaveBeenCalledWith(
      `/mail/login?next=%2Fmail%2F%23inbox%3Fproject%3D${projectOne.id}`,
    );
    expect(screen.getAllByText("Your session expired. Redirecting to sign in.")).not.toHaveLength(0);
  });

  it("validates and preserves the complete projects contract", () => {
    const operator = {
      ...projectOne,
      id: 33,
      role: "operator" as const,
      archived_at: "2026-08-11T11:00:00Z",
    };
    expect(
      parseProjects({ items: [...projectsResponse.items, operator], total: 3 }),
    ).toEqual({ items: [...projectsResponse.items, operator], total: 3 });

    const invalidPayloads: unknown[] = [
      null,
      [],
      { items: {}, total: 0 },
      { items: [null], total: 1 },
      { items: [{ ...projectOne, id: "11" }], total: 1 },
      { items: [{ ...projectOne, id: 1.5 }], total: 1 },
      { items: [{ ...projectOne, id: 0 }], total: 1 },
      { items: [{ ...projectOne, slug: null }], total: 1 },
      { items: [{ ...projectOne, human_key: null }], total: 1 },
      { items: [{ ...projectOne, created_at: "" }], total: 1 },
      { items: [{ ...projectOne, created_at: "not-a-date" }], total: 1 },
      { items: [{ ...projectOne, archived_at: 7 }], total: 1 },
      { items: [{ ...projectOne, archived_at: "not-a-date" }], total: 1 },
      { items: [{ ...projectOne, role: "owner" }], total: 1 },
      { items: [{ ...projectOne, can_reply: "yes" }], total: 1 },
      { items: [{ ...projectOne, invented_count: 3 }], total: 1 },
      { items: [], total: -1 },
      { items: [], total: 1.5 },
      { items: [], total: "1" },
      { items: [], total: 0, debug: true },
    ];
    for (const payload of invalidPayloads) {
      expect(() => parseProjects(payload)).toThrow(TypeError);
    }
  });

  it("validates every inbox field and all supported importance values", () => {
    const low = { ...messageOne, id: 201, importance: "low" as const };
    const urgent = { ...messageTwo, id: 202, importance: "urgent" as const };
    expect(
      parseInboxPage({ items: [low, urgent], next_cursor: "next", total: 2 }),
    ).toEqual({ items: [low, urgent], next_cursor: "next", total: 2 });

    const invalidMessages = [
      null,
      { ...messageOne, id: 0 },
      { ...messageOne, project_id: "11" },
      { ...messageOne, project_slug: null },
      { ...messageOne, subject: null },
      { ...messageOne, sender: null },
      { ...messageOne, sender_name: null },
      { ...messageOne, sender_display_name: 4 },
      { ...messageOne, importance: "critical" },
      { ...messageOne, thread_id: 9 },
      { ...messageOne, reply_to: 0 },
      { ...messageOne, created_ts: "bad" },
      { ...messageOne, ack_required: "yes" },
      { ...messageOne, can_reply: "yes" },
      { ...messageOne, unread: true },
    ];
    expect(() => parseInboxPage(null)).toThrow(TypeError);
    expect(() =>
      parseInboxPage({ items: {}, next_cursor: null, total: 0 }),
    ).toThrow(TypeError);
    for (const message of invalidMessages) {
      expect(() =>
        parseInboxPage({ items: [message], next_cursor: null, total: 1 }),
      ).toThrow(TypeError);
    }
    expect(() =>
      parseInboxPage({ items: [], next_cursor: 9, total: 0 }),
    ).toThrow(TypeError);
    expect(() =>
      parseInboxPage({ items: [], next_cursor: null, total: 0, debug: true }),
    ).toThrow(TypeError);
    expect(() =>
      parseInboxPage({
        items: [],
        next_cursor: null,
        subject: "Thread-only field",
        total: 0,
      }),
    ).toThrow(TypeError);
  });

  it("validates message bodies, recipient lists, and safe attachment metadata", () => {
    expect(parseMessageDetail(messageDetail)).toEqual(messageDetail);
    expect(parseMessageDetail({ ...messageDetail, reply_target: null })).toEqual({
      ...messageDetail,
      reply_target: null,
    });
    const invalidDetails = [
      { ...messageDetail, body_md: null },
      { ...messageDetail, to: null },
      { ...messageDetail, to: ["agent", 7] },
      { ...messageDetail, cc: null },
      { ...messageDetail, cc: [7] },
      { ...messageDetail, attachments: null },
      { ...messageDetail, attachments: [null] },
      { ...messageDetail, reply_target: "agent" },
      {
        ...messageDetail,
        reply_target: { ...messageDetail.reply_target, agent_id: 0 },
      },
      {
        ...messageDetail,
        reply_target: { ...messageDetail.reply_target, agent_generation: "bad" },
      },
      {
        ...messageDetail,
        reply_target: { ...messageDetail.reply_target, project_id: 0 },
      },
      {
        ...messageDetail,
        reply_target: { ...messageDetail.reply_target, project_generation: "bad" },
      },
      {
        ...messageDetail,
        reply_target: { ...messageDetail.reply_target, canonical_name: 7 },
      },
      {
        ...messageDetail,
        reply_target: { ...messageDetail.reply_target, debug: true },
      },
      {
        ...messageDetail,
        attachments: [{ type: 7, media_type: null, size_bytes: null }],
      },
      {
        ...messageDetail,
        attachments: [{ type: null, media_type: 7, size_bytes: null }],
      },
      {
        ...messageDetail,
        attachments: [{ type: null, media_type: null, size_bytes: -1 }],
      },
      {
        ...messageDetail,
        bcc: ["hidden-agent"],
      },
      {
        ...messageDetail,
        attachments: [
          { type: null, media_type: null, size_bytes: null, storage_path: "/secret" },
        ],
      },
    ];
    for (const detail of invalidDetails) {
      expect(() => parseMessageDetail(detail)).toThrow(TypeError);
    }
  });

  it("validates every thread page field through the shared message parser", () => {
    expect(parseThreadPage(threadResponse)).toEqual(threadResponse);
    for (const payload of [
      null,
      { items: {}, next_cursor: null, subject: "Subject", total: 0 },
      { items: [messageOne], next_cursor: null, subject: "Subject", total: 1 },
      { items: [], next_cursor: 7, subject: "Subject", total: 0 },
      { items: [], next_cursor: null, subject: "Subject", total: -1 },
      {
        items: [],
        next_cursor: null,
        subject: "Subject",
        total: Number.MAX_SAFE_INTEGER + 1,
      },
      {
        items: [{ ...messageDetail, id: Number.MAX_SAFE_INTEGER + 1 }],
        next_cursor: null,
        subject: "Subject",
        total: 1,
      },
      { items: [], next_cursor: null, subject: 7, total: 0 },
      { items: [], next_cursor: null, subject: "Subject", total: 0, debug: true },
    ]) {
      expect(() => parseThreadPage(payload)).toThrow(TypeError);
    }
  });

  it.each([
    ["empty", ""],
    ["dot segment", "."],
    ["control character", "bad\u0000thread"],
    ["overlong", "x".repeat(129)],
    ["lone surrogate", "\ud800"],
  ])("declines a noncanonical %s thread link without throwing", (_label, threadId) => {
    expect(mailThreadRouteHash(projectOne.id, threadId)).toBeNull();
  });

  it.each([0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1])(
    "declines an unsafe thread project id %s",
    (projectId) => {
      expect(mailThreadRouteHash(projectId, "valid-thread")).toBeNull();
    },
  );

  it("round-trips valid hashes and rejects malformed deep links", () => {
    expect(parseMailRoute("")).toEqual({ view: "inbox", projectId: null });
    expect(parseMailRoute("inbox?project=11")).toEqual({
      view: "inbox",
      projectId: 11,
    });
    expect(parseMailRoute("#projects")).toEqual({ view: "projects" });
    expect(
      parseMailRoute("#search?q=rainbow&project=11&scope=body&order=newest"),
    ).toEqual({
      view: "search",
      query: "rainbow",
      projectId: 11,
      scope: "body",
      order: "newest",
    });
    expect(parseMailRoute("#search?scope=raw&order=raw")).toEqual({
      view: "search",
      query: "",
      projectId: null,
      scope: "all",
      order: "relevance",
    });
    expect(parseMailRoute(`#search?q=${"x".repeat(257)}`)).toEqual({
      view: "search",
      query: "",
      projectId: null,
      scope: "all",
      order: "relevance",
    });
    expect(parseMailRoute("#message/11/101")).toEqual({
      view: "message",
      projectId: 11,
      messageId: 101,
    });
    expect(parseMailRoute("#thread/11/release%2F%E2%9C%A8")).toEqual({
      view: "thread",
      projectId: 11,
      threadId: "release/✨",
    });
    expect(parseMailRoute("#thread/11/%250a")).toEqual({
      view: "thread",
      projectId: 11,
      threadId: "%0a",
    });
    expect(parseMailRoute("#thread/11/%252F")).toEqual({
      view: "thread",
      projectId: 11,
      threadId: "%2F",
    });
    for (const hash of [
      "#inbox?project=nope",
      "#inbox?project=0",
      "#message/11",
      "#message/0/1",
      "#message/1/0",
      "#message/9007199254740992/1",
      "#thread/01/release",
      "#thread/11/",
      "#thread/11/%",
      "#thread/11/%00",
      "#thread/11/%C2%85",
      "#thread/11/%72elease",
      "#thread/11/release%2fchild",
      "#thread/11/release✨",
      "#thread/11/.",
      "#thread/11/..",
      "#thread/11/release?ambiguous",
      "#thread/11/release?",
      `#thread/11/${encodeURIComponent("x".repeat(129))}`,
      "#unknown",
    ]) {
      expect(parseMailRoute(hash)).toEqual({ view: "inbox", projectId: null });
    }
    expect(mailRouteHash({ view: "projects" })).toBe("#projects");
    expect(mailRouteHash({ view: "inbox", projectId: null })).toBe("#inbox");
    expect(mailRouteHash({ view: "inbox", projectId: 11 })).toBe(
      "#inbox?project=11",
    );
    expect(
      mailRouteHash({ view: "message", projectId: 11, messageId: 101 }),
    ).toBe("#message/11/101");
    expect(
      mailRouteHash({ view: "thread", projectId: 11, threadId: "release/✨" }),
    ).toBe("#thread/11/release%2F%E2%9C%A8");
    for (const threadId of ["", ".", "..", "bad\u0000id", "x".repeat(129), "\ud800"]) {
      expect(() =>
        mailRouteHash({ view: "thread", projectId: 11, threadId }),
      ).toThrow(TypeError);
    }
    expect(() =>
      mailRouteHash({ view: "thread", projectId: 0, threadId: "release" }),
    ).toThrow(TypeError);
    expect(
      mailRouteHash({
        view: "search",
        query: "exact phrase",
        projectId: 11,
        scope: "subject",
        order: "newest",
      }),
    ).toBe("#search?q=exact+phrase&project=11&scope=subject&order=newest");
    expect(
      mailRouteHash({
        view: "search",
        query: "",
        projectId: null,
        scope: "all",
        order: "relevance",
      }),
    ).toBe("#search?scope=all&order=relevance");
  });

  it("uses exact same-origin endpoints in the standalone mail client", async () => {
    const urls: string[] = [];
    const encodedThreadId = "release/✨";
    const encodedThreadResponse = {
      ...threadResponse,
      items: threadResponse.items.map((message) => ({
        ...message,
        thread_id: encodedThreadId,
      })),
    };
    server.use(
      http.get("*/mail/api/v1/projects", ({ request }) => {
        urls.push(request.url);
        return HttpResponse.json(projectsResponse);
      }),
      http.get("*/mail/api/v1/inbox", ({ request }) => {
        urls.push(request.url);
        return HttpResponse.json(inboxResponse);
      }),
      http.get(
        "*/mail/api/v1/projects/:projectId/messages/:messageId",
        ({ request }) => {
          urls.push(request.url);
          return HttpResponse.json(messageDetail);
        },
      ),
      http.get("*/mail/api/v1/search", ({ request }) => {
        urls.push(request.url);
        return HttpResponse.json(searchResponse);
      }),
      http.get("*/mail/api/v1/projects/:projectId/threads", ({ request }) => {
        urls.push(request.url);
        return HttpResponse.json(encodedThreadResponse);
      }),
    );

    await expect(loadProjects()).resolves.toEqual(projectsResponse);
    await expect(
      loadInbox({ cursor: "opaque", projectId: projectOne.id }),
    ).resolves.toEqual(inboxResponse);
    await expect(
      loadMessage(projectOne.id, messageOne.id),
    ).resolves.toEqual(messageDetail);
    await expect(
      loadSearch({
        query: "release marker",
        projectId: projectOne.id,
        scope: "body",
        order: "newest",
        cursor: "search-opaque",
      }),
    ).resolves.toEqual(searchResponse);
    await expect(
      loadThread(projectOne.id, encodedThreadId, { cursor: "thread-opaque" }),
    ).resolves.toEqual(encodedThreadResponse);
    expect(urls).toEqual([
      "http://localhost:3000/mail/api/v1/projects",
      `http://localhost:3000/mail/api/v1/inbox?limit=${inboxPageSize}&cursor=opaque&project_id=${projectOne.id}`,
      `http://localhost:3000/mail/api/v1/projects/${projectOne.id}/messages/${messageOne.id}`,
      `http://localhost:3000/mail/api/v1/search?q=release+marker&scope=body&order=newest&limit=50&project_id=${projectOne.id}&cursor=search-opaque`,
      `http://localhost:3000/mail/api/v1/projects/${projectOne.id}` +
        `/threads?thread_id=release%2F%E2%9C%A8&limit=${threadPageSize}&cursor=thread-opaque`,
    ]);
  });

  it("accepts only the exact numeric starter exception in a thread page", async () => {
    const numericStarter = {
      ...messageDetail,
      id: 101,
      // Legacy numeric starter semantics are row-id based even when that row
      // has since acquired a different explicit thread identifier.
      thread_id: "different-explicit-thread",
    };
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/threads", () =>
        HttpResponse.json({
          items: [numericStarter],
          total: 1,
          next_cursor: null,
          subject: numericStarter.subject,
        }),
      ),
    );

    await expect(loadThread(projectOne.id, "101")).resolves.toEqual({
      items: [numericStarter],
      total: 1,
      next_cursor: null,
      subject: numericStarter.subject,
    });
    for (const threadId of ["0101", "+101", "１０１", "9223372036854775808"] ) {
      await expect(loadThread(projectOne.id, threadId)).rejects.toThrow(
        "Invalid thread message identity.",
      );
    }
    await expect(loadThread(projectOne.id, "9007199254740993")).rejects.toThrow(
      "Invalid thread message identity.",
    );
    await expect(loadThread(0, "101")).rejects.toThrow("Invalid thread project id.");
    await expect(loadThread(projectOne.id, ".")).rejects.toThrow("Invalid thread id.");
  });

  it("renders a full accessible search route and links results to message detail", async () => {
    const user = userEvent.setup();
    const requested: URL[] = [];
    window.history.replaceState(
      {},
      "",
      `/mail/#search?q=rollout&project=${projectOne.id}&scope=body&order=newest`,
    );
    server.use(
      http.get("*/mail/api/v1/search", ({ request }) => {
        requested.push(new URL(request.url));
        return HttpResponse.json(searchResponse);
      }),
    );
    render(<App />);
    await waitForEnglishPreferences();

    expect(await screen.findByRole("heading", { name: "Search" })).toBeVisible();
    expect(screen.getByLabelText("Search messages")).toHaveValue("rollout");
    expect(screen.getByLabelText("Project")).toHaveValue(String(projectOne.id));
    expect(screen.getByLabelText("Search in")).toHaveValue("body");
    expect(screen.getByLabelText("Order")).toHaveValue("newest");
    const result = await screen.findByRole("link", {
      name: `Open search result: ${messageOne.subject}`,
    });
    expect(result).toHaveAttribute(
      "href",
      `#message/${projectOne.id}/${messageOne.id}`,
    );
    expect(screen.getByText(searchResponse.items[0]!.snippet)).toBeVisible();
    expect(requested.at(-1)?.searchParams.get("q")).toBe("rollout");
    expect(requested.at(-1)?.searchParams.get("project_id")).toBe(
      String(projectOne.id),
    );

    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(window.location.hash).toContain(`project=${projectOne.id}`);

    await user.clear(screen.getByLabelText("Search messages"));
    await user.type(screen.getByLabelText("Search messages"), "archive window");
    await user.selectOptions(screen.getByLabelText("Project"), "");
    await user.selectOptions(screen.getByLabelText("Search in"), "subject");
    await user.selectOptions(screen.getByLabelText("Order"), "relevance");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(window.location.hash).toBe(
      "#search?q=archive+window&scope=subject&order=relevance",
    );
  });

  it("renders search results without linking malformed persisted threads", async () => {
    window.history.replaceState(
      {},
      "",
      "/mail/#search?q=rollout&scope=all&order=relevance",
    );
    server.use(
      http.get("*/mail/api/v1/search", () =>
        HttpResponse.json({
          ...searchResponse,
          items: [{ ...searchResponse.items[0], thread_id: "." }],
        }),
      ),
    );
    render(<App />);

    expect(
      await screen.findByRole("link", {
        name: `Open search result: ${messageOne.subject}`,
      }),
    ).toBeVisible();
    expect(
      document.querySelector(".search-result-list .message-thread-link"),
    ).not.toBeInTheDocument();
  });

  it("renders prompt, empty, and privacy-minimal blank-snippet search states", async () => {
    window.history.replaceState({}, "", "/mail/#search");
    const prompt = render(<App />);
    expect(
      await screen.findByText("Enter a search query to find Iris correspondence."),
    ).toBeVisible();
    prompt.unmount();

    window.history.replaceState({}, "", "/mail/#search?q=missing");
    server.use(
      http.get("*/mail/api/v1/search", () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
    );
    const empty = render(<App />);
    expect(
      await screen.findByText("No visible messages match this search."),
    ).toBeVisible();
    empty.unmount();

    const unlistedResult = {
      ...messageTwo,
      project_id: 909,
      project_slug: "unlisted-search-project",
      snippet: "",
    };
    window.history.replaceState({}, "", "/mail/#search?q=plain");
    server.use(
      http.get("*/mail/api/v1/search", () =>
        HttpResponse.json({ items: [unlistedResult], next_cursor: null }),
      ),
    );
    render(<App />);
    expect(
      await screen.findByRole("link", {
        name: `Open search result: ${unlistedResult.subject}`,
      }),
    ).toBeVisible();
    expect(screen.getByText("Project: unlisted-search-project")).toBeVisible();
  });

  it("redirects safely when an initial search loses authorization", async () => {
    const onUnauthorized = vi.fn();
    window.history.replaceState({}, "", "/mail/#search?q=expired");
    server.use(
      http.get("*/mail/api/v1/search", () =>
        HttpResponse.json({ detail: "private search failure" }, { status: 401 }),
      ),
    );
    render(<App onUnauthorized={onUnauthorized} />);

    expect(
      await screen.findByText("Your session expired. Redirecting to sign in."),
    ).toBeVisible();
    expect(onUnauthorized).toHaveBeenCalledOnce();
    expect(screen.queryByText("private search failure")).not.toBeInTheDocument();
  });

  it("ignores an aborted initial search without exposing an error", async () => {
    window.history.replaceState({}, "", "/mail/#search?q=cancelled");
    let searchRequests = 0;
    const originalFetch = globalThis.fetch.bind(globalThis);
    vi.stubGlobal(
      "fetch",
      (input: RequestInfo | URL, init?: RequestInit) => {
        const requestUrl =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        if (requestUrl.includes("/mail/api/v1/search?")) {
          searchRequests += 1;
          return Promise.reject(new DOMException("cancelled", "AbortError"));
        }
        return originalFetch(input, init);
      },
    );
    render(<App />);

    await waitFor(() => expect(searchRequests).toBe(1));
    expect(screen.getByText("Searching messages…")).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each(["resolve", "reject"] as const)(
    "ignores a stale initial search %s after submitting a new query",
    async (outcome) => {
      const user = userEvent.setup();
      window.history.replaceState({}, "", "/mail/#search?q=old-query");
      let oldRequests = 0;
      let resolveOld: (response: Response) => void = () => undefined;
      let rejectOld: (reason: unknown) => void = () => undefined;
      const oldSearch = new Promise<Response>((resolve, reject) => {
        resolveOld = resolve;
        rejectOld = reject;
      });
      const originalFetch = globalThis.fetch.bind(globalThis);
      vi.stubGlobal(
        "fetch",
        (input: RequestInfo | URL, init?: RequestInit) => {
          const requestUrl =
            typeof input === "string"
              ? input
              : input instanceof URL
                ? input.href
                : input.url;
          const url = new URL(requestUrl);
          if (
            url.pathname === "/mail/api/v1/search" &&
            url.searchParams.get("q") === "old-query"
          ) {
            oldRequests += 1;
            return oldSearch;
          }
          return originalFetch(input, init);
        },
      );
      render(<App />);
      await waitFor(() => expect(oldRequests).toBe(1));

      const query = screen.getByLabelText("Search messages");
      await user.clear(query);
      await user.type(query, "new-query");
      await user.click(screen.getByRole("button", { name: "Search" }));
      expect(await screen.findByText(searchResponse.items[0]!.snippet)).toBeVisible();

      await act(async () => {
        if (outcome === "resolve") {
          resolveOld(
            new Response(
              JSON.stringify({
                items: [{ ...searchResponse.items[0], snippet: "Stale search result." }],
                next_cursor: null,
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        } else {
          rejectOld(new TypeError("late private search failure"));
        }
        await Promise.resolve();
      });
      expect(screen.queryByText("Stale search result.")).not.toBeInTheDocument();
      expect(screen.queryByText("late private search failure")).not.toBeInTheDocument();
    },
  );

  it.each([
    [422, "invalid_search_query", "This query is invalid or too broad"],
    [503, "search_unavailable", "Search is temporarily unavailable"],
    [500, null, "Messages could not be searched"],
  ] as const)(
    "renders a redacted search failure for HTTP %s",
    async (status, code, expectedMessage) => {
      window.history.replaceState({}, "", "/mail/#search?q=private-marker");
      server.use(
        http.get("*/mail/api/v1/search", () =>
          HttpResponse.json(
            code === null
              ? { detail: "private-marker server failure" }
              : { detail: { code } },
            { status },
          ),
        ),
      );
      render(<App />);
      expect(await screen.findByText(new RegExp(expectedMessage))).toBeVisible();
      expect(screen.queryByText("private-marker server failure")).not.toBeInTheDocument();
    },
  );

  it("paginates search results without duplicates", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#search?q=rollout");
    server.use(
      http.get("*/mail/api/v1/search", ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("cursor");
        return HttpResponse.json(
          cursor === null
            ? { ...searchResponse, next_cursor: "second-page" }
            : {
                items: [{ ...messageTwo, snippet: "Second plain result." }],
                next_cursor: null,
              },
        );
      }),
    );
    render(<App />);
    await screen.findByText(searchResponse.items[0]!.snippet);
    await user.click(screen.getByRole("button", { name: "Load more results" }));
    expect(await screen.findByText("Second plain result.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Load more results" }))
      .not.toBeInTheDocument();
  });

  it.each([
    [401, "Your session expired. Redirecting to sign in."],
    [500, "More results could not be loaded. Try again."],
  ] as const)(
    "renders a safe cursor-search failure for HTTP %s",
    async (status, expectedMessage) => {
      const user = userEvent.setup();
      const onUnauthorized = vi.fn();
      window.history.replaceState({}, "", "/mail/#search?q=rollout");
      server.use(
        http.get("*/mail/api/v1/search", ({ request }) =>
          new URL(request.url).searchParams.has("cursor")
            ? HttpResponse.json({ detail: "private cursor failure" }, { status })
            : HttpResponse.json({ ...searchResponse, next_cursor: "next-page" }),
        ),
      );
      render(<App onUnauthorized={onUnauthorized} />);
      await screen.findByText(searchResponse.items[0]!.snippet);

      await user.click(screen.getByRole("button", { name: "Load more results" }));

      expect(await screen.findByText(expectedMessage)).toBeVisible();
      expect(onUnauthorized).toHaveBeenCalledTimes(status === 401 ? 1 : 0);
      expect(screen.queryByText("private cursor failure")).not.toBeInTheDocument();
    },
  );

  it("aborts a pending cursor search on unmount", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#search?q=rollout");
    server.use(
      http.get("*/mail/api/v1/search", () =>
        HttpResponse.json({ ...searchResponse, next_cursor: "pending-page" }),
      ),
    );
    const view = render(<App />);
    await screen.findByText(searchResponse.items[0]!.snippet);
    let cursorRequests = 0;
    const originalFetch = globalThis.fetch.bind(globalThis);
    vi.stubGlobal(
      "fetch",
      (input: RequestInfo | URL, init?: RequestInit) => {
        const requestUrl =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        if (
          requestUrl.includes("/mail/api/v1/search?") &&
          new URL(requestUrl).searchParams.has("cursor")
        ) {
          cursorRequests += 1;
          return new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () =>
              reject(new DOMException("cancelled", "AbortError")),
            );
          });
        }
        return originalFetch(input, init);
      },
    );

    await user.click(screen.getByRole("button", { name: "Load more results" }));
    await waitFor(() => expect(cursorRequests).toBe(1));
    expect(screen.getByRole("button", { name: "Loading more results…" })).toBeDisabled();
    view.unmount();
    await act(async () => Promise.resolve());
  });

  it("strictly parses privacy-minimal search responses", () => {
    expect(parseSearchPage(searchResponse)).toEqual(searchResponse);
    expect(() =>
      parseSearchPage({ items: {}, next_cursor: null }),
    ).toThrow(TypeError);
    expect(() =>
      parseSearchPage({
        ...searchResponse,
        items: [{ ...searchResponse.items[0], body_md: "private body" }],
      }),
    ).toThrow(TypeError);
    expect(() =>
      parseSearchPage({ ...searchResponse, total: 1 }),
    ).toThrow(TypeError);
  });

  it("ignores aborted project, inbox, and detail requests during cleanup", async () => {
    window.history.replaceState(
      {},
      "",
      `/mail/#message/${projectOne.id}/${messageOne.id}`,
    );
    const pendingFetch = vi.spyOn(globalThis, "fetch").mockImplementation(
      (_input, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("cancelled", "AbortError")),
          );
        }),
    );
    const view = render(<App />);
    await waitFor(() => expect(pendingFetch).toHaveBeenCalledTimes(5));

    view.unmount();
    await act(async () => Promise.resolve());
  });

  it("filters back to all projects", async () => {
    const user = userEvent.setup();
    window.history.replaceState(
      {},
      "",
      `/mail/#inbox?project=${projectOne.id}`,
    );
    render(<App />);
    const filter = await screen.findByLabelText("Filter by project");
    await waitFor(() =>
      expect(filter).toHaveValue(String(projectOne.id)),
    );

    await user.selectOptions(filter, "");

    expect(filter).toHaveValue("");
    expect(window.location.hash).toBe("#inbox");
  });

  it("renders project error and unauthorized states on the projects route", async () => {
    window.history.replaceState({}, "", "/mail/#projects");
    server.use(
      http.get("*/mail/api/v1/projects", () =>
        HttpResponse.json({ detail: "private" }, { status: 500 }),
      ),
    );
    const failed = render(<App />);
    expect(
      await screen.findByText("Projects could not be loaded. Try again later."),
    ).toBeVisible();
    failed.unmount();

    const onUnauthorized = vi.fn();
    server.use(
      http.get("*/mail/api/v1/projects", () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );
    render(<App onUnauthorized={onUnauthorized} />);
    expect(
      await screen.findByText("Your session expired. Redirecting to sign in."),
    ).toBeVisible();
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("shows a load-more pending state and aborts it on unmount", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("*/mail/api/v1/inbox", () =>
        HttpResponse.json({
          items: [messageOne],
          total: 2,
          next_cursor: "pending-page",
        }),
      ),
    );
    const view = render(<App />);
    await screen.findByText(messageOne.subject);
    const pendingFetch = vi.spyOn(globalThis, "fetch").mockImplementation(
      (_input, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("cancelled", "AbortError")),
          );
        }),
    );

    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(screen.getByRole("button", { name: "Loading more…" })).toBeDisabled();
    view.unmount();
    await act(async () => Promise.resolve());
    expect(pendingFetch).toHaveBeenCalledOnce();
  });

  it("redirects when a cursor page returns 401", async () => {
    const user = userEvent.setup();
    const onUnauthorized = vi.fn();
    server.use(
      http.get("*/mail/api/v1/inbox", ({ request }) =>
        new URL(request.url).searchParams.has("cursor")
          ? HttpResponse.json({ detail: "expired" }, { status: 401 })
          : HttpResponse.json({
              items: [messageOne],
              total: 2,
              next_cursor: "expired-page",
            }),
      ),
    );
    render(<App onUnauthorized={onUnauthorized} />);
    await screen.findByText(messageOne.subject);

    await user.click(screen.getByRole("button", { name: "Load more" }));

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledOnce());
    expect(
      screen.getByText("Your session expired. Redirecting to sign in."),
    ).toBeVisible();
  });

  it("renders detail fallbacks without inventing hidden metadata", async () => {
    const sparseDetail = {
      ...messageDetail,
      project_id: 909,
      project_slug: "external-project",
      sender_display_name: null,
      to: [],
      cc: ["observer-agent"],
      attachments: [],
    };
    window.history.replaceState(
      {},
      "",
      `/mail/#message/${sparseDetail.project_id}/${sparseDetail.id}`,
    );
    server.use(
      http.get("*/mail/api/v1/projects", () =>
        HttpResponse.json({ items: [], total: 0 }),
      ),
      http.get("*/mail/api/v1/projects/:projectId/messages/:messageId", () =>
        HttpResponse.json(sparseDetail),
      ),
    );
    render(<App />);

    expect(
      await screen.findByRole("heading", { name: sparseDetail.subject }),
    ).toBeVisible();
    expect(screen.getByText("external-project")).toBeVisible();
    expect(screen.getByText("observer-agent")).toBeVisible();
    expect(screen.getByText(/From claude-linux-holzera-1/)).toBeVisible();
    expect(screen.getByText("None")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Attachments" })).not.toBeInTheDocument();
  });

  it("redirects when a deep-linked detail loses authorization", async () => {
    const onUnauthorized = vi.fn();
    window.history.replaceState(
      {},
      "",
      `/mail/#message/${projectOne.id}/${messageOne.id}`,
    );
    server.use(
      http.get("*/mail/api/v1/projects/:projectId/messages/:messageId", () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );
    render(<App onUnauthorized={onUnauthorized} />);

    expect(
      await screen.findByText("Your session expired. Redirecting to sign in."),
    ).toBeVisible();
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("falls back to the inbox project slug when the project list omits it", async () => {
    const externalMessage = {
      ...messageTwo,
      project_id: 808,
      project_slug: "unlisted-project",
    };
    server.use(
      http.get("*/mail/api/v1/inbox", () =>
        HttpResponse.json({
          items: [externalMessage],
          total: 1,
          next_cursor: null,
        }),
      ),
    );
    render(<App />);

    expect(await screen.findByText("Project: unlisted-project")).toBeVisible();
  });

  it("renders Account from real profile data without opening inbox transport", async () => {
    window.history.replaceState({}, "", "/mail/#account");
    let mailReads = 0;
    const sourceFactory = vi.fn(() => new FakeEventSource("/mail/events"));
    server.use(
      http.get("*/mail/api/v1/projects", () => {
        mailReads += 1;
        return HttpResponse.json(projectsResponse);
      }),
      http.get("*/mail/api/v1/inbox", () => {
        mailReads += 1;
        return HttpResponse.json(inboxResponse);
      }),
    );

    render(<App createEventSource={sourceFactory} />);

    expect(await screen.findByRole("heading", { name: "Account" })).toBeVisible();
    expect(await screen.findByDisplayValue(adminProfile.display_name)).toBeVisible();
    expect(screen.getByText(adminProfile.username)).toBeVisible();
    expect(screen.getByText("Administrator")).toBeVisible();
    expect(screen.getByRole("link", { name: "Account" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "Administration" })).toBeVisible();
    expect(screen.getByLabelText("Current password")).toHaveAttribute(
      "autocomplete",
      "current-password",
    );
    expect(screen.getByLabelText("New password")).toHaveAttribute(
      "autocomplete",
      "new-password",
    );
    expect(screen.getByLabelText("Confirm new password")).toHaveAttribute(
      "autocomplete",
      "new-password",
    );
    expect(mailReads).toBe(0);
    expect(sourceFactory).not.toHaveBeenCalled();
  });

  it("saves and clears the display name with profile compare-and-swap", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#account");
    const requests: Array<{ request: Request; body: unknown }> = [];
    server.use(
      http.patch("*/mail/api/v1/me/profile", async ({ request }) => {
        const body = await request.json();
        requests.push({ request, body });
        const candidate = body as {
          display_name: string | null;
          expected_profile_revision: number;
        };
        return HttpResponse.json({
          changed: true,
          display_name: candidate.display_name,
          profile_revision: candidate.expected_profile_revision + 1,
        });
      }),
    );
    render(<App />);
    const input = await screen.findByLabelText("Display name");

    await user.clear(input);
    await user.type(input, "M. Klatt");
    await user.click(screen.getByRole("button", { name: "Save display name" }));

    expect(await screen.findByText("Display name saved.")).toBeVisible();
    expect(input).toHaveValue("M. Klatt");
    expect(requests[0]?.request.credentials).toBe("same-origin");
    expect(requests[0]?.request.cache).toBe("no-store");
    expect(requests[0]?.request.headers.get("content-type")).toBe(
      "application/json",
    );
    expect(requests[0]?.body).toEqual({
      display_name: "M. Klatt",
      expected_profile_revision: adminProfile.profile_revision,
    });

    await user.clear(input);
    await user.click(screen.getByRole("button", { name: "Save display name" }));
    await waitFor(() => expect(requests).toHaveLength(2));
    expect(requests[1]?.body).toEqual({
      display_name: null,
      expected_profile_revision: adminProfile.profile_revision + 1,
    });
    expect(input).toHaveValue("");
  });

  it("refreshes a conflicting display-name revision and keeps errors generic", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#account");
    let profileReads = 0;
    server.use(
      http.get("*/mail/api/v1/me/profile", () => {
        profileReads += 1;
        return HttpResponse.json(
          profileReads === 1
            ? adminProfile
            : { ...adminProfile, display_name: "Remote name", profile_revision: 9 },
        );
      }),
      http.patch("*/mail/api/v1/me/profile", () =>
        HttpResponse.json({ detail: { code: "profile_revision_conflict" } }, { status: 409 }),
      ),
    );
    render(<App />);
    const input = await screen.findByLabelText("Display name");
    await user.clear(input);
    await user.type(input, "Local name");
    await user.click(screen.getByRole("button", { name: "Save display name" }));

    expect(
      await screen.findByText(/Your profile changed elsewhere/),
    ).toBeVisible();
    expect(input).toHaveValue("Remote name");
    expect(profileReads).toBe(2);

    server.use(
      http.patch("*/mail/api/v1/me/profile", () =>
        HttpResponse.json({ detail: "sensitive database failure" }, { status: 500 }),
      ),
    );
    await user.clear(input);
    await user.type(input, "Another name");
    await user.click(screen.getByRole("button", { name: "Save display name" }));
    expect(await screen.findByText("The display name could not be saved.")).toBeVisible();
    expect(screen.queryByText("sensitive database failure")).not.toBeInTheDocument();
  });

  it("persists both Account language choices without coupling them", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#account");
    const bodies: unknown[] = [];
    server.use(
      http.patch(preferencesUrl, async ({ request }) => {
        const body = (await request.json()) as {
          preferred_ui_locale?: SupportedLocale;
          preferred_correspondence_locale?: SupportedLocale | null;
        };
        bodies.push(body);
        return HttpResponse.json(
          preferencesResponse(
            body.preferred_ui_locale ?? "pl",
            body.preferred_correspondence_locale ?? null,
          ),
        );
      }),
    );
    render(<App />);
    await screen.findByRole("heading", { name: "Account" });

    await user.selectOptions(screen.getByLabelText("Interface language"), "pl");
    expect(await screen.findByRole("heading", { name: "Konto" })).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Język korespondencji"), "en");

    expect(
      await screen.findByText("Język korespondencji zapisany."),
    ).toBeVisible();
    expect(bodies).toEqual([
      { preferred_ui_locale: "pl" },
      { preferred_correspondence_locale: "en" },
    ]);
    expect(screen.getByLabelText("Język korespondencji")).toHaveValue("en");
    expect(document.documentElement).toHaveAttribute("lang", "pl");
  });

  it("validates and changes a password without echoing secrets", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#account");
    let capturedRequest: Request | undefined;
    let capturedBody: unknown;
    server.use(
      http.patch("*/mail/api/v1/me/password", async ({ request }) => {
        capturedRequest = request;
        capturedBody = await request.json();
        return HttpResponse.json({ changed: true });
      }),
    );
    render(<App />);
    const current = await screen.findByLabelText("Current password");
    const next = screen.getByLabelText("New password");
    const confirm = screen.getByLabelText("Confirm new password");
    await user.type(current, "current secret");
    await user.type(next, "a sufficiently long password");
    await user.type(confirm, "a different long password");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText(/do not match/)).toBeVisible();

    await user.clear(next);
    await user.clear(confirm);
    await user.type(next, "short");
    await user.type(confirm, "short");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(
      await screen.findByText(
        "The new password must contain at least 15 characters.",
      ),
    ).toBeVisible();

    await user.clear(next);
    await user.clear(confirm);
    await user.type(next, "new secure password 2026");
    await user.type(confirm, "new secure password 2026");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText("Password changed.")).toBeVisible();
    expect(capturedRequest?.credentials).toBe("same-origin");
    expect(capturedRequest?.cache).toBe("no-store");
    expect(capturedBody).toEqual({
      current_password: "current secret",
      new_password: "new secure password 2026",
    });
    expect(current).toHaveValue("");
    expect(next).toHaveValue("");
    expect(confirm).toHaveValue("");
    expect(screen.queryByText("current secret")).not.toBeInTheDocument();
    expect(screen.queryByText("new secure password 2026")).not.toBeInTheDocument();
  });

  it("shows generic password failures and a distinct rate-limit message", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#account");
    server.use(
      http.patch("*/mail/api/v1/me/password", () =>
        HttpResponse.json({ detail: "Current password is incorrect." }, { status: 400 }),
      ),
    );
    render(<App />);
    await user.type(await screen.findByLabelText("Current password"), "bad current");
    await user.type(screen.getByLabelText("New password"), "long replacement password");
    await user.type(
      screen.getByLabelText("Confirm new password"),
      "long replacement password",
    );
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(
      await screen.findByText(/could not be changed/),
    ).toBeVisible();
    expect(screen.queryByText("Current password is incorrect.")).not.toBeInTheDocument();

    server.use(
      http.patch("*/mail/api/v1/me/password", () =>
        HttpResponse.json({ detail: "limited" }, { status: 429 }),
      ),
    );
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText(/Too many attempts/)).toBeVisible();
  });

  it("fails closed for a member deep-linking to Administration", async () => {
    window.history.replaceState({}, "", "/mail/#admin");
    let adminReads = 0;
    let inboxReads = 0;
    server.use(
      http.get("*/mail/api/v1/me/profile", () => HttpResponse.json(memberProfile)),
      http.get("*/mail/api/v1/admin/access", () => {
        adminReads += 1;
        return HttpResponse.json(adminAccessResponse);
      }),
      http.get("*/mail/api/v1/inbox", () => {
        inboxReads += 1;
        return HttpResponse.json(inboxResponse);
      }),
    );
    render(<App />);

    expect(
      await screen.findByText("Administrator access is required for this page."),
    ).toBeVisible();
    expect(screen.queryByRole("link", { name: "Administration" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Account" })).toBeVisible();
    expect(adminReads).toBe(0);
    expect(inboxReads).toBe(0);
  });

  it("manages member assignments pessimistically, including archived projects", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#admin");
    const requests: Array<{ request: Request; body: unknown }> = [];
    let releaseRequest: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      releaseRequest = resolve;
    });
    server.use(
      http.put(
        "*/mail/api/v1/admin/users/:userId/projects/:projectId",
        async ({ request }) => {
          const body = (await request.json()) as {
            role: "viewer" | "operator" | null;
            expected_access_version: number;
          };
          requests.push({ request, body });
          if (requests.length === 1) {
            await gate;
          }
          return HttpResponse.json({
            changed: true,
            role: body.role,
            access_version: body.expected_access_version + 1,
          });
        },
      ),
    );
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Administration" })).toBeVisible();
    expect(await screen.findByText(projectTwo.human_key)).toBeVisible();
    expect(screen.getByText("Archived")).toBeVisible();

    await user.click(screen.getByRole("button", { name: /Operator One/ }));
    const activeSelect = screen.getByLabelText(`Access to ${projectOne.human_key}`);
    const archivedSelect = screen.getByLabelText(`Access to ${projectTwo.human_key}`);
    expect(activeSelect).toHaveValue("viewer");
    await user.selectOptions(archivedSelect, "operator");
    expect(archivedSelect).toHaveValue("");
    expect(activeSelect).toBeDisabled();
    expect(await screen.findByText(`Saving access to ${projectTwo.human_key}…`)).toBeVisible();
    act(() => releaseRequest());
    expect(await screen.findByText(`Access to ${projectTwo.human_key} saved.`)).toBeVisible();
    expect(archivedSelect).toHaveValue("operator");
    expect(requests[0]?.request.method).toBe("PUT");
    expect(requests[0]?.request.credentials).toBe("same-origin");
    expect(requests[0]?.request.cache).toBe("no-store");
    expect(requests[0]?.body).toEqual({
      role: "operator",
      expected_access_version: memberUser.access_version,
      account_generation: memberUser.account_generation,
      expected_project_generation: adminProjects[1]?.project_generation,
    });

    await user.selectOptions(archivedSelect, "viewer");
    await waitFor(() => expect(requests).toHaveLength(2));
    expect(requests[1]?.body).toMatchObject({ expected_access_version: 8 });

    await user.click(screen.getByRole("button", { name: /disabled/i }));
    expect(screen.getByText("Assignments cannot be changed for this account.")).toBeVisible();
    expect(screen.getByLabelText(`Access to ${projectOne.human_key}`)).toBeDisabled();
  });

  it("refreshes the full access snapshot after an assignment conflict", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#admin");
    let adminReads = 0;
    server.use(
      http.get("*/mail/api/v1/admin/access", () => {
        adminReads += 1;
        return HttpResponse.json(
          adminReads === 1
            ? adminAccessResponse
            : {
                ...adminAccessResponse,
                users: [
                  adminUser,
                  {
                    ...memberUser,
                    access_version: 8,
                    assignments: [{ project_id: projectOne.id, role: "operator" }],
                  },
                  disabledUser,
                ],
              },
        );
      }),
      http.put("*/mail/api/v1/admin/users/:userId/projects/:projectId", () =>
        HttpResponse.json({ detail: { code: "access_version_conflict" } }, { status: 409 }),
      ),
    );
    render(<App />);
    await user.click(await screen.findByRole("button", { name: /Operator One/ }));
    const select = screen.getByLabelText(`Access to ${projectOne.human_key}`);
    await user.selectOptions(select, "operator");

    expect(await screen.findByText(/Access changed elsewhere/)).toBeVisible();
    expect(select).toHaveValue("operator");
    expect(adminReads).toBe(2);
  });

  it("keeps administrator and disabled targets read-only", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#admin");
    render(<App />);
    const adminSelect = await screen.findByLabelText(`Access to ${projectOne.human_key}`);
    expect(adminSelect).toBeDisabled();
    expect(screen.getByText("Assignments cannot be changed for this account.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /disabled/i }));
    expect(screen.getByLabelText(`Access to ${projectOne.human_key}`)).toBeDisabled();
  });

  it("renders honest Administration loading failures and empty snapshots", async () => {
    window.history.replaceState({}, "", "/mail/#admin");
    server.use(
      http.get("*/mail/api/v1/admin/access", () =>
        HttpResponse.json({ detail: "private query" }, { status: 500 }),
      ),
    );
    const failed = render(<App />);
    expect(await screen.findByText("Access assignments could not be loaded.")).toBeVisible();
    expect(screen.queryByText("private query")).not.toBeInTheDocument();
    failed.unmount();

    server.use(
      http.get("*/mail/api/v1/admin/access", () =>
        HttpResponse.json({ users: [], projects: [] }),
      ),
    );
    render(<App />);
    expect(await screen.findAllByText("No accounts are available.")).toHaveLength(2);
  });

  it("strictly validates every Account and Administration success contract", () => {
    expect(parseProfile(adminProfile)).toEqual(adminProfile);
    expect(
      parseProfile({ ...memberProfile, display_name: null }),
    ).toEqual({ ...memberProfile, display_name: null });
    expect(
      parseProfileMutation({
        changed: false,
        display_name: null,
        profile_revision: 4,
      }),
    ).toEqual({ changed: false, display_name: null, profile_revision: 4 });
    expect(parseAdminAccess(adminAccessResponse)).toEqual(adminAccessResponse);
    expect(
      parseAssignmentMutation({ changed: false, role: null, access_version: 8 }),
    ).toEqual({ changed: false, role: null, access_version: 8 });
    expect(
      parseAssignmentMutation({ changed: true, role: "viewer", access_version: 9 }),
    ).toEqual({ changed: true, role: "viewer", access_version: 9 });
    expect(
      parseAssignmentMutation({ changed: true, role: "operator", access_version: 10 }),
    ).toEqual({ changed: true, role: "operator", access_version: 10 });
    expect(parsePasswordMutation({ changed: true })).toEqual({ changed: true });
  });

  it("rejects malformed or over-broad profile contracts", () => {
    const invalidProfiles: unknown[] = [
      null,
      [],
      { ...adminProfile, debug: true },
      {
        id: adminProfile.id,
        username: adminProfile.username,
        display_name: adminProfile.display_name,
        global_role: adminProfile.global_role,
        invented_revision: adminProfile.profile_revision,
      },
      { ...adminProfile, id: 0 },
      { ...adminProfile, id: Number.MAX_SAFE_INTEGER + 1 },
      { ...adminProfile, username: null },
      { ...adminProfile, display_name: 7 },
      { ...adminProfile, global_role: "operator" },
      { ...adminProfile, profile_revision: 0 },
    ];
    for (const payload of invalidProfiles) {
      expect(() => parseProfile(payload)).toThrow(TypeError);
    }

    const validMutation = {
      changed: true,
      display_name: "New name",
      profile_revision: 4,
    };
    for (const payload of [
      null,
      { ...validMutation, debug: true },
      { ...validMutation, changed: "yes" },
      { ...validMutation, display_name: 7 },
      { ...validMutation, profile_revision: 0 },
    ]) {
      expect(() => parseProfileMutation(payload)).toThrow(TypeError);
    }
  });

  it("rejects malformed users, projects, assignments, generations, and timestamps", () => {
    const invalidSnapshots: unknown[] = [
      null,
      { users: {}, projects: [] },
      { users: [], projects: {} },
      { users: [], projects: [], debug: true },
      { users: [null], projects: [] },
      { users: [{ ...memberUser, debug: true }], projects: [] },
      { users: [{ ...memberUser, id: 0 }], projects: [] },
      { users: [{ ...memberUser, username: null }], projects: [] },
      { users: [{ ...memberUser, display_name: 9 }], projects: [] },
      { users: [{ ...memberUser, disabled: "no" }], projects: [] },
      { users: [{ ...memberUser, global_role: "viewer" }], projects: [] },
      { users: [{ ...memberUser, account_generation: "not-a-generation" }], projects: [] },
      { users: [{ ...memberUser, access_version: 0 }], projects: [] },
      { users: [{ ...memberUser, assignments: {} }], projects: [] },
      {
        users: [{ ...memberUser, assignments: [{ project_id: 0, role: "viewer" }] }],
        projects: [],
      },
      {
        users: [{ ...memberUser, assignments: [{ project_id: 1, role: "admin" }] }],
        projects: [],
      },
      {
        users: [
          {
            ...memberUser,
            assignments: [{ project_id: 1, role: "viewer", debug: true }],
          },
        ],
        projects: [],
      },
      { users: [], projects: [null] },
      { users: [], projects: [{ ...adminProjects[0], debug: true }] },
      { users: [], projects: [{ ...adminProjects[0], id: 0 }] },
      { users: [], projects: [{ ...adminProjects[0], slug: null }] },
      { users: [], projects: [{ ...adminProjects[0], human_key: null }] },
      {
        users: [],
        projects: [{ ...adminProjects[0], project_generation: "bad" }],
      },
      { users: [], projects: [{ ...adminProjects[0], archived_at: 4 }] },
      { users: [], projects: [{ ...adminProjects[0], archived_at: "" }] },
      { users: [], projects: [{ ...adminProjects[0], archived_at: "bad-date" }] },
    ];
    for (const payload of invalidSnapshots) {
      expect(() => parseAdminAccess(payload)).toThrow(TypeError);
    }
  });

  it("rejects malformed mutation contracts and any secret or debug extras", () => {
    for (const payload of [
      null,
      { changed: true, role: null, access_version: 1, debug: true },
      { changed: "yes", role: null, access_version: 1 },
      { changed: true, role: "admin", access_version: 1 },
      { changed: true, role: null, access_version: 0 },
    ]) {
      expect(() => parseAssignmentMutation(payload)).toThrow(TypeError);
    }
    for (const payload of [
      null,
      { changed: false },
      { changed: true, password: "secret" },
    ]) {
      expect(() => parsePasswordMutation(payload)).toThrow(TypeError);
    }
  });

  it("uses same-origin no-store requests for every typed account API", async () => {
    const requests: Request[] = [];
    server.use(
      http.get("*/mail/api/v1/me/profile", ({ request }) => {
        requests.push(request);
        return HttpResponse.json(adminProfile);
      }),
      http.patch("*/mail/api/v1/me/profile", ({ request }) => {
        requests.push(request);
        return HttpResponse.json({
          changed: true,
          display_name: "Typed name",
          profile_revision: 4,
        });
      }),
      http.patch("*/mail/api/v1/me/password", ({ request }) => {
        requests.push(request);
        return HttpResponse.json({ changed: true });
      }),
      http.get("*/mail/api/v1/admin/access", ({ request }) => {
        requests.push(request);
        return HttpResponse.json(adminAccessResponse);
      }),
      http.put(
        "*/mail/api/v1/admin/users/:userId/projects/:projectId",
        ({ request }) => {
          requests.push(request);
          return HttpResponse.json({
            changed: true,
            role: "operator",
            access_version: 8,
          });
        },
      ),
    );

    await expect(loadProfile()).resolves.toEqual(adminProfile);
    await expect(saveDisplayName("Typed name", 3)).resolves.toEqual({
      changed: true,
      display_name: "Typed name",
      profile_revision: 4,
    });
    await expect(changePassword("old secret", "new secure password")).resolves.toEqual({
      changed: true,
    });
    await expect(loadAdminAccess()).resolves.toEqual(adminAccessResponse);
    await expect(
      saveProjectAssignment(memberUser, adminProjects[0]!, "operator"),
    ).resolves.toEqual({ changed: true, role: "operator", access_version: 8 });

    expect(requests).toHaveLength(5);
    for (const request of requests) {
      expect(request.credentials).toBe("same-origin");
      expect(request.cache).toBe("no-store");
      expect(request.headers.get("accept")).toBe("application/json");
      expect(new URL(request.url).origin).toBe(window.location.origin);
    }
    expect(requests[0]?.headers.get("content-type")).toBeNull();
    expect(requests[1]?.headers.get("content-type")).toBe("application/json");
  });

  it("throws status-only HTTP errors and validates assignment inputs before fetch", async () => {
    server.use(
      http.get("*/mail/api/v1/me/profile", () =>
        HttpResponse.json({ detail: "private" }, { status: 503 }),
      ),
    );
    const failure = await loadProfile().catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(AccountHttpError);
    expect((failure as AccountHttpError).status).toBe(503);
    expect((failure as Error).message).not.toContain("private");

    await expect(
      saveProjectAssignment({ ...memberUser, id: 0 }, adminProjects[0]!, "viewer"),
    ).rejects.toThrow(TypeError);
    await expect(
      saveProjectAssignment(memberUser, { ...adminProjects[0]!, id: 0 }, "viewer"),
    ).rejects.toThrow(TypeError);
    await expect(
      saveProjectAssignment(
        { ...memberUser, access_version: 0 },
        adminProjects[0]!,
        "viewer",
      ),
    ).rejects.toThrow(TypeError);
    await expect(
      saveProjectAssignment(
        { ...memberUser, account_generation: "bad" },
        adminProjects[0]!,
        "viewer",
      ),
    ).rejects.toThrow(TypeError);
    await expect(
      saveProjectAssignment(
        memberUser,
        { ...adminProjects[0]!, project_generation: "bad" },
        "viewer",
      ),
    ).rejects.toThrow(TypeError);
  });

  it("strictly validates preference envelopes and the correspondence request", async () => {
    for (const payload of [
      null,
      { ...preferencesResponse("en"), debug: true },
      {
        ...preferencesResponse("en"),
        stored: { ...preferencesResponse("en").stored, debug: true },
      },
      {
        ...preferencesResponse("en"),
        effective: { ...preferencesResponse("en").effective, debug: true },
      },
    ]) {
      expect(() => parsePreferences(payload)).toThrow(TypeError);
    }

    let captured: Request | undefined;
    server.use(
      http.patch(preferencesUrl, ({ request }) => {
        captured = request;
        return HttpResponse.json(preferencesResponse("en", "pl"));
      }),
    );
    await expect(loadPreferences()).resolves.toEqual(preferencesResponse("en"));
    await expect(saveCorrespondenceLocale("pl")).resolves.toEqual(
      preferencesResponse("en", "pl"),
    );
    expect(await captured?.json()).toEqual({ preferred_correspondence_locale: "pl" });
  });

  it.each([
    ["account", 500, "Your account could not be loaded. Try again later."],
    ["account", 401, "Your session expired. Redirecting to sign in."],
    ["admin", 500, "Your account could not be loaded. Try again later."],
    ["admin", 401, "Your session expired. Redirecting to sign in."],
  ])(
    "renders a safe %s profile failure for HTTP %s",
    async (view, status, expected) => {
      window.history.replaceState({}, "", `/mail/#${view}`);
      const onUnauthorized = vi.fn();
      server.use(
        http.get("*/mail/api/v1/me/profile", () =>
          HttpResponse.json({ detail: "private identity error" }, { status }),
        ),
      );
      render(<App onUnauthorized={onUnauthorized} />);
      expect(await screen.findByText(expected)).toBeVisible();
      expect(screen.queryByText("private identity error")).not.toBeInTheDocument();
      expect(onUnauthorized).toHaveBeenCalledTimes(status === 401 ? 1 : 0);
    },
  );

  it("aborts pending profile and Administration reads on unmount", async () => {
    window.history.replaceState({}, "", "/mail/#account");
    let profileStarted = false;
    server.use(
      http.get("*/mail/api/v1/me/profile", async ({ request }) => {
        profileStarted = true;
        await new Promise<void>((_resolve, reject) => {
          request.signal.addEventListener("abort", () =>
            reject(new DOMException("cancelled", "AbortError")),
          );
        });
        return HttpResponse.json(adminProfile);
      }),
    );
    const profileView = render(<App />);
    await waitFor(() => expect(profileStarted).toBe(true));
    profileView.unmount();
    await act(async () => Promise.resolve());

    window.history.replaceState({}, "", "/mail/#admin");
    let adminStarted = false;
    server.use(
      http.get("*/mail/api/v1/me/profile", () => HttpResponse.json(adminProfile)),
      http.get("*/mail/api/v1/admin/access", async ({ request }) => {
        adminStarted = true;
        await new Promise<void>((_resolve, reject) => {
          request.signal.addEventListener("abort", () =>
            reject(new DOMException("cancelled", "AbortError")),
          );
        });
        return HttpResponse.json(adminAccessResponse);
      }),
    );
    const adminView = render(<App />);
    await waitFor(() => expect(adminStarted).toBe(true));
    adminView.unmount();
    await act(async () => Promise.resolve());
  });

  it("redirects when the Administration snapshot loses authorization", async () => {
    window.history.replaceState({}, "", "/mail/#admin");
    const onUnauthorized = vi.fn();
    server.use(
      http.get("*/mail/api/v1/admin/access", () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );
    render(<App onUnauthorized={onUnauthorized} />);
    expect(
      await screen.findByText("Your session expired. Redirecting to sign in."),
    ).toBeVisible();
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("saves correspondence inheritance and reports generic write errors", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#account");
    const bodies: unknown[] = [];
    server.use(
      http.patch(preferencesUrl, async ({ request }) => {
        const body = await request.json();
        bodies.push(body);
        return HttpResponse.json(
          preferencesResponse(
            "en",
            (body as { preferred_correspondence_locale: "pl" | null })
              .preferred_correspondence_locale,
          ),
        );
      }),
    );
    render(<App />);
    const select = await screen.findByLabelText("Correspondence language");
    await user.selectOptions(select, "pl");
    await screen.findByText("Correspondence language saved.");
    await user.selectOptions(select, "");
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1]).toEqual({ preferred_correspondence_locale: null });

    server.use(
      http.patch(preferencesUrl, () =>
        HttpResponse.json({ detail: "private" }, { status: 500 }),
      ),
    );
    await user.selectOptions(select, "pl");
    expect(
      await screen.findByText("The correspondence language could not be saved."),
    ).toBeVisible();
  });

  it("redirects on unauthorized correspondence, profile, and password writes", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#account");
    const onUnauthorized = vi.fn();
    server.use(
      http.patch(preferencesUrl, () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );
    const correspondenceView = render(<App onUnauthorized={onUnauthorized} />);
    await user.selectOptions(
      await screen.findByLabelText("Correspondence language"),
      "pl",
    );
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledOnce());
    correspondenceView.unmount();

    const profileRedirect = vi.fn();
    server.use(
      http.patch("*/mail/api/v1/me/profile", () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );
    const profileView = render(<App onUnauthorized={profileRedirect} />);
    await user.click(await screen.findByRole("button", { name: "Save display name" }));
    await waitFor(() => expect(profileRedirect).toHaveBeenCalledOnce());
    profileView.unmount();

    const passwordRedirect = vi.fn();
    server.use(
      http.patch("*/mail/api/v1/me/password", () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );
    render(<App onUnauthorized={passwordRedirect} />);
    await user.type(await screen.findByLabelText("Current password"), "current");
    await user.type(screen.getByLabelText("New password"), "long secure password");
    await user.type(screen.getByLabelText("Confirm new password"), "long secure password");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    await waitFor(() => expect(passwordRedirect).toHaveBeenCalledOnce());
  });

  it("handles an authorization loss while refreshing a profile conflict", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#account");
    const onUnauthorized = vi.fn();
    let profileReads = 0;
    server.use(
      http.get("*/mail/api/v1/me/profile", () => {
        profileReads += 1;
        return profileReads === 1
          ? HttpResponse.json(adminProfile)
          : HttpResponse.json({ detail: "expired" }, { status: 401 });
      }),
      http.patch("*/mail/api/v1/me/profile", () =>
        HttpResponse.json({ detail: { code: "profile_revision_conflict" } }, { status: 409 }),
      ),
    );
    render(<App onUnauthorized={onUnauthorized} />);
    await user.click(await screen.findByRole("button", { name: "Save display name" }));
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledOnce());
  });

  it("reports assignment failures and redirects on authorization loss", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#admin");
    server.use(
      http.put("*/mail/api/v1/admin/users/:userId/projects/:projectId", () =>
        HttpResponse.json({ detail: "private failure" }, { status: 500 }),
      ),
    );
    const failed = render(<App />);
    await user.click(await screen.findByRole("button", { name: /Operator One/ }));
    await user.selectOptions(
      screen.getByLabelText(`Access to ${projectTwo.human_key}`),
      "operator",
    );
    expect(await screen.findByText("Access could not be saved.")).toBeVisible();
    expect(screen.queryByText("private failure")).not.toBeInTheDocument();
    failed.unmount();

    const onUnauthorized = vi.fn();
    server.use(
      http.put("*/mail/api/v1/admin/users/:userId/projects/:projectId", () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );
    render(<App onUnauthorized={onUnauthorized} />);
    await user.click(await screen.findByRole("button", { name: /Operator One/ }));
    await user.selectOptions(
      screen.getByLabelText(`Access to ${projectTwo.human_key}`),
      "operator",
    );
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledOnce());
  });

  it.each([401, 500])(
    "handles HTTP %s while refreshing a conflicting assignment",
    async (refreshStatus) => {
      const user = userEvent.setup();
      window.history.replaceState({}, "", "/mail/#admin");
      const onUnauthorized = vi.fn();
      let adminReads = 0;
      server.use(
        http.get("*/mail/api/v1/admin/access", () => {
          adminReads += 1;
          return adminReads === 1
            ? HttpResponse.json(adminAccessResponse)
            : HttpResponse.json({ detail: "refresh failed" }, { status: refreshStatus });
        }),
        http.put("*/mail/api/v1/admin/users/:userId/projects/:projectId", () =>
          HttpResponse.json({ detail: { code: "access_version_conflict" } }, { status: 409 }),
        ),
      );
      render(<App onUnauthorized={onUnauthorized} />);
      await user.click(await screen.findByRole("button", { name: /Operator One/ }));
      await user.selectOptions(
        screen.getByLabelText(`Access to ${projectTwo.human_key}`),
        "operator",
      );
      if (refreshStatus === 401) {
        await waitFor(() => expect(onUnauthorized).toHaveBeenCalledOnce());
      } else {
        expect(await screen.findByText("Access assignments could not be loaded.")).toBeVisible();
      }
    },
  );

  it("revokes access and keeps the selected user across an Administration reload", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#admin");
    render(<App />);
    await user.click(await screen.findByRole("button", { name: /Operator One/ }));
    const select = screen.getByLabelText(`Access to ${projectOne.human_key}`);
    await user.selectOptions(select, "");
    await waitFor(() => expect(select).toHaveValue(""));

    await user.click(screen.getByRole("link", { name: "Account" }));
    expect(await screen.findByRole("heading", { name: "Account" })).toBeVisible();
    await user.click(screen.getByRole("link", { name: "Administration" }));
    expect(await screen.findByText("Selected account: Operator One")).toBeVisible();
  });

  it("shows an empty project list for a selected member", async () => {
    window.history.replaceState({}, "", "/mail/#admin");
    server.use(
      http.get("*/mail/api/v1/admin/access", () =>
        HttpResponse.json({ users: [memberUser], projects: [] }),
      ),
    );
    render(<App />);
    expect(await screen.findByText("No projects are available.")).toBeVisible();
  });

  it("renders and conflict-refreshes an explicitly empty display name", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#account");
    let profileReads = 0;
    server.use(
      http.get("*/mail/api/v1/me/profile", () => {
        profileReads += 1;
        return HttpResponse.json({
          ...adminProfile,
          display_name: null,
          profile_revision: profileReads === 1 ? 3 : 4,
        });
      }),
      http.patch("*/mail/api/v1/me/profile", () =>
        HttpResponse.json({ detail: { code: "profile_revision_conflict" } }, { status: 409 }),
      ),
    );
    render(<App />);
    const input = await screen.findByLabelText("Display name");
    expect(input).toHaveValue("");
    await user.type(input, "Temporary");
    await user.click(screen.getByRole("button", { name: "Save display name" }));
    expect(await screen.findByText(/Your profile changed elsewhere/)).toBeVisible();
    expect(input).toHaveValue("");
  });

  it("shows a generic error when a profile conflict cannot be refreshed", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/mail/#account");
    let profileReads = 0;
    server.use(
      http.get("*/mail/api/v1/me/profile", () => {
        profileReads += 1;
        return profileReads === 1
          ? HttpResponse.json(adminProfile)
          : HttpResponse.json({ detail: "private refresh" }, { status: 500 });
      }),
      http.patch("*/mail/api/v1/me/profile", () =>
        HttpResponse.json({ detail: { code: "profile_revision_conflict" } }, { status: 409 }),
      ),
    );
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Save display name" }));
    expect(await screen.findByText("The display name could not be saved.")).toBeVisible();
    expect(screen.queryByText("private refresh")).not.toBeInTheDocument();
  });

  it("ignores an aborted Administration snapshot without replacing the page", async () => {
    window.history.replaceState({}, "", "/mail/#admin");
    const originalFetch = globalThis.fetch;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      return url.endsWith("/mail/api/v1/admin/access")
        ? Promise.reject(new DOMException("cancelled", "AbortError"))
        : originalFetch(input, init);
    });
    render(<App />);
    expect(await screen.findByText("Loading access assignments…")).toBeVisible();
    expect(screen.getByText("Loading access assignments…")).toBeVisible();
  });

  it.each([
    [[adminUser], "Selected account: Mateusz"],
    [[], "No accounts are available."],
  ])(
    "selects a safe fallback after the current account disappears on conflict",
    async (refreshedUsers, expectedText) => {
      const user = userEvent.setup();
      window.history.replaceState({}, "", "/mail/#admin");
      let adminReads = 0;
      server.use(
        http.get("*/mail/api/v1/admin/access", () => {
          adminReads += 1;
          return HttpResponse.json(
            adminReads === 1
              ? adminAccessResponse
              : { users: refreshedUsers, projects: adminProjects },
          );
        }),
        http.put("*/mail/api/v1/admin/users/:userId/projects/:projectId", () =>
          HttpResponse.json({ detail: { code: "access_version_conflict" } }, { status: 409 }),
        ),
      );
      render(<App />);
      await user.click(await screen.findByRole("button", { name: /Operator One/ }));
      await user.selectOptions(
        screen.getByLabelText(`Access to ${projectTwo.human_key}`),
        "operator",
      );
      if (refreshedUsers.length === 0) {
        expect(await screen.findAllByText(expectedText)).toHaveLength(2);
      } else {
        expect(await screen.findByText(expectedText)).toBeVisible();
      }
    },
  );
});
