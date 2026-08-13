import {
  type FocusEvent,
  type KeyboardEvent,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";

import {
  localeMetadata,
  supportedLocales,
  type SupportedLocale,
} from "./i18n";

interface LocalePickerProps {
  describedBy?: string;
  disabled: boolean;
  locale: SupportedLocale;
  onSelect: (locale: SupportedLocale) => void;
}

const localeColumns = 5;

export default function LocalePicker({
  describedBy,
  disabled,
  locale,
  onSelect,
}: LocalePickerProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [activeLocale, setActiveLocale] = useState(locale);
  const optionsId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef(new Map<SupportedLocale, HTMLButtonElement>());
  const metadata = localeMetadata[locale];

  useEffect(() => {
    if (disabled) {
      setOpen(false);
    }
  }, [disabled]);

  useEffect(() => {
    if (!open) {
      return;
    }
    setActiveLocale(locale);
    optionRefs.current.get(locale)?.focus();
  }, [locale, open]);

  const focusAt = (index: number) => {
    const count = supportedLocales.length;
    const wrapped = ((index % count) + count) % count;
    const candidate = supportedLocales[wrapped] as SupportedLocale;
    setActiveLocale(candidate);
    optionRefs.current.get(candidate)?.focus();
  };

  const handleBlur = (event: FocusEvent<HTMLDivElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget)) {
      setOpen(false);
    }
  };

  const handleOptionKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    const rightwardStep = document.documentElement.dir === "rtl" ? -1 : 1;
    let nextIndex: number | undefined;
    switch (event.key) {
      case "ArrowDown":
        nextIndex = index + localeColumns;
        break;
      case "ArrowLeft":
        nextIndex = index - rightwardStep;
        break;
      case "ArrowRight":
        nextIndex = index + rightwardStep;
        break;
      case "ArrowUp":
        nextIndex = index - localeColumns;
        break;
    }
    if (nextIndex !== undefined) {
      event.preventDefault();
      focusAt(nextIndex);
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      focusAt(event.key === "Home" ? 0 : supportedLocales.length - 1);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    }
  };

  return (
    <div className="locale-picker" onBlur={handleBlur}>
      <button
        ref={triggerRef}
        className="locale-picker-trigger"
        name="ui-language"
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={optionsId}
        aria-describedby={describedBy}
        aria-label={t("localePicker.trigger", { language: metadata.nativeName })}
        data-locale={locale}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
      >
        <span aria-hidden="true">{metadata.flag}</span>
        <span lang={locale}>{metadata.nativeName}</span>
        <span aria-hidden="true">▾</span>
      </button>
      {open ? (
        <div
          id={optionsId}
          className="locale-picker-popover"
          role="listbox"
          aria-label={t("localePicker.menu")}
        >
          <div className="locale-picker-grid">
            {supportedLocales.map((candidate, index) => {
              const candidateMetadata = localeMetadata[candidate];
              const current = candidate === locale;
              return (
                <button
                  key={candidate}
                  ref={(element) => {
                    if (element === null) {
                      optionRefs.current.delete(candidate);
                    } else {
                      optionRefs.current.set(candidate, element);
                    }
                  }}
                  className="locale-picker-option"
                  type="button"
                  role="option"
                  aria-selected={current}
                  aria-label={t(current ? "localePicker.current" : "localePicker.option", {
                    language: candidateMetadata.nativeName,
                  })}
                  data-current={current ? "true" : undefined}
                  tabIndex={candidate === activeLocale ? 0 : -1}
                  onClick={() => {
                    setOpen(false);
                    onSelect(candidate);
                    triggerRef.current?.focus();
                  }}
                  onFocus={() => setActiveLocale(candidate)}
                  onKeyDown={(event) => handleOptionKeyDown(event, index)}
                >
                  <span className="locale-picker-flag" aria-hidden="true">
                    {candidateMetadata.flag}
                  </span>
                  <span lang={candidate}>{candidateMetadata.nativeName}</span>
                </button>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}
