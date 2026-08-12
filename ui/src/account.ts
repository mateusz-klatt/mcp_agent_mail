export const profileEndpoint = "/mail/api/v1/me/profile";
export const passwordEndpoint = "/mail/api/v1/me/password";
export const adminAccessEndpoint = "/mail/api/v1/admin/access";

export type GlobalRole = "admin" | "member";
export type AssignmentRole = "viewer" | "operator";

export interface MailUiProfile {
  id: number;
  username: string;
  display_name: string | null;
  global_role: GlobalRole;
  profile_revision: number;
}

export interface ProfileMutation {
  changed: boolean;
  display_name: string | null;
  profile_revision: number;
}

export interface AdminAssignment {
  project_id: number;
  role: AssignmentRole;
}

export interface AdminUser {
  id: number;
  username: string;
  display_name: string | null;
  disabled: boolean;
  global_role: GlobalRole;
  account_generation: string;
  access_version: number;
  assignments: AdminAssignment[];
}

export interface AdminProject {
  id: number;
  slug: string;
  human_key: string;
  project_generation: string;
  archived_at: string | null;
}

export interface AdminAccessSnapshot {
  users: AdminUser[];
  projects: AdminProject[];
}

export interface AssignmentMutation {
  changed: boolean;
  role: AssignmentRole | null;
  access_version: number;
}

interface RequestOptions {
  method?: "GET" | "PATCH" | "PUT";
  body?: unknown;
  signal?: AbortSignal;
}

export class AccountHttpError extends Error {
  constructor(readonly status: number) {
    super(`Account request failed with HTTP ${status}.`);
    this.name = "AccountHttpError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactRecord(
  value: unknown,
  label: string,
  expectedKeys: readonly string[],
): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new TypeError(`Invalid ${label}.`);
  }
  const actualKeys = Object.keys(value);
  const expected = new Set(expectedKeys);
  if (
    actualKeys.length !== expectedKeys.length ||
    actualKeys.some((key) => !expected.has(key))
  ) {
    throw new TypeError(`Invalid ${label}.`);
  }
  return value;
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
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) {
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

function globalRole(value: unknown): GlobalRole {
  if (value !== "admin" && value !== "member") {
    throw new TypeError("Invalid global role.");
  }
  return value;
}

function assignmentRole(value: unknown): AssignmentRole {
  if (value !== "viewer" && value !== "operator") {
    throw new TypeError("Invalid project assignment role.");
  }
  return value;
}

function generation(value: unknown, label: string): string {
  const candidate = stringValue(value, label);
  if (!/^[0-9a-f]{64}$/.test(candidate)) {
    throw new TypeError(`Invalid ${label}.`);
  }
  return candidate;
}

function nullableTimestamp(value: unknown): string | null {
  if (value === null) {
    return null;
  }
  const timestamp = stringValue(value, "project archive timestamp");
  if (timestamp.length === 0 || Number.isNaN(Date.parse(timestamp))) {
    throw new TypeError("Invalid project archive timestamp.");
  }
  return timestamp;
}

export function parseProfile(payload: unknown): MailUiProfile {
  const value = exactRecord(payload, "profile response", [
    "id",
    "username",
    "display_name",
    "global_role",
    "profile_revision",
  ]);
  return {
    id: positiveInteger(value.id, "profile id"),
    username: stringValue(value.username, "username"),
    display_name: nullableString(value.display_name, "display name"),
    global_role: globalRole(value.global_role),
    profile_revision: positiveInteger(value.profile_revision, "profile revision"),
  };
}

export function parseProfileMutation(payload: unknown): ProfileMutation {
  const value = exactRecord(payload, "profile mutation response", [
    "changed",
    "display_name",
    "profile_revision",
  ]);
  return {
    changed: booleanValue(value.changed, "profile changed flag"),
    display_name: nullableString(value.display_name, "display name"),
    profile_revision: positiveInteger(value.profile_revision, "profile revision"),
  };
}

function parseAdminAssignment(payload: unknown): AdminAssignment {
  const value = exactRecord(payload, "admin assignment", ["project_id", "role"]);
  return {
    project_id: positiveInteger(value.project_id, "assignment project id"),
    role: assignmentRole(value.role),
  };
}

function parseAdminUser(payload: unknown): AdminUser {
  const value = exactRecord(payload, "admin user", [
    "id",
    "username",
    "display_name",
    "disabled",
    "global_role",
    "account_generation",
    "access_version",
    "assignments",
  ]);
  if (!Array.isArray(value.assignments)) {
    throw new TypeError("Invalid admin user assignments.");
  }
  return {
    id: positiveInteger(value.id, "admin user id"),
    username: stringValue(value.username, "admin username"),
    display_name: nullableString(value.display_name, "admin display name"),
    disabled: booleanValue(value.disabled, "admin disabled flag"),
    global_role: globalRole(value.global_role),
    account_generation: generation(value.account_generation, "account generation"),
    access_version: positiveInteger(value.access_version, "access version"),
    assignments: value.assignments.map(parseAdminAssignment),
  };
}

function parseAdminProject(payload: unknown): AdminProject {
  const value = exactRecord(payload, "admin project", [
    "id",
    "slug",
    "human_key",
    "project_generation",
    "archived_at",
  ]);
  return {
    id: positiveInteger(value.id, "admin project id"),
    slug: stringValue(value.slug, "admin project slug"),
    human_key: stringValue(value.human_key, "admin project human key"),
    project_generation: generation(
      value.project_generation,
      "project generation",
    ),
    archived_at: nullableTimestamp(value.archived_at),
  };
}

export function parseAdminAccess(payload: unknown): AdminAccessSnapshot {
  const value = exactRecord(payload, "admin access response", ["users", "projects"]);
  if (!Array.isArray(value.users) || !Array.isArray(value.projects)) {
    throw new TypeError("Invalid admin access collections.");
  }
  return {
    users: value.users.map(parseAdminUser),
    projects: value.projects.map(parseAdminProject),
  };
}

export function parseAssignmentMutation(payload: unknown): AssignmentMutation {
  const value = exactRecord(payload, "assignment mutation response", [
    "changed",
    "role",
    "access_version",
  ]);
  return {
    changed: booleanValue(value.changed, "assignment changed flag"),
    role: value.role === null ? null : assignmentRole(value.role),
    access_version: positiveInteger(value.access_version, "access version"),
  };
}

export function parsePasswordMutation(payload: unknown): { changed: true } {
  const value = exactRecord(payload, "password mutation response", ["changed"]);
  if (value.changed !== true) {
    throw new TypeError("Invalid password mutation response.");
  }
  return { changed: true };
}

async function accountRequest(
  path: string,
  options: RequestOptions = {},
): Promise<unknown> {
  const hasBody = options.body !== undefined;
  const response = await fetch(new URL(path, window.location.origin), {
    method: options.method ?? "GET",
    body: hasBody ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
    cache: "no-store",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(hasBody ? { "Content-Type": "application/json" } : {}),
    },
  });
  if (!response.ok) {
    throw new AccountHttpError(response.status);
  }
  return response.json();
}

