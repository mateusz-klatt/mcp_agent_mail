/**
 * Per-sender notification tones for the Iris reader.
 *
 * Ported from the server-rendered UI (`templates/base.html`), which had this
 * before the React client replaced it and lost it. The behaviour is deliberately
 * the same, including its two safety properties:
 *
 * 1. **Tones are synthesised, never fetched.** Only the vocabulary word from
 *    `set_agent_notify_sound` crosses the wire. A colleague's preference must
 *    never turn into a request from this browser to a host that colleague chose.
 * 2. **Sound is opt-in and remembered.** Browsers refuse audio until a gesture
 *    inside the page, and arriving from the login form does not count, so the
 *    reader turns it on once and the choice persists.
 *
 * Silence is always an acceptable outcome: no device, a blocked context or an
 * unknown agent must never take the notification path down with it.
 */

export const soundPreferenceKey = "agentMailSound";

interface Tone {
  hz: number;
  wave: OscillatorType;
}

/**
 * The `set_agent_notify_sound` vocabulary, mirrored.
 *
 * Declared with literal keys rather than as `Record<string, Tone>` so that
 * `tones.click` is a `Tone` and not `Tone | undefined`; lookups by an arbitrary
 * string go through {@link toneFor}, which supplies the fallback.
 */
export const tones = {
  chime: { hz: 880, wave: "sine" },
  low: { hz: 440, wave: "sine" },
  high: { hz: 1320, wave: "sine" },
  soft: { hz: 660, wave: "triangle" },
  click: { hz: 220, wave: "square" },
} as const satisfies Record<string, Tone>;

// Spelled out rather than read back from `tones`: the record is indexed by
// arbitrary strings, so a lookup is `Tone | undefined` and the default must not
// be.
const defaultTone: Tone = { hz: 880, wave: "sine" };

export function soundEnabled(
  // Read off `globalThis` rather than the bare identifier: in a non-DOM
  // environment `localStorage` is an undeclared name and evaluating it throws,
  // while the property is simply absent. That keeps the default a plain lookup
  // instead of an environment test with a branch no browser ever takes.
  storage: Pick<Storage, "getItem"> | undefined = globalThis.localStorage,
): boolean {
  try {
    return storage?.getItem(soundPreferenceKey) === "on";
  } catch {
    // Private mode, or storage disabled: treat as off rather than throwing on a
    // path whose whole job is to be unobtrusive.
    return false;
  }
}

export function setSoundEnabled(
  on: boolean,
  storage: Pick<Storage, "setItem"> | undefined = globalThis.localStorage,
): void {
  try {
    storage?.setItem(soundPreferenceKey, on ? "on" : "off");
  } catch {
    /* nothing to do: the toggle simply will not persist */
  }
}

/** Resolve an agent's chosen word to a tone, falling back to the default. */
export function toneFor(sound: string | null | undefined): Tone {
  if (!sound) {
    return defaultTone;
  }
  // The literal key type is what makes `tones.click` a `Tone`; widening only
  // here keeps that guarantee while still accepting an arbitrary word from the
  // server.
  return (tones as Record<string, Tone>)[sound] ?? defaultTone;
}

type AudioContextConstructor = new () => AudioContext;

function audioContextConstructor(): AudioContextConstructor | null {
  // `globalThis`, not `window`: the module is imported by tests that run without
  // a DOM, and referencing an undeclared `window` there is a ReferenceError
  // rather than `undefined`.
  const scope = globalThis as {
    AudioContext?: AudioContextConstructor;
    webkitAudioContext?: AudioContextConstructor;
  };
  return scope.AudioContext ?? scope.webkitAudioContext ?? null;
}

/**
 * One context for the life of the page.
 *
 * The first implementation opened a fresh `AudioContext` per notification and
 * closed it when the oscillator ended. That is what the server-rendered UI
 * effectively did -- but only because it reloaded the page after every ding, so
 * a context never outlived one tone. This client does not reload, and browsers
 * cap how many contexts a document may hold (Chrome allows roughly six):
 * a burst of messages, or a `close()` that has not settled yet, and the next
 * construction throws. The failure is invisible, because a notification path
 * must never surface an error -- it just goes quiet. "Sometimes there is no
 * sound" is exactly that.
 */
let sharedContext: AudioContext | null = null;

function acquireContext(): AudioContext | null {
  if (sharedContext !== null && sharedContext.state !== "closed") {
    return sharedContext;
  }
  const Ctor = audioContextConstructor();
  if (Ctor === null) {
    return null;
  }
  try {
    sharedContext = new Ctor();
    return sharedContext;
  } catch {
    sharedContext = null;
    return null;
  }
}

/** Test seam: drop the cached context so each case starts from nothing. */
export function resetNotificationAudio(): void {
  sharedContext = null;
}

/**
 * Play one short tone for `sound`. Pass nothing when the caller cannot know who
 * sent the message -- that yields the default tone, which is what every caller
 * did before per-sender tones existed.
 */
export function playNotificationTone(
  sound?: string | null,
  storage?: Pick<Storage, "getItem">,
): void {
  // `soundEnabled` supplies the same default when `storage` is omitted, so the
  // environment lookup lives in exactly one place.
  if (!soundEnabled(storage)) {
    return;
  }
  const ctx = acquireContext();
  if (ctx === null) {
    return;
  }
  const tone = toneFor(sound);
  try {
    // A context created outside a user gesture starts suspended and stays
    // silent until resumed. The toggle creates it during a click, but a page
    // restored from the back/forward cache can hand back a suspended one.
    if (ctx.state === "suspended") {
      void ctx.resume().catch(() => undefined);
    }
    const oscillator = ctx.createOscillator();
    const gain = ctx.createGain();
    oscillator.connect(gain);
    gain.connect(ctx.destination);
    oscillator.frequency.value = tone.hz;
    oscillator.type = tone.wave;
    // Ramp rather than switch: an instant edge on a square wave clicks audibly
    // on some devices, which reads as a defect rather than a notification.
    gain.gain.setValueAtTime(0.0001, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.35);
    oscillator.start();
    oscillator.stop(ctx.currentTime + 0.36);
    // Only the nodes are disposable; the context is not. Releasing the nodes
    // keeps a long-lived context from accumulating them.
    oscillator.onended = () => {
      oscillator.disconnect();
      gain.disconnect();
    };
  } catch {
    /* no device, or autoplay blocked: silence is acceptable */
  }
}
