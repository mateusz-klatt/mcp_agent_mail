export const projectsEndpoint = "/mail/api/v1/projects";
export const inboxEndpoint = "/mail/api/v1/inbox";
export const mailEventsEndpoint = "/mail/events";

export const inboxPageSize = 50;

const maximumInlineImageBytes = 2 * 1024 * 1024;
const maximumInlineImageBase64Length = Math.ceil(maximumInlineImageBytes / 3) * 4;

export function isCanonicalInlineRasterImageSource(source: string): boolean {
  const formats = [
    ["data:image/png;base64,", (raw: string) => raw.startsWith("\x89PNG\r\n\x1a\n")],
    ["data:image/jpeg;base64,", (raw: string) => raw.startsWith("\xff\xd8\xff")],
    [
      "data:image/gif;base64,",
      (raw: string) => raw.startsWith("GIF87a") || raw.startsWith("GIF89a"),
    ],
    [
      "data:image/webp;base64,",
      (raw: string) =>
        raw.length >= 12 && raw.startsWith("RIFF") && raw.slice(8, 12) === "WEBP",
    ],
  ] as const;

  if (source !== source.trim()) {
    return false;
  }
  for (const [prefix, signatureMatches] of formats) {
    if (!source.startsWith(prefix)) {
      continue;
    }
    const payload = source.slice(prefix.length);
    if (payload.length === 0 || payload.length > maximumInlineImageBase64Length) {
      return false;
    }
    try {
      const raw = window.atob(payload);
      return (
        raw.length > 0 &&
        raw.length <= maximumInlineImageBytes &&
        window.btoa(raw) === payload &&
        signatureMatches(raw)
      );
    } catch {
      return false;
    }
  }
  return false;
}

export type AccessRole = "admin" | "operator" | "viewer";
export type Importance = "low" | "normal" | "high" | "urgent";

export interface MailProject {
  id: number;
  slug: string;
  human_key: string;
  created_at: string;
  archived_at: string | null;
  role: AccessRole;
  can_reply: boolean;
}

export interface ProjectsPage {
  items: MailProject[];
  total: number;
}

export interface InboxMessage {
  id: number;
  project_id: number;
  project_slug: string;
  subject: string;
  sender: string;
  sender_name: string;
  sender_display_name: string | null;
  importance: Importance;
  thread_id: string | null;
  reply_to: number | null;
  created_ts: string;
  ack_required: boolean;
  can_reply: boolean;
}

export interface InboxPage {
  items: InboxMessage[];
  next_cursor: string | null;
  total: number;
}

export interface MessageAttachment {
  type: string | null;
  media_type: string | null;
  size_bytes: number | null;
}

export interface MessageDetail extends InboxMessage {
  body_md: string;
  to: string[];
  cc: string[];
  attachments: MessageAttachment[];
}

export type MailRoute =
  | { view: "projects" }
  | { view: "inbox"; projectId: number | null }
  | { view: "message"; projectId: number; messageId: number };

interface FetchOptions {
  signal?: AbortSignal;
}

interface InboxOptions extends FetchOptions {
  cursor?: string;
  projectId?: number;
}

export class MailHttpError extends Error {
  constructor(readonly status: number) {
    super(`Mail request failed with HTTP ${status}.`);
    this.name = "MailHttpError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new TypeError(`Invalid ${label}.`);
  }
  return value;
}

function exactRecord(
  value: unknown,
  label: string,
  expectedKeys: readonly string[],
): Record<string, unknown> {
  const candidate = record(value, label);
  const expected = new Set(expectedKeys);
  if (Object.keys(candidate).some((key) => !expected.has(key))) {
    throw new TypeError(`Invalid ${label}.`);
  }
  return candidate;
}

function stringValue(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw new TypeError(`Invalid ${label}.`);
  }
  return value;
}

function nullableString(value: unknown, label: string): string | null {
  if (value !== null && typeof value !== "string") {
    throw new TypeError(`Invalid ${label}.`);
  }
  return value;
}

function positiveInteger(value: unknown, label: string): number {
  if (!Number.isInteger(value) || typeof value !== "number" || value <= 0) {
    throw new TypeError(`Invalid ${label}.`);
  }
  return value;
}

