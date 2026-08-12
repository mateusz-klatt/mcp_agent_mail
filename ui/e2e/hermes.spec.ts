import {
  expect,
  test,
  type Page,
  type Route,
} from "@playwright/test";

import type {
  AdminAccessSnapshot,
  AssignmentRole,
  MailUiProfile,
} from "../src/account";
import type {
  DeliveryResult,
  InboxMessage,
  InboxPage,
  MailProject,
  MessageDetail,
} from "../src/mail";
import type { MailUiPreferences } from "../src/preferences";

const project: MailProject = {
  id: 11,
  slug: "mcp-agent-mail",
  human_key: "/mateusz-klatt/mcp_agent_mail",
  created_at: "2026-08-10T08:00:00Z",
  archived_at: null,
  role: "admin",
  can_reply: true,
};

const messageSummary: InboxMessage = {
  id: 101,
  project_id: project.id,
  project_slug: project.slug,
  subject: "Production rollout verified",
  sender: "claude-linux-holzera-1",
  sender_name: "claude-linux-holzera-1",
  sender_display_name: "Gospodarz",
  importance: "high",
  thread_id: "release-101",
  reply_to: null,
  created_ts: "2026-08-11T10:15:00Z",
  ack_required: true,
  can_reply: true,
};

const message: MessageDetail = {
  ...messageSummary,
  body_md: "# Release\n\nAll checks passed. `<script>` remains plain text.",
  to: ["codex-wsl-home-1"],
  cc: [],
  attachments: [
    { type: "artifact", media_type: "application/json", size_bytes: 1280 },
  ],
};

const accountGeneration = "b".repeat(64);
const projectGeneration = "d".repeat(64);
const deliveryId = "01234567-89ab-4cde-8f01-23456789abcd";
const publishedDelivery: DeliveryResult = {
  id: deliveryId,
  status: "published",
  reused: false,
  message_id: 202,
  commit_sha: "e".repeat(40),
  next_attempt_ts: null,
};
const pendingDelivery: DeliveryResult = {
  id: deliveryId,
  status: "pending",
  reused: false,
  message_id: null,
  commit_sha: null,
  next_attempt_ts: "2026-08-12T20:30:00Z",
};

type StubActorRole = "admin" | "operator";

interface LocalStubOptions {
  actorRole?: StubActorRole;
  composeResult?: DeliveryResult;
  replyResult?: DeliveryResult;
  retryResult?: DeliveryResult;
}

interface TypedWrite {
  method: "POST";
  path: string;
  body: unknown;
}

interface StubState {
  preferences: MailUiPreferences;
  profile: MailUiProfile;
  admin: AdminAccessSnapshot;
  passwordChanges: number;
  typedWrites: TypedWrite[];
  externalRequests: string[];
  browserErrors: string[];
}

