// Alpine and its official collapse plugin do not publish TypeScript declarations.
// @ts-expect-error -- runtime package is pinned and exercised by the legacy build/UAT.
import collapse from "@alpinejs/collapse";
import * as Popper from "@popperjs/core";
// @ts-expect-error -- runtime package is pinned and exercised by the legacy build/UAT.
import Alpine from "alpinejs";
import dayjs from "dayjs";
import calendar from "dayjs/plugin/calendar";
import relativeTime from "dayjs/plugin/relativeTime";
import { Diff2HtmlUI } from "diff2html/lib-esm/ui/js/diff2html-ui";
import DOMPurify from "dompurify";
import Fuse from "fuse.js";
import * as lucideModule from "lucide";
import { marked } from "marked";
import mermaid from "mermaid";
import NProgress from "nprogress";
import Prism from "prismjs";
import "prismjs/components/prism-json";
import "prismjs/components/prism-markdown";
import "prismjs/components/prism-python";
import tippy from "tippy.js";
import * as vis from "vis-network/standalone";

import "./legacy.css";
import { isCanonicalInlineRasterImageSource } from "./mail";

const lucide = {
  ...lucideModule,
  createIcons: (
    options: Parameters<typeof lucideModule.createIcons>[0] = {},
  ) => lucideModule.createIcons({ icons: lucideModule.icons, ...options }),
};

DOMPurify.setConfig({
  ALLOWED_TAGS: [
    "a",
    "abbr",
    "acronym",
    "b",
    "blockquote",
    "br",
    "code",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
  ],
  ALLOWED_ATTR: [
    "alt",
    "class",
    "decoding",
    "height",
    "href",
    "loading",
    "rel",
    "src",
    "title",
    "width",
  ],
  ALLOW_ARIA_ATTR: false,
  ALLOW_DATA_ATTR: false,
});

DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node instanceof HTMLImageElement) {
    const source = node.getAttribute("src");
    if (source === null || !isCanonicalInlineRasterImageSource(source)) {
      node.remove();
    }
  }
});

type LegacyWindow = Window & {
  Alpine: typeof Alpine;
  Diff2HtmlUI: typeof Diff2HtmlUI;
  DOMPurify: typeof DOMPurify;
  Fuse: typeof Fuse;
  NProgress: typeof NProgress;
  Popper: typeof Popper;
  Prism: typeof Prism;
  dayjs: typeof dayjs;
  lucide: typeof lucide;
  marked: typeof marked;
  mermaid: typeof mermaid;
  tippy: typeof tippy;
  vis: typeof vis;
};

const legacyWindow = window as unknown as LegacyWindow;

dayjs.extend(relativeTime);
dayjs.extend(calendar);
Alpine.plugin(collapse);

legacyWindow.Alpine = Alpine;
legacyWindow.Diff2HtmlUI = Diff2HtmlUI;
legacyWindow.DOMPurify = DOMPurify;
legacyWindow.Fuse = Fuse;
legacyWindow.NProgress = NProgress;
legacyWindow.Popper = Popper;
legacyWindow.Prism = Prism;
legacyWindow.dayjs = dayjs;
legacyWindow.lucide = lucide;
legacyWindow.marked = marked;
legacyWindow.mermaid = mermaid;
legacyWindow.tippy = tippy;
legacyWindow.vis = vis;

mermaid.initialize({
  startOnLoad: false,
  securityLevel: "strict",
  theme: "base",
  themeVariables: {
    primaryColor: "#6366f1",
    primaryTextColor: "#fff",
    primaryBorderColor: "#4f46e5",
    lineColor: "#6366f1",
    secondaryColor: "#818cf8",
    tertiaryColor: "#a5b4fc",
    background: "#ffffff",
    mainBkg: "#6366f1",
    secondBkg: "#818cf8",
    tertiaryBkg: "#a5b4fc",
  },
  gitGraph: {
    rotateCommitLabel: false,
    mainBranchName: "main",
    showBranches: true,
    showCommitLabel: true,
  },
});

const startAlpine = () => {
  if (!document.documentElement.hasAttribute("data-hermes-alpine-started")) {
    document.documentElement.setAttribute("data-hermes-alpine-started", "true");
    Alpine.start();
  }
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", startAlpine, { once: true });
} else {
  startAlpine();
}
