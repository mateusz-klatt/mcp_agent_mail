import {
  type ChangeEvent,
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import ReactMarkdown, {
  type Components as MarkdownComponents,
} from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

import i18n, { supportedLocales, type SupportedLocale } from "./i18n";
import {
  AccountHttpError,
  changePassword,
  loadAdminAccess,
  loadProfile,
  saveDisplayName,
  saveProjectAssignment,
  type AdminAccessSnapshot,
  type AdminProject,
  type AdminUser,
  type AssignmentRole,
  type MailUiProfile,
} from "./account";
import {
  composeMessage,
  loadInbox,
  loadMessage,
  loadProjects,
  mailEventsEndpoint,
  MailHttpError,
  mailRouteHash,
  markdownUrlTransform,
  parseMailRoute,
  replyToMessage,
  retryDelivery,
  type DeliveryResult,
  type InboxMessage,
  type MailProject,
  type MailRoute,
  type MessageAttachment,
  type MessageDetail,
} from "./mail";
import {
  loadPreferences,
  mailLoginUrl,
  PreferencesHttpError,
  saveCorrespondenceLocale,
  saveUiLocale,
  type MailUiPreferences,
} from "./preferences";
import "./app.css";

const mailNavigation = ["projects", "inbox"] as const;

const markdownRemarkPlugins = [remarkGfm, remarkBreaks];

type ShellRoute =
  | MailRoute
  | { view: "compose" }
  | { view: "account" }
  | { view: "admin" };
type NavigationItem = "projects" | "inbox" | "compose" | "account" | "admin";

function parseShellRoute(hash: string): ShellRoute {
  const normalized = hash.replace(/^#/, "");
  if (
    normalized === "compose" ||
    normalized === "account" ||
    normalized === "admin"
  ) {
    return { view: normalized };
  }
  return parseMailRoute(hash);
}

function shellRouteHash(route: ShellRoute): string {
  return route.view === "compose" || route.view === "account" || route.view === "admin"
    ? `#${route.view}`
    : mailRouteHash(route);
}

type PreferenceStatus =
  | "loading"
  | "saved"
  | "saving"
  | "loadError"
  | "saveError"
  | "unauthorized";

const preferenceStatusKey: Record<PreferenceStatus, string> = {
  loading: "localeStatus.loading",
  saved: "localeStatus.saved",
  saving: "localeStatus.saving",
  loadError: "localeStatus.loadError",
  saveError: "localeStatus.saveError",
  unauthorized: "localeStatus.unauthorized",
};

type LoadStatus = "loading" | "ready" | "error" | "unauthorized";
type DetailStatus = "idle" | LoadStatus;
type PaginationStatus = "idle" | "loading" | "error";
type StreamStatus = "connecting" | "live" | "reconnecting";
type MutationStatus = "idle" | "saving" | "saved" | "conflict" | "error";
type PasswordStatus =
  | "idle"
  | "saving"
  | "saved"
  | "mismatch"
  | "tooShort"
  | "rateLimit"
  | "error";
type EventSourceLike = Pick<
  EventSource,
  "close" | "onerror" | "onmessage" | "onopen"
>;

type DeliveryFormStatus = "idle" | "sending" | "conflict" | "error";

interface DeliveryAttempt {
  fingerprint: string;
  key: string;
}

interface AppProps {
  onUnauthorized?: (loginUrl: string) => void;
  navigateTo?: (url: string) => void;
  createEventSource?: (url: string) => EventSourceLike;
}

const defaultNavigate = window.location.assign.bind(window.location);
const defaultCreateEventSource = (url: string) => new EventSource(url);

function newIdempotencyKey(): string {
  return `human-ui:${window.crypto.randomUUID()}`;
}

function idempotencyKeyFor(
  attemptRef: { current: DeliveryAttempt | null },
  fingerprint: string,
): string {
  if (attemptRef.current?.fingerprint === fingerprint) {
    return attemptRef.current.key;
  }
  const key = newIdempotencyKey();
  attemptRef.current = { fingerprint, key };
  return key;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function mergeMessages(
  current: InboxMessage[],
  incoming: InboxMessage[],
): InboxMessage[] {
  const merged = new Map(current.map((message) => [message.id, message]));
  for (const message of incoming) {
    merged.set(message.id, message);
  }
  return [...merged.values()];
}

export function App({
  onUnauthorized,
  navigateTo = defaultNavigate,
  createEventSource = defaultCreateEventSource,
}: AppProps = {}) {
  const { t } = useTranslation();
  const markdownComponents = useMemo<MarkdownComponents>(
    () => ({
      h1({ children }) {
        return <h2>{children}</h2>;
      },
      h2({ children }) {
        return <h3>{children}</h3>;
      },
      h3({ children }) {
        return <h4>{children}</h4>;
      },
      h4({ children }) {
        return <h5>{children}</h5>;
      },
      h5({ children }) {
        return <h6>{children}</h6>;
      },
      h6({ children }) {
        return <h6>{children}</h6>;
      },
      a({ href, children, title }) {
        if (href === undefined) {
          return <span className="markdown-rejected-link">{children}</span>;
        }
        return <a href={href} title={title}>{children}</a>;
      },
      img({ src, alt, title }) {
        if (src === undefined) {
          return <span className="markdown-image-alt">{alt}</span>;
        }
        return (
          <img
            src={src}
            alt={alt}
            title={title}
            loading="lazy"
            decoding="async"
          />
        );
      },
      input({ checked, disabled }) {
        return (
          <input
            type="checkbox"
            checked={checked}
            disabled={disabled}
            aria-label={t(
              checked ? "markdown.completedTask" : "markdown.incompleteTask",
            )}
          />
        );
      },
      pre({ children }) {
        return (
          <pre tabIndex={0} aria-label={t("markdown.codeBlock")}>
            {children}
          </pre>
        );
      },
      table({ children }) {
        return (
          <div
            className="markdown-table-scroll"
            role="region"
            aria-label={t("markdown.table")}
            tabIndex={0}
          >
            <table>{children}</table>
          </div>
        );
      },
    }),
    [t],
  );
  const [locale, setLocale] = useState<SupportedLocale>("en");
  const [preferenceStatus, setPreferenceStatus] =
    useState<PreferenceStatus>("loading");
  const [preferences, setPreferences] = useState<MailUiPreferences | null>(null);
  const [route, setRoute] = useState<ShellRoute>(() =>
    parseShellRoute(window.location.hash),
  );
  const [profile, setProfile] = useState<MailUiProfile | null>(null);
  const [profileStatus, setProfileStatus] = useState<LoadStatus>("loading");
  const [displayName, setDisplayName] = useState("");
  const [profileMutationStatus, setProfileMutationStatus] =
    useState<MutationStatus>("idle");
  const [correspondenceStatus, setCorrespondenceStatus] =
    useState<MutationStatus>("idle");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordStatus, setPasswordStatus] =
    useState<PasswordStatus>("idle");
  const [adminSnapshot, setAdminSnapshot] =
    useState<AdminAccessSnapshot | null>(null);
  const [adminStatus, setAdminStatus] = useState<LoadStatus>("loading");
  const [selectedUserId, setSelectedUserId] = useState<number | null>(null);
  const [pendingProjectId, setPendingProjectId] = useState<number | null>(null);
  const [adminMutationStatus, setAdminMutationStatus] =
    useState<MutationStatus>("idle");
  const [adminMutationProject, setAdminMutationProject] = useState("");
  const [projects, setProjects] = useState<MailProject[]>([]);
  const [projectTotal, setProjectTotal] = useState(0);
  const [projectsStatus, setProjectsStatus] = useState<LoadStatus>("loading");
  const [messages, setMessages] = useState<InboxMessage[]>([]);
  const [messageTotal, setMessageTotal] = useState(0);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [inboxStatus, setInboxStatus] = useState<LoadStatus>("loading");
  const [paginationStatus, setPaginationStatus] =
    useState<PaginationStatus>("idle");
  const [detail, setDetail] = useState<MessageDetail | null>(null);
  const [detailStatus, setDetailStatus] = useState<DetailStatus>("idle");
  const [composeProjectId, setComposeProjectId] = useState("");
  const [composeRecipients, setComposeRecipients] = useState("");
  const [composeSubject, setComposeSubject] = useState("");
  const [composeBody, setComposeBody] = useState("");
  const [composeThreadId, setComposeThreadId] = useState("");
  const [composeStatus, setComposeStatus] =
    useState<DeliveryFormStatus>("idle");
  const [composeDelivery, setComposeDelivery] =
    useState<DeliveryResult | null>(null);
  const [replyBody, setReplyBody] = useState("");
  const [replyStatus, setReplyStatus] =
    useState<DeliveryFormStatus>("idle");
  const [replyDelivery, setReplyDelivery] =
    useState<DeliveryResult | null>(null);
  const [streamStatus, setStreamStatus] =
    useState<StreamStatus>("connecting");
  const [refreshVersion, setRefreshVersion] = useState(0);
  const redirectedRef = useRef(false);
  const paginationControllerRef = useRef<AbortController | null>(null);
  const detailRequestGenerationRef = useRef(0);
  const composeAttemptRef = useRef<DeliveryAttempt | null>(null);
  const replyAttemptRef = useRef<DeliveryAttempt | null>(null);
  const mailRouteActive =
    mailNavigation.some((item) => route.view === item) ||
    route.view === "message" ||
    route.view === "compose";
  const routeProjectId =
    route.view === "inbox" || route.view === "message" ? route.projectId : null;
  const routeMessageId = route.view === "message" ? route.messageId : null;

  const applyLocale = useCallback(
    async (nextLocale: SupportedLocale) => {
      await i18n.changeLanguage(nextLocale);
      setLocale(nextLocale);
      document.documentElement.lang = nextLocale;
    },
    [],
  );

  const redirectUnauthorized = useCallback(() => {
    if (redirectedRef.current) {
      return;
    }
    redirectedRef.current = true;
    const loginUrl = mailLoginUrl(window.location);
    if (onUnauthorized !== undefined) {
      onUnauthorized(loginUrl);
      return;
    }
    navigateTo(loginUrl);
  }, [navigateTo, onUnauthorized]);

  const isPreferenceUnauthorized = useCallback(
    (error: unknown) => {
      if (error instanceof PreferencesHttpError && error.status === 401) {
        redirectUnauthorized();
        return true;
      }
      return false;
    },
    [redirectUnauthorized],
  );

  const isAccountUnauthorized = useCallback(
    (error: unknown) => {
      if (error instanceof AccountHttpError && error.status === 401) {
        redirectUnauthorized();
        return true;
      }
      return false;
    },
    [redirectUnauthorized],
  );

  const dataFailureStatus = useCallback(
    (error: unknown): LoadStatus | null => {
      if (isAbortError(error)) {
        return null;
      }
      if (error instanceof MailHttpError && error.status === 401) {
        redirectUnauthorized();
        return "unauthorized";
      }
      return "error";
    },
    [redirectUnauthorized],
  );

  useEffect(() => {
    const syncRoute = () => {
      const next = parseShellRoute(window.location.hash);
      setRoute((current) =>
        shellRouteHash(current) === shellRouteHash(next) ? current : next,
      );
    };
    window.addEventListener("hashchange", syncRoute);
    window.addEventListener("popstate", syncRoute);
    return () => {
      window.removeEventListener("hashchange", syncRoute);
      window.removeEventListener("popstate", syncRoute);
    };
  }, []);

  useEffect(() => {
    if (!mailRouteActive) {
      setStreamStatus("connecting");
      return undefined;
    }
    let debounceTimer: number | undefined;
    const events = createEventSource(mailEventsEndpoint);
    events.onopen = () => setStreamStatus("live");
    events.onerror = () => setStreamStatus("reconnecting");
    events.onmessage = () => {
      if (debounceTimer !== undefined) {
        window.clearTimeout(debounceTimer);
      }
      debounceTimer = window.setTimeout(() => {
        setRefreshVersion((version) => version + 1);
      }, 250);
    };
    return () => {
      if (debounceTimer !== undefined) {
        window.clearTimeout(debounceTimer);
      }
      events.onopen = null;
      events.onerror = null;
      events.onmessage = null;
      events.close();
    };
  }, [createEventSource, mailRouteActive]);

  useEffect(() => {
    const detailRequestGeneration = ++detailRequestGenerationRef.current;
    if (!mailRouteActive) {
      paginationControllerRef.current?.abort();
      return undefined;
    }
    const controller = new AbortController();
    const projectId = routeProjectId ?? undefined;
    setProjectsStatus("loading");
    setInboxStatus("loading");
    setPaginationStatus("idle");

    void loadProjects({ signal: controller.signal })
      .then((page) => {
        setProjects(page.items);
        setProjectTotal(page.total);
        setProjectsStatus("ready");
      })
      .catch((error: unknown) => {
        const status = dataFailureStatus(error);
        if (status !== null) {
          setProjectsStatus(status);
        }
      });

    void loadInbox({ projectId, signal: controller.signal })
      .then((page) => {
        setMessages(page.items);
        setMessageTotal(page.total);
        setNextCursor(page.next_cursor);
        setInboxStatus("ready");
      })
      .catch((error: unknown) => {
        const status = dataFailureStatus(error);
        if (status !== null) {
          setInboxStatus(status);
        }
      });

    if (routeMessageId !== null && routeProjectId !== null) {
      setDetail(null);
      setDetailStatus("loading");
      void loadMessage(routeProjectId, routeMessageId, {
        signal: controller.signal,
      })
        .then((message) => {
          if (detailRequestGeneration !== detailRequestGenerationRef.current) {
            return;
          }
          if (
            message.project_id !== routeProjectId ||
            message.id !== routeMessageId
          ) {
            setDetail(null);
            setDetailStatus("error");
            return;
          }
          setDetail(message);
          setDetailStatus("ready");
        })
        .catch((error: unknown) => {
          if (detailRequestGeneration !== detailRequestGenerationRef.current) {
            return;
          }
          const status = dataFailureStatus(error);
          if (status !== null) {
            setDetailStatus(status);
          }
        });
    } else {
      setDetail(null);
      setDetailStatus("idle");
    }

    return () => controller.abort();
  }, [
    dataFailureStatus,
    mailRouteActive,
    refreshVersion,
    routeMessageId,
    routeProjectId,
  ]);

  useEffect(
    () => () => {
      paginationControllerRef.current?.abort();
    },
    [],
  );

  useEffect(() => {
    setReplyBody("");
    setReplyStatus("idle");
    setReplyDelivery(null);
    replyAttemptRef.current = null;
  }, [routeMessageId, routeProjectId]);

  useEffect(() => {
    void loadPreferences()
      .then(async (preferences) => {
        await applyLocale(preferences.effective.ui_locale);
        setPreferences(preferences);
        setPreferenceStatus("saved");
      })
      .catch(async (error: unknown) => {
        if (isPreferenceUnauthorized(error)) {
          setPreferenceStatus("unauthorized");
          return;
        }
        setPreferences(null);
        await applyLocale("en");
        setPreferenceStatus("loadError");
      });
  }, [applyLocale, isPreferenceUnauthorized]);

  useEffect(() => {
    const controller = new AbortController();
    setProfileStatus("loading");
    void loadProfile(controller.signal)
      .then((loadedProfile) => {
        setProfile(loadedProfile);
        setDisplayName(loadedProfile.display_name ?? "");
        setProfileStatus("ready");
      })
      .catch((error: unknown) => {
        if (isAbortError(error)) {
          return;
        }
        if (isAccountUnauthorized(error)) {
          setProfileStatus("unauthorized");
          return;
        }
        setProfileStatus("error");
      });
    return () => controller.abort();
  }, [isAccountUnauthorized]);

  useEffect(() => {
    if (
      route.view !== "admin" ||
      profileStatus !== "ready" ||
      profile?.global_role !== "admin"
    ) {
      return undefined;
    }
    const controller = new AbortController();
    setAdminStatus("loading");
    void loadAdminAccess(controller.signal)
      .then((snapshot) => {
        setAdminSnapshot(snapshot);
        setSelectedUserId((current) =>
          current !== null && snapshot.users.some((user) => user.id === current)
            ? current
            : (snapshot.users[0]?.id ?? null),
        );
        setAdminStatus("ready");
      })
      .catch((error: unknown) => {
        if (isAbortError(error)) {
          return;
        }
        if (isAccountUnauthorized(error)) {
          setAdminStatus("unauthorized");
          return;
        }
        setAdminStatus("error");
      });
    return () => controller.abort();
  }, [isAccountUnauthorized, profile?.global_role, profileStatus, route.view]);

  const handleLocaleChange = async (event: ChangeEvent<HTMLSelectElement>) => {
    const nextLocale = event.target.value as SupportedLocale;
    if (preferenceStatus === "loadError") {
      await applyLocale(nextLocale);
      return;
    }
    const previousLocale = locale;
    setPreferenceStatus("saving");
    try {
      const preferences = await saveUiLocale(nextLocale);
      await applyLocale(preferences.effective.ui_locale);
      setPreferences(preferences);
      setPreferenceStatus("saved");
    } catch (error) {
      await applyLocale(previousLocale);
      if (isPreferenceUnauthorized(error)) {
        setPreferenceStatus("unauthorized");
        return;
      }
      setPreferenceStatus("saveError");
    }
  };

  const handleCorrespondenceChange = async (
    event: ChangeEvent<HTMLSelectElement>,
  ) => {
    const selected = event.target.value;
    const nextLocale = selected === "" ? null : (selected as SupportedLocale);
    setCorrespondenceStatus("saving");
    try {
      const updatedPreferences = await saveCorrespondenceLocale(nextLocale);
      setPreferences(updatedPreferences);
      setCorrespondenceStatus("saved");
    } catch (error) {
      if (isPreferenceUnauthorized(error)) {
        setCorrespondenceStatus("error");
        return;
      }
      setCorrespondenceStatus("error");
    }
  };

  const handleDisplayNameSubmit = async (
    event: FormEvent<HTMLFormElement>,
    currentProfile: MailUiProfile,
  ) => {
    event.preventDefault();
    const requestedName = displayName.trim() === "" ? null : displayName;
    setProfileMutationStatus("saving");
    try {
      const result = await saveDisplayName(
        requestedName,
        currentProfile.profile_revision,
      );
      setProfile({
        ...currentProfile,
        display_name: result.display_name,
        profile_revision: result.profile_revision,
      });
      setDisplayName(result.display_name ?? "");
      setProfileMutationStatus("saved");
    } catch (error) {
      if (isAccountUnauthorized(error)) {
        setProfileMutationStatus("error");
        return;
      }
      if (error instanceof AccountHttpError && error.status === 409) {
        try {
          const refreshed = await loadProfile();
          setProfile(refreshed);
          setDisplayName(refreshed.display_name ?? "");
          setProfileMutationStatus("conflict");
          return;
        } catch (refreshError) {
          if (isAccountUnauthorized(refreshError)) {
            setProfileMutationStatus("error");
            return;
          }
        }
      }
      setProfileMutationStatus("error");
    }
  };

  const handlePasswordSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (newPassword !== confirmPassword) {
      setPasswordStatus("mismatch");
      return;
    }
    if (newPassword.length < 15) {
      setPasswordStatus("tooShort");
      return;
    }
    setPasswordStatus("saving");
    try {
      await changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setPasswordStatus("saved");
    } catch (error) {
      if (isAccountUnauthorized(error)) {
        setPasswordStatus("error");
        return;
      }
      setPasswordStatus(
        error instanceof AccountHttpError && error.status === 429
          ? "rateLimit"
          : "error",
      );
    }
  };

  const applyAdminSnapshot = (snapshot: AdminAccessSnapshot) => {
    setAdminSnapshot(snapshot);
    setSelectedUserId((current) =>
      current !== null && snapshot.users.some((user) => user.id === current)
        ? current
        : (snapshot.users[0]?.id ?? null),
    );
    setAdminStatus("ready");
  };

  const handleAssignmentChange = async (
    user: AdminUser,
    project: AdminProject,
    role: AssignmentRole | null,
    snapshot: AdminAccessSnapshot,
  ) => {
    setPendingProjectId(project.id);
    setAdminMutationProject(project.human_key);
    setAdminMutationStatus("saving");
    try {
      const result = await saveProjectAssignment(user, project, role);
      setAdminSnapshot({
          ...snapshot,
          users: snapshot.users.map((candidate) =>
            candidate.id === user.id
              ? {
                  ...candidate,
                  access_version: result.access_version,
                  assignments: [
                    ...candidate.assignments.filter(
                      (assignment) => assignment.project_id !== project.id,
                    ),
                    ...(result.role === null
                      ? []
                      : [{ project_id: project.id, role: result.role }]),
                  ],
                }
              : candidate,
          ),
      });
      setAdminMutationStatus("saved");
    } catch (error) {
      if (isAccountUnauthorized(error)) {
        setAdminMutationStatus("error");
      } else if (error instanceof AccountHttpError && error.status === 409) {
        try {
          applyAdminSnapshot(await loadAdminAccess());
          setAdminMutationStatus("conflict");
        } catch (refreshError) {
          if (isAccountUnauthorized(refreshError)) {
            setAdminMutationStatus("error");
          } else {
            setAdminStatus("error");
            setAdminMutationStatus("error");
          }
        }
      } else {
        setAdminMutationStatus("error");
      }
    } finally {
      setPendingProjectId(null);
    }
  };

  const handleProjectFilter = (event: ChangeEvent<HTMLSelectElement>) => {
    const value = event.target.value;
    const next: MailRoute = {
      view: "inbox",
      projectId: value === "" ? null : Number(value),
    };
    window.history.pushState({}, "", mailRouteHash(next));
    setRoute(next);
  };

  const handleLoadMore = async (cursor: string) => {
    paginationControllerRef.current?.abort();
    const controller = new AbortController();
    paginationControllerRef.current = controller;
    setPaginationStatus("loading");
    try {
      const page = await loadInbox({
        cursor,
        projectId: routeProjectId ?? undefined,
        signal: controller.signal,
      });
      setMessages((current) => mergeMessages(current, page.items));
      setMessageTotal(page.total);
      setNextCursor(page.next_cursor);
      setPaginationStatus("idle");
    } catch (error) {
      const status = dataFailureStatus(error);
      if (status === "unauthorized") {
        setInboxStatus("unauthorized");
        setPaginationStatus("idle");
      } else if (status === "error") {
        setPaginationStatus("error");
      }
    }
  };

  const deliveryFailureStatus = (error: unknown): DeliveryFormStatus => {
    if (error instanceof MailHttpError && error.status === 401) {
      redirectUnauthorized();
      return "error";
    }
    return error instanceof MailHttpError && error.status === 409
      ? "conflict"
      : "error";
  };

  const handleComposeSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const projectId = Number(composeProjectId);
    const recipients = [
      ...new Set(
        composeRecipients
          .split(",")
          .map((name) => name.trim())
          .filter((name) => name.length > 0),
      ),
    ];
    if (!Number.isSafeInteger(projectId) || projectId < 1 || recipients.length === 0) {
      setComposeStatus("error");
      return;
    }
    const canonicalInput = {
      projectId,
      recipients,
      subject: composeSubject,
      body_md: composeBody,
      thread_id: composeThreadId.trim() === "" ? null : composeThreadId.trim(),
    };
    const idempotencyKey = idempotencyKeyFor(
      composeAttemptRef,
      JSON.stringify(canonicalInput),
    );
    setComposeStatus("sending");
    try {
      const delivery = await composeMessage(projectId, {
        idempotency_key: idempotencyKey,
        recipients,
        subject: composeSubject,
        body_md: composeBody,
        thread_id: canonicalInput.thread_id,
      });
      setComposeDelivery(delivery);
      setComposeStatus("idle");
      if (delivery.status === "published") {
        setComposeRecipients("");
        setComposeSubject("");
        setComposeBody("");
        setComposeThreadId("");
        composeAttemptRef.current = null;
        setRefreshVersion((version) => version + 1);
      }
    } catch (error) {
      setComposeStatus(deliveryFailureStatus(error));
    }
  };

  const handleReplySubmit = async (
    event: FormEvent<HTMLFormElement>,
    message: MessageDetail,
  ) => {
    event.preventDefault();
    const canonicalInput = {
      projectId: message.project_id,
      messageId: message.id,
      body_md: replyBody,
    };
    const idempotencyKey = idempotencyKeyFor(
      replyAttemptRef,
      JSON.stringify(canonicalInput),
    );
    setReplyStatus("sending");
    try {
      const delivery = await replyToMessage(message.project_id, message.id, {
        idempotency_key: idempotencyKey,
        body_md: replyBody,
      });
      setReplyDelivery(delivery);
      setReplyStatus("idle");
      if (delivery.status === "published") {
        setReplyBody("");
        replyAttemptRef.current = null;
        setRefreshVersion((version) => version + 1);
      }
    } catch (error) {
      setReplyStatus(deliveryFailureStatus(error));
    }
  };

  const refreshComposeDelivery = async (deliveryId: string) => {
    setComposeStatus("sending");
    try {
      const delivery = await retryDelivery(deliveryId);
      setComposeDelivery(delivery);
      setComposeStatus("idle");
      if (delivery.status === "published") {
        setRefreshVersion((version) => version + 1);
      }
    } catch (error) {
      setComposeStatus(deliveryFailureStatus(error));
    }
  };

  const refreshReplyDelivery = async (deliveryId: string) => {
    setReplyStatus("sending");
    try {
      const delivery = await retryDelivery(deliveryId);
      setReplyDelivery(delivery);
      setReplyStatus("idle");
      if (delivery.status === "published") {
        setRefreshVersion((version) => version + 1);
      }
    } catch (error) {
      setReplyStatus(deliveryFailureStatus(error));
    }
  };

  const projectNames = useMemo(
    () => new Map(projects.map((project) => [project.id, project.human_key])),
    [projects],
  );
  const navigationItems = useMemo<NavigationItem[]>(
    () => [
      ...mailNavigation,
      ...(profile?.global_role === "admin" ? (["compose"] as const) : []),
      "account",
      ...(profile?.global_role === "admin" ? (["admin"] as const) : []),
    ],
    [profile?.global_role],
  );
  const selectedAdminUser = useMemo(
    () =>
      adminSnapshot?.users.find((user) => user.id === selectedUserId) ?? null,
    [adminSnapshot, selectedUserId],
  );

  const profileMutationMessage: Record<MutationStatus, string | null> = {
    idle: null,
    saving: "account.savingDisplayName",
    saved: "account.displayNameSaved",
    conflict: "account.displayNameConflict",
    error: "account.displayNameError",
  };
  const correspondenceMessage: Record<MutationStatus, string | null> = {
    idle: null,
    saving: "account.correspondenceSaving",
    saved: "account.correspondenceSaved",
    conflict: "account.correspondenceError",
    error: "account.correspondenceError",
  };
  const passwordMessage: Record<PasswordStatus, string | null> = {
    idle: null,
    saving: "account.changingPassword",
    saved: "account.passwordSaved",
    mismatch: "account.passwordMismatch",
    tooShort: "account.passwordTooShort",
    rateLimit: "account.passwordRateLimit",
    error: "account.passwordError",
  };
  const adminMutationMessage: Record<MutationStatus, string | null> = {
    idle: null,
    saving: "admin.saving",
    saved: "admin.saved",
    conflict: "admin.conflict",
    error: "admin.saveError",
  };

  const formatDate = (timestamp: string) =>
    new Intl.DateTimeFormat(locale, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(timestamp));

  const attachmentText = (attachment: MessageAttachment) =>
    t("message.attachment", {
      type: attachment.type ?? t("message.unknownType"),
      mediaType: attachment.media_type ?? t("message.unknownMediaType"),
      size:
        attachment.size_bytes === null
          ? t("message.unknownSize")
          : `${attachment.size_bytes.toLocaleString(locale)} B`,
    });

  const deliveryFeedback = (
    formStatus: DeliveryFormStatus,
    delivery: DeliveryResult | null,
    refresh: (deliveryId: string) => Promise<void>,
  ) => {
    if (formStatus === "sending") {
      return <p className="form-status" role="status">{t("delivery.sending")}</p>;
    }
    if (formStatus === "conflict") {
      return <p className="form-status state-error" role="alert">{t("delivery.conflict")}</p>;
    }
    if (formStatus === "error") {
      return <p className="form-status state-error" role="alert">{t("delivery.error")}</p>;
    }
    if (delivery === null) {
      return null;
    }
    const terminal = delivery.status === "published" || delivery.status === "quarantined";
    return (
      <div
        className={`delivery-result delivery-${delivery.status}`}
        role={delivery.status === "quarantined" ? "alert" : "status"}
        aria-live="polite"
      >
        <strong>{t(`delivery.status.${delivery.status}`)}</strong>
        <span>{t("delivery.reference", { id: delivery.id })}</span>
        {!terminal ? (
          <button
            type="button"
            className="secondary-button"
            onClick={() => void refresh(delivery.id)}
          >
            {t("delivery.checkStatus")}
          </button>
        ) : null}
      </div>
    );
  };

  const renderCompose = () => {
    const profileIsAdmin = profile?.global_role === "admin";
    const activeProjects = projects.filter((project) => project.archived_at === null);
    return (
      <section aria-labelledby="compose-heading">
        <div className="page-heading">
          <div>
            <p className="eyebrow">{t("compose.eyebrow")}</p>
            <h1 id="compose-heading">{t("compose.title")}</h1>
            <p>{t("compose.hint")}</p>
          </div>
        </div>
        {profileStatus === "loading" || projectsStatus === "loading" ? (
          <p className="state-panel" role="status">{t("compose.loading")}</p>
        ) : null}
        {profileStatus === "ready" && !profileIsAdmin ? (
          <p className="state-panel state-error" role="alert">{t("compose.forbidden")}</p>
        ) : null}
        {profileStatus === "error" || projectsStatus === "error" ? (
          <p className="state-panel state-error" role="alert">{t("compose.loadError")}</p>
        ) : null}
        {profileStatus === "ready" && profileIsAdmin && projectsStatus === "ready" ? (
          activeProjects.length === 0 ? (
            <p className="state-panel">{t("compose.noProjects")}</p>
          ) : (
            <form className="delivery-form settings-card" onSubmit={handleComposeSubmit}>
              <label htmlFor="compose-project">{t("compose.project")}</label>
              <select
                id="compose-project"
                name="compose-project"
                value={composeProjectId}
                onChange={(event) => setComposeProjectId(event.target.value)}
                required
                disabled={composeStatus === "sending"}
              >
                <option value="">{t("compose.chooseProject")}</option>
                {activeProjects.map((project) => (
                  <option key={project.id} value={project.id}>{project.human_key}</option>
                ))}
              </select>
              <label htmlFor="compose-recipients">{t("compose.recipients")}</label>
              <input
                id="compose-recipients"
                name="compose-recipients"
                value={composeRecipients}
                onChange={(event) => setComposeRecipients(event.target.value)}
                maxLength={12_899}
                required
                disabled={composeStatus === "sending"}
                aria-describedby="compose-recipients-hint"
              />
              <small id="compose-recipients-hint">{t("compose.recipientsHint")}</small>
              <label htmlFor="compose-subject">{t("compose.subject")}</label>
              <input
                id="compose-subject"
                name="compose-subject"
                value={composeSubject}
                onChange={(event) => setComposeSubject(event.target.value)}
                maxLength={200}
                required
                disabled={composeStatus === "sending"}
              />
              <label htmlFor="compose-thread">{t("compose.thread")}</label>
              <input
                id="compose-thread"
                name="compose-thread"
                value={composeThreadId}
                onChange={(event) => setComposeThreadId(event.target.value)}
                maxLength={128}
                disabled={composeStatus === "sending"}
              />
              <label htmlFor="compose-body">{t("compose.body")}</label>
              <textarea
                id="compose-body"
                name="compose-body"
                value={composeBody}
                onChange={(event) => setComposeBody(event.target.value)}
                maxLength={50_000}
                rows={12}
                required
                disabled={composeStatus === "sending"}
              />
              <button
                type="submit"
                className="primary-button"
                disabled={composeStatus === "sending"}
              >
                {t("compose.send")}
              </button>
              {deliveryFeedback(
                composeStatus,
                composeDelivery,
                refreshComposeDelivery,
              )}
            </form>
          )
        ) : null}
      </section>
    );
  };

  const renderProjects = () => (
    <section aria-labelledby="projects-heading">
      <div className="page-heading">
        <div>
          <p className="eyebrow">{t("projects.eyebrow")}</p>
          <h1 id="projects-heading">{t("projects.title")}</h1>
          <p>{t("projects.hint")}</p>
        </div>
        {projectsStatus === "ready" ? (
          <span className="count-pill">{t("projects.count", { count: projectTotal })}</span>
        ) : null}
      </div>
      {projectsStatus === "loading" ? (
        <p className="state-panel" role="status">{t("projects.loading")}</p>
      ) : null}
      {projectsStatus === "error" ? (
        <p className="state-panel state-error" role="alert">{t("errors.projects")}</p>
      ) : null}
      {projectsStatus === "unauthorized" ? (
        <p className="state-panel" role="status">{t("errors.unauthorized")}</p>
      ) : null}
      {projectsStatus === "ready" && projects.length === 0 ? (
        <p className="state-panel">{t("projects.empty")}</p>
      ) : null}
      {projectsStatus === "ready" && projects.length > 0 ? (
        <ul className="project-list project-grid">
          {projects.map((project) => (
            <li key={project.id}>
              <a
                className="project-row project-card"
                href={mailRouteHash({ view: "inbox", projectId: project.id })}
                aria-label={t("projects.openInbox", { project: project.human_key })}
              >
                <span className="project-avatar" aria-hidden="true">
                  {project.human_key.slice(0, 1).toUpperCase()}
                </span>
                <span className="project-copy">
                  <strong>{project.human_key}</strong>
                  <small>{project.slug}</small>
                  <small>{t("projects.added", { date: formatDate(project.created_at) })}</small>
                </span>
                <span className="project-badges">
                  <span className={`status role-${project.role}`}>
                    {t(`accessRole.${project.role}`)}
                  </span>
                  <span className={`status ${project.archived_at === null ? "status-live" : "status-archived"}`}>
                    {t(project.archived_at === null ? "projects.active" : "projects.archived")}
                  </span>
                </span>
              </a>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );

  const renderInbox = () => (
    <section aria-labelledby="inbox-heading">
      <div className="page-heading inbox-heading">
        <div>
          <p className="eyebrow">{t("inbox.eyebrow")}</p>
          <h1 id="inbox-heading">{t("inbox.title")}</h1>
          <p>{t("inbox.hint")}</p>
        </div>
        <div className="inbox-tools">
          <label htmlFor="project-filter">{t("inbox.projectFilter")}</label>
          <select
            id="project-filter"
            name="project-filter"
            value={routeProjectId ?? ""}
            onChange={handleProjectFilter}
            disabled={projectsStatus !== "ready"}
          >
            <option value="">{t("inbox.allProjects")}</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>{project.human_key}</option>
            ))}
          </select>
        </div>
      </div>
      <div className="inbox-summary">
        {inboxStatus === "ready" ? <strong>{t("inbox.count", { count: messageTotal })}</strong> : <span />}
        <span className={`stream-status stream-${streamStatus}`}>{t(`stream.${streamStatus}`)}</span>
      </div>
      {inboxStatus === "loading" ? (
        <p className="state-panel" role="status">{t("inbox.loading")}</p>
      ) : null}
      {inboxStatus === "error" ? (
        <p className="state-panel state-error" role="alert">{t("errors.inbox")}</p>
      ) : null}
      {inboxStatus === "unauthorized" ? (
        <p className="state-panel" role="status">{t("errors.unauthorized")}</p>
      ) : null}
      {inboxStatus === "ready" && messages.length === 0 ? (
        <p className="state-panel">{t("inbox.empty")}</p>
      ) : null}
      {inboxStatus === "ready" && messages.length > 0 ? (
        <ul className="panel message-list">
          {messages.map((message) => {
            const sender = message.sender_display_name ?? message.sender_name;
            const senderIdentity = message.sender === sender ? "" : ` · ${message.sender}`;
            const project = projectNames.get(message.project_id) ?? message.project_slug;
            return (
              <li key={message.id}>
                <a
                  className="message-row"
                  href={mailRouteHash({
                    view: "message",
                    projectId: message.project_id,
                    messageId: message.id,
                  })}
                >
                  <span className="sr-only">{t("inbox.openMessageCue")}</span>
                  <span className={`importance-mark importance-${message.importance}`} aria-hidden="true" />
                  <span className="message-copy">
                    <strong>{message.subject}</strong>
                    <small>{t("inbox.from", { sender })}{senderIdentity}</small>
                    <small>{t("inbox.project", { project })}</small>
                    {message.ack_required ? <small className="ack-label">{t("inbox.acknowledge")}</small> : null}
                  </span>
                  <span className="message-meta">
                    <span className={`status importance-${message.importance}`}>
                      {t(`importance.${message.importance}`)}
                    </span>
                    <time dateTime={message.created_ts}>{formatDate(message.created_ts)}</time>
                  </span>
                </a>
              </li>
            );
          })}
        </ul>
      ) : null}
      {inboxStatus === "ready" && nextCursor !== null ? (
        <div className="load-more-area">
          <button
            type="button"
            className="primary-button"
            onClick={() => void handleLoadMore(nextCursor)}
            disabled={paginationStatus === "loading"}
          >
            {t(paginationStatus === "loading" ? "inbox.loadingMore" : "inbox.loadMore")}
          </button>
          {paginationStatus === "error" ? <p role="alert">{t("errors.loadMore")}</p> : null}
        </div>
      ) : null}
    </section>
  );

  const renderMessage = (projectId: number, messageId: number) => {
    const inboxHash = mailRouteHash({ view: "inbox", projectId });
    const currentDetail =
      detail?.project_id === projectId && detail.id === messageId ? detail : null;
    const detailSender =
      currentDetail?.sender_display_name ?? currentDetail?.sender_name;
    const detailSenderIdentity =
      currentDetail !== null &&
      detailSender !== undefined &&
      currentDetail.sender !== detailSender
        ? ` · ${currentDetail.sender}`
        : "";
    return (
      <section aria-labelledby="message-heading">
        <a className="back-link" href={inboxHash}>← {t("message.back")}</a>
        {detailStatus === "loading" ? (
          <p className="state-panel" role="status">{t("message.loading")}</p>
        ) : null}
        {detailStatus === "error" ? (
          <p className="state-panel state-error" role="alert">{t("errors.message")}</p>
        ) : null}
        {detailStatus === "unauthorized" ? (
          <p className="state-panel" role="status">{t("errors.unauthorized")}</p>
        ) : null}
        {detailStatus === "ready" && currentDetail !== null ? (
          <article className="message-detail">
            <header>
              <p className="eyebrow">{t("message.eyebrow")}</p>
              <h1 id="message-heading">{currentDetail.subject}</h1>
              <p>{t("inbox.from", { sender: detailSender })}{detailSenderIdentity}</p>
            </header>
            <dl className="message-facts">
              <div><dt>{t("message.project")}</dt><dd>{projectNames.get(currentDetail.project_id) ?? currentDetail.project_slug}</dd></div>
              <div><dt>{t("message.sent")}</dt><dd><time dateTime={currentDetail.created_ts}>{formatDate(currentDetail.created_ts)}</time></dd></div>
              <div><dt>{t("message.to")}</dt><dd>{currentDetail.to.length > 0 ? currentDetail.to.join(", ") : t("message.emptyRecipients")}</dd></div>
              <div><dt>{t("message.cc")}</dt><dd>{currentDetail.cc.length > 0 ? currentDetail.cc.join(", ") : t("message.emptyRecipients")}</dd></div>
            </dl>
            <div className="message-body">
              <ReactMarkdown
                remarkPlugins={markdownRemarkPlugins}
                skipHtml
                urlTransform={markdownUrlTransform}
                components={markdownComponents}
              >
                {currentDetail.body_md}
              </ReactMarkdown>
            </div>
            {currentDetail.can_reply ? (
              <section className="reply-panel" aria-labelledby="reply-heading">
                <h2 id="reply-heading">{t("reply.title")}</h2>
                <p>{t("reply.hint")}</p>
                <form
                  className="delivery-form"
                  onSubmit={(event) => void handleReplySubmit(event, currentDetail)}
                >
                  <label htmlFor="reply-body">{t("reply.body")}</label>
                  <textarea
                    id="reply-body"
                    name="reply-body"
                    value={replyBody}
                    onChange={(event) => setReplyBody(event.target.value)}
                    maxLength={50_000}
                    rows={8}
                    required
                    disabled={replyStatus === "sending"}
                  />
                  <button
                    type="submit"
                    className="primary-button"
                    disabled={replyStatus === "sending"}
                  >
                    {t("reply.send")}
                  </button>
                  {deliveryFeedback(
                    replyStatus,
                    replyDelivery,
                    refreshReplyDelivery,
                  )}
                </form>
              </section>
            ) : null}
            {currentDetail.attachments.length > 0 ? (
              <section className="attachment-panel" aria-labelledby="attachments-heading">
                <h2 id="attachments-heading">{t("message.attachments")}</h2>
                <p>{t("message.attachmentCount", { count: currentDetail.attachments.length })}</p>
                <ul>{currentDetail.attachments.map((attachment, index) => <li key={`${attachment.type ?? "attachment"}-${index}`}>{attachmentText(attachment)}</li>)}</ul>
              </section>
            ) : null}
          </article>
        ) : null}
      </section>
    );
  };

  const renderAccount = () => (
    <section aria-labelledby="account-heading">
      <div className="page-heading">
        <div>
          <p className="eyebrow">{t("account.eyebrow")}</p>
          <h1 id="account-heading">{t("account.title")}</h1>
          <p>{t("account.hint")}</p>
        </div>
      </div>
      {profileStatus === "loading" ? (
        <p className="state-panel" role="status">{t("account.loading")}</p>
      ) : null}
      {profileStatus === "error" ? (
        <p className="state-panel state-error" role="alert">{t("account.loadError")}</p>
      ) : null}
      {profileStatus === "unauthorized" ? (
        <p className="state-panel" role="status">{t("errors.unauthorized")}</p>
      ) : null}
      {profileStatus === "ready" && profile !== null ? (
        <div className="settings-grid">
          <section className="settings-card" aria-labelledby="identity-heading">
            <h2 id="identity-heading">{t("account.identityTitle")}</h2>
            <dl className="account-facts">
              <div>
                <dt>{t("account.username")}</dt>
                <dd>{profile.username}</dd>
              </div>
              <div>
                <dt>{t("account.globalRole")}</dt>
                <dd>{t(`globalRole.${profile.global_role}`)}</dd>
              </div>
            </dl>
            <form
              className="settings-form"
              onSubmit={(event) => void handleDisplayNameSubmit(event, profile)}
            >
              <label htmlFor="display-name">{t("account.displayName")}</label>
              <input
                id="display-name"
                name="display-name"
                autoComplete="name"
                maxLength={128}
                value={displayName}
                onChange={(event) => {
                  setDisplayName(event.target.value);
                  setProfileMutationStatus("idle");
                }}
                aria-describedby="display-name-hint display-name-status"
                disabled={profileMutationStatus === "saving"}
              />
              <small id="display-name-hint">{t("account.displayNameHint")}</small>
              <button
                className="primary-button"
                type="submit"
                disabled={profileMutationStatus === "saving"}
              >
                {t("account.saveDisplayName")}
              </button>
              <p
                id="display-name-status"
                className="form-status"
                role="status"
                aria-live="polite"
                data-state={profileMutationStatus}
              >
                {profileMutationMessage[profileMutationStatus] === null
                  ? ""
                  : t(profileMutationMessage[profileMutationStatus])}
              </p>
            </form>
          </section>

          <section className="settings-card" aria-labelledby="languages-heading">
            <h2 id="languages-heading">{t("account.languagesTitle")}</h2>
            <p>{t("account.languagesHint")}</p>
            <div className="settings-form">
              <label htmlFor="account-ui-language">{t("account.uiLanguage")}</label>
              <select
                id="account-ui-language"
                name="account-ui-language"
                value={locale}
                onChange={(event) => void handleLocaleChange(event)}
                disabled={["loading", "saving", "unauthorized"].includes(
                  preferenceStatus,
                )}
              >
                {supportedLocales.map((supportedLocale) => (
                  <option key={supportedLocale} value={supportedLocale}>
                    {t(`languageName.${supportedLocale}`)}
                  </option>
                ))}
              </select>
              <label htmlFor="correspondence-language">
                {t("account.correspondenceLanguage")}
              </label>
              <select
                id="correspondence-language"
                name="correspondence-language"
                value={preferences?.stored.preferred_correspondence_locale ?? ""}
                onChange={(event) => void handleCorrespondenceChange(event)}
                disabled={
                  preferences === null ||
                  correspondenceStatus === "saving" ||
                  preferenceStatus === "unauthorized"
                }
                aria-describedby="correspondence-status"
              >
                <option value="">{t("account.correspondenceInherit")}</option>
                {supportedLocales.map((supportedLocale) => (
                  <option key={supportedLocale} value={supportedLocale}>
                    {t(`languageName.${supportedLocale}`)}
                  </option>
                ))}
              </select>
              <p
                id="correspondence-status"
                className="form-status"
                role="status"
                aria-live="polite"
                data-state={correspondenceStatus}
              >
                {correspondenceMessage[correspondenceStatus] === null
                  ? ""
                  : t(correspondenceMessage[correspondenceStatus])}
              </p>
            </div>
          </section>

          <section className="settings-card" aria-labelledby="password-heading">
            <h2 id="password-heading">{t("account.passwordTitle")}</h2>
            <p>{t("account.passwordHint")}</p>
            <form
              className="settings-form"
              noValidate
              onSubmit={(event) => void handlePasswordSubmit(event)}
            >
              <label htmlFor="current-password">{t("account.currentPassword")}</label>
              <input
                id="current-password"
                name="current-password"
                type="password"
                autoComplete="current-password"
                required
                maxLength={1024}
                value={currentPassword}
                onChange={(event) => {
                  setCurrentPassword(event.target.value);
                  setPasswordStatus("idle");
                }}
                disabled={passwordStatus === "saving"}
              />
              <label htmlFor="new-password">{t("account.newPassword")}</label>
              <input
                id="new-password"
                name="new-password"
                type="password"
                autoComplete="new-password"
                required
                minLength={15}
                maxLength={1024}
                value={newPassword}
                onChange={(event) => {
                  setNewPassword(event.target.value);
                  setPasswordStatus("idle");
                }}
                disabled={passwordStatus === "saving"}
              />
              <label htmlFor="confirm-password">{t("account.confirmPassword")}</label>
              <input
                id="confirm-password"
                name="confirm-password"
                type="password"
                autoComplete="new-password"
                required
                minLength={15}
                maxLength={1024}
                value={confirmPassword}
                onChange={(event) => {
                  setConfirmPassword(event.target.value);
                  setPasswordStatus("idle");
                }}
                disabled={passwordStatus === "saving"}
              />
              <button
                className="primary-button"
                type="submit"
                disabled={passwordStatus === "saving"}
              >
                {t("account.changePassword")}
              </button>
              <p
                className="form-status"
                role="status"
                aria-live="polite"
                data-state={passwordStatus}
              >
                {passwordMessage[passwordStatus] === null
                  ? ""
                  : t(passwordMessage[passwordStatus])}
              </p>
            </form>
          </section>
        </div>
      ) : null}
    </section>
  );

  const renderAdmin = () => {
    const profileIsAdmin = profile?.global_role === "admin";
    const selectedUserReadOnly =
      selectedAdminUser?.disabled === true ||
      selectedAdminUser?.global_role === "admin";
    return (
      <section aria-labelledby="admin-heading">
        <div className="page-heading">
          <div>
            <p className="eyebrow">{t("admin.eyebrow")}</p>
            <h1 id="admin-heading">{t("admin.title")}</h1>
            <p>{t("admin.hint")}</p>
          </div>
        </div>
        {profileStatus === "loading" ? (
          <p className="state-panel" role="status">{t("account.loading")}</p>
        ) : null}
        {profileStatus === "error" ? (
          <p className="state-panel state-error" role="alert">{t("account.loadError")}</p>
        ) : null}
        {profileStatus === "unauthorized" ? (
          <p className="state-panel" role="status">{t("errors.unauthorized")}</p>
        ) : null}
        {profileStatus === "ready" && !profileIsAdmin ? (
          <p className="state-panel state-error" role="alert">{t("admin.forbidden")}</p>
        ) : null}
        {profileStatus === "ready" && profileIsAdmin && adminStatus === "loading" ? (
          <p className="state-panel" role="status">{t("admin.loading")}</p>
        ) : null}
        {profileStatus === "ready" && profileIsAdmin && adminStatus === "error" ? (
          <p className="state-panel state-error" role="alert">{t("admin.loadError")}</p>
        ) : null}
        {profileStatus === "ready" && profileIsAdmin && adminStatus === "unauthorized" ? (
          <p className="state-panel" role="status">{t("errors.unauthorized")}</p>
        ) : null}
        {profileStatus === "ready" &&
        profileIsAdmin &&
        adminStatus === "ready" &&
        adminSnapshot !== null ? (
          <div className="admin-layout">
            <section className="settings-card admin-people" aria-labelledby="people-heading">
              <h2 id="people-heading">{t("admin.usersTitle")}</h2>
              {adminSnapshot.users.length === 0 ? <p>{t("admin.usersEmpty")}</p> : null}
              <ul className="admin-user-list">
                {adminSnapshot.users.map((user) => {
                  const label = user.display_name ?? user.username;
                  return (
                    <li key={user.id}>
                      <button
                        type="button"
                        className={user.id === selectedUserId ? "admin-user is-selected" : "admin-user"}
                        aria-pressed={user.id === selectedUserId}
                        onClick={() => {
                          setSelectedUserId(user.id);
                          setAdminMutationStatus("idle");
                        }}
                      >
                        <strong>{label}</strong>
                        {label === user.username ? null : <small>{user.username}</small>}
                        <span>
                          {t(
                            user.disabled
                              ? "admin.disabled"
                              : user.global_role === "admin"
                                ? "admin.administrator"
                                : "admin.member",
                          )}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </section>

            <section className="settings-card admin-projects" aria-labelledby="assignments-heading">
              <h2 id="assignments-heading">{t("admin.projectsTitle")}</h2>
              {selectedAdminUser === null ? (
                <p>{t("admin.usersEmpty")}</p>
              ) : (
                <>
                  <p className="selected-account">
                    {t("admin.selectedUser", {
                      user: selectedAdminUser.display_name ?? selectedAdminUser.username,
                    })}
                  </p>
                  {selectedUserReadOnly ? (
                    <p className="readonly-notice">{t("admin.readOnly")}</p>
                  ) : null}
                  {adminSnapshot.projects.length === 0 ? (
                    <p>{t("admin.projectsEmpty")}</p>
                  ) : (
                    <ul className="assignment-list">
                      {adminSnapshot.projects.map((project) => {
                        const assignment = selectedAdminUser.assignments.find(
                          (candidate) => candidate.project_id === project.id,
                        );
                        return (
                          <li key={project.id}>
                            <div>
                              <strong>{project.human_key}</strong>
                              <small>{project.slug}</small>
                              <span className={`status ${project.archived_at === null ? "status-live" : "status-archived"}`}>
                                {t(project.archived_at === null ? "admin.active" : "admin.archived")}
                              </span>
                            </div>
                            <label>
                              <span className="sr-only">
                                {t("admin.assignment", { project: project.human_key })}
                              </span>
                              <select
                                aria-label={t("admin.assignment", { project: project.human_key })}
                                name={`project-access-${project.id}`}
                                value={assignment?.role ?? ""}
                                disabled={selectedUserReadOnly || pendingProjectId !== null}
                                onChange={(event) => {
                                  const value = event.target.value;
                                  void handleAssignmentChange(
                                    selectedAdminUser,
                                    project,
                                    value === "" ? null : (value as AssignmentRole),
                                    adminSnapshot,
                                  );
                                }}
                              >
                                <option value="">{t("admin.noAccess")}</option>
                                <option value="viewer">{t("accessRole.viewer")}</option>
                                <option value="operator">{t("accessRole.operator")}</option>
                              </select>
                            </label>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                  <p
                    className="form-status"
                    role="status"
                    aria-live="polite"
                    data-state={adminMutationStatus}
                  >
                    {adminMutationMessage[adminMutationStatus] === null
                      ? ""
                      : t(adminMutationMessage[adminMutationStatus], {
                          project: adminMutationProject,
                        })}
                  </p>
                </>
              )}
            </section>
          </div>
        ) : null}
      </section>
    );
  };

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        {t("skipToContent")}
      </a>

      <header className="topbar">
        <div className="brand" aria-label={t("appName")}>
          <span className="brand-mark" aria-hidden="true">
            H
          </span>
          <span>
            <strong>{t("appName")}</strong>
            <small>{t("appSubtitle")}</small>
          </span>
        </div>

        <div className="topbar-controls">
          <span className="read-only-badge">
            {t("mailboxSecureDelivery")}
          </span>
          <div className="locale-control">
            <label>
              <span>{t("language")}</span>
              <select
                aria-label={t("language")}
                aria-describedby="locale-preference-status"
                name="ui-language"
                value={locale}
                onChange={handleLocaleChange}
                disabled={["loading", "saving", "unauthorized"].includes(
                  preferenceStatus,
                )}
              >
                {supportedLocales.map((locale) => (
                  <option key={locale} value={locale}>
                    {t(`languageName.${locale}`)}
                  </option>
                ))}
              </select>
            </label>
            <small
              id="locale-preference-status"
              className="locale-hint"
              data-state={preferenceStatus}
              role="status"
              aria-live="polite"
            >
              {t(preferenceStatusKey[preferenceStatus])}
            </small>
          </div>
          <form className="logout-form" action="/mail/logout" method="post">
            <button className="logout-button" type="submit">
              {t("signOut")}
            </button>
          </form>
        </div>
      </header>

      <div className="workspace">
        <aside className="sidebar">
          <nav aria-label={t("navigation")}>
            {navigationItems.map((item) => (
              <a
                className={
                  (route.view === item || (route.view === "message" && item === "inbox"))
                    ? "nav-link is-active"
                    : "nav-link"
                }
                href={`#${item}`}
                key={item}
                aria-current={
                  route.view === item || (route.view === "message" && item === "inbox")
                    ? "page"
                    : undefined
                }
              >
                <span className="nav-dot" aria-hidden="true" />
                {t(`nav.${item}`)}
              </a>
            ))}
          </nav>
        </aside>

        <main id="main-content" className="content">
          {route.view === "projects" ? renderProjects() : null}
          {route.view === "inbox" ? renderInbox() : null}
          {route.view === "compose" ? renderCompose() : null}
          {route.view === "message"
            ? renderMessage(route.projectId, route.messageId)
            : null}
          {route.view === "account" ? renderAccount() : null}
          {route.view === "admin" ? renderAdmin() : null}
        </main>
      </div>
    </div>
  );
}