function json(route: Route, body: unknown, status = 200): Promise<void> {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

function isExternal(urlText: string): boolean {
  const url = new URL(urlText);
  return (
    ["http:", "https:", "ws:", "wss:"].includes(url.protocol) &&
    url.hostname !== "127.0.0.1" &&
    url.hostname !== "localhost"
  );
}

async function installLocalStub(
  page: Page,
  messageDetail: MessageDetail = message,
  options: LocalStubOptions = {},
): Promise<StubState> {
  const actorRole = options.actorRole ?? "admin";
  const assignedProject: MailProject = { ...project, role: actorRole };
  const state: StubState = {
    preferences: {
      stored: {
        preferred_ui_locale: "en",
        preferred_correspondence_locale: null,
      },
      effective: { ui_locale: "en", correspondence_locale: "en" },
    },
    profile: {
      id: 1,
      username: "mateusz",
      display_name: "Mateusz",
      global_role: actorRole === "admin" ? "admin" : "member",
      profile_revision: 3,
    },
    admin: {
      users: [
        {
          id: 1,
          username: "mateusz",
          display_name: "Mateusz",
          disabled: false,
          global_role: "admin",
          account_generation: "a".repeat(64),
          access_version: 4,
          assignments: [],
        },
        {
          id: 2,
          username: "operator",
          display_name: "Operator One",
          disabled: false,
          global_role: "member",
          account_generation: accountGeneration,
          access_version: 7,
          assignments: [{ project_id: project.id, role: "viewer" }],
        },
      ],
      projects: [
        {
          id: project.id,
          slug: project.slug,
          human_key: project.human_key,
          project_generation: projectGeneration,
          archived_at: null,
        },
      ],
    },
    passwordChanges: 0,
    typedWrites: [],
    externalRequests: [],
    browserErrors: [],
  };

  page.on("request", (request) => {
    if (isExternal(request.url())) {
      state.externalRequests.push(request.url());
    }
  });
  page.on("websocket", (socket) => {
    if (isExternal(socket.url())) {
      state.externalRequests.push(socket.url());
    }
  });
  page.on("pageerror", (error) => state.browserErrors.push(error.message));
  page.on("console", (entry) => {
    if (entry.type() === "error") {
      state.browserErrors.push(entry.text());
    }
  });

  await page.route("**/mail/events", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-store" },
      body: "retry: 60000\n\n",
    }),
  );

  await page.route("**/mail/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/mail/api/v1/me/preferences") {
      if (method === "PATCH") {
        const body = request.postDataJSON() as {
          preferred_ui_locale?: "en" | "pl";
          preferred_correspondence_locale?: "en" | "pl" | null;
        };
        const uiLocale = body.preferred_ui_locale ?? state.preferences.stored.preferred_ui_locale;
        const correspondenceLocale = Object.hasOwn(body, "preferred_correspondence_locale")
          ? (body.preferred_correspondence_locale ?? null)
          : state.preferences.stored.preferred_correspondence_locale;
        state.preferences = {
          stored: {
            preferred_ui_locale: uiLocale,
            preferred_correspondence_locale: correspondenceLocale,
          },
          effective: {
            ui_locale: uiLocale,
            correspondence_locale: correspondenceLocale ?? uiLocale,
          },
        };
      }
      return json(route, state.preferences);
    }

    if (path === "/mail/api/v1/me/profile") {
      if (method === "PATCH") {
        const body = request.postDataJSON() as {
          display_name: string | null;
          expected_profile_revision: number;
        };
        state.profile = {
          ...state.profile,
          display_name: body.display_name,
          profile_revision: body.expected_profile_revision + 1,
        };
        return json(route, {
          changed: true,
          display_name: state.profile.display_name,
          profile_revision: state.profile.profile_revision,
        });
      }
      return json(route, state.profile);
    }

    if (path === "/mail/api/v1/me/password" && method === "PATCH") {
      state.passwordChanges += 1;
      return json(route, { changed: true });
    }

    if (path === "/mail/api/v1/admin/access" && method === "GET") {
      return json(route, state.admin);
    }

    const assignmentMatch = path.match(
      /^\/mail\/api\/v1\/admin\/users\/(\d+)\/projects\/(\d+)$/,
    );
    if (assignmentMatch !== null && method === "PUT") {
      const userId = Number(assignmentMatch[1]);
      const projectId = Number(assignmentMatch[2]);
      const body = request.postDataJSON() as {
        role: AssignmentRole | null;
        expected_access_version: number;
        account_generation: string;
        expected_project_generation: string;
      };
      expect(body.account_generation).toBe(accountGeneration);
      expect(body.expected_project_generation).toBe(projectGeneration);
      const accessVersion = body.expected_access_version + 1;
      state.admin = {
        ...state.admin,
        users: state.admin.users.map((user) =>
          user.id === userId
            ? {
                ...user,
                access_version: accessVersion,
                assignments: [
                  ...user.assignments.filter(
                    (assignment) => assignment.project_id !== projectId,
                  ),
                  ...(body.role === null ? [] : [{ project_id: projectId, role: body.role }]),
                ],
              }
            : user,
        ),
      };
      return json(route, { changed: true, role: body.role, access_version: accessVersion });
    }

    if (path === "/mail/api/v1/projects" && method === "GET") {
      return json(route, { items: [assignedProject], total: 1 });
    }

    if (path === "/mail/api/v1/inbox" && method === "GET") {
      const requestedProject = url.searchParams.get("project_id");
      const items = requestedProject === null || requestedProject === String(project.id)
        ? [messageSummary]
        : [];
      const response: InboxPage = { items, next_cursor: null, total: items.length };
      return json(route, response);
    }

    if (
      path ===
        `/mail/api/v1/projects/${project.id}/messages/${messageDetail.id}` &&
      method === "GET"
    ) {
      return json(route, messageDetail);
    }

    const replyMatch = path.match(
      /^\/mail\/api\/v1\/projects\/(\d+)\/messages\/(\d+)\/replies$/,
    );
    if (replyMatch !== null && method === "POST") {
      state.typedWrites.push({ method, path, body: request.postDataJSON() });
      return json(route, options.replyResult ?? publishedDelivery);
    }

    const composeMatch = path.match(
      /^\/mail\/api\/v1\/projects\/(\d+)\/messages$/,
    );
    if (composeMatch !== null && method === "POST") {
      state.typedWrites.push({ method, path, body: request.postDataJSON() });
      return json(route, options.composeResult ?? publishedDelivery);
    }

    const retryMatch = path.match(
      /^\/mail\/api\/v1\/deliveries\/([0-9a-f-]+)\/retry$/,
    );
    if (retryMatch !== null && method === "POST") {
      state.typedWrites.push({ method, path, body: request.postDataJSON() });
      return json(route, options.retryResult ?? publishedDelivery);
    }

    return json(route, { detail: `Unexpected local stub request: ${method} ${path}` }, 404);
  });

  return state;
}

