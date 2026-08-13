import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import LocalePicker from "./LocalePicker";
import i18n, { localeMetadata, supportedLocales } from "./i18n";

describe("LocalePicker", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
    document.documentElement.lang = "en";
    document.documentElement.dir = "ltr";
  });

  it("shows every supported language as a flag-labelled option", async () => {
    const user = userEvent.setup();
    render(
      <LocalePicker
        describedBy="saved-language-status"
        disabled={false}
        locale="en"
        onSelect={vi.fn()}
      />,
    );

    const trigger = screen.getByRole("button", { name: /current language: english/i });
    expect(trigger).toHaveAttribute("aria-describedby", "saved-language-status");
    expect(trigger).toHaveAttribute("aria-haspopup", "listbox");
    const triggerFlag = within(trigger).getByText(localeMetadata.en.flag);
    expect(triggerFlag).toHaveClass("locale-picker-flag");
    expect(triggerFlag).toHaveAttribute("aria-hidden", "true");
    await user.click(trigger);

    const listbox = screen.getByRole("listbox", { name: /choose interface language/i });
    const options = within(listbox).getAllByRole("option");
    expect(options).toHaveLength(45);
    expect(listbox.querySelectorAll(".locale-picker-flag")).toHaveLength(45);
    expect(options.filter((option) => option.tabIndex === 0)).toEqual([
      within(listbox).getByRole("option", { name: /current language: english/i }),
    ]);
    expect(supportedLocales).toHaveLength(45);
    for (const locale of supportedLocales) {
      const metadata = localeMetadata[locale];
      const option = within(listbox).getByRole("option", {
        name: new RegExp(metadata.nativeName, "i"),
      });
      const flag = within(option).getByText(metadata.flag);
      expect(flag).toHaveClass("locale-picker-flag");
      expect(flag).toHaveAttribute("aria-hidden", "true");
      expect(option).toHaveTextContent(metadata.nativeName);
      expect(within(option).getByText(metadata.nativeName)).toHaveAttribute("lang", locale);
    }
    expect(within(listbox).getByRole("option", { name: /current language: english/i }))
      .toHaveAttribute("aria-selected", "true");
  });

  it("supports grid keyboard navigation, selection, escape, and focus return", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <LocalePicker
        disabled={false}
        locale="en"
        onSelect={onSelect}
      />,
    );

    const trigger = screen.getByRole("button", { name: /current language: english/i });
    expect(within(trigger).getByText("English")).toHaveAttribute("lang", "en");
    await user.click(trigger);
    const current = screen.getByRole("option", { name: /current language: english/i });
    expect(current).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    const french = screen.getByRole("option", { name: /use français/i });
    expect(french).toHaveFocus();
    expect(screen.getAllByRole("option").filter((option) => option.tabIndex === 0)).toEqual([
      french,
    ]);
    await user.keyboard("{ArrowUp}");
    expect(current).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(screen.getByRole("option", { name: /use ελληνικά/i })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(current).toHaveFocus();
    await user.keyboard("{Home}");
    const arabic = screen.getByRole("option", { name: /use العربية/i });
    expect(arabic).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(screen.getByRole("option", { name: /use 简体中文/i })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(arabic).toHaveFocus();
    await user.keyboard("{End}");
    const simplifiedChinese = screen.getByRole("option", { name: /use 简体中文/i });
    expect(simplifiedChinese).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect).toHaveBeenCalledWith("zh");
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    await user.click(trigger);
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("maps horizontal arrows to visual direction in right-to-left documents", async () => {
    const user = userEvent.setup();
    document.documentElement.dir = "rtl";
    render(<LocalePicker disabled={false} locale="ar" onSelect={vi.fn()} />);

    const trigger = screen.getByRole("button", { name: /العربية/i });
    await user.click(trigger);
    const arabic = screen.getByRole("option", { name: /current language: العربية/i });
    expect(arabic).toHaveFocus();

    await user.keyboard("{ArrowRight}");
    const simplifiedChinese = screen.getByRole("option", { name: /use 简体中文/i });
    expect(simplifiedChinese).toHaveFocus();
    expect(simplifiedChinese).toHaveAttribute("tabindex", "0");
    expect(arabic).toHaveAttribute("tabindex", "-1");

    await user.keyboard("{ArrowLeft}");
    expect(arabic).toHaveFocus();
  });

  it("closes on focus departure and when account persistence disables it", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <>
        <LocalePicker disabled={false} locale="en" onSelect={vi.fn()} />
        <button type="button">Outside</button>
      </>,
    );

    await user.click(screen.getByRole("button", { name: /current language: english/i }));
    await user.click(screen.getByRole("button", { name: "Outside" }));
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /current language: english/i }));
    rerender(
      <>
        <LocalePicker disabled locale="en" onSelect={vi.fn()} />
        <button type="button">Outside</button>
      </>,
    );
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });
});
