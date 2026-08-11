import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

export const preferencesResponse = (
  uiLocale: "en" | "pl",
  correspondenceLocale: "en" | "pl" | null = null,
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

export const server = setupServer(
  http.get("http://localhost/mail/api/v1/health", () =>
    HttpResponse.json({ status: "ok" }),
  ),
  http.get("*/mail/api/v1/me/preferences", () =>
    HttpResponse.json(preferencesResponse("en")),
  ),
  http.patch("*/mail/api/v1/me/preferences", async ({ request }) => {
    const body = (await request.json()) as { preferred_ui_locale: "en" | "pl" };
    return HttpResponse.json(preferencesResponse(body.preferred_ui_locale));
  }),
);