async function expectMobileLayout(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }));
  expect(overflow.content).toBeLessThanOrEqual(overflow.viewport);

  const undersizedTargets = await page
    .locator("a[href], button, input, select, textarea")
    .evaluateAll((elements) =>
      elements.flatMap((element) => {
        const target = element as HTMLElement;
        const rect = target.getBoundingClientRect();
        const style = getComputedStyle(target);
        const disabled =
          target instanceof HTMLButtonElement ||
          target instanceof HTMLInputElement ||
          target instanceof HTMLSelectElement ||
          target instanceof HTMLTextAreaElement
            ? target.disabled
            : false;
        const rendered =
          style.display !== "none" &&
          style.visibility !== "hidden" &&
          rect.width > 0 &&
          rect.height > 0;
        return rendered && !disabled && (rect.width < 44 || rect.height < 44)
          ? [{
              label: target.getAttribute("aria-label") ?? target.textContent?.trim() ?? target.tagName,
              width: Math.round(rect.width * 10) / 10,
              height: Math.round(rect.height * 10) / 10,
            }]
          : [];
      }),
    );
  expect(undersizedTargets).toEqual([]);
}

test("administrator composes through the typed delivery API", async ({ page }) => {
  const state = await installLocalStub(page);
  await page.goto("#compose");

  await expect(page.getByRole("heading", { name: "Compose", level: 1 })).toBeVisible();
  await page.getByLabel("Project").selectOption(String(project.id));
  await page.getByLabel("Recipients").fill("GreenDog, BlueLake");
  await page.getByLabel("Subject").fill("Review the release");
  await page.getByLabel("Thread ID (optional)").fill("release-2026");
  await page.getByLabel("Message in Markdown").fill("**Proceed** after UAT.");
  await expectMobileLayout(page);

  await page.getByRole("button", { name: "Send message" }).click();

  await expect(page.getByText("Published exactly once.")).toBeVisible();
  await expect(page.getByText(`Delivery reference: ${deliveryId}`)).toBeVisible();
  expect(state.typedWrites).toEqual([
    {
      method: "POST",
      path: `/mail/api/v1/projects/${project.id}/messages`,
      body: {
        idempotency_key: expect.stringMatching(/^human-ui:/u),
        recipients: ["GreenDog", "BlueLake"],
        subject: "Review the release",
        body_md: "**Proceed** after UAT.",
        thread_id: "release-2026",
      },
    },
  ]);
  await expect(page.getByLabel("Recipients")).toHaveValue("");
  await expect(page.getByLabel("Subject")).toHaveValue("");
  await expect(page.getByLabel("Message in Markdown")).toHaveValue("");
  await expectMobileLayout(page);
  expect(state.externalRequests).toEqual([]);
  expect(state.browserErrors).toEqual([]);
});

