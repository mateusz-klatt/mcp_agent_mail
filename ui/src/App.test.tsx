import { act, render, screen, waitFor, within } from "@testing-library/react";
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
import i18n from "./i18n";
import {
  inboxPageSize,
  isCanonicalInlineRasterImageSource,
  loadInbox,
  loadMessage,
  loadProjects,
  mailRouteHash,
  parseInboxPage,
  parseMailRoute,
  parseMessageDetail,
  parseProjects,
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
  inboxResponse,
  memberProfile,
  memberUser,
  messageDetail,
  messageOne,
  messageTwo,
  preferencesResponse,
  projectOne,
  projectsResponse,
  projectTwo,
  server,
} from "./test/server";

const preferencesUrl = "*/mail/api/v1/me/preferences";

describe("legacy Markdown image boundary", () => {
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
});

async function waitForEnglishPreferences() {
  expect(await screen.findByText("Language saved for your account")).toBeVisible();
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

describe("Hermes landing shell", () => {
  beforeEach(async () => {
    window.history.replaceState({}, "", "/mail/v2/");
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

  it("renders the real inbox without demo controls or invented unread data", async () => {
    render(<App />);
    await waitForEnglishPreferences();

    expect(await screen.findByRole("heading", { name: "Inbox" })).toBeVisible();
    expect(await screen.findByText(messageOne.subject)).toBeVisible();
    expect(screen.getByText("Mailbox read-only")).toBeVisible();
    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    const links = within(navigation).getAllByRole("link");
    expect(links).toHaveLength(4);
    expect(links.map((link) => link.textContent)).toEqual([
      "Projects",
      "Inbox",
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
    expect(screen.getByLabelText("Language")).toHaveAttribute("name", "ui-language");
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
    window.history.replaceState({}, "", "/mail/v2/#projects");
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

  it("loads a stored Polish UI locale while keeping correspondence independent", async () => {
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
    expect(screen.getByLabelText("Język")).toHaveValue("pl");
    expect(screen.getByLabelText("Język")).toHaveAccessibleDescription(
      "Język zapisany na Twoim koncie",
    );
    expect(screen.getByRole("button", { name: "Wyloguj się" })).toBeVisible();
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

    await user.selectOptions(screen.getByLabelText("Language"), "pl");
    expect(await screen.findByText("Saving language…")).toBeVisible();
    expect(screen.getByLabelText("Language")).toBeDisabled();
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
    expect(screen.getByLabelText("Język")).toHaveValue("pl");
    expect(screen.getByRole("status")).toHaveTextContent("Język zapisany na Twoim koncie");
  });

  it("keeps the future API boundary testable through MSW", async () => {
    const response = await fetch("http://localhost/mail/api/v1/health");

    await expect(response.json()).resolves.toEqual({ status: "ok" });
  });

  it("calls the injected login callback on an unauthorized preference read", async () => {
    window.history.replaceState({}, "", "/mail/v2/?project=mail#inbox");
    const onUnauthorized = vi.fn();
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );

    render(<App onUnauthorized={onUnauthorized} />);

    await waitFor(() =>
      expect(onUnauthorized).toHaveBeenCalledWith(
        "/mail/login?next=%2Fmail%2Fv2%2F%3Fproject%3Dmail%23inbox",
      ),
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Your session expired. Redirecting to sign in.",
    );
    expect(screen.getByLabelText("Language")).toBeDisabled();
  });

  it("uses same-tab login navigation when no unauthorized callback is supplied", async () => {
    window.history.replaceState({}, "", "/mail/v2/#projects");
    const navigateTo = vi.fn();
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );

    render(<App navigateTo={navigateTo} />);

    await waitFor(() =>
      expect(navigateTo).toHaveBeenCalledWith(
        "/mail/login?next=%2Fmail%2Fv2%2F%23projects",
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
            preferred_ui_locale: "fr",
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
    expect(screen.getByLabelText("Language")).toBeEnabled();
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
    expect(screen.getByLabelText("Language")).toHaveValue("en");

    await user.selectOptions(screen.getByLabelText("Language"), "pl");

    expect(patchRequests).toBe(0);
    expect(document.documentElement).toHaveAttribute("lang", "pl");
    expect(screen.getByRole("heading", { name: "Skrzynka" })).toBeVisible();
    expect(screen.getByLabelText("Język")).toHaveValue("pl");
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

    await user.selectOptions(screen.getByLabelText("Language"), "pl");

    expect(
      await screen.findByText(
        "Could not save your language. Your previous language is still active.",
      ),
    ).toBeVisible();
    expect(screen.getByLabelText("Language")).toHaveValue("en");
    expect(document.documentElement).toHaveAttribute("lang", "en");
    expect(screen.getByRole("heading", { name: "Inbox" })).toBeVisible();
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

    await user.selectOptions(screen.getByLabelText("Language"), "pl");

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledOnce());
    expect(screen.getByLabelText("Language")).toHaveValue("en");
    expect(screen.getByLabelText("Language")).toBeDisabled();
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
          preferred_ui_locale: "fr",
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
          preferred_correspondence_locale: "fr",
        },
      },
    ],
    [
      "effective UI locale",
      {
        ...preferencesResponse("en"),
        effective: { ui_locale: "fr", correspondence_locale: "en" },
      },
    ],
    [
      "effective correspondence locale",
      {
        ...preferencesResponse("en"),
        effective: { ui_locale: "en", correspondence_locale: "fr" },
      },
    ],
  ])("rejects an invalid %s at runtime", (_label, payload) => {
    expect(() => parsePreferences(payload)).toThrow(TypeError);
  });

  it("opens a real message detail and keeps Markdown as plain text", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(
      await screen.findByRole("link", {
        name: new RegExp(`Open message.*${messageOne.subject}`),
      }),
    );

    expect(
      await screen.findByRole("heading", { name: messageOne.subject }),
    ).toBeVisible();
    expect(document.querySelector(".message-body")).toHaveTextContent(
      "# Release All checks passed. `<script>` remains plain text.",
    );
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
      `/mail/v2/#message/${projectTwo.id}/${messageTwo.id}`,
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
      `/mail/v2/#message/${projectTwo.id}/${messageTwo.id}`,
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
        `/mail/v2/#message/${projectOne.id}/${messageOne.id}`,
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
          `/mail/v2/#message/${projectTwo.id}/${messageTwo.id}`,
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
      `/mail/v2/#message/${projectOne.id}/${messageOne.id}`,
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
      `/mail/v2/#message/${projectOne.id}/9999`,
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
    window.history.replaceState({}, "", "/mail/v2/#projects");
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
      `/mail/v2/#inbox?project=${projectOne.id}`,
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
      `/mail/login?next=%2Fmail%2Fv2%2F%23inbox%3Fproject%3D${projectOne.id}`,
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
  });

  it("validates message bodies, recipient lists, and safe attachment metadata", () => {
    expect(parseMessageDetail(messageDetail)).toEqual(messageDetail);
    const invalidDetails = [
      { ...messageDetail, body_md: null },
      { ...messageDetail, to: null },
      { ...messageDetail, to: ["agent", 7] },
      { ...messageDetail, cc: null },
      { ...messageDetail, cc: [7] },
      { ...messageDetail, attachments: null },
      { ...messageDetail, attachments: [null] },
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

  it("round-trips valid hashes and rejects malformed deep links", () => {
    expect(parseMailRoute("")).toEqual({ view: "inbox", projectId: null });
    expect(parseMailRoute("inbox?project=11")).toEqual({
      view: "inbox",
      projectId: 11,
    });
    expect(parseMailRoute("#projects")).toEqual({ view: "projects" });
    expect(parseMailRoute("#message/11/101")).toEqual({
      view: "message",
      projectId: 11,
      messageId: 101,
    });
    for (const hash of [
      "#inbox?project=nope",
      "#inbox?project=0",
      "#message/11",
      "#message/0/1",
      "#message/1/0",
      "#message/9007199254740992/1",
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
  });

  it("uses exact same-origin endpoints in the standalone mail client", async () => {
    const urls: string[] = [];
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
    );

    await expect(loadProjects()).resolves.toEqual(projectsResponse);
    await expect(
      loadInbox({ cursor: "opaque", projectId: projectOne.id }),
    ).resolves.toEqual(inboxResponse);
    await expect(
      loadMessage(projectOne.id, messageOne.id),
    ).resolves.toEqual(messageDetail);
    expect(urls).toEqual([
      "http://localhost:3000/mail/api/v1/projects",
      `http://localhost:3000/mail/api/v1/inbox?limit=${inboxPageSize}&cursor=opaque&project_id=${projectOne.id}`,
      `http://localhost:3000/mail/api/v1/projects/${projectOne.id}/messages/${messageOne.id}`,
    ]);
  });

  it("ignores aborted project, inbox, and detail requests during cleanup", async () => {
    window.history.replaceState(
      {},
      "",
      `/mail/v2/#message/${projectOne.id}/${messageOne.id}`,
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
      `/mail/v2/#inbox?project=${projectOne.id}`,
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
    window.history.replaceState({}, "", "/mail/v2/#projects");
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
      `/mail/v2/#message/${sparseDetail.project_id}/${sparseDetail.id}`,
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
      `/mail/v2/#message/${projectOne.id}/${messageOne.id}`,
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
    window.history.replaceState({}, "", "/mail/v2/#account");
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
    window.history.replaceState({}, "", "/mail/v2/#account");
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
    window.history.replaceState({}, "", "/mail/v2/#account");
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
    window.history.replaceState({}, "", "/mail/v2/#account");
    const bodies: unknown[] = [];
    server.use(
      http.patch(preferencesUrl, async ({ request }) => {
        const body = (await request.json()) as {
          preferred_ui_locale?: "en" | "pl";
          preferred_correspondence_locale?: "en" | "pl" | null;
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
    window.history.replaceState({}, "", "/mail/v2/#account");
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
    window.history.replaceState({}, "", "/mail/v2/#account");
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
    window.history.replaceState({}, "", "/mail/v2/#admin");
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
    window.history.replaceState({}, "", "/mail/v2/#admin");
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
    window.history.replaceState({}, "", "/mail/v2/#admin");
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
    window.history.replaceState({}, "", "/mail/v2/#admin");
    render(<App />);
    const adminSelect = await screen.findByLabelText(`Access to ${projectOne.human_key}`);
    expect(adminSelect).toBeDisabled();
    expect(screen.getByText("Assignments cannot be changed for this account.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /disabled/i }));
    expect(screen.getByLabelText(`Access to ${projectOne.human_key}`)).toBeDisabled();
  });

  it("renders honest Administration loading failures and empty snapshots", async () => {
    window.history.replaceState({}, "", "/mail/v2/#admin");
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
      window.history.replaceState({}, "", `/mail/v2/#${view}`);
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
    window.history.replaceState({}, "", "/mail/v2/#account");
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

    window.history.replaceState({}, "", "/mail/v2/#admin");
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
    window.history.replaceState({}, "", "/mail/v2/#admin");
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
    window.history.replaceState({}, "", "/mail/v2/#account");
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
    window.history.replaceState({}, "", "/mail/v2/#account");
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
    window.history.replaceState({}, "", "/mail/v2/#account");
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
    window.history.replaceState({}, "", "/mail/v2/#admin");
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
      window.history.replaceState({}, "", "/mail/v2/#admin");
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
    window.history.replaceState({}, "", "/mail/v2/#admin");
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
    window.history.replaceState({}, "", "/mail/v2/#admin");
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
    window.history.replaceState({}, "", "/mail/v2/#account");
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
    window.history.replaceState({}, "", "/mail/v2/#account");
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
    window.history.replaceState({}, "", "/mail/v2/#admin");
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
      window.history.replaceState({}, "", "/mail/v2/#admin");
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
