import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  browserSupportsEmoji,
  FLAG_FONT_FAMILY,
  FLAG_FONT_URL,
  initFlagPolyfill,
} from "./flagPolyfill";

const appStyles = readFileSync(resolve(process.cwd(), "src/app.css"), "utf8");

const imageData = (red: number): ImageData =>
  ({ data: Uint8ClampedArray.from([red, red, red, red]) }) as ImageData;

const mockCanvasContext = (...pixels: ImageData[]) => {
  const context = {
    clearRect: vi.fn(),
    fillStyle: "",
    fillText: vi.fn(),
    font: "",
    getImageData: vi.fn().mockReturnValueOnce(pixels[0]),
    scale: vi.fn(),
    textBaseline: "",
  };
  for (const pixel of pixels.slice(1)) {
    context.getImageData.mockReturnValueOnce(pixel);
  }
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
    context as unknown as CanvasRenderingContext2D,
  );
  return context;
};

describe("initFlagPolyfill", () => {
  afterEach(() => {
    delete document.documentElement.dataset.flagPolyfill;
    vi.restoreAllMocks();
  });

  it("enables the self-hosted font only when flags are unsupported", () => {
    const supportsEmoji = vi
      .fn<(emoji: string) => boolean>()
      .mockReturnValueOnce(true)
      .mockReturnValueOnce(false);

    expect(
      initFlagPolyfill(supportsEmoji, { document, window }),
    ).toBe(true);
    expect(supportsEmoji).toHaveBeenNthCalledWith(1, "😊");
    expect(supportsEmoji).toHaveBeenNthCalledWith(2, "🇨🇭");
    expect(document.documentElement).toHaveAttribute("data-flag-polyfill", "true");
    expect(FLAG_FONT_FAMILY).toBe("Twemoji Country Flags");
    expect(FLAG_FONT_URL).toBe(
      "/mail/assets/TwemojiCountryFlags.woff2?v=9f04f144",
    );
  });

  it("uses canvas detection by default", () => {
    mockCanvasContext(
      imageData(1),
      imageData(1),
      imageData(2),
      imageData(3),
    );

    expect(initFlagPolyfill()).toBe(true);
    expect(document.documentElement).toHaveAttribute("data-flag-polyfill", "true");
  });

  it("leaves native flag support untouched", () => {
    const supportsEmoji = vi.fn<(emoji: string) => boolean>(() => true);
    document.documentElement.dataset.flagPolyfill = "true";

    expect(
      initFlagPolyfill(supportsEmoji, { document, window }),
    ).toBe(false);
    expect(document.documentElement).not.toHaveAttribute("data-flag-polyfill");
  });

  it("does nothing outside a browser", () => {
    const supportsEmoji = vi.fn<(emoji: string) => boolean>();

    expect(
      initFlagPolyfill(supportsEmoji, { document, window: undefined }),
    ).toBe(false);
    expect(
      initFlagPolyfill(supportsEmoji, { document: undefined, window }),
    ).toBe(false);
    expect(supportsEmoji).not.toHaveBeenCalled();
    expect(document.documentElement).not.toHaveAttribute("data-flag-polyfill");
  });

  it("never blocks application startup when feature detection throws", () => {
    const supportsEmoji = vi.fn<(emoji: string) => boolean>(() => {
      throw new DOMException("Canvas access denied", "SecurityError");
    });

    expect(initFlagPolyfill(supportsEmoji, { document, window })).toBe(false);
    expect(document.documentElement).not.toHaveAttribute("data-flag-polyfill");
  });

  it("detects color emoji support from canvas pixels", () => {
    const context = mockCanvasContext(imageData(1), imageData(1));

    expect(browserSupportsEmoji("😊")).toBe(true);
    expect(context.clearRect).toHaveBeenCalledTimes(2);
    expect(context.fillText).toHaveBeenNthCalledWith(1, "😊", 0, 0);
    expect(context.fillText).toHaveBeenNthCalledWith(2, "😊", 0, 0);
    expect(context.scale).toHaveBeenCalledWith(0.01, 0.01);
  });

  it("rejects missing, monochrome, and blank canvas glyphs", () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    expect(browserSupportsEmoji("😊")).toBe(false);
    vi.restoreAllMocks();

    mockCanvasContext(imageData(1), imageData(2));
    expect(browserSupportsEmoji("😊")).toBe(false);
    vi.restoreAllMocks();

    mockCanvasContext(imageData(0), imageData(0));
    expect(browserSupportsEmoji("😊")).toBe(false);
  });

  it("treats denied canvas reads as unsupported instead of throwing", () => {
    const context = mockCanvasContext(imageData(1), imageData(1));
    context.getImageData.mockReset();
    context.getImageData.mockImplementation(() => {
      throw new DOMException("Canvas access denied", "SecurityError");
    });

    expect(browserSupportsEmoji("😊")).toBe(false);
  });

  it("keeps the static font CSP-safe and gated to polyfilled flag surfaces", () => {
    expect(appStyles).toContain('@font-face {\n  font-family: "Twemoji Country Flags";');
    expect(appStyles).toContain(
      'src: url("/mail/assets/TwemojiCountryFlags.woff2?v=9f04f144") format("woff2");',
    );
    expect(appStyles).toContain("U+1F1E6-1F1FF");
    expect(appStyles).toContain(
      'html[data-flag-polyfill="true"] .locale-picker-flag',
    );
    expect(appStyles).toContain('html[data-flag-polyfill="true"] select');
    expect(appStyles).not.toContain("unsafe-inline");
  });
});