export async function loadProfile(signal?: AbortSignal): Promise<MailUiProfile> {
  return parseProfile(await accountRequest(profileEndpoint, { signal }));
}

export async function saveDisplayName(
  displayName: string | null,
  expectedProfileRevision: number,
): Promise<ProfileMutation> {
  return parseProfileMutation(
    await accountRequest(profileEndpoint, {
      method: "PATCH",
      body: {
        display_name: displayName,
        expected_profile_revision: expectedProfileRevision,
      },
    }),
  );
}

export async function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<{ changed: true }> {
  return parsePasswordMutation(
    await accountRequest(passwordEndpoint, {
      method: "PATCH",
      body: { current_password: currentPassword, new_password: newPassword },
    }),
  );
}

export async function loadAdminAccess(
  signal?: AbortSignal,
): Promise<AdminAccessSnapshot> {
  return parseAdminAccess(await accountRequest(adminAccessEndpoint, { signal }));
}

export async function saveProjectAssignment(
  user: Pick<AdminUser, "id" | "access_version" | "account_generation">,
  project: Pick<AdminProject, "id" | "project_generation">,
  role: AssignmentRole | null,
): Promise<AssignmentMutation> {
  const userId = positiveInteger(user.id, "admin user id");
  const projectId = positiveInteger(project.id, "admin project id");
  return parseAssignmentMutation(
    await accountRequest(
      `/mail/api/v1/admin/users/${userId}/projects/${projectId}`,
      {
        method: "PUT",
        body: {
          role,
          expected_access_version: positiveInteger(
            user.access_version,
            "access version",
          ),
          account_generation: generation(
            user.account_generation,
            "account generation",
          ),
          expected_project_generation: generation(
            project.project_generation,
            "project generation",
          ),
        },
      },
    ),
  );
}
