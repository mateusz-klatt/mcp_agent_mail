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

interface StubState {
  preferences: MailUiPreferences;
  profile: MailUiProfile;
  admin: AdminAccessSnapshot;
  passwordChanges: number;
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

async function installLocalStub(page: Page): Promise<StubState> {
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
      global_role: "admin",
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
      return json(route, { items: [project], total: 1 });
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
      path === `/mail/api/v1/projects/${project.id}/messages/${message.id}` &&
      method === "GET"
    ) {
      return json(route, message);
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
        const rendered =
          style.display !== "none" &&
          style.visibility !== "hidden" &&
          rect.width > 0 &&
          rect.height > 0;
        return rendered && (rect.width < 44 || rect.height < 44)
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

test("legacy Markdown sanitizer cannot create active resource requests", async ({ page }) => {
  await installLocalStub(page);
  const resourceRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("markdown-probe")) {
      resourceRequests.push(request.url());
    }
  });
  await page.goto("#inbox");
  await page.addScriptTag({ url: "/mail/v2/assets/legacy.js" });
  await page.waitForFunction(() => "DOMPurify" in window);

  const result = await page.evaluate(() => {
    const purifier = (
      window as unknown as Window & { DOMPurify: { sanitize: (input: string) => string } }
    ).DOMPurify;
    const hostile = [
      '<img src="/mail/events?markdown-probe=img">',
      '<svg><image href="/mail/events?markdown-probe=svg-image"></image></svg>',
      '<svg><feImage href="/mail/events?markdown-probe=svg-filter"></feImage></svg>',
      '<picture><source srcset="/mail/logout?markdown-probe=source"></picture>',
      '<video poster="/mail/logout?markdown-probe=poster"></video>',
      '<audio src="/mail/logout?markdown-probe=audio"></audio>',
      '<input type="image" src="/mail/logout?markdown-probe=input">',
      '<div style="background-image:url(/mail/logout?markdown-probe=css)">styled</div>',
      '<img alt="inline" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=">',
    ].join("");
    const sanitized = purifier.sanitize(hostile);
    const container = document.createElement("div");
    container.id = "legacy-sanitizer-probe";
    container.innerHTML = sanitized;
    document.body.append(container);
    return {
      dangerousElements: container.querySelectorAll(
        "svg, image, feImage, picture, source, video, audio, input, [style]",
      ).length,
      imageSources: Array.from(container.querySelectorAll("img"), (image) =>
        image.getAttribute("src"),
      ),
    };
  });

  await page.waitForTimeout(100);
  expect(result.dangerousElements).toBe(0);
  expect(result.imageSources).toEqual([
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  ]);
  expect(resourceRequests).toEqual([]);
});
