import { afterEach, describe, expect, it } from "vitest";

import i18n, {
  canonicalLocale,
  englishTranslation,
  loadLocale,
  localeMetadata,
  supportedLocales,
  type SupportedLocale,
  type TranslationResource,
} from "./i18n";

const localeCatalogs = import.meta.glob<{ default: TranslationResource }>(
  "./locales/*.ts",
  { eager: true },
);

const flatten = (
  value: unknown,
  prefix = "",
): Record<string, string> => {
  if (typeof value === "string") {
    return { [prefix]: value };
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError(`Invalid translation value at ${prefix || "<root>"}.`);
  }
  return Object.fromEntries(
    Object.entries(value).flatMap(([key, child]) =>
      Object.entries(flatten(child, prefix.length === 0 ? key : `${prefix}.${key}`)),
    ),
  );
};

const placeholders = (value: string) =>
  [...value.matchAll(/{{\s*([A-Za-z0-9_]+)\s*}}/g)]
    .map((match) => match[1])
    .sort();

const pluralRoots = ["projects.count", "inbox.count", "message.attachmentCount"];
const pluralCategories = ["zero", "one", "two", "few", "many", "other"];

describe("Iris locale catalog contract", () => {
  afterEach(async () => {
    await loadLocale("en");
  });

  it("keeps translated catalogs out of the initial resource bundle", () => {
    expect(i18n.hasResourceBundle("en", "translation")).toBe(true);
    expect(i18n.hasResourceBundle("pl", "translation")).toBe(false);
  });

  it("has English plus exactly 44 complete translated catalogs", () => {
    expect(supportedLocales).toHaveLength(45);
    expect(new Set(supportedLocales).size).toBe(45);
    expect(Object.keys(localeMetadata).sort()).toEqual([...supportedLocales].sort());
    const translated = supportedLocales.filter((locale) => locale !== "en");
    const expectedPaths = translated.map((locale) => `./locales/${locale}.ts`).sort();
    expect(Object.keys(localeCatalogs).sort()).toEqual(expectedPaths);

    const english = flatten(englishTranslation);
    for (const locale of translated) {
      const module = localeCatalogs[`./locales/${locale}.ts`];
      expect(module, locale).toBeDefined();
      const catalog = flatten(module?.default);
      expect(Object.keys(catalog).sort(), locale).toEqual(Object.keys(english).sort());
      for (const [key, value] of Object.entries(catalog)) {
        expect(value.trim().length, `${locale}:${key}`).toBeGreaterThan(0);
        const englishValue = english[key];
        expect(englishValue, key).toBeDefined();
        expect(placeholders(value), `${locale}:${key}`).toEqual(
          placeholders(englishValue ?? ""),
        );
      }
      for (const root of pluralRoots) {
        for (const category of pluralCategories) {
          expect(catalog[`${root}_${category}`], `${locale}:${root}_${category}`)
            .toBeTruthy();
        }
      }
    }
  });

  it("canonicalizes human input without corrupting mixed-case BCP-47 tags", () => {
    expect(canonicalLocale(" MY-mm ")).toBe("my-MM");
    expect(canonicalLocale("zh-hAnT")).toBe("zh-Hant");
    expect(canonicalLocale("PL")).toBe("pl");
    expect(canonicalLocale("not-a-locale")).toBeNull();
  });

  it("fails closed when an unregistered catalog reaches the typed loader boundary", async () => {
    await expect(
      loadLocale("not-a-locale" as SupportedLocale),
    ).rejects.toThrow("No translation catalog is registered for not-a-locale.");
    expect(i18n.resolvedLanguage ?? i18n.language).toBe("en");
  });

  it("loads every catalog and applies only the three declared RTL directions", async () => {
    const rtlLocales = supportedLocales.filter(
      (locale) => localeMetadata[locale].direction === "rtl",
    );
    expect(rtlLocales).toEqual(["ar", "fa", "he"]);

    for (const locale of supportedLocales) {
      await loadLocale(locale);
      expect(i18n.resolvedLanguage ?? i18n.language).toBe(locale);
      expect(document.documentElement.lang).toBe(locale);
      expect(document.documentElement.dir).toBe(localeMetadata[locale].direction);
      expect(i18n.t("appName")).toBe("Iris");
      expect(i18n.t("nav.inbox").trim().length).toBeGreaterThan(0);
    }
  });
});