for (const actorRole of ["admin", "operator"] as const) {
  test(`${actorRole} replies through typed delivery writes`, async ({ page }) => {
    const requiresRetry = actorRole === "admin";
    const state = await installLocalStub(page, message, {
      actorRole,
      replyResult: requiresRetry ? pendingDelivery : publishedDelivery,
      retryResult: publishedDelivery,
    });
    await page.goto(`#message/${project.id}/${message.id}`);

    await expect(
      page.getByRole("heading", { name: message.subject, level: 1 }),
    ).toBeVisible();
    await page.getByLabel("Reply in Markdown").fill(`Approved by ${actorRole}.`);
    await expectMobileLayout(page);
    await page.getByRole("button", { name: "Send reply" }).click();

    if (requiresRetry) {
      await expect(page.getByText("Accepted and waiting to publish.")).toBeVisible();
      await expectMobileLayout(page);
      await page.getByRole("button", { name: "Check status" }).click();
    }

    await expect(page.getByText("Published exactly once.")).toBeVisible();
    await expect(page.getByText(`Delivery reference: ${deliveryId}`)).toBeVisible();
    expect(state.typedWrites[0]).toEqual({
      method: "POST",
      path: `/mail/api/v1/projects/${project.id}/messages/${message.id}/replies`,
      body: {
        idempotency_key: expect.stringMatching(/^human-ui:/u),
        body_md: `Approved by ${actorRole}.`,
      },
    });
    if (requiresRetry) {
      expect(state.typedWrites[1]).toEqual({
        method: "POST",
        path: `/mail/api/v1/deliveries/${deliveryId}/retry`,
        body: {},
      });
    }
    expect(state.typedWrites).toHaveLength(requiresRetry ? 2 : 1);
    await expectMobileLayout(page);
    expect(state.externalRequests).toEqual([]);
    expect(state.browserErrors).toEqual([]);
  });
}

test("account, admin, inbox, and detail remain mobile-safe and keyboard-operable", async ({
  page,
}) => {
  const state = await installLocalStub(page);
  await page.goto("#inbox");

  await expect(page.getByRole("heading", { name: "Inbox", level: 1 })).toBeVisible();
  await expect(page.getByText(message.subject, { exact: true })).toBeVisible();
  await expectMobileLayout(page);

  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  const focusRing = await page.getByRole("link", { name: "Skip to content" }).evaluate((element) =>
    getComputedStyle(element).boxShadow,
  );
  expect(focusRing).not.toBe("none");

  await page.getByRole("link", { name: "Account" }).click();
  await expect(page.getByRole("heading", { name: "Account", level: 1 })).toBeVisible();
  await expectMobileLayout(page);

  await page.getByLabel("Display name").fill("Mateusz Klatt");
  await page.getByRole("button", { name: "Save display name" }).click();
  await expect(page.getByText("Display name saved.")).toBeVisible();
  expect(state.profile.display_name).toBe("Mateusz Klatt");

  await page.getByLabel("Correspondence language").selectOption("pl");
  await expect(page.getByText("Correspondence language saved.")).toBeVisible();
  expect(state.preferences.effective.correspondence_locale).toBe("pl");

  await page.getByLabel("Current password").fill("current-secret");
  await page.getByLabel("New password", { exact: true }).fill("new-secret-value-123");
  await page.getByLabel("Confirm new password").fill("new-secret-value-123");
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByText("Password changed.")).toBeVisible();
  expect(state.passwordChanges).toBe(1);

  await page.getByRole("link", { name: "Administration" }).click();
  await expect(page.getByRole("heading", { name: "Administration", level: 1 })).toBeVisible();
  await page.getByRole("button", { name: /Operator One/ }).click();
  await page.getByLabel(`Access to ${project.human_key}`).selectOption("operator");
  await expect(page.getByText(`Access to ${project.human_key} saved.`)).toBeVisible();
  expect(state.admin.users[1]?.assignments).toEqual([
    { project_id: project.id, role: "operator" },
  ]);
  await expectMobileLayout(page);

  await page.getByRole("link", { name: "Inbox" }).click();
  const messageLink = page.getByRole("link").filter({ hasText: message.subject });
  await messageLink.focus();
  await expect(messageLink).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: message.subject, level: 1 })).toBeVisible();
  await expect(page.getByText("<script>", { exact: false })).toBeVisible();
  await expectMobileLayout(page);

  expect(state.externalRequests).toEqual([]);
  expect(state.browserErrors).toEqual([]);
});

