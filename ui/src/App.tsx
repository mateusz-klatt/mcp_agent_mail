import { type ChangeEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { supportedLocales, type SupportedLocale } from "./i18n";
import "./app.css";

type ViewerRole = "admin" | "operator" | "viewer";

const roles: ViewerRole[] = ["admin", "operator", "viewer"];

const navigation = ["projects", "inbox"] as const;

const projects = [
  { id: "mail", agents: 7, unread: 3, status: "statusLive" },
  { id: "snapper", agents: 4, unread: 1, status: "statusLive" },
  { id: "hestia", agents: 2, unread: 0, status: "statusQuiet" },
] as const;

const messages = [
  { subject: "deployment", sender: "claude-linux-holzera-1", age: "2m" },
  { subject: "roles", sender: "codex-wsl-home-1", age: "18m" },
  { subject: "watcher", sender: "claude-win-home-1", age: "41m" },
] as const;

const visibleProjectCount: Record<ViewerRole, number> = {
  admin: 3,
  operator: 2,
  viewer: 2,
};

export function App() {
  const { i18n, t } = useTranslation();
  const [role, setRole] = useState<ViewerRole>("admin");

  const handleLocaleChange = (event: ChangeEvent<HTMLSelectElement>) => {
    const locale = event.target.value as SupportedLocale;
    document.documentElement.lang = locale;
    void i18n.changeLanguage(locale);
  };

  const handleRoleChange = (event: ChangeEvent<HTMLSelectElement>) => {
    setRole(event.target.value as ViewerRole);
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
          <span className="read-only-badge">{t("readOnly")}</span>
          <div className="locale-control">
            <label>
              <span>{t("language")}</span>
              <select
                aria-label={t("language")}
                aria-describedby="locale-preview-hint"
                value={i18n.language}
                onChange={handleLocaleChange}
              >
                {supportedLocales.map((locale) => (
                  <option key={locale} value={locale}>
                    {locale.toUpperCase()}
                  </option>
                ))}
              </select>
            </label>
            <small id="locale-preview-hint" className="locale-hint">
              {t("localePreviewHint")}
            </small>
          </div>
        </div>
      </header>

      <div className="workspace">
        <aside className="sidebar">
          <nav aria-label={t("navigation")}>
            {navigation.map((item) => (
              <a
                className={item === "inbox" ? "nav-link is-active" : "nav-link"}
                href={`#${item}`}
                key={item}
                aria-current={item === "inbox" ? "page" : undefined}
              >
                <span className="nav-dot" aria-hidden="true" />
                {t(`nav.${item}`)}
              </a>
            ))}
          </nav>

          <section className="role-card" aria-labelledby="role-heading">
            <p id="role-heading" className="eyebrow">
              {t("rolePanel")}
            </p>
            <label>
              <span>{t("role")}</span>
              <select value={role} onChange={handleRoleChange}>
                {roles.map((item) => (
                  <option key={item} value={item}>
                    {t(`roleName.${item}`)}
                  </option>
                ))}
              </select>
            </label>
            <p className="role-description">{t(`roleDescription.${role}`)}</p>
          </section>
        </aside>

        <main id="main-content" className="content">
          <section className="welcome" aria-labelledby="overview-heading">
            <div>
              <p className="eyebrow">{t("nav.inbox")}</p>
              <h1 id="overview-heading">{t("overview")}</h1>
              <p>{t("overviewHint")}</p>
            </div>
            <span className={`role-pill role-${role}`}>{t(`roleName.${role}`)}</span>
          </section>

          <section className="metrics" aria-label={t("overviewHint")}>
            <article>
              <span>{t("metrics.projects")}</span>
              <strong>{visibleProjectCount[role]}</strong>
            </article>
            <article>
              <span>{t("metrics.agents")}</span>
              <strong>13</strong>
            </article>
            <article>
              <span>{t("metrics.unread")}</span>
              <strong>4</strong>
            </article>
          </section>

          <div className="content-grid">
            <section className="panel" id="projects" aria-labelledby="projects-heading">
              <div className="panel-heading">
                <h2 id="projects-heading">{t("projects")}</h2>
                <span>3</span>
              </div>
              <div className="project-list" role="list">
                {projects.slice(0, visibleProjectCount[role]).map((project) => (
                  <article className="project-row" role="listitem" key={project.id}>
                    <span className="project-avatar" aria-hidden="true">
                      {project.id.slice(0, 1).toUpperCase()}
                    </span>
                    <div>
                      <strong>{t(`projectRows.${project.id}`)}</strong>
                      <small>
                        {project.agents} {t("agents")} · {project.unread} {t("unread")}
                      </small>
                    </div>
                    <span className={`status status-${project.status}`}>
                      {t(project.status)}
                    </span>
                  </article>
                ))}
              </div>
            </section>

            <section className="panel" id="inbox" aria-labelledby="messages-heading">
              <div className="panel-heading">
                <h2 id="messages-heading">{t("recentMessages")}</h2>
                <a href="#inbox">{t("viewInbox")}</a>
              </div>
              <div className="message-list">
                {messages.map((message) => (
                  <article className="message-row" key={message.subject}>
                    <span className="unread-mark">
                      <span className="sr-only">{t("unreadMessage")}</span>
                    </span>
                    <div>
                      <strong>{t(`messageSubjects.${message.subject}`)}</strong>
                      <small>{t("messageFrom", { sender: message.sender })}</small>
                    </div>
                    <time>{message.age}</time>
                  </article>
                ))}
              </div>
            </section>
          </div>
        </main>
      </div>
    </div>
  );
}