function nonNegativeInteger(value: unknown, label: string): number {
  if (!Number.isInteger(value) || typeof value !== "number" || value < 0) {
    throw new TypeError(`Invalid ${label}.`);
  }
  return value;
}

function booleanValue(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new TypeError(`Invalid ${label}.`);
  }
  return value;
}

function validTimestamp(value: unknown, label: string): string {
  const timestamp = stringValue(value, label);
  if (timestamp.length === 0 || Number.isNaN(Date.parse(timestamp))) {
    throw new TypeError(`Invalid ${label}.`);
  }
  return timestamp;
}

function nullableTimestamp(value: unknown, label: string): string | null {
  if (value === null) {
    return null;
  }
  return validTimestamp(value, label);
}

function accessRole(value: unknown): AccessRole {
  if (value !== "admin" && value !== "operator" && value !== "viewer") {
    throw new TypeError("Invalid project access role.");
  }
  return value;
}

function importance(value: unknown): Importance {
  if (value !== "low" && value !== "normal" && value !== "high" && value !== "urgent") {
    throw new TypeError("Invalid message importance.");
  }
  return value;
}

function stringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) {
    throw new TypeError(`Invalid ${label}.`);
  }
  return value;
}

function nullablePositiveInteger(value: unknown, label: string): number | null {
  if (value === null) {
    return null;
  }
  return positiveInteger(value, label);
}

const inboxMessageKeys = [
  "id",
  "project_id",
  "project_slug",
  "subject",
  "sender",
  "sender_name",
  "sender_display_name",
  "importance",
  "thread_id",
  "reply_to",
  "created_ts",
  "ack_required",
  "can_reply",
] as const;

function parseInboxMessage(
  value: unknown,
  additionalKeys: readonly string[] = [],
): InboxMessage {
  const candidate = exactRecord(
    value,
    "inbox message",
    [...inboxMessageKeys, ...additionalKeys],
  );
  return {
    id: positiveInteger(candidate.id, "message id"),
    project_id: positiveInteger(candidate.project_id, "message project id"),
    project_slug: stringValue(candidate.project_slug, "message project slug"),
    subject: stringValue(candidate.subject, "message subject"),
    sender: stringValue(candidate.sender, "message sender"),
    sender_name: stringValue(candidate.sender_name, "sender name"),
    sender_display_name: nullableString(
      candidate.sender_display_name,
      "sender display name",
    ),
    importance: importance(candidate.importance),
    thread_id: nullableString(candidate.thread_id, "message thread id"),
    reply_to: nullablePositiveInteger(candidate.reply_to, "reply-to id"),
    created_ts: validTimestamp(candidate.created_ts, "message timestamp"),
    ack_required: booleanValue(candidate.ack_required, "acknowledgement flag"),
    can_reply: booleanValue(candidate.can_reply, "reply permission"),
  };
}

function parseAttachment(value: unknown): MessageAttachment {
  const candidate = exactRecord(value, "message attachment", [
    "type",
    "media_type",
    "size_bytes",
  ]);
  const rawSize = candidate.size_bytes;
  return {
    type: nullableString(candidate.type, "attachment type"),
    media_type: nullableString(candidate.media_type, "attachment media type"),
    size_bytes:
      rawSize === null ? null : nonNegativeInteger(rawSize, "attachment size"),
  };
}

export function parseProjects(payload: unknown): ProjectsPage {
  const response = exactRecord(payload, "projects response", ["items", "total"]);
  if (!Array.isArray(response.items)) {
    throw new TypeError("Invalid project items.");
  }
  const items = response.items.map((value) => {
    const candidate = exactRecord(value, "project", [
      "id",
      "slug",
      "human_key",
      "created_at",
      "archived_at",
      "role",
      "can_reply",
    ]);
    return {
      id: positiveInteger(candidate.id, "project id"),
      slug: stringValue(candidate.slug, "project slug"),
      human_key: stringValue(candidate.human_key, "project key"),
      created_at: validTimestamp(candidate.created_at, "project creation timestamp"),
      archived_at: nullableTimestamp(candidate.archived_at, "project archive timestamp"),
      role: accessRole(candidate.role),
      can_reply: booleanValue(candidate.can_reply, "project reply permission"),
    };
  });
  return {
    items,
    total: nonNegativeInteger(response.total, "project total"),
  };
}

