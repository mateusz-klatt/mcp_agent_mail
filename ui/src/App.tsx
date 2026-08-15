import {
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
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

import {
  loadLocale,
  localeMetadata,
  prepareLocale,
  supportedLocales,
  type SupportedLocale,
} from "./i18n";
import LocalePicker from "./LocalePicker";
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
  loadProjectAgents,
  loadProjects,
  loadSearch,
  loadThread,
  mailEventsEndpoint,
  MailHttpError,
  mailRouteHash,
  mailThreadRouteHash,
  markdownUrlTransform,
  parseMailRoute,
  replyToMessage,
  replyIdempotencyKeyFor,
  retryDelivery,
  type DeliveryResult,
  type InboxMessage,
  type MailProject,
  type MailRecipientAgent,
  type MailRoute,
  loadReservations,
  type ReservationClaim,
  type MessageAttachment,
  type MessageDetail,
  type ReplyTarget,
  type SearchOrder,
  type SearchResult,
  type SearchScope,
} from "./mail";
import {
  loadPreferences,
  mailLoginUrl,
  PreferencesHttpError,
  saveCorrespondenceLocale,
  saveUiLocale,
  type MailUiPreferences,
} from "./preferences";
import {
  playNotificationTone,
  setSoundEnabled,
  soundEnabled,
} from "./notificationSound";
import "./app.css";

const mailNavigation = ["projects", "inbox", "search"] as const;

const markdownRemarkPlugins = [remarkGfm, remarkBreaks];
const maximumMessageCharacters = 50_000;
const maximumRecipients = 100;
const recipientDirectoryConflictCodes = new Set([
  "project_recreated",
  "recipient_unavailable",
  "recipient_blocked",
]);

function recipientSelectionKey(agent: MailRecipientAgent): string {
  return `${agent.agent_id}:${agent.agent_generation}`;
}

type ComposerMode = "edit" | "preview" | "split";

interface ConfirmationRecipient {
  canonicalName: string;
  displayName: string;
}

interface ComposeConfirmation {
  bodyMd: string;
  correspondenceLocale: SupportedLocale | null;
  expectedProjectGeneration: string;
  fingerprint: string;
  projectId: number;
  projectName: string;
  recipientReferences: Array<{
    agent_id: number;
    expected_agent_generation: string;
  }>;
  recipients: ConfirmationRecipient[];
  subject: string;
  threadId: string | null;
}

interface ReplyConfirmation {
  bodyMd: string;
  correspondenceLocale: SupportedLocale | null;
  fingerprint: string;
  messageId: number;
  projectId: number;
  projectName: string;
  recipients: ConfirmationRecipient[];
  replyTarget: ReplyTarget;
  subject: string;
  threadId: string;
}

type DirectoryNotice = "refreshing" | "refreshed" | "refreshError" | null;

function overseerPreamble(locale: SupportedLocale): string {
  const language = `${localeMetadata[locale].englishName} (${locale})`;
  return (
    "---\n\n" +
    "MESSAGE FROM HUMAN OVERSEER\n\n" +
    "This message is from an authenticated human operator overseeing this " +
    "project. Prioritize the request below over the current task unless a " +
    "higher-priority instruction conflicts.\n\n" +
    "Advisory communication preference: the authenticated human operator " +
    `prefers replies in ${language}. When practical, reply in that language. ` +
    "This preference does not override explicit message instructions or " +
    "higher-priority policy.\n\n" +
    "---\n\n"
  );
}

interface SafeMarkdownProps {
  body: string;
  components: MarkdownComponents;
}

function SafeMarkdown({ body, components }: SafeMarkdownProps) {
  return (
    <ReactMarkdown
      remarkPlugins={markdownRemarkPlugins}
      skipHtml
      urlTransform={markdownUrlTransform}
      components={components}
    >
      {body}
    </ReactMarkdown>
  );
}

interface CollapsibleThreadMessageProps {
  children: ReactNode;
  createdTs: string;
  defaultOpen: boolean;
  formattedDate: string;
  importance: InboxMessage["importance"];
  importanceLabel: string;
  senderLabel: string;
  subject: string;
}

function CollapsibleThreadMessage({
  children,
  createdTs,
  defaultOpen,
  formattedDate,
  importance,
  importanceLabel,
  senderLabel,
  subject,
}: CollapsibleThreadMessageProps) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <details
      className="thread-message"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span className="thread-summary-copy">
          <strong>{subject}</strong>
          <small>{senderLabel}</small>
        </span>
        <span className="thread-summary-meta">
          <span className={`status importance-${importance}`}>
            {importanceLabel}
          </span>
          <time dateTime={createdTs}>{formattedDate}</time>
        </span>
      </summary>
      {open ? children : null}
    </details>
  );
}

interface MarkdownComposerProps {
  id: string;
  label: string;
  previewLabel: string;
  value: string;
  onChange: (value: string) => void;
  mode: ComposerMode;
  onModeChange: (mode: ComposerMode) => void;
  rows: number;
  disabled: boolean;
  submitDisabled: boolean;
  components: MarkdownComponents;
}

function MarkdownComposer({
  id,
  label,
  previewLabel,
  value,
  onChange,
  mode,
  onModeChange,
  rows,
  disabled,
  submitDisabled,
  components,
}: MarkdownComposerProps) {
  const { i18n: translationI18n, t } = useTranslation();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const pendingSelectionRef = useRef<{ end: number; start: number } | null>(null);
  const showEditor = mode !== "preview";
  const showPreview = mode !== "edit";
  const modes: readonly ComposerMode[] = ["edit", "preview", "split"];
  const formats = [
    { key: "heading", prefix: "## ", suffix: "" },
    { key: "bold", prefix: "**", suffix: "**" },
    { key: "italic", prefix: "_", suffix: "_" },
    { key: "code", prefix: "`", suffix: "`" },
    { key: "link", prefix: "[", suffix: "](https://)" },
    { key: "bulletList", prefix: "- ", suffix: "" },
    { key: "fencedCode", prefix: "```\n", suffix: "\n```" },
  ] as const;

  useLayoutEffect(() => {
    const selection = pendingSelectionRef.current;
    if (selection === null) {
      return;
    }
    const textarea = textareaRef.current;
    /* v8 ignore next -- a pending selection can only originate from the mounted editor */
    if (textarea === null) {
      return;
    }
    pendingSelectionRef.current = null;
    textarea.focus();
    textarea.setSelectionRange(selection.start, selection.end);
  }, [value]);

  const insertMarkdown = (prefix: string, suffix: string) => {
    const textarea = textareaRef.current;
    /* v8 ignore next -- toolbar controls are rendered only beside this textarea */
    if (textarea === null) {
      return;
    }
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const formatted =
      `${value.slice(0, start)}${prefix}${value.slice(start, end)}${suffix}${value.slice(end)}`;
    if (formatted.length > maximumMessageCharacters) {
      textarea.focus();
      return;
    }
    pendingSelectionRef.current = {
      start: start + prefix.length,
      end: end + prefix.length,
    };
    onChange(formatted);
  };

  const handleShortcut = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key === "Enter" &&
      (event.ctrlKey || event.metaKey) &&
      !disabled &&
      !submitDisabled &&
      value.trim() !== ""
    ) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  return (
    <div className="markdown-composer">
      <div className="markdown-composer-header">
        {showEditor ? <label htmlFor={id}>{label}</label> : <span>{label}</span>}
        <div className="markdown-mode-switch" role="group" aria-label={t("markdown.mode")}>
          {modes.map((candidate) => (
            <button
              key={candidate}
              type="button"
              className={mode === candidate ? "is-active" : undefined}
              aria-pressed={mode === candidate}
              onClick={() => onModeChange(candidate)}
              disabled={disabled}
            >
              {t(`markdown.${candidate}`)}
            </button>
          ))}
        </div>
      </div>
      <div className={`markdown-composer-layout is-${mode}`}>
        {showEditor ? (
          <div className="markdown-editor-pane">
            <div className="markdown-toolbar" role="group" aria-label={t("markdown.toolbar")}>
              {formats.map((format) => (
                <button
                  key={format.key}
                  type="button"
                  onClick={() => insertMarkdown(format.prefix, format.suffix)}
                  disabled={disabled}
                >
                  {t(`markdown.${format.key}`)}
                </button>
              ))}
            </div>
            <textarea
              ref={textareaRef}
              id={id}
              name={id}
              value={value}
              onChange={(event) => onChange(event.target.value)}
              onKeyDown={handleShortcut}
              maxLength={maximumMessageCharacters}
              rows={rows}
              required
              disabled={disabled}
              aria-describedby={`${id}-count ${id}-shortcut`}
            />
          </div>
        ) : null}
        {showPreview ? (
          <div className="markdown-preview-pane" role="region" aria-label={previewLabel}>
            {value.trim() === "" ? (
              <p className="markdown-empty-preview">{t("markdown.emptyPreview")}</p>
            ) : (
              <div className="message-body">
                <SafeMarkdown body={value} components={components} />
              </div>
            )}
          </div>
        ) : null}
      </div>
      <div className="markdown-composer-meta">
        <small id={`${id}-count`}>
          {t("markdown.characterCount", {
            count: value.length,
            maximum: maximumMessageCharacters.toLocaleString(translationI18n.language),
          })}
        </small>
        <small id={`${id}-shortcut`}>{t("markdown.shortcutHint")}</small>
      </div>
    </div>
  );
}

