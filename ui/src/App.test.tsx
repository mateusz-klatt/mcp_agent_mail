import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "./App";
import i18n from "./i18n";

describe("Hermes landing shell", () => {
  beforeEach(async () => {
    document.documentElement.lang = "en";
    await i18n.changeLanguage("en");
  });

  it("renders the read-only administrator overview", () => {
    render(<App />);

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

    await user.selectOptions(screen.getByLabelText("Demo role"), "operator");
    expect(screen.getByText("Can read and reply inside assigned projects.")).toBeVisible();
    expect(screen.getByText("2", { selector: ".metrics strong" })).toBeVisible();
    expect(screen.queryByText("Hestia")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Demo role"), "viewer");
    expect(screen.getByText("Can read messages inside assigned projects.")).toBeVisible();
    expect(screen.getAllByText("Viewer")).toHaveLength(2);
  });

  it("switches the visual locale preview without claiming persistence", async () => {
    const user = userEvent.setup();
    render(<App />);

    const language = screen.getByLabelText("Language");
    expect(language).toHaveAccessibleDescription(
      "Visual preview · preference is not saved yet",
    );

    await user.selectOptions(language, "pl");

    expect(document.documentElement).toHaveAttribute("lang", "pl");
    expect(screen.getByRole("heading", { name: "Dzień dobry, Mateusz" })).toBeVisible();
    expect(screen.getByRole("navigation", { name: "Główna nawigacja" })).toBeVisible();
    expect(screen.getByText("Wdrożenie produkcyjne zweryfikowane")).toBeVisible();
    expect(screen.getAllByText("Nieprzeczytana wiadomość")).toHaveLength(3);
    expect(screen.getByLabelText("Język")).toHaveAccessibleDescription(
      "Podgląd wizualny · wybór nie jest jeszcze zapisywany",
    );

    await user.selectOptions(screen.getByLabelText("Język"), "en");
    expect(document.documentElement).toHaveAttribute("lang", "en");
    expect(screen.getByRole("heading", { name: "Good afternoon, Mateusz" })).toBeVisible();
  });

  it("keeps the future API boundary testable through MSW", async () => {
    const response = await fetch("http://localhost/mail/api/v1/health");

    await expect(response.json()).resolves.toEqual({ status: "ok" });
  });
});
