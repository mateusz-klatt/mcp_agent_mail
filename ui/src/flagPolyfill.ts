export const FLAG_FONT_FAMILY = "Twemoji Country Flags";
export const FLAG_FONT_URL =
  "/mail/assets/TwemojiCountryFlags.woff2?v=9f04f144";

const emojiFontStack =
  '"Twemoji Mozilla","Apple Color Emoji","Segoe UI Emoji","Segoe UI Symbol",' +
  '"Noto Color Emoji","EmojiOne Color","Android Emoji",sans-serif';

const renderedPixel = (
  context: CanvasRenderingContext2D,
  emoji: string,
  color: string,
): string => {
  context.clearRect(0, 0, 100, 100);
  context.fillStyle = color;
  context.fillText(emoji, 0, 0);
  return context.getImageData(0, 0, 1, 1).data.join(",");
};

// Canvas detection adapted from country-flag-emoji-polyfill 0.1.10 (MIT).
export const browserSupportsEmoji = (emoji: string): boolean => {
  try {
    const canvas = document.createElement("canvas");
    canvas.width = 1;
    canvas.height = 1;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (context === null) {
      return false;
    }
    context.textBaseline = "top";
    context.font = `100px ${emojiFontStack}`;
    context.scale(0.01, 0.01);
    const whitePixel = renderedPixel(context, emoji, "#fff");
    const blackPixel = renderedPixel(context, emoji, "#000");
    return blackPixel === whitePixel && !blackPixel.startsWith("0,0,0,");
  } catch {
    // Hardened browsers may deny canvas reads. Flag detection is cosmetic and
    // must never prevent the authenticated application from starting.
    return false;
  }
};

type EmojiSupportProbe = (emoji: string) => boolean;

interface FlagPolyfillEnvironment {
  readonly document: Document | undefined;
  readonly window: Window | undefined;
}

export const initFlagPolyfill = (
  supportsEmoji: EmojiSupportProbe = browserSupportsEmoji,
  environment: FlagPolyfillEnvironment = {
    document: globalThis.document,
    window: globalThis.window,
  },
): boolean => {
  if (environment.window === undefined) {
    return false;
  }
  if (environment.document === undefined) {
    return false;
  }
  const browserDocument = environment.document;
  delete browserDocument.documentElement.dataset.flagPolyfill;
  let needsPolyfill: boolean;
  try {
    needsPolyfill = supportsEmoji("😊") && !supportsEmoji("🇨🇭");
  } catch {
    return false;
  }
  if (!needsPolyfill) {
    return false;
  }
  browserDocument.documentElement.dataset.flagPolyfill = "true";
  return true;
};
