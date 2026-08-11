import i18n from "i18next";
import { initReactI18next } from "react-i18next";

export const supportedLocales = ["en", "pl"] as const;
export type SupportedLocale = (typeof supportedLocales)[number];

const resources = {
  en: {
    translation: {
      appName: "Hermes",
      appSubtitle: "Agent Mail operator preview",
      skipToContent: "Skip to content",
      readOnly: "Read-only preview",
      navigation: "Primary navigation",
      language: "Language",
      localePreviewHint: "Visual preview · preference is not saved yet",
      role: "Demo role",
      nav: {
        projects: "Projects",
        inbox: "Inbox",
      },
      overview: "Good afternoon, Mateusz",
      overviewHint: "A calm view of agent activity across your assigned projects.",
      rolePanel: "Access preview",
      roleName: {
        admin: "Administrator",
        operator: "Operator",
        viewer: "Viewer",
      },
      roleDescription: {
        admin: "Can manage every project, user and agent.",
        operator: "Can read and reply inside assigned projects.",
        viewer: "Can read messages inside assigned projects.",
      },
      metrics: {
        projects: "Visible projects",
        agents: "Active agents",
        unread: "Unread messages",
      },
      projects: "Project activity",
      project: "Project",
      agents: "Agents",
      status: "Status",
      statusLive: "Live",
      statusQuiet: "Quiet",
      recentMessages: "Recent messages",
      viewInbox: "Open inbox",
      unread: "unread",
      unreadMessage: "Unread message",
      messageFrom: "From {{sender}}",
      projectRows: {
        mail: "MCP Agent Mail",
        snapper: "Snapper",
        hestia: "Hestia",
      },
      messageSubjects: {
        deployment: "Production deployment verified",
        roles: "Project roles proposal",
        watcher: "Inbox watcher canary complete",
      },
    },
  },
  pl: {
    translation: {
      appName: "Hermes",
      appSubtitle: "Podgląd operatora Agent Mail",
      skipToContent: "Przejdź do treści",
      readOnly: "Podgląd tylko do odczytu",
      navigation: "Główna nawigacja",
      language: "Język",
      localePreviewHint: "Podgląd wizualny · wybór nie jest jeszcze zapisywany",
      role: "Rola demonstracyjna",
      nav: {
        projects: "Projekty",
        inbox: "Skrzynka",
      },
      overview: "Dzień dobry, Mateusz",
      overviewHint: "Spokojny widok aktywności agentów w przypisanych projektach.",
      rolePanel: "Podgląd uprawnień",
      roleName: {
        admin: "Administrator",
        operator: "Operator",
        viewer: "Obserwator",
      },
      roleDescription: {
        admin: "Może zarządzać każdym projektem, użytkownikiem i agentem.",
        operator: "Może czytać i odpowiadać w przypisanych projektach.",
        viewer: "Może czytać wiadomości w przypisanych projektach.",
      },
      metrics: {
        projects: "Widoczne projekty",
        agents: "Aktywni agenci",
        unread: "Nieprzeczytane wiadomości",
      },
      projects: "Aktywność projektów",
      project: "Projekt",
      agents: "Agenci",
      status: "Stan",
      statusLive: "Aktywny",
      statusQuiet: "Spokojny",
      recentMessages: "Ostatnie wiadomości",
      viewInbox: "Otwórz skrzynkę",
      unread: "nieprzeczytane",
      unreadMessage: "Nieprzeczytana wiadomość",
      messageFrom: "Od {{sender}}",
      projectRows: {
        mail: "MCP Agent Mail",
        snapper: "Snapper",
        hestia: "Hestia",
      },
      messageSubjects: {
        deployment: "Wdrożenie produkcyjne zweryfikowane",
        roles: "Propozycja ról projektowych",
        watcher: "Test obserwatora skrzynki zakończony",
      },
    },
  },
} as const;

void i18n.use(initReactI18next).init({
  resources,
  lng: "en",
  fallbackLng: "en",
  interpolation: {
    escapeValue: false,
  },
});

export default i18n;
