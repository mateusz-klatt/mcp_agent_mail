import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

import type { SupportedLocale } from "../i18n";

export const projectOne = {
  id: 11,
  slug: "mcp-agent-mail",
  human_key: "/mateusz-klatt/mcp_agent_mail",
  created_at: "2026-08-10T08:00:00Z",
  archived_at: null,
  role: "admin" as const,
  can_reply: true,
};

export const projectTwo = {
  id: 22,
  slug: "archive-lab",
  human_key: "/mateusz-klatt/archive_lab",
  created_at: "2026-07-01T10:30:00Z",
  archived_at: "2026-08-01T12:00:00Z",
  role: "viewer" as const,
  can_reply: false,
};

export const projectsResponse = {
  items: [projectOne, projectTwo],
  total: 2,
};

export const projectAgentsResponse = {
  project_id: projectOne.id,
  project_generation: "d".repeat(64),
  items: [
    {
      agent_id: 1,
      agent_generation: "1".repeat(64),
      name: "BlueLake",
      display_name: null,
      // Deliberately mixed: an agent with no chosen tone must fall through to
      // the default rather than silence the notification.
      notify_sound: null,
    },
    {
      agent_id: 2,
      agent_generation: "2".repeat(64),
      name: "GreenDog",
      display_name: "Release operator",
      notify_sound: "chime",
    },
    {
      agent_id: 3,
      agent_generation: "3".repeat(64),
      name: "IndigoBridge",
      display_name: "Schema keeper",
      notify_sound: "click",
    },
  ],
  total: 3,
};

export const messageOne = {
  id: 101,
  project_id: projectOne.id,
  project_slug: projectOne.slug,
  subject: "Production rollout verified",
  sender: "claude-linux-holzera-1",
  sender_name: "claude-linux-holzera-1",
  sender_display_name: "Gospodarz",
  // Two senders with different tones, so a test can tell the
  // per-sender lookup apart from the default fallback.
  sender_notify_sound: "chime",
  importance: "high" as const,
  ack_required: true,
  thread_id: "release-101",
  reply_to: null,
  created_ts: "2026-08-11T10:15:00Z",
  can_reply: true,
};

export const messageTwo = {
  id: 102,
  project_id: projectTwo.id,
  project_slug: projectTwo.slug,
  subject: "Archive maintenance window",
  sender: "archive-agent",
  sender_name: "archive-agent",
  sender_display_name: null,
  sender_notify_sound: null,
  importance: "normal" as const,
  ack_required: false,
  thread_id: null,
  reply_to: 101,
  created_ts: "2026-08-11T09:00:00Z",
  can_reply: false,
};

export const inboxResponse = {
  items: [messageOne, messageTwo],
  total: 2,
  next_cursor: null,
};

export const searchResponse = {
  items: [
    {
      ...messageOne,
      snippet: "All rollout checks passed with the release marker.",
    },
  ],
  next_cursor: null,
};

export const deliveryResponse = {
  id: "12345678-1234-4234-8234-123456789abc",
  status: "published" as const,
  reused: false,
  message_id: 103,
  commit_sha: "f".repeat(40),
  next_attempt_ts: null,
};

export const messageDetail = {
  ...messageOne,
  body_md: "# Release\n\nAll checks passed. `<script>` remains plain text.",
  to: ["codex-wsl-home-1"],
  cc: [],
  reply_target: {
    agent_id: 41,
    agent_generation: "4".repeat(64),
    project_id: projectOne.id,
    project_generation: projectAgentsResponse.project_generation,
    canonical_name: messageOne.sender,
  },
  attachments: [
    { type: "artifact", media_type: "application/json", size_bytes: 1280 },
    { type: null, media_type: null, size_bytes: null },
  ],
};

export const threadReply = {
  ...messageDetail,
  id: 103,
  subject: "Re: Production rollout verified",
  body_md: "## Follow-up\n\nDeployment remains healthy.",
  reply_to: messageOne.id,
  created_ts: "2026-08-11T11:15:00Z",
  attachments: [],
};

export const threadResponse = {
  items: [threadReply, messageDetail],
  total: 2,
  next_cursor: null,
  subject: messageOne.subject,
};

export const preferencesResponse = (
  uiLocale: SupportedLocale,
  correspondenceLocale: SupportedLocale | null = null,
) => ({
  stored: {
    preferred_ui_locale: uiLocale,
    preferred_correspondence_locale: correspondenceLocale,
  },
  effective: {
    ui_locale: uiLocale,
    correspondence_locale: correspondenceLocale ?? uiLocale,
  },
});

export const adminProfile = {
  id: 1,
  username: "mateusz",
  display_name: "Mateusz",
  global_role: "admin" as const,
  profile_revision: 3,
};

export const memberProfile = {
  ...adminProfile,
  id: 2,
  username: "operator",
  display_name: "Operator One",
  global_role: "member" as const,
};

export const adminUser = {
  id: adminProfile.id,
  username: adminProfile.username,
  display_name: adminProfile.display_name,
  disabled: false,
  global_role: "admin" as const,
  account_generation: "a".repeat(64),
  access_version: 4,
  assignments: [],
};

export const memberUser = {
  id: memberProfile.id,
  username: memberProfile.username,
  display_name: memberProfile.display_name,
  disabled: false,
  global_role: "member" as const,
  account_generation: "b".repeat(64),
  access_version: 7,
  assignments: [{ project_id: projectOne.id, role: "viewer" as const }],
};

export const disabledUser = {
  ...memberUser,
  id: 3,
  username: "disabled",
  display_name: null,
  disabled: true,
  account_generation: "c".repeat(64),
  access_version: 2,
  assignments: [],
};

