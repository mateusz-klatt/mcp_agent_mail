import { supportedLocales, type SupportedLocale } from "./i18n";

export const preferencesEndpoint = "/mail/api/v1/me/preferences";

export interface MailUiPreferences {
  stored: {
    preferred_ui_locale: SupportedLocale;
    preferred_correspondence_locale: SupportedLocale | null;
  };
  effective: {
    ui_locale: SupportedLocale;
    correspondence_locale: SupportedLocale;
  };
}

interface PreferencesShape {
  stored?: {
    preferred_ui_locale?: unknown;
    preferred_correspondence_locale?: unknown;
  };
  effective?: {
    ui_locale?: unknown;
    correspondence_locale?: unknown;
  };
}

export class PreferencesHttpError extends Error {
  constructor(readonly status: number) {
    super(`Preferences request failed with HTTP ${status}.`);
    this.name = "PreferencesHttpError";
  }
}

export function isSupportedLocale(value: unknown): value is SupportedLocale {
  return supportedLocales.some((locale) => locale === value);
}

export function parsePreferences(payload: unknown): MailUiPreferences {
  const candidate = payload as PreferencesShape | null;
  const preferredUiLocale = candidate?.stored?.preferred_ui_locale;
  const preferredCorrespondenceLocale =
    candidate?.stored?.preferred_correspondence_locale;
  const effectiveUiLocale = candidate?.effective?.ui_locale;
  const effectiveCorrespondenceLocale = candidate?.effective?.correspondence_locale;

  if (!isSupportedLocale(preferredUiLocale)) {
    throw new TypeError("Invalid preferred UI locale in preferences response.");
  }
  if (
    preferredCorrespondenceLocale !== null &&
    !isSupportedLocale(preferredCorrespondenceLocale)
  ) {
    throw new TypeError("Invalid preferred correspondence locale in preferences response.");
  }
  if (!isSupportedLocale(effectiveUiLocale)) {
    throw new TypeError("Invalid effective UI locale in preferences response.");
  }
  if (!isSupportedLocale(effectiveCorrespondenceLocale)) {
    throw new TypeError("Invalid effective correspondence locale in preferences response.");
  }

  return {
    stored: {
      preferred_ui_locale: preferredUiLocale,
      preferred_correspondence_locale: preferredCorrespondenceLocale,
    },
    effective: {
      ui_locale: effectiveUiLocale,
      correspondence_locale: effectiveCorrespondenceLocale,
    },
  };
}

async function preferencesRequest(init: RequestInit): Promise<MailUiPreferences> {
  const response = await fetch(new URL(preferencesEndpoint, window.location.origin), {
    ...init,
    cache: "no-store",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...init.headers,
    },
  });

  if (!response.ok) {
    throw new PreferencesHttpError(response.status);
  }

  return parsePreferences(await response.json());
}

export function loadPreferences(): Promise<MailUiPreferences> {
  return preferencesRequest({ method: "GET" });
}

export function saveUiLocale(locale: SupportedLocale): Promise<MailUiPreferences> {
  return preferencesRequest({
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ preferred_ui_locale: locale }),
  });
}

export function mailLoginUrl(
  location: Pick<Location, "pathname" | "search" | "hash">,
): string {
  const next = `${location.pathname}${location.search}${location.hash}`;
  return `/mail/login?next=${encodeURIComponent(next)}`;
}