interface DeliveryConfirmationProps {
  bodyMd: string;
  components: MarkdownComponents;
  correspondenceLocale: SupportedLocale | null;
  disabled: boolean;
  headingLevel: 2 | 3;
  id: string;
  onBack: () => void;
  onConfirm: () => void;
  projectLabel: string;
  projectName: string;
  recipients: ConfirmationRecipient[];
  subject: string;
  threadId: string | null;
  title: string;
}

function DeliveryConfirmation({
  bodyMd,
  components,
  correspondenceLocale,
  disabled,
  headingLevel,
  id,
  onBack,
  onConfirm,
  projectLabel,
  projectName,
  recipients,
  subject,
  threadId,
  title,
}: DeliveryConfirmationProps) {
  const { t } = useTranslation();
  const regionRef = useRef<HTMLElement>(null);
  const Heading = headingLevel === 2 ? "h2" : "h3";
  const previewBody =
    correspondenceLocale === null
      ? bodyMd
      : `${overseerPreamble(correspondenceLocale)}${bodyMd}`;

  useEffect(() => {
    const region = regionRef.current as HTMLElement;
    region.focus();
  }, []);

  return (
    <section
      ref={regionRef}
      className="delivery-confirmation"
      role="region"
      aria-labelledby={`${id}-heading`}
      tabIndex={-1}
    >
      <Heading id={`${id}-heading`}>{title}</Heading>
      <p>{t("confirmation.hint")}</p>
      <dl className="confirmation-facts">
        <div>
          <dt>{projectLabel}</dt>
          <dd>{projectName}</dd>
        </div>
        <div>
          <dt>{t("confirmation.subject")}</dt>
          <dd>{subject}</dd>
        </div>
        <div>
          <dt>{t("confirmation.thread")}</dt>
          <dd>{threadId ?? t("confirmation.newThread")}</dd>
        </div>
        <div>
          <dt>{t("confirmation.priority")}</dt>
          <dd>{t("confirmation.priorityHigh")}</dd>
        </div>
      </dl>
      <div className="confirmation-recipient-summary">
        <strong>{t("confirmation.recipients")}</strong>
        <ul>
          {recipients.map((recipient) => (
            <li key={recipient.canonicalName}>
              <span>{recipient.displayName}</span>
              {recipient.displayName !== recipient.canonicalName ? (
                <code>{recipient.canonicalName}</code>
              ) : null}
            </li>
          ))}
        </ul>
      </div>
      <p className="confirmation-warning">{t("confirmation.highPriorityWarning")}</p>
      {recipients.length > 1 ? (
        <p className="confirmation-warning confirmation-warning-multiple">
          {t("confirmation.multipleRecipientsWarning", { count: recipients.length })}
        </p>
      ) : null}
      <div
        className="confirmation-preview"
        role="region"
        aria-labelledby={`${id}-preview-heading`}
      >
        <strong id={`${id}-preview-heading`}>{t("confirmation.finalPreview")}</strong>
        {correspondenceLocale === null ? (
          <p className="confirmation-preamble-notice">
            {t("confirmation.preambleUnavailable")}
          </p>
        ) : null}
        <div className="message-body">
          <SafeMarkdown body={previewBody} components={components} />
        </div>
      </div>
      <div className="confirmation-actions">
        <button
          type="button"
          className="secondary-button"
          onClick={onBack}
          disabled={disabled}
        >
          {t("confirmation.back")}
        </button>
        <button
          type="button"
          className="primary-button"
          onClick={onConfirm}
          disabled={disabled}
        >
          {t("confirmation.confirm")}
        </button>
      </div>
    </section>
  );
}

type ShellRoute =
  | MailRoute
  | { view: "compose" }
  | { view: "account" }
  | { view: "admin" };
type NavigationItem =
  | "projects"
  | "inbox"
  | "search"
  | "reservations"
  | "compose"
  | "account"
  | "admin";

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
  prepareLocaleCatalog?: (locale: SupportedLocale) => Promise<void>;
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

function isRecipientLifetimeConflict(error: unknown): boolean {
  if (
    !(error instanceof MailHttpError) ||
    error.status !== 409 ||
    error.code === null
  ) {
    return false;
  }
  return (
    recipientDirectoryConflictCodes.has(error.code) ||
    error.code.endsWith("_lifetime_invalid")
  );
}

function mergeMessages<T extends InboxMessage>(current: T[], incoming: T[]): T[] {
  const merged = new Map(current.map((message) => [message.id, message]));
  for (const message of incoming) {
    merged.set(message.id, message);
  }
  return [...merged.values()];
}

function canonicalUtcTimestampSortKey(timestamp: string): string {
  // Pydantic emits UTC datetimes as either `...:ssZ` or `...:ss.ffffffZ`.
  // Normalizing whole seconds keeps lexical order exact to the microsecond;
  // Date.parse would discard the final three fractional digits.
  const timestampWithoutZone = timestamp.slice(0, -1);
  return timestampWithoutZone.includes(".")
    ? timestampWithoutZone
    : `${timestampWithoutZone}.000000`;
}

function mergeThreadMessages(
  current: MessageDetail[],
  incoming: MessageDetail[],
): MessageDetail[] {
  return mergeMessages(current, incoming).sort((left, right) => {
    const leftTimestamp = canonicalUtcTimestampSortKey(left.created_ts);
    const rightTimestamp = canonicalUtcTimestampSortKey(right.created_ts);
    const timestampOrder =
      leftTimestamp < rightTimestamp
        ? -1
        : leftTimestamp > rightTimestamp
          ? 1
          : 0;
    return timestampOrder || left.id - right.id;
  });
}