export const adminProjects = [
  {
    id: projectOne.id,
    slug: projectOne.slug,
    human_key: projectOne.human_key,
    project_generation: "d".repeat(64),
    archived_at: null,
  },
  {
    id: projectTwo.id,
    slug: projectTwo.slug,
    human_key: projectTwo.human_key,
    project_generation: "e".repeat(64),
    archived_at: projectTwo.archived_at,
  },
];

export const adminAccessResponse = {
  users: [adminUser, memberUser, disabledUser],
  projects: adminProjects,
};

export const reservationsResponse = {
  items: [
    {
      id: 501,
      project_id: projectOne.id,
      project_slug: projectOne.slug,
      holder_name: "claude-wsl-home-1",
      holder_display_name: "Kanarek",
      path_pattern: "src/mcp_agent_mail/http.py",
      exclusive: true,
      reason: "editing the reservations endpoint",
      created_ts: "2026-08-14 18:00:00.000000",
      expires_ts: "2026-08-14 19:00:00.000000",
      origin: "auto",
      scope_state: "execution_scoped" as const,
    },
    {
      id: 502,
      project_id: projectOne.id,
      project_slug: projectOne.slug,
      holder_name: "codex-wsl-home-1",
      holder_display_name: null,
      path_pattern: "docs/**",
      exclusive: false,
      reason: "",
      created_ts: "2026-08-14 17:00:00.000000",
      expires_ts: null,
      origin: "explicit",
      scope_state: "legacy_unscoped" as const,
    },
    {
      id: 503,
      project_id: projectOne.id,
      project_slug: projectOne.slug,
      // The agent row is gone; the claim outlives it on purpose.
      holder_name: null,
      holder_display_name: null,
      path_pattern: "scripts/hooks/**",
      exclusive: true,
      reason: "",
      created_ts: "2026-08-14 16:00:00.000000",
      expires_ts: "2026-08-14 20:00:00.000000",
      origin: "auto",
      scope_state: "orphaned" as const,
    },
  ],
  next_cursor: null,
};

export const server = setupServer(
  http.get("*/mail/api/v1/reservations", () =>
    HttpResponse.json(reservationsResponse),
  ),
  http.get("http://localhost/mail/api/v1/health", () =>
    HttpResponse.json({ status: "ok" }),
  ),
  http.get("*/mail/api/v1/me/preferences", () =>
    HttpResponse.json(preferencesResponse("en")),
  ),
  http.patch("*/mail/api/v1/me/preferences", async ({ request }) => {
    const body = (await request.json()) as {
      preferred_ui_locale?: SupportedLocale;
      preferred_correspondence_locale?: SupportedLocale | null;
    };
    return HttpResponse.json(
      preferencesResponse(
        body.preferred_ui_locale ?? "en",
        body.preferred_correspondence_locale ?? null,
      ),
    );
  }),
  http.get("*/mail/api/v1/me/profile", () => HttpResponse.json(adminProfile)),
  http.patch("*/mail/api/v1/me/profile", async ({ request }) => {
    const body = (await request.json()) as {
      display_name: string | null;
      expected_profile_revision: number;
    };
    return HttpResponse.json({
      changed: true,
      display_name: body.display_name,
      profile_revision: body.expected_profile_revision + 1,
    });
  }),
  http.patch("*/mail/api/v1/me/password", () =>
    HttpResponse.json({ changed: true }),
  ),
  http.get("*/mail/api/v1/admin/access", () =>
    HttpResponse.json(adminAccessResponse),
  ),
  http.put(
    "*/mail/api/v1/admin/users/:userId/projects/:projectId",
    async ({ request }) => {
      const body = (await request.json()) as {
        role: "viewer" | "operator" | null;
        expected_access_version: number;
      };
      return HttpResponse.json({
        changed: true,
        role: body.role,
        access_version: body.expected_access_version + 1,
      });
    },
  ),
  http.get("*/mail/api/v1/projects", () => HttpResponse.json(projectsResponse)),
  http.get("*/mail/api/v1/projects/:projectId/agents", ({ params }) =>
    HttpResponse.json({
      ...projectAgentsResponse,
      project_id: Number(params.projectId),
    }),
  ),
  http.get("*/mail/api/v1/inbox", () => HttpResponse.json(inboxResponse)),
  http.get("*/mail/api/v1/search", () => HttpResponse.json(searchResponse)),
  http.get(
    "*/mail/api/v1/projects/:projectId/messages/:messageId",
    ({ params }) =>
      params.projectId === String(projectOne.id) &&
      params.messageId === String(messageOne.id)
        ? HttpResponse.json(messageDetail)
        : HttpResponse.json({ detail: "not found" }, { status: 404 }),
  ),
  http.get(
    "*/mail/api/v1/projects/:projectId/threads",
    ({ params, request }) =>
      params.projectId === String(projectOne.id) &&
      new URL(request.url).searchParams.get("thread_id") === messageOne.thread_id
        ? HttpResponse.json(threadResponse)
        : HttpResponse.json({ detail: "not found" }, { status: 404 }),
  ),
  http.post("*/mail/api/v1/projects/:projectId/messages", () =>
    HttpResponse.json(deliveryResponse),
  ),
  http.post(
    "*/mail/api/v1/projects/:projectId/messages/:messageId/replies",
    () => HttpResponse.json(deliveryResponse),
  ),
  http.get("*/mail/api/v1/deliveries/:deliveryId", ({ params }) =>
    HttpResponse.json({
      ...deliveryResponse,
      id: String(params.deliveryId),
      reused: true,
    }),
  ),
  http.post("*/mail/api/v1/deliveries/:deliveryId/retry", ({ params }) =>
    HttpResponse.json({
      ...deliveryResponse,
      id: String(params.deliveryId),
      reused: true,
    }),
  ),
);
