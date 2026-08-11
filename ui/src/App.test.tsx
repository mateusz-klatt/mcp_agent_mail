import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import i18n from "./i18n";
import { parsePreferences } from "./preferences";
import { preferencesResponse, server } from "./test/server";

const preferencesUrl = "*/mail/api/v1/me/preferences";

async function waitForEnglishPreferences() {
  expect(await screen.findByText("Language saved for your account")).toBeVisible();
}

describe("Hermes landing shell", () => {
  beforeEach(async () => {
    window.history.replaceState({}, "", "/mail/v2/");
    document.documentElement.lang = "en";
    await i18n.changeLanguage("en");
  });

  it("renders the read-only administrator overview", async () => {
    render(<App />);
    await waitForEnglishPreferences();

    expect(screen.getByRole("heading", { name: "Good afternoon, Mateusz" })).toBeVisible();
    expect(screen.getByText("Read-only preview")).toBeVisible();
    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    const links = within(navigation).getAllByRole("link");
    expect(links).toHaveLength(2);
    expect(links.map((link) => link.textContent)).toEqual(["Projects", "Inbox"]);
    expect(screen.getByRole("link", { name: "Projects" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "Inbox" })).toHaveAttribute("aria-current", "page");
    for (const link of links) {
      expect(document.querySelector(link.getAttribute("href") ?? "missing")).toBeInTheDocument();
    }
    expect(screen.getByText("Can manage every project, user and agent.")).toBeVisible();
    expect(screen.getByText("Production deployment verified")).toBeVisible();
    expect(screen.getAllByText("Unread message")).toHaveLength(3);
    expect(screen.getByText("3", { selector: ".metrics strong" })).toBeVisible();
  });

  it("previews operator and viewer project access", async () => {
    const user = userEvent.setup();
    render(<App />);
    await waitForEnglishPreferences();

    await user.selectOptions(screen.getByLabelText("Demo role"), "operator");
    expect(screen.getByText("Can read and reply inside assigned projects.")).toBeVisible();
    expect(screen.getByText("2", { selector: ".metrics strong" })).toBeVisible();
    expect(screen.queryByText("Hestia")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Demo role"), "viewer");
    expect(screen.getByText("Can read messages inside assigned projects.")).toBeVisible();
    expect(screen.getAllByText("Viewer")).toHaveLength(2);
  });

  it("loads a stored Polish UI locale while keeping correspondence independent", async () => {
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json(preferencesResponse("pl", "en")),
      ),
    );
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Dzień dobry, Mateusz" })).toBeVisible();
    expect(document.documentElement).toHaveAttribute("lang", "pl");
    expect(screen.getByRole("navigation", { name: "Główna nawigacja" })).toBeVisible();
    expect(screen.getByText("Wdrożenie produkcyjne zweryfikowane")).toBeVisible();
    expect(screen.getAllByText("Nieprzeczytana wiadomość")).toHaveLength(3);
    expect(screen.getByLabelText("Język")).toHaveValue("pl");
    expect(screen.getByLabelText("Język")).toHaveAccessibleDescription(
      "Język zapisany na Twoim koncie",
    );
  });

  it("persists only the UI locale with same-origin no-store requests", async () => {
    const user = userEvent.setup();
    let preferenceReads = 0;
    let capturedRequest: Request | undefined;
    let capturedBody: unknown;
    let finishRequest: () => void = () => undefined;
    const requestGate = new Promise<void>((resolve) => {
      finishRequest = resolve;
    });

    server.use(
      http.get(preferencesUrl, () => {
        preferenceReads += 1;
        return HttpResponse.json(preferencesResponse("en"));
      }),
      http.patch(preferencesUrl, async ({ request }) => {
        capturedRequest = request;
        capturedBody = await request.json();
        await requestGate;
        return HttpResponse.json(preferencesResponse("pl"));
      }),
    );

    render(<App />);
    await waitForEnglishPreferences();

    await user.selectOptions(screen.getByLabelText("Language"), "pl");
    expect(await screen.findByText("Saving language…")).toBeVisible();
    expect(screen.getByLabelText("Language")).toBeDisabled();
    expect(screen.getByRole("heading", { name: "Good afternoon, Mateusz" })).toBeVisible();

    expect(capturedRequest?.method).toBe("PATCH");
    expect(capturedRequest?.credentials).toBe("same-origin");
    expect(capturedRequest?.cache).toBe("no-store");
    expect(capturedRequest?.headers.get("accept")).toBe("application/json");
    expect(capturedRequest?.headers.get("content-type")).toBe("application/json");
    expect(capturedBody).toEqual({ preferred_ui_locale: "pl" });
    expect(preferenceReads).toBe(1);

    act(() => finishRequest());

    expect(await screen.findByRole("heading", { name: "Dzień dobry, Mateusz" })).toBeVisible();
    expect(preferenceReads).toBe(1);
    expect(screen.getByLabelText("Język")).toHaveValue("pl");
    expect(screen.getByRole("status")).toHaveTextContent("Język zapisany na Twoim koncie");
  });

  it("keeps the future API boundary testable through MSW", async () => {
    const response = await fetch("http://localhost/mail/api/v1/health");

    await expect(response.json()).resolves.toEqual({ status: "ok" });
  });

  it("calls the injected login callback on an unauthorized preference read", async () => {
    window.history.replaceState({}, "", "/mail/v2/?project=mail#inbox");
    const onUnauthorized = vi.fn();
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );

    render(<App onUnauthorized={onUnauthorized} />);

    await waitFor(() =>
      expect(onUnauthorized).toHaveBeenCalledWith(
        "/mail/login?next=%2Fmail%2Fv2%2F%3Fproject%3Dmail%23inbox",
      ),
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Your session expired. Redirecting to sign in.",
    );
    expect(screen.getByLabelText("Language")).toBeDisabled();
  });

  it("uses same-tab login navigation when no unauthorized callback is supplied", async () => {
    window.history.replaceState({}, "", "/mail/v2/#projects");
    const navigateTo = vi.fn();
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );

    render(<App navigateTo={navigateTo} />);

    await waitFor(() =>
      expect(navigateTo).toHaveBeenCalledWith(
        "/mail/login?next=%2Fmail%2Fv2%2F%23projects",
      ),
    );
  });

  it("fails loud and falls back to an English preview for invalid responses", async () => {
    document.documentElement.lang = "pl";
    await i18n.changeLanguage("pl");
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json({
          ...preferencesResponse("pl"),
          stored: {
            preferred_ui_locale: "fr",
            preferred_correspondence_locale: null,
          },
        }),
      ),
    );

    render(<App />);

    expect(
      await screen.findByText(
        "Could not load your saved language. Language changes are preview-only.",
      ),
    ).toBeVisible();
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
    expect(document.documentElement).toHaveAttribute("lang", "en");
    expect(screen.getByRole("heading", { name: "Good afternoon, Mateusz" })).toBeVisible();
    expect(screen.getByLabelText("Language")).toBeEnabled();
  });

  it("keeps language changes local when the preference read fails", async () => {
    const user = userEvent.setup();
    let patchRequests = 0;
    server.use(
      http.get(preferencesUrl, () =>
        HttpResponse.json({ detail: "unavailable" }, { status: 503 }),
      ),
      http.patch(preferencesUrl, () => {
        patchRequests += 1;
        return HttpResponse.json({ detail: "must not be called" }, { status: 500 });
      }),
    );

    render(<App />);

    expect(
      await screen.findByText(
        "Could not load your saved language. Language changes are preview-only.",
      ),
    ).toBeVisible();
    expect(screen.getByLabelText("Language")).toHaveValue("en");

    await user.selectOptions(screen.getByLabelText("Language"), "pl");

    expect(patchRequests).toBe(0);
    expect(document.documentElement).toHaveAttribute("lang", "pl");
    expect(screen.getByRole("heading", { name: "Dzień dobry, Mateusz" })).toBeVisible();
    expect(screen.getByLabelText("Język")).toHaveValue("pl");
    expect(screen.getByRole("status")).toHaveTextContent(
      "Nie udało się wczytać zapisanego języka. Zmiany języka są tylko podglądem.",
    );
  });

  it("keeps the previous locale when saving fails", async () => {
    const user = userEvent.setup();
    server.use(
      http.patch(preferencesUrl, () =>
        HttpResponse.json({ detail: "unavailable" }, { status: 503 }),
      ),
    );
    render(<App />);
    await waitForEnglishPreferences();

    await user.selectOptions(screen.getByLabelText("Language"), "pl");

    expect(
      await screen.findByText(
        "Could not save your language. Your previous language is still active.",
      ),
    ).toBeVisible();
    expect(screen.getByLabelText("Language")).toHaveValue("en");
    expect(document.documentElement).toHaveAttribute("lang", "en");
    expect(screen.getByRole("heading", { name: "Good afternoon, Mateusz" })).toBeVisible();
  });

  it("rolls back and redirects when the preference write loses authorization", async () => {
    const user = userEvent.setup();
    const onUnauthorized = vi.fn();
    server.use(
      http.patch(preferencesUrl, () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );
    render(<App onUnauthorized={onUnauthorized} />);
    await waitForEnglishPreferences();

    await user.selectOptions(screen.getByLabelText("Language"), "pl");

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledOnce());
    expect(screen.getByLabelText("Language")).toHaveValue("en");
    expect(screen.getByLabelText("Language")).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Your session expired. Redirecting to sign in.",
    );
  });

  it.each([
    [
      "preferred UI locale",
      {
        ...preferencesResponse("en"),
        stored: {
          preferred_ui_locale: "fr",
          preferred_correspondence_locale: null,
        },
      },
    ],
    [
      "preferred correspondence locale",
      {
        ...preferencesResponse("en"),
        stored: {
          preferred_ui_locale: "en",
          preferred_correspondence_locale: "fr",
        },
      },
    ],
    [
      "effective UI locale",
      {
        ...preferencesResponse("en"),
        effective: { ui_locale: "fr", correspondence_locale: "en" },
      },
    ],
    [
      "effective correspondence locale",
      {
        ...preferencesResponse("en"),
        effective: { ui_locale: "en", correspondence_locale: "fr" },
      },
    ],
  ])("rejects an invalid %s at runtime", (_label, payload) => {
    expect(() => parsePreferences(payload)).toThrow(TypeError);
  });
});