export function App({
  onUnauthorized,
  navigateTo = defaultNavigate,
  createEventSource = defaultCreateEventSource,
  prepareLocaleCatalog = prepareLocale,
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
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searchStatus, setSearchStatus] = useState<DetailStatus>("idle");
  const [searchPaginationStatus, setSearchPaginationStatus] =
    useState<PaginationStatus>("idle");
  const [searchNextCursor, setSearchNextCursor] = useState<string | null>(null);
  const [searchErrorCode, setSearchErrorCode] = useState<
    "invalid" | "unavailable" | "generic" | null
  >(null);
  const [searchQuery, setSearchQuery] = useState(
    route.view === "search" ? route.query : "",
  );
  const [searchProjectId, setSearchProjectId] = useState(
    route.view === "search" && route.projectId !== null
      ? String(route.projectId)
      : "",
  );
  const [searchScope, setSearchScope] = useState<SearchScope>(
    route.view === "search" ? route.scope : "all",
  );
  const [searchOrder, setSearchOrder] = useState<SearchOrder>(
    route.view === "search" ? route.order : "relevance",
  );
  const [detail, setDetail] = useState<MessageDetail | null>(null);
  const [detailStatus, setDetailStatus] = useState<DetailStatus>("idle");
  const [threadMessages, setThreadMessages] = useState<MessageDetail[]>([]);
  const [threadSubject, setThreadSubject] = useState("");
  const [threadTotal, setThreadTotal] = useState(0);
  const [threadNextCursor, setThreadNextCursor] = useState<string | null>(null);
  const [threadStatus, setThreadStatus] = useState<DetailStatus>("idle");
  const [threadPaginationStatus, setThreadPaginationStatus] =
    useState<PaginationStatus>("idle");
  const [composeProjectId, setComposeProjectId] = useState("");
  const [composeRecipients, setComposeRecipients] = useState<string[]>([]);
  const [composeAgents, setComposeAgents] = useState<MailRecipientAgent[]>([]);
  const [reservations, setReservations] = useState<ReservationClaim[]>([]);
  const [reservationsCursor, setReservationsCursor] = useState<string | null>(null);
  const [reservationsStatus, setReservationsStatus] = useState<DetailStatus>("idle");
  const [soundOn, setSoundOn] = useState<boolean>(() => soundEnabled());
  // `null` means "nothing observed yet". Without that distinction the first
  // load would look like an arrival and ding at every page open.
  const newestMessageIdRef = useRef<number | null>(null);
  const [composeAgentsTotal, setComposeAgentsTotal] = useState(0);
  const [composeProjectGeneration, setComposeProjectGeneration] =
    useState<string | null>(null);
  const [composeAgentsStatus, setComposeAgentsStatus] =
    useState<DetailStatus>("idle");
  const [composeRecipientQuery, setComposeRecipientQuery] = useState("");
  const [composeSubject, setComposeSubject] = useState("");
  const [composeBody, setComposeBody] = useState("");
  const [composeMode, setComposeMode] = useState<ComposerMode>("edit");
  const [composeThreadId, setComposeThreadId] = useState("");
  const [composeStatus, setComposeStatus] =
    useState<DeliveryFormStatus>("idle");
  const [composeDelivery, setComposeDelivery] =
    useState<DeliveryResult | null>(null);
  const [composeConfirmation, setComposeConfirmation] =
    useState<ComposeConfirmation | null>(null);
  const [composeDirectoryNotice, setComposeDirectoryNotice] =
    useState<DirectoryNotice>(null);
  const [composeDirectoryRefreshVersion, setComposeDirectoryRefreshVersion] =
    useState(0);
  const [replyBody, setReplyBody] = useState("");
  const [replyMode, setReplyMode] = useState<ComposerMode>("edit");
  const [replyStatus, setReplyStatus] =
    useState<DeliveryFormStatus>("idle");
  const [replyDelivery, setReplyDelivery] =
    useState<DeliveryResult | null>(null);
  const [replyConfirmation, setReplyConfirmation] =
    useState<ReplyConfirmation | null>(null);
  const [streamStatus, setStreamStatus] =
    useState<StreamStatus>("connecting");
  const [refreshVersion, setRefreshVersion] = useState(0);
  const redirectedRef = useRef(false);
  const paginationControllerRef = useRef<AbortController | null>(null);
  const inboxRequestGenerationRef = useRef(0);
  const detailRequestGenerationRef = useRef(0);
  const threadRequestGenerationRef = useRef(0);
  const threadPaginationControllerRef = useRef<AbortController | null>(null);
  const searchRequestGenerationRef = useRef(0);
  const searchPaginationControllerRef = useRef<AbortController | null>(null);
  const composeAgentsRequestGenerationRef = useRef(0);
  const composeDirectoryReconcileRef = useRef(false);
  const composeDirectorySnapshotRef = useRef<{
    projectGeneration: string;
    projectId: number;
  } | null>(null);
  const composeAttemptRef = useRef<DeliveryAttempt | null>(null);
  const replyAttemptRef = useRef<Map<string, string>>(new Map());
  const localeChangeBusyRef = useRef(false);
  const mailRouteActive =
    mailNavigation.some((item) => route.view === item) ||
    route.view === "message" ||
    route.view === "thread" ||
    route.view === "compose";
  const routeProjectId =
    route.view === "inbox" ||
    route.view === "message" ||
    route.view === "search" ||
    route.view === "thread"
      ? route.projectId
      : null;
  const routeMessageId = route.view === "message" ? route.messageId : null;
  const routeThreadId = route.view === "thread" ? route.threadId : null;

  const applyLocale = useCallback(
    async (nextLocale: SupportedLocale) => {
      await loadLocale(nextLocale);
      setLocale(nextLocale);
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
    if (route.view !== "search") {
      return;
    }
    setSearchQuery(route.query);
    setSearchProjectId(
      route.projectId === null ? "" : String(route.projectId),
    );
    setSearchScope(route.scope);
    setSearchOrder(route.order);
  }, [route]);

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

  // Sound one tone per arrival, in the voice of whoever wrote.
  //
  // The stream frame deliberately carries no sender — so that a project watcher
  // cannot learn who a BCC went to — which leaves the re-rendered list as the
  // only honest source. Comparing the newest id is enough: the list is ordered
  // newest first, and a refresh that changes nothing must stay silent.
  //
  // The tone rides on the message. It was first read from the recipient
  // directory, which is fetched only on the compose route and cleared
  // everywhere else, so on the inbox the map was always empty and every sender
  // sounded like the default.
  useEffect(() => {
    const newest = messages[0];
    if (!newest) {
      return;
    }
    const previous = newestMessageIdRef.current;
    newestMessageIdRef.current = newest.id;
    if (previous === null || newest.id === previous) {
      return;
    }
    playNotificationTone(newest.sender_notify_sound);
  }, [messages]);

  useEffect(() => {
    if (route.view !== "reservations") {
      return undefined;
    }
    const controller = new AbortController();
    setReservationsStatus("loading");
    void loadReservations({
      signal: controller.signal,
      projectId: route.projectId,
    })
      .then((page) => {
        setReservations(page.items);
        setReservationsCursor(page.next_cursor);
        setReservationsStatus("ready");
      })
      .catch((error: unknown) => {
        // `dataFailureStatus` returns null for an abort: navigating away must
        // not flash an error on a view the reader has already left. An earlier
        // version wrote `?? "error"` here and did exactly that.
        const status = dataFailureStatus(error);
        /* v8 ignore next -- the request layer aborts for real in a browser, but
           the test double resolves rather than rejecting, so the null arm cannot
           be reached from here */
        if (status !== null) {
          setReservationsStatus(status);
        }
      });
    return () => controller.abort();
    // `refreshVersion` is included so the same server-sent event that refreshes
    // the inbox also refreshes this view; claims change on the same edits.
  }, [route, refreshVersion, dataFailureStatus]);

  useEffect(() => {
    const detailRequestGeneration = ++detailRequestGenerationRef.current;
    ++inboxRequestGenerationRef.current;
    paginationControllerRef.current?.abort();
    paginationControllerRef.current = null;
    if (!mailRouteActive) {
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

  useEffect(() => {
    const requestGeneration = ++threadRequestGenerationRef.current;
    threadPaginationControllerRef.current?.abort();
    threadPaginationControllerRef.current = null;
    setThreadPaginationStatus("idle");
    if (routeThreadId === null || routeProjectId === null) {
      setThreadMessages([]);
      setThreadSubject("");
      setThreadTotal(0);
      setThreadNextCursor(null);
      setThreadStatus("idle");
      return undefined;
    }
    const controller = new AbortController();
    setThreadMessages([]);
    setThreadSubject("");
    setThreadTotal(0);
    setThreadNextCursor(null);
    setThreadStatus("loading");
    void loadThread(routeProjectId, routeThreadId, {
      signal: controller.signal,
    })
      .then((page) => {
        if (requestGeneration !== threadRequestGenerationRef.current) {
          return;
        }
        setThreadMessages(mergeThreadMessages([], page.items));
        setThreadSubject(page.subject);
        setThreadTotal(page.total);
        setThreadNextCursor(page.next_cursor);
        setThreadStatus("ready");
      })
      .catch((error: unknown) => {
        if (requestGeneration !== threadRequestGenerationRef.current) {
          return;
        }
        const status = dataFailureStatus(error);
        if (status !== null) {
          setThreadStatus(status);
        }
      });
    return () => controller.abort();
  }, [dataFailureStatus, refreshVersion, routeProjectId, routeThreadId]);

  useEffect(() => {
    const requestGeneration = ++searchRequestGenerationRef.current;
    searchPaginationControllerRef.current?.abort();
    setSearchPaginationStatus("idle");
    if (route.view !== "search" || route.query.trim() === "") {
      setSearchResults([]);
      setSearchNextCursor(null);
      setSearchErrorCode(null);
      setSearchStatus("idle");
      return undefined;
    }
    const controller = new AbortController();
    setSearchResults([]);
    setSearchNextCursor(null);
    setSearchErrorCode(null);
    setSearchStatus("loading");
    void loadSearch({
      query: route.query,
      projectId: route.projectId ?? undefined,
      scope: route.scope,
      order: route.order,
      signal: controller.signal,
    })
      .then((page) => {
        if (requestGeneration !== searchRequestGenerationRef.current) {
          return;
        }
        setSearchResults(page.items);
        setSearchNextCursor(page.next_cursor);
        setSearchStatus("ready");
      })
      .catch((error: unknown) => {
        if (requestGeneration !== searchRequestGenerationRef.current) {
          return;
        }
        const status = dataFailureStatus(error);
        if (status === null) {
          return;
        }
        if (status === "unauthorized") {
          setSearchStatus("unauthorized");
          return;
        }
        setSearchErrorCode(
          error instanceof MailHttpError &&
            (error.status === 422 || error.code === "invalid_search_query")
            ? "invalid"
            : error instanceof MailHttpError &&
                (error.status === 503 || error.code === "search_unavailable")
              ? "unavailable"
              : "generic",
        );
        setSearchStatus("error");
      });
    return () => controller.abort();
  }, [dataFailureStatus, refreshVersion, route]);

  useEffect(() => {
    const requestGeneration = ++composeAgentsRequestGenerationRef.current;
    if (route.view !== "compose" || composeProjectId === "") {
      setComposeAgents([]);
      setComposeAgentsTotal(0);
      setComposeProjectGeneration(null);
      setComposeAgentsStatus("idle");
      setComposeDirectoryNotice(null);
      setComposeConfirmation(null);
      composeDirectorySnapshotRef.current = null;
      composeDirectoryReconcileRef.current = false;
      return undefined;
    }
    const controller = new AbortController();
    const projectId = Number(composeProjectId);
    const reconcile = composeDirectoryReconcileRef.current;
    setComposeAgentsStatus("loading");
    void loadProjectAgents(projectId, { signal: controller.signal })
      .then((page) => {
        if (requestGeneration !== composeAgentsRequestGenerationRef.current) {
          return;
        }
        const previousSnapshot = composeDirectorySnapshotRef.current;
        setComposeAgents(page.items);
        setComposeAgentsTotal(page.total);
        setComposeProjectGeneration(page.project_generation);
        if (
          reconcile &&
          previousSnapshot?.projectId === projectId &&
          previousSnapshot.projectGeneration === page.project_generation
        ) {
          const currentAgents = new Set(page.items.map(recipientSelectionKey));
          setComposeRecipients((current) =>
            current.filter((selectionKey) => currentAgents.has(selectionKey)),
          );
        } else {
          setComposeRecipients([]);
        }
        composeDirectorySnapshotRef.current = {
          projectGeneration: page.project_generation,
          projectId,
        };
        composeDirectoryReconcileRef.current = false;
        setComposeDirectoryNotice(reconcile ? "refreshed" : null);
        setComposeAgentsStatus("ready");
      })
      .catch((error: unknown) => {
        if (requestGeneration !== composeAgentsRequestGenerationRef.current) {
          return;
        }
        const status = dataFailureStatus(error);
        if (status !== null) {
          setComposeAgentsStatus(status);
          if (reconcile) {
            setComposeDirectoryNotice("refreshError");
            composeDirectoryReconcileRef.current = false;
          }
        }
      });
    return () => controller.abort();
  }, [
    composeDirectoryRefreshVersion,
    composeProjectId,
    dataFailureStatus,
    route.view,
  ]);

  useEffect(
    () => () => {
      paginationControllerRef.current?.abort();
      threadPaginationControllerRef.current?.abort();
      searchPaginationControllerRef.current?.abort();
    },
    [],
  );

  useEffect(() => {
    setReplyBody("");
    setReplyMode("edit");
    setReplyStatus("idle");
    setReplyDelivery(null);
    setReplyConfirmation(null);
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

  const handleLocaleChange = async (nextLocale: SupportedLocale) => {
    if (localeChangeBusyRef.current || nextLocale === locale) {
      return;
    }
    localeChangeBusyRef.current = true;
    // The correspondence locale can inherit this UI preference. Once the user
    // changes it, an open confirmation is no longer guaranteed to match the
    // server-added preamble, so return to the preserved draft first.
    setComposeConfirmation(null);
    setReplyConfirmation(null);
    const previousLocale = locale;
    const persistenceUnavailable = preferenceStatus === "loadError";
    setPreferenceStatus("saving");
    try {
      await prepareLocaleCatalog(nextLocale);
      if (persistenceUnavailable) {
        await applyLocale(nextLocale);
        setPreferenceStatus("loadError");
        return;
      }
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
    } finally {
      localeChangeBusyRef.current = false;
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

  const handleSearchSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const next: MailRoute = {
      view: "search",
      query: searchQuery.trim(),
      projectId: searchProjectId === "" ? null : Number(searchProjectId),
      scope: searchScope,
      order: searchOrder,
    };
    window.history.pushState({}, "", mailRouteHash(next));
    setRoute(next);
  };

  const handleLoadMore = async (cursor: string) => {
    paginationControllerRef.current?.abort();
    const controller = new AbortController();
    const requestGeneration = inboxRequestGenerationRef.current;
    paginationControllerRef.current = controller;
    setPaginationStatus("loading");
    try {
      const page = await loadInbox({
        cursor,
        projectId: routeProjectId ?? undefined,
        signal: controller.signal,
      });
      if (requestGeneration !== inboxRequestGenerationRef.current) {
        return;
      }
      setMessages((current) => mergeMessages(current, page.items));
      setMessageTotal(page.total);
      setNextCursor(page.next_cursor);
      setPaginationStatus("idle");
    } catch (error) {
      if (requestGeneration !== inboxRequestGenerationRef.current) {
        return;
      }
      const status = dataFailureStatus(error);
      if (status === "unauthorized") {
        setInboxStatus("unauthorized");
        setPaginationStatus("idle");
      } else if (status === "error") {
        setPaginationStatus("error");
      }
    }
  };

  const handleSearchLoadMore = async (
    cursor: string,
    activeRoute: Extract<MailRoute, { view: "search" }>,
  ) => {
    searchPaginationControllerRef.current?.abort();
    const controller = new AbortController();
    searchPaginationControllerRef.current = controller;
    setSearchPaginationStatus("loading");
    try {
      const page = await loadSearch({
        query: activeRoute.query,
        projectId: activeRoute.projectId ?? undefined,
        scope: activeRoute.scope,
        order: activeRoute.order,
        cursor,
        signal: controller.signal,
      });
      setSearchResults((current) => mergeMessages(current, page.items));
      setSearchNextCursor(page.next_cursor);
      setSearchPaginationStatus("idle");
    } catch (error) {
      const status = dataFailureStatus(error);
      if (status === "unauthorized") {
        setSearchStatus("unauthorized");
        setSearchPaginationStatus("idle");
      } else if (status === "error") {
        setSearchPaginationStatus("error");
      }
    }
  };

  const handleThreadLoadMore = async (
    cursor: string,
    activeRoute: Extract<MailRoute, { view: "thread" }>,
  ) => {
    threadPaginationControllerRef.current?.abort();
    const controller = new AbortController();
    const requestGeneration = threadRequestGenerationRef.current;
    threadPaginationControllerRef.current = controller;
    setThreadPaginationStatus("loading");
    try {
      const page = await loadThread(
        activeRoute.projectId,
        activeRoute.threadId,
        { cursor, signal: controller.signal },
      );
      if (requestGeneration !== threadRequestGenerationRef.current) {
        return;
      }
      setThreadMessages((current) =>
        mergeThreadMessages(current, page.items),
      );
      setThreadSubject(page.subject);
      setThreadTotal(page.total);
      setThreadNextCursor(page.next_cursor);
      setThreadPaginationStatus("idle");
    } catch (error) {
      if (requestGeneration !== threadRequestGenerationRef.current) {
        return;
      }
      const status = dataFailureStatus(error);
      if (status === "unauthorized") {
        setThreadStatus("unauthorized");
        setThreadPaginationStatus("idle");
      } else if (status === "error") {
        setThreadPaginationStatus("error");
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

  const handleComposeProjectChange = (event: ChangeEvent<HTMLSelectElement>) => {
    setComposeProjectId(event.target.value);
    setComposeRecipients([]);
    setComposeRecipientQuery("");
    setComposeAgents([]);
    setComposeAgentsTotal(0);
    setComposeProjectGeneration(null);
    setComposeAgentsStatus("idle");
    setComposeConfirmation(null);
    setComposeDirectoryNotice(null);
    composeAttemptRef.current = null;
    composeDirectoryReconcileRef.current = false;
    composeDirectorySnapshotRef.current = null;
  };

  const handleRecipientToggle = (key: string, checked: boolean) => {
    setComposeConfirmation(null);
    setComposeRecipients((current) =>
      checked ? [...current, key] : current.filter((candidate) => candidate !== key),
    );
  };

  const handleComposeSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const projectId = Number(composeProjectId);
    const selectedRecipients = new Set(composeRecipients);
    const selectedAgents = composeAgents.filter((agent) =>
      selectedRecipients.has(recipientSelectionKey(agent)),
    );
    const recipientReferences = selectedAgents.map((agent) => ({
        agent_id: agent.agent_id,
        expected_agent_generation: agent.agent_generation,
      }));
    if (
      !Number.isSafeInteger(projectId) ||
      projectId < 1 ||
      composeAgentsStatus !== "ready" ||
      composeProjectGeneration === null ||
      recipientReferences.length === 0 ||
      composeSubject.trim() === "" ||
      composeBody.trim() === ""
    ) {
      setComposeStatus("error");
      return;
    }
    const correspondenceLocale =
      preferences?.effective.correspondence_locale ?? null;
    const threadId =
      composeThreadId.trim() === "" ? null : composeThreadId.trim();
    const canonicalInput = {
      projectId,
      expected_project_generation: composeProjectGeneration,
      recipients: recipientReferences,
      subject: composeSubject,
      body_md: composeBody,
      thread_id: threadId,
      correspondence_locale: correspondenceLocale,
    };
    setComposeDelivery(null);
    setComposeStatus("idle");
    setComposeConfirmation({
      bodyMd: composeBody,
      correspondenceLocale,
      expectedProjectGeneration: composeProjectGeneration,
      fingerprint: JSON.stringify(canonicalInput),
      projectId,
      projectName:
        projects.find((project) => project.id === projectId)?.human_key ??
        String(projectId),
      recipientReferences,
      recipients: selectedAgents.map((agent) => ({
        canonicalName: agent.name,
        displayName: agent.display_name ?? agent.name,
      })),
      subject: composeSubject,
      threadId,
    });
  };

  const confirmCompose = async (confirmation: ComposeConfirmation) => {
    const idempotencyKey = idempotencyKeyFor(
      composeAttemptRef,
      confirmation.fingerprint,
    );
    setComposeStatus("sending");
    try {
      const delivery = await composeMessage(confirmation.projectId, {
        idempotency_key: idempotencyKey,
        expected_project_generation: confirmation.expectedProjectGeneration,
        recipients: confirmation.recipientReferences,
        subject: confirmation.subject,
        body_md: confirmation.bodyMd,
        thread_id: confirmation.threadId,
      });
      setComposeDelivery(delivery);
      setComposeConfirmation(null);
      setComposeStatus("idle");
      if (delivery.status === "published") {
        setComposeRecipients([]);
        setComposeRecipientQuery("");
        setComposeSubject("");
        setComposeBody("");
        setComposeMode("edit");
        setComposeThreadId("");
        composeAttemptRef.current = null;
        setRefreshVersion((version) => version + 1);
      }
    } catch (error) {
      if (isRecipientLifetimeConflict(error)) {
        setComposeConfirmation(null);
        setComposeStatus("idle");
        setComposeAgentsStatus("loading");
        setComposeDirectoryNotice("refreshing");
        composeAttemptRef.current = null;
        composeDirectoryReconcileRef.current = true;
        setComposeDirectoryRefreshVersion((version) => version + 1);
        return;
      }
      setComposeStatus(deliveryFailureStatus(error));
    }
  };

  const handleReplySubmit = (
    event: FormEvent<HTMLFormElement>,
    message: MessageDetail,
  ) => {
    event.preventDefault();
    if (replyBody.trim() === "" || message.reply_target === null) {
      setReplyStatus("error");
      return;
    }
    const correspondenceLocale =
      preferences?.effective.correspondence_locale ?? null;
    const subject = (
      message.subject.toLowerCase().startsWith("re:")
        ? message.subject
        : `Re: ${message.subject}`
    ).slice(0, 200);
    const threadId = message.thread_id ?? String(message.id);
    const recipients = [{
      canonicalName: message.reply_target.canonical_name,
      displayName: message.sender_display_name ?? message.sender,
    }];
    const canonicalInput = {
      projectId: message.project_id,
      messageId: message.id,
      body_md: replyBody,
      correspondence_locale: correspondenceLocale,
      reply_target: {
        agent_id: message.reply_target.agent_id,
        agent_generation: message.reply_target.agent_generation,
        project_id: message.reply_target.project_id,
        project_generation: message.reply_target.project_generation,
      },
      subject,
      thread_id: threadId,
    };
    setReplyDelivery(null);
    setReplyStatus("idle");
    setReplyConfirmation({
      bodyMd: replyBody,
      correspondenceLocale,
      fingerprint: JSON.stringify(canonicalInput),
      messageId: message.id,
      projectId: message.project_id,
      projectName: message.reply_target.canonical_name,
      recipients,
      replyTarget: message.reply_target,
      subject,
      threadId,
    });
  };

  const confirmReply = async (confirmation: ReplyConfirmation) => {
    const idempotencyKey = replyIdempotencyKeyFor(
      replyAttemptRef.current,
      confirmation.fingerprint,
    );
    setReplyStatus("sending");
    try {
      const delivery = await replyToMessage(
        confirmation.projectId,
        confirmation.messageId,
        {
          idempotency_key: idempotencyKey,
          expected_sender_agent_id: confirmation.replyTarget.agent_id,
          expected_sender_agent_generation:
            confirmation.replyTarget.agent_generation,
          expected_sender_project_id: confirmation.replyTarget.project_id,
          expected_sender_project_generation:
            confirmation.replyTarget.project_generation,
          body_md: confirmation.bodyMd,
        },
      );
      setReplyDelivery(delivery);
      setReplyConfirmation(null);
      setReplyStatus("idle");
      if (delivery.status === "published") {
        setReplyBody("");
        setReplyMode("edit");
        replyAttemptRef.current.delete(confirmation.fingerprint);
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
      // Operators see it too, not only global administrators: the endpoint
      // authorizes per project, and a member with an `operator` assignment on
      // any project has something to look at.
      ...(profile?.global_role === "admin" ||
      projects.some((project) => project.role !== "viewer")
        ? (["reservations"] as const)
        : []),
      ...(profile?.global_role === "admin" ? (["compose"] as const) : []),
      "account",
      ...(profile?.global_role === "admin" ? (["admin"] as const) : []),
    ],
    [profile?.global_role, projects],
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

  // Reservation timestamps do not carry an offset. The server normalises them to
  // `YYYY-MM-DD HH:MM:SS.ffffff` (see `_mail_ui_optional_timestamp_key`) from a
  // value produced by `datetime.now(timezone.utc).replace(tzinfo=None)`, so the
  // instant is UTC while the text says nothing about it. `new Date()` reads that
  // shape as LOCAL time, which would render every expiry shifted by the viewer's
  // offset — an hour that looks entirely plausible and is simply wrong. Message
  // timestamps are unaffected: those arrive with an explicit `+00:00`.
  const formatUtcNaiveDate = (timestamp: string) => {
    const normalised = `${timestamp.replace(" ", "T")}Z`;
    const parsed = new Date(normalised);
    return Number.isNaN(parsed.getTime())
      ? timestamp
      : new Intl.DateTimeFormat(locale, {
          dateStyle: "medium",
          timeStyle: "short",
        }).format(parsed);
  };

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
    const normalizedRecipientQuery = composeRecipientQuery.trim().toLocaleLowerCase(locale);
    const filteredAgents = composeAgents.filter((agent) =>
      normalizedRecipientQuery === "" ||
      agent.name.toLocaleLowerCase(locale).includes(normalizedRecipientQuery) ||
      (agent.display_name ?? "").toLocaleLowerCase(locale).includes(normalizedRecipientQuery),
    );
    const selectedRecipients = new Set(composeRecipients);
    const composeIsSending = composeStatus === "sending";
    const composeSubmitDisabled =
      composeIsSending ||
      composeAgentsStatus !== "ready" ||
      composeProjectGeneration === null ||
      composeRecipients.length === 0 ||
      composeSubject.trim() === "" ||
      composeBody.trim() === "";
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
                onChange={handleComposeProjectChange}
                required
                disabled={composeIsSending}
              >
                <option value="">{t("compose.chooseProject")}</option>
                {activeProjects.map((project) => (
                  <option key={project.id} value={project.id}>{project.human_key}</option>
                ))}
              </select>
              <fieldset className="recipient-picker" aria-describedby="compose-recipients-hint">
                <legend>{t("compose.recipients")}</legend>
                <small id="compose-recipients-hint">{t("compose.recipientsHint")}</small>
                {composeProjectId === "" ? (
                  <p className="recipient-picker-state">{t("compose.chooseRecipientsProject")}</p>
                ) : null}
                {composeAgentsStatus === "loading" ? (
                  <p className="recipient-picker-state" role="status">{t("compose.loadingRecipients")}</p>
                ) : null}
                {composeDirectoryNotice !== null ? (
                  <p
                    className={`recipient-picker-state ${composeDirectoryNotice === "refreshError" ? "state-error" : ""}`}
                    role={composeDirectoryNotice === "refreshError" ? "alert" : "status"}
                  >
                    {t(`compose.directory.${composeDirectoryNotice}`)}
                  </p>
                ) : null}
                {composeAgentsStatus === "error" || composeAgentsStatus === "unauthorized" ? (
                  <p className="recipient-picker-state state-error" role="alert">{t("compose.recipientsLoadError")}</p>
                ) : null}
                {composeAgentsStatus === "ready" && composeAgents.length === 0 ? (
                  <p className="recipient-picker-state">{t("compose.noRecipients")}</p>
                ) : null}
                {composeAgentsStatus === "ready" && composeAgents.length > 0 ? (
                  <div className="recipient-picker-controls">
                    <label htmlFor="compose-recipient-search">{t("compose.recipientSearch")}</label>
                    <input
                      id="compose-recipient-search"
                      name="compose-recipient-search"
                      type="search"
                      value={composeRecipientQuery}
                      onChange={(event) => setComposeRecipientQuery(event.target.value)}
                      maxLength={200}
                      disabled={composeIsSending}
                      aria-describedby="compose-recipient-search-hint"
                    />
                    <small id="compose-recipient-search-hint">{t("compose.recipientSearchHint")}</small>
                    <div className="recipient-picker-actions">
                      <button
                        type="button"
                        className="secondary-button"
                        onClick={() => {
                          setComposeConfirmation(null);
                          setComposeRecipients((current) =>
                            Array.from(
                              new Set([
                                ...current,
                                ...filteredAgents.map(recipientSelectionKey),
                              ]),
                            ).slice(0, maximumRecipients),
                          );
                        }}
                        disabled={composeIsSending}
                      >
                        {t("compose.selectAll")}
                      </button>
                      <button
                        type="button"
                        className="secondary-button"
                        onClick={() => {
                          setComposeConfirmation(null);
                          setComposeRecipients([]);
                        }}
                        disabled={composeIsSending || composeRecipients.length === 0}
                      >
                        {t("compose.clearRecipients")}
                      </button>
                    </div>
                    <p className="recipient-selected-count">
                      {t("compose.selectedCount", {
                        count: composeRecipients.length,
                        maximum: maximumRecipients,
                      })}
                    </p>
                    {composeAgentsTotal > maximumRecipients ? (
                      <p className="recipient-limit-notice">
                        {t("compose.recipientLimit", {
                          available: composeAgentsTotal,
                          maximum: maximumRecipients,
                        })}
                      </p>
                    ) : null}
                    {filteredAgents.length === 0 ? (
                      <p className="recipient-picker-state">{t("compose.noRecipientResults")}</p>
                    ) : (
                      <ul className="recipient-options">
                        {filteredAgents.map((agent) => {
                          const selectionKey = recipientSelectionKey(agent);
                          const selected = selectedRecipients.has(selectionKey);
                          return (
                            <li key={selectionKey}>
                              <label>
                                <input
                                  type="checkbox"
                                  name="compose-recipient"
                                  value={agent.name}
                                  checked={selected}
                                  onChange={(event) =>
                                    handleRecipientToggle(selectionKey, event.target.checked)
                                  }
                                  disabled={
                                    composeIsSending ||
                                    (!selected && composeRecipients.length >= maximumRecipients)
                                  }
                                />
                                <span>
                                  <strong>{agent.display_name ?? agent.name}</strong>
                                  {agent.display_name !== null ? (
                                    <code>{agent.name}</code>
                                  ) : null}
                                </span>
                              </label>
                            </li>
                          );
                        })}
                      </ul>
                    )}
                  </div>
                ) : null}
              </fieldset>
              <label htmlFor="compose-subject">{t("compose.subject")}</label>
              <input
                id="compose-subject"
                name="compose-subject"
                value={composeSubject}
                onChange={(event) => {
                  setComposeConfirmation(null);
                  setComposeSubject(event.target.value);
                }}
                maxLength={200}
                required
                disabled={composeIsSending}
              />
              <label htmlFor="compose-thread">{t("compose.thread")}</label>
              <input
                id="compose-thread"
                name="compose-thread"
                value={composeThreadId}
                onChange={(event) => {
                  setComposeConfirmation(null);
                  setComposeThreadId(event.target.value);
                }}
                maxLength={128}
                disabled={composeIsSending}
              />
              <MarkdownComposer
                id="compose-body"
                label={t("compose.body")}
                previewLabel={t("compose.preview")}
                value={composeBody}
                onChange={(value) => {
                  setComposeConfirmation(null);
                  setComposeBody(value);
                }}
                mode={composeMode}
                onModeChange={setComposeMode}
                rows={12}
                disabled={composeIsSending}
                submitDisabled={composeSubmitDisabled}
                components={markdownComponents}
              />
              {composeConfirmation === null ? (
                <button
                  type="submit"
                  className="primary-button"
                  disabled={composeSubmitDisabled}
                >
                  {t("compose.review")}
                </button>
              ) : (
                <DeliveryConfirmation
                  id="compose-confirmation"
                  title={t("confirmation.composeTitle")}
                  headingLevel={2}
                  projectLabel={t("confirmation.project")}
                  projectName={composeConfirmation.projectName}
                  recipients={composeConfirmation.recipients}
                  subject={composeConfirmation.subject}
                  threadId={composeConfirmation.threadId}
                  bodyMd={composeConfirmation.bodyMd}
                  correspondenceLocale={composeConfirmation.correspondenceLocale}
                  components={markdownComponents}
                  disabled={composeIsSending}
                  onBack={() => {
                    setComposeConfirmation(null);
                    setComposeStatus("idle");
                  }}
                  onConfirm={() => void confirmCompose(composeConfirmation)}
                />
              )}
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

  const renderReservations = () => (
    <section aria-labelledby="reservations-heading">
      <div className="page-heading">
        <div>
          <p className="eyebrow">{t("nav.reservations")}</p>
          <h1 id="reservations-heading">{t("reservations.title")}</h1>
          <p>{t("reservations.subtitle")}</p>
        </div>
      </div>
      {reservationsStatus === "loading" ? (
        <p className="state-panel" role="status">{t("reservations.title")}</p>
      ) : null}
      {reservationsStatus === "error" ? (
        <p className="state-panel state-error" role="alert">{t("errors.projects")}</p>
      ) : null}
      {reservationsStatus === "unauthorized" ? (
        <p className="state-panel" role="status">{t("errors.unauthorized")}</p>
      ) : null}
      {reservationsStatus === "ready" && reservations.length === 0 ? (
        <p className="state-panel">{t("reservations.empty")}</p>
      ) : null}
      {reservationsStatus === "ready" && reservations.length > 0 ? (
        <div className="reservation-table-wrap">
          <table className="reservation-table">
            <thead>
              <tr>
                <th scope="col">{t("reservations.path")}</th>
                <th scope="col">{t("reservations.holder")}</th>
                <th scope="col">{t("reservations.state")}</th>
                <th scope="col">{t("reservations.expires")}</th>
              </tr>
            </thead>
            <tbody>
              {reservations.map((claim) => (
                <tr key={claim.id}>
                  <td>
                    <code className="reservation-path">{claim.path_pattern}</code>
                    <small className="reservation-meta">
                      {claim.project_slug}
                      {" · "}
                      {claim.exclusive
                        ? t("reservations.exclusive")
                        : t("reservations.shared")}
                      {claim.reason === "" ? null : ` · ${claim.reason}`}
                    </small>
                  </td>
                  <td>
                    {claim.holder_display_name ??
                      claim.holder_name ??
                      t("reservations.unknownHolder")}
                    {claim.holder_display_name !== null &&
                    claim.holder_name !== null ? (
                      <small className="reservation-meta">{claim.holder_name}</small>
                    ) : null}
                  </td>
                  <td>
                    <span
                      className="reservation-state"
                      data-state={claim.scope_state}
                    >
                      {claim.scope_state === "execution_scoped"
                        ? t("reservations.stateScoped")
                        : claim.scope_state === "legacy_unscoped"
                          ? t("reservations.stateLegacy")
                          : t("reservations.stateOrphaned")}
                    </span>
                  </td>
                  <td>
                    {claim.expires_ts === null ? (
                      t("reservations.unknownExpiry")
                    ) : (
                      <time dateTime={`${claim.expires_ts.replace(" ", "T")}Z`}>
                        {formatUtcNaiveDate(claim.expires_ts)}
                      </time>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {reservationsCursor === null ? null : (
        <p className="state-panel">{t("reservations.loadMore")}</p>
      )}
    </section>
  );

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
            const threadId = message.thread_id ?? String(message.id);
            const threadHash = mailThreadRouteHash(message.project_id, threadId);
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
                {threadHash === null ? null : (
                  <a className="message-thread-link" href={threadHash}>
                    {t("confirmation.thread")}: {threadId}
                  </a>
                )}
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

  const renderSearch = (activeRoute: Extract<MailRoute, { view: "search" }>) => (
    <section aria-labelledby="search-heading">
      <div className="page-heading search-heading">
        <div>
          <p className="eyebrow">{t("search.eyebrow")}</p>
          <h1 id="search-heading">{t("search.title")}</h1>
          <p>{t("search.hint")}</p>
        </div>
      </div>
      <form className="search-form" role="search" onSubmit={handleSearchSubmit}>
        <div className="search-query-field">
          <label htmlFor="message-search">{t("search.query")}</label>
          <input
            id="message-search"
            name="q"
            type="search"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
            minLength={1}
            maxLength={256}
            required
            aria-describedby="message-search-hint"
          />
          <small id="message-search-hint">{t("search.queryHint")}</small>
        </div>
        <div className="search-options">
          <label htmlFor="search-project">{t("search.project")}</label>
          <select
            id="search-project"
            name="project"
            value={searchProjectId}
            onChange={(event) => setSearchProjectId(event.target.value)}
            disabled={projectsStatus !== "ready"}
          >
            <option value="">{t("search.allProjects")}</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.human_key}
              </option>
            ))}
          </select>
          <label htmlFor="search-scope">{t("search.scope")}</label>
          <select
            id="search-scope"
            name="scope"
            value={searchScope}
            onChange={(event) =>
              setSearchScope(event.target.value as SearchScope)
            }
          >
            <option value="all">{t("search.scopeAll")}</option>
            <option value="subject">{t("search.scopeSubject")}</option>
            <option value="body">{t("search.scopeBody")}</option>
          </select>
          <label htmlFor="search-order">{t("search.order")}</label>
          <select
            id="search-order"
            name="order"
            value={searchOrder}
            onChange={(event) =>
              setSearchOrder(event.target.value as SearchOrder)
            }
          >
            <option value="relevance">{t("search.orderRelevance")}</option>
            <option value="newest">{t("search.orderNewest")}</option>
          </select>
          <button
            type="submit"
            className="primary-button"
            disabled={searchQuery.trim() === ""}
          >
            {t("search.submit")}
          </button>
        </div>
      </form>
      {activeRoute.query.trim() === "" ? (
        <p className="state-panel">{t("search.prompt")}</p>
      ) : null}
      {searchStatus === "loading" ? (
        <p className="state-panel" role="status">{t("search.loading")}</p>
      ) : null}
      {searchStatus === "error" ? (
        <p className="state-panel state-error" role="alert">
          {t(
            searchErrorCode === "invalid"
              ? "search.invalid"
              : searchErrorCode === "unavailable"
                ? "search.unavailable"
                : "search.error",
          )}
        </p>
      ) : null}
      {searchStatus === "unauthorized" ? (
        <p className="state-panel" role="status">{t("errors.unauthorized")}</p>
      ) : null}
      {searchStatus === "ready" && searchResults.length === 0 ? (
        <p className="state-panel">{t("search.empty")}</p>
      ) : null}
      {searchStatus === "ready" && searchResults.length > 0 ? (
        <ul className="panel message-list search-result-list">
          {searchResults.map((message) => {
            const sender = message.sender_display_name ?? message.sender_name;
            const senderIdentity =
              message.sender === sender ? "" : ` · ${message.sender}`;
            const project =
              projectNames.get(message.project_id) ?? message.project_slug;
            const threadId = message.thread_id ?? String(message.id);
            const threadHash = mailThreadRouteHash(message.project_id, threadId);
            return (
              <li key={message.id}>
                <a
                  className="message-row search-result-row"
                  href={mailRouteHash({
                    view: "message",
                    projectId: message.project_id,
                    messageId: message.id,
                  })}
                  aria-label={t("search.openMessage", {
                    subject: message.subject,
                  })}
                >
                  <span
                    className={`importance-mark importance-${message.importance}`}
                    aria-hidden="true"
                  />
                  <span className="message-copy">
                    <strong>{message.subject}</strong>
                    <small>{t("inbox.from", { sender })}{senderIdentity}</small>
                    <small>{t("inbox.project", { project })}</small>
                    {message.snippet !== "" ? (
                      <span className="search-snippet">{message.snippet}</span>
                    ) : null}
                  </span>
                  <span className="message-meta">
                    <span className={`status importance-${message.importance}`}>
                      {t(`importance.${message.importance}`)}
                    </span>
                    <time dateTime={message.created_ts}>
                      {formatDate(message.created_ts)}
                    </time>
                  </span>
                </a>
                {threadHash === null ? null : (
                  <a className="message-thread-link" href={threadHash}>
                    {t("confirmation.thread")}: {threadId}
                  </a>
                )}
              </li>
            );
          })}
        </ul>
      ) : null}
      {searchStatus === "ready" && searchNextCursor !== null ? (
        <div className="load-more-area">
          <button
            type="button"
            className="primary-button"
            onClick={() => void handleSearchLoadMore(searchNextCursor, activeRoute)}
            disabled={searchPaginationStatus === "loading"}
          >
            {t(
              searchPaginationStatus === "loading"
                ? "search.loadingMore"
                : "search.loadMore",
            )}
          </button>
          {searchPaginationStatus === "error" ? (
            <p role="alert">{t("search.loadMoreError")}</p>
          ) : null}
        </div>
      ) : null}
    </section>
  );

  const renderThread = (
    activeRoute: Extract<MailRoute, { view: "thread" }>,
  ) => {
    const inboxHash = mailRouteHash({
      view: "inbox",
      projectId: activeRoute.projectId,
    });
    const project = projectNames.get(activeRoute.projectId);
    const heading = threadSubject === "" ? activeRoute.threadId : threadSubject;
    return (
      <section aria-labelledby="thread-heading">
        <a className="back-link" href={inboxHash}>← {t("message.back")}</a>
        <div className="page-heading thread-heading">
          <div>
            <p className="eyebrow">{t("confirmation.thread")}</p>
            <h1 id="thread-heading">{heading}</h1>
            <p className="thread-identifier">
              <strong>{t("confirmation.thread")}:</strong>{" "}
              <code>{activeRoute.threadId}</code>
            </p>
            {project === undefined ? null : (
              <p>{t("inbox.project", { project })}</p>
            )}
          </div>
          {threadStatus === "ready" ? (
            <span className="count-pill">
              {t("inbox.count", { count: threadTotal })}
            </span>
          ) : null}
        </div>
        {threadStatus === "loading" ? (
          <p className="state-panel" role="status">{t("message.loading")}</p>
        ) : null}
        {threadStatus === "error" ? (
          <p className="state-panel state-error" role="alert">
            {t("errors.message")}
          </p>
        ) : null}
        {threadStatus === "unauthorized" ? (
          <p className="state-panel" role="status">{t("errors.unauthorized")}</p>
        ) : null}
        {threadStatus === "ready" && threadMessages.length === 0 ? (
          <p className="state-panel">{t("inbox.empty")}</p>
        ) : null}
        {threadStatus === "ready" && threadMessages.length > 0 ? (
          <ol className="thread-list">
            {threadMessages.map((message, index) => {
              const sender = message.sender_display_name ?? message.sender_name;
              const senderIdentity =
                message.sender === sender ? "" : ` · ${message.sender}`;
              return (
                <li key={message.id}>
                  <CollapsibleThreadMessage
                    defaultOpen={index === threadMessages.length - 1}
                    subject={message.subject}
                    senderLabel={`${t("inbox.from", { sender })}${senderIdentity}`}
                    importance={message.importance}
                    importanceLabel={t(`importance.${message.importance}`)}
                    createdTs={message.created_ts}
                    formattedDate={formatDate(message.created_ts)}
                  >
                    <article className="message-detail">
                      <header>
                        <a
                          className="thread-detail-link"
                          href={mailRouteHash({
                            view: "message",
                            projectId: message.project_id,
                            messageId: message.id,
                          })}
                        >
                          {t("inbox.openMessage", { subject: message.subject })}
                        </a>
                      </header>
                      <dl className="message-facts">
                        <div>
                          <dt>{t("message.to")}</dt>
                          <dd>
                            {message.to.length > 0
                              ? message.to.join(", ")
                              : t("message.emptyRecipients")}
                          </dd>
                        </div>
                        <div>
                          <dt>{t("message.cc")}</dt>
                          <dd>
                            {message.cc.length > 0
                              ? message.cc.join(", ")
                              : t("message.emptyRecipients")}
                          </dd>
                        </div>
                      </dl>
                      <div className="message-body">
                        <SafeMarkdown
                          body={message.body_md}
                          components={markdownComponents}
                        />
                      </div>
                      {message.attachments.length > 0 ? (
                        <section
                          className="attachment-panel"
                          aria-labelledby={`thread-attachments-${message.id}`}
                        >
                          <h3 id={`thread-attachments-${message.id}`}>
                            {t("message.attachments")}
                          </h3>
                          <p>
                            {t("message.attachmentCount", {
                              count: message.attachments.length,
                            })}
                          </p>
                          <ul>
                            {message.attachments.map((attachment, itemIndex) => (
                              <li key={`${attachment.type ?? "attachment"}-${itemIndex}`}>
                                {attachmentText(attachment)}
                              </li>
                            ))}
                          </ul>
                        </section>
                      ) : null}
                    </article>
                  </CollapsibleThreadMessage>
                </li>
              );
            })}
          </ol>
        ) : null}
        {threadStatus === "ready" && threadNextCursor !== null ? (
          <div className="load-more-area">
            <button
              type="button"
              className="primary-button"
              onClick={() =>
                void handleThreadLoadMore(threadNextCursor, activeRoute)
              }
              disabled={threadPaginationStatus === "loading"}
            >
              {t(
                threadPaginationStatus === "loading"
                  ? "inbox.loadingMore"
                  : "inbox.loadMore",
              )}
            </button>
            {threadPaginationStatus === "error" ? (
              <p role="alert">{t("errors.loadMore")}</p>
            ) : null}
          </div>
        ) : null}
      </section>
    );
  };

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
    const detailThreadId =
      currentDetail === null
        ? null
        : currentDetail.thread_id ?? String(currentDetail.id);
    const detailThreadHash =
      currentDetail === null || detailThreadId === null
        ? null
        : mailThreadRouteHash(currentDetail.project_id, detailThreadId);
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
              <div>
                <dt>{t("confirmation.thread")}</dt>
                <dd>
                  {detailThreadHash === null ? detailThreadId : (
                    <a className="thread-inline-link" href={detailThreadHash}>
                      {detailThreadId}
                    </a>
                  )}
                </dd>
              </div>
            </dl>
            <div className="message-body">
              <SafeMarkdown body={currentDetail.body_md} components={markdownComponents} />
            </div>
            {currentDetail.can_reply ? (
              <section className="reply-panel" aria-labelledby="reply-heading">
                <h2 id="reply-heading">{t("reply.title")}</h2>
                <p>{t("reply.hint")}</p>
                <form
                  className="delivery-form"
                  onSubmit={(event) => void handleReplySubmit(event, currentDetail)}
                >
                  <MarkdownComposer
                    id="reply-body"
                    label={t("reply.body")}
                    previewLabel={t("reply.preview")}
                    value={replyBody}
                    onChange={(value) => {
                      setReplyConfirmation(null);
                      setReplyBody(value);
                    }}
                    mode={replyMode}
                    onModeChange={setReplyMode}
                    rows={8}
                    disabled={replyStatus === "sending"}
                    submitDisabled={
                      replyStatus === "sending" || replyBody.trim() === ""
                    }
                    components={markdownComponents}
                  />
                  {replyConfirmation === null ? (
                    <button
                      type="submit"
                      className="primary-button"
                      disabled={replyStatus === "sending" || replyBody.trim() === ""}
                    >
                      {t("reply.review")}
                    </button>
                  ) : (
                    <DeliveryConfirmation
                      id="reply-confirmation"
                      title={t("confirmation.replyTitle")}
                      headingLevel={3}
                      projectLabel={t("confirmation.targetRoute")}
                      projectName={replyConfirmation.projectName}
                      recipients={replyConfirmation.recipients}
                      subject={replyConfirmation.subject}
                      threadId={replyConfirmation.threadId}
                      bodyMd={replyConfirmation.bodyMd}
                      correspondenceLocale={replyConfirmation.correspondenceLocale}
                      components={markdownComponents}
                      disabled={replyStatus === "sending"}
                      onBack={() => {
                        setReplyConfirmation(null);
                        setReplyStatus("idle");
                      }}
                      onConfirm={() => void confirmReply(replyConfirmation)}
                    />
                  )}
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
                onChange={(event) =>
                  void handleLocaleChange(event.target.value as SupportedLocale)
                }
                disabled={["loading", "saving", "unauthorized"].includes(
                  preferenceStatus,
                )}
              >
                {supportedLocales.map((supportedLocale) => (
                  <option key={supportedLocale} value={supportedLocale}>
                    {localeMetadata[supportedLocale].flag}{" "}
                    {localeMetadata[supportedLocale].nativeName}
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
                    {localeMetadata[supportedLocale].flag}{" "}
                    {localeMetadata[supportedLocale].nativeName}
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
            🌈
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
          {/* Browsers refuse audio until a gesture inside the page, and arriving
              from the login form does not count — so the reader arms it here,
              once, and the choice persists. */}
          <button
            className="sound-toggle"
            type="button"
            data-on={soundOn ? "yes" : "no"}
            aria-pressed={soundOn}
            aria-label={t(
              soundOn ? "notificationSoundOn" : "notificationSoundOff",
            )}
            title={t(soundOn ? "notificationSoundOn" : "notificationSoundOff")}
            onClick={() => {
              const next = !soundOn;
              setSoundEnabled(next);
              setSoundOn(next);
              // Play the default tone on enabling: it doubles as the gesture the
              // browser wants and as proof to the reader that audio works here.
              if (next) {
                playNotificationTone();
              }
            }}
          >
            <span aria-hidden="true">{soundOn ? "🔊" : "🔇"}</span>
          </button>
          <div className="locale-control">
            <span className="locale-control-label">{t("language")}</span>
            <LocalePicker
              locale={locale}
              describedBy="locale-preference-status"
              disabled={["loading", "saving", "unauthorized"].includes(
                preferenceStatus,
              )}
              onSelect={(nextLocale) => void handleLocaleChange(nextLocale)}
            />
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
                  (route.view === item ||
                  ((route.view === "message" || route.view === "thread") &&
                    item === "inbox"))
                    ? "nav-link is-active"
                    : "nav-link"
                }
                href={`#${item}`}
                key={item}
                aria-current={
                  route.view === item ||
                  ((route.view === "message" || route.view === "thread") &&
                    item === "inbox")
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
          {route.view === "search" ? renderSearch(route) : null}
          {route.view === "reservations" ? renderReservations() : null}
          {route.view === "compose" ? renderCompose() : null}
          {route.view === "message"
            ? renderMessage(route.projectId, route.messageId)
            : null}
          {route.view === "thread" ? renderThread(route) : null}
          {route.view === "account" ? renderAccount() : null}
          {route.view === "admin" ? renderAdmin() : null}
        </main>
      </div>
    </div>
  );
}