test("renders GFM while Markdown resources and raw HTML stay inert", async ({ page }) => {
  const canonicalPng =
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
  const oversizedPng =
    `data:image/png;base64,${"A".repeat(Math.ceil((2 * 1024 * 1024) / 3) * 4 + 1)}`;
  const hostileDetail: MessageDetail = {
    ...message,
    body_md: [
      "# Markdown delivery",
      "## Nested evidence",
      "### Validation details",
      "#### Parser outcome",
      "##### Boundary note",
      "###### Terminal note",
      "**Strong result** and ~~retired path~~.",
      "- [x] Reviewed\n- [ ] Pending",
      "First line\nSecond line",
      "> Audited quote",
      "| Identifier | A deliberately long result column that must scroll inside the labelled table |\n| --- | --- |\n| HERMES-101 | Passed without widening the mobile document viewport |",
      "```ts\nconst immutableDelivery = true; const deliberatelyLongCodeLine = 'scroll inside the focusable code block';\n```",
      "[safe HTTPS](https://example.test/report) [safe mail](mailto:ops@example.test) [safe Polish](https://example.test/Wrocław) [safe relative](../messages/101)",
      "[blocked fragment](#markdown-delivery) [blocked routed fragment](/mail/#account) [blocked JavaScript](javascript:alert%281%29) [blocked data](data:text/html,unsafe) [blocked blob](blob:https://example.test/id) [blocked control](https://example.test/%0Aprobe)",
      `![inline proof](${canonicalPng})`,
      "![remote tracker](https://tracker.invalid/pixel.png?markdown-probe=remote)",
      "![protocol tracker](//tracker.invalid/pixel.png?markdown-probe=protocol-relative)",
      "![same origin tracker](/mail/logout?markdown-probe=same-origin)",
      "![relative tracker](relative.png?markdown-probe=relative)",
      "![blob tracker](blob:http://127.0.0.1/markdown-probe-blob)",
      "![SVG tracker](data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=)",
      `![oversized tracker](${oversizedPng})`,
      "![MIME mismatch](data:image/png;base64,R0lGODlhcmVzdA==)",
      '<script>window.__markdownXss = "script"</script>',
      '<svg onload="window.__markdownXss = \'svg\'"><image href="/mail/events?markdown-probe=svg"></image></svg>',
      '<img src="/mail/logout?markdown-probe=raw-image" alt="raw image" onerror="window.__markdownXss = \'image\'">',
      '<div onclick="window.__markdownXss = \'event\'" style="background-image:url(/mail/logout?markdown-probe=style)">raw event element</div>',
    ].join("\n\n"),
  };
  const networkImageRequests: string[] = [];
  const probeRequests: string[] = [];
  page.on("request", (request) => {
    const url = request.url();
    if (
      request.resourceType() === "image" &&
      (url.startsWith("http:") || url.startsWith("https:") || url.startsWith("blob:"))
    ) {
      networkImageRequests.push(url);
    }
    if (url.includes("markdown-probe")) {
      probeRequests.push(url);
    }
  });
  const state = await installLocalStub(page, hostileDetail);

  await page.goto(`#message/${project.id}/${message.id}`);

  await expect(
    page.getByRole("heading", { name: message.subject, level: 1 }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Markdown delivery", level: 2 }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Nested evidence", level: 3 }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Validation details", level: 4 }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Parser outcome", level: 5 }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Boundary note", level: 6 }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Terminal note", level: 6 }),
  ).toBeVisible();
  await expect(page.getByText("Strong result", { exact: true })).toBeVisible();
  await expect(page.locator("del", { hasText: "retired path" })).toBeVisible();
  await expect(
    page.getByRole("checkbox", { name: "Completed task" }),
  ).toBeChecked();
  await expect(
    page.getByRole("checkbox", { name: "Completed task" }),
  ).toBeDisabled();
  await expect(
    page.getByRole("checkbox", { name: "Incomplete task" }),
  ).not.toBeChecked();
  await expect(
    page.getByRole("checkbox", { name: "Incomplete task" }),
  ).toBeDisabled();
  await expect(page.locator(".message-body p br")).toHaveCount(1);
  await expect(page.locator("blockquote", { hasText: "Audited quote" })).toBeVisible();

  const tableRegion = page.getByRole("region", { name: "Markdown table" });
  await expect(tableRegion).toHaveAttribute("tabindex", "0");
  await tableRegion.focus();
  await expect(tableRegion).toBeFocused();
  const tableDimensions = await tableRegion.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
    parentClientWidth: element.parentElement?.clientWidth ?? 0,
  }));
  expect(tableDimensions.scrollWidth).toBeGreaterThan(tableDimensions.clientWidth);

  const codeBlock = page.getByLabel("Code block");
  await expect(codeBlock).toHaveAttribute("tabindex", "0");
  await codeBlock.focus();
  await expect(codeBlock).toBeFocused();
  const codeDimensions = await codeBlock.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }));
  expect(codeDimensions.scrollWidth).toBeGreaterThan(codeDimensions.clientWidth);

  await expect(page.getByRole("link", { name: "safe HTTPS" })).toHaveAttribute(
    "href",
    "https://example.test/report",
  );
  await expect(page.getByRole("link", { name: "safe mail" })).toHaveAttribute(
    "href",
    "mailto:ops@example.test",
  );
  await expect(page.getByRole("link", { name: "safe Polish" })).toHaveAttribute(
    "href",
    "https://example.test/Wroc%C5%82aw",
  );
  await expect(page.getByRole("link", { name: "safe relative" })).toHaveAttribute(
    "href",
    "../messages/101",
  );
  for (const name of [
    "blocked fragment",
    "blocked routed fragment",
    "blocked JavaScript",
    "blocked data",
    "blocked blob",
    "blocked control",
  ]) {
    await expect(page.getByText(name, { exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name })).toHaveCount(0);
  }
  const messageRouteHash = await page.evaluate(() => window.location.hash);
  await page.getByText("blocked fragment", { exact: true }).click();
  expect(await page.evaluate(() => window.location.hash)).toBe(messageRouteHash);
  await page.getByText("blocked routed fragment", { exact: true }).click();
  expect(await page.evaluate(() => window.location.hash)).toBe(messageRouteHash);

  await expect(page.getByRole("img", { name: "inline proof" })).toHaveAttribute(
    "src",
    canonicalPng,
  );
  await expect(page.locator(".message-body img")).toHaveCount(1);
  await expect(page.locator(".markdown-image-alt")).toHaveText([
    "remote tracker",
    "protocol tracker",
    "same origin tracker",
    "relative tracker",
    "blob tracker",
    "SVG tracker",
    "oversized tracker",
    "MIME mismatch",
  ]);
  await expect(
    page.locator(
      ".message-body script, .message-body svg, .message-body [onload], .message-body [onerror], .message-body [onclick], .message-body [style]",
    ),
  ).toHaveCount(0);
  await expect(page.getByText("raw image", { exact: true })).toHaveCount(0);
  await expect(page.getByText("raw event element", { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => "__markdownXss" in window)).toBe(false);

  await expectMobileLayout(page);
  expect(networkImageRequests).toEqual([]);
  expect(probeRequests).toEqual([]);
  expect(state.externalRequests).toEqual([]);
  expect(state.browserErrors).toEqual([]);
});

test("loads shell resources only from the canonical asset namespace", async ({ page }) => {
  const assetRequests: string[] = [];
  page.on("request", (request) => {
    if (["script", "stylesheet"].includes(request.resourceType())) {
      assetRequests.push(new URL(request.url()).pathname);
    }
  });
  const state = await installLocalStub(page);

  await page.goto("#inbox");
  await expect(page.getByRole("heading", { name: "Inbox", level: 1 })).toBeVisible();

  expect(assetRequests.length).toBeGreaterThanOrEqual(2);
  expect(assetRequests.every((path) => path.startsWith("/mail/assets/"))).toBe(true);
  expect(assetRequests.some((path) => path.startsWith("/mail/v2/"))).toBe(false);
  expect(state.externalRequests).toEqual([]);
  expect(state.browserErrors).toEqual([]);
});