export function parseInboxPage(payload: unknown): InboxPage {
  const candidate = exactRecord(payload, "inbox response", [
    "items",
    "next_cursor",
    "total",
  ]);
  if (!Array.isArray(candidate.items)) {
    throw new TypeError("Invalid inbox items.");
  }
  return {
    items: candidate.items.map((value) => parseInboxMessage(value)),
    next_cursor: nullableString(candidate.next_cursor, "inbox cursor"),
    total: nonNegativeInteger(candidate.total, "inbox total"),
  };
}

export function parseMessageDetail(payload: unknown): MessageDetail {
  const detailKeys = ["body_md", "to", "cc", "attachments"] as const;
  const summary = parseInboxMessage(payload, detailKeys);
  const candidate = exactRecord(payload, "message detail", [
    ...inboxMessageKeys,
    ...detailKeys,
  ]);
  if (!Array.isArray(candidate.attachments)) {
    throw new TypeError("Invalid message attachments.");
  }
  return {
    ...summary,
    body_md: stringValue(candidate.body_md, "message body"),
    to: stringArray(candidate.to, "message to recipients"),
    cc: stringArray(candidate.cc, "message cc recipients"),
    attachments: candidate.attachments.map(parseAttachment),
  };
}

async function mailRequest<T>(
  endpoint: string,
  parser: (payload: unknown) => T,
  options: FetchOptions = {},
): Promise<T> {
  const response = await fetch(new URL(endpoint, window.location.origin), {
    method: "GET",
    cache: "no-store",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal: options.signal,
  });
  if (!response.ok) {
    throw new MailHttpError(response.status);
  }
  return parser(await response.json());
}

export function loadProjects(options: FetchOptions = {}): Promise<ProjectsPage> {
  return mailRequest(projectsEndpoint, parseProjects, options);
}

export function loadInbox(options: InboxOptions = {}): Promise<InboxPage> {
  const url = new URL(inboxEndpoint, window.location.origin);
  url.searchParams.set("limit", String(inboxPageSize));
  if (options.cursor !== undefined) {
    url.searchParams.set("cursor", options.cursor);
  }
  if (options.projectId !== undefined) {
    url.searchParams.set("project_id", String(options.projectId));
  }
  return mailRequest(url.pathname + url.search, parseInboxPage, options);
}

export function loadMessage(
  projectId: number,
  messageId: number,
  options: FetchOptions = {},
): Promise<MessageDetail> {
  return mailRequest(
    `/mail/api/v1/projects/${projectId}/messages/${messageId}`,
    parseMessageDetail,
    options,
  );
}

function routeInteger(value: string | undefined): number | null {
  if (value === undefined || !/^\d+$/.test(value)) {
    return null;
  }
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

export function parseMailRoute(hash: string): MailRoute {
  const normalized = hash.startsWith("#") ? hash.slice(1) : hash;
  const [path = "", query = ""] = normalized.split("?", 2);
  if (path === "projects") {
    return { view: "projects" };
  }
  if (path === "inbox" || path === "") {
    const projectId = routeInteger(new URLSearchParams(query).get("project") ?? undefined);
    return { view: "inbox", projectId };
  }
  const parts = path.split("/");
  if (parts[0] === "message" && parts.length === 3) {
    const projectId = routeInteger(parts[1]);
    const messageId = routeInteger(parts[2]);
    if (projectId !== null && messageId !== null) {
      return { view: "message", projectId, messageId };
    }
  }
  return { view: "inbox", projectId: null };
}

export function mailRouteHash(route: MailRoute): string {
  if (route.view === "projects") {
    return "#projects";
  }
  if (route.view === "message") {
    return `#message/${route.projectId}/${route.messageId}`;
  }
  return route.projectId === null ? "#inbox" : `#inbox?project=${route.projectId}`;
}
