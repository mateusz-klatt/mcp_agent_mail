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

interface ToneNote {
  hz: number;
  wave: OscillatorType;
  start: number;
  duration: number;
  gain: number;
}

interface Tone {
  notes: readonly ToneNote[];
}

/**
 * The `set_agent_notify_sound` vocabulary, mirrored.
 *
 * Declared with literal keys rather than as `Record<string, Tone>` so that
 * `tones.click` is a `Tone` and not `Tone | undefined`; lookups by an arbitrary
 * string go through {@link toneFor}, which supplies the fallback.
 */
export const tones = {
  chime: {
    notes: [{ hz: 880, wave: "sine", start: 0, duration: 0.36, gain: 0.25 }],
  },
  low: {
    notes: [{ hz: 392, wave: "sine", start: 0, duration: 0.42, gain: 0.25 }],
  },
  high: {
    notes: [{ hz: 1319, wave: "sine", start: 0, duration: 0.3, gain: 0.2 }],
  },
  soft: {
    notes: [
      { hz: 659, wave: "triangle", start: 0, duration: 0.55, gain: 0.13 },
    ],
  },
  click: {
    notes: [{ hz: 196, wave: "square", start: 0, duration: 0.07, gain: 0.11 }],
  },
  double: {
    notes: [
      { hz: 784, wave: "sine", start: 0, duration: 0.16, gain: 0.21 },
      { hz: 784, wave: "sine", start: 0.22, duration: 0.16, gain: 0.21 },
    ],
  },
  rising: {
    notes: [
      { hz: 440, wave: "sine", start: 0, duration: 0.18, gain: 0.2 },
      { hz: 880, wave: "sine", start: 0.2, duration: 0.24, gain: 0.22 },
    ],
  },
  falling: {
    notes: [
      { hz: 988, wave: "sine", start: 0, duration: 0.18, gain: 0.2 },
      { hz: 494, wave: "sine", start: 0.2, duration: 0.24, gain: 0.22 },
    ],
  },
  knock: {
    notes: [
      { hz: 196, wave: "square", start: 0, duration: 0.08, gain: 0.12 },
      { hz: 196, wave: "square", start: 0.16, duration: 0.08, gain: 0.12 },
    ],
  },
  pulse: {
    notes: [
      { hz: 659, wave: "triangle", start: 0, duration: 0.1, gain: 0.16 },
      { hz: 659, wave: "triangle", start: 0.14, duration: 0.1, gain: 0.16 },
      { hz: 659, wave: "triangle", start: 0.28, duration: 0.1, gain: 0.16 },
    ],
  },
  bell: {
    notes: [
      { hz: 784, wave: "sine", start: 0, duration: 0.5, gain: 0.18 },
      { hz: 1568, wave: "sine", start: 0, duration: 0.36, gain: 0.06 },
    ],
  },
  sparkle: {
    notes: [
      { hz: 1047, wave: "sine", start: 0, duration: 0.1, gain: 0.15 },
      { hz: 1319, wave: "sine", start: 0.12, duration: 0.1, gain: 0.15 },
      { hz: 1568, wave: "sine", start: 0.24, duration: 0.16, gain: 0.16 },
    ],
  },
} as const satisfies Record<string, Tone>;

export type NotificationSoundName = keyof typeof tones;

export const notificationSoundNames = Object.freeze(
  Object.keys(tones) as NotificationSoundName[],
);

export function isNotificationSoundName(
  value: unknown,
): value is NotificationSoundName {
  return (
    typeof value === "string" &&
    Object.prototype.hasOwnProperty.call(tones, value)
  );
}

// Spelled out rather than read back from `tones`: the record is indexed by
// arbitrary strings, so a lookup is `Tone | undefined` and the default must not
// be.
const defaultTone: Tone = tones.chime;

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
  playTone(sound);
}

/**
 * Preview a selected Agent tone from the settings button's user gesture.
 *
 * This deliberately does not alter or consult the independent global inbox
 * sound preference. Auditioning one choice must never opt the reader into
 * future notification audio.
 */
export function previewNotificationTone(sound: NotificationSoundName): void {
  playTone(sound);
}

function playTone(sound?: string | null): void {
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
    for (const note of tone.notes) {
      const oscillator = ctx.createOscillator();
      const gain = ctx.createGain();
      const start = ctx.currentTime + note.start;
      const end = start + note.duration;
      oscillator.connect(gain);
      gain.connect(ctx.destination);
      oscillator.frequency.value = note.hz;
      oscillator.type = note.wave;
      // Ramp rather than switch: an instant edge on a square wave clicks
      // audibly on some devices. Short patterns keep a shorter attack so their
      // rhythm remains crisp without producing an uncontrolled edge.
      const attack = Math.min(0.01, note.duration / 4);
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(note.gain, start + attack);
      gain.gain.exponentialRampToValueAtTime(0.0001, end);
      oscillator.start(start);
      oscillator.stop(end + 0.01);
      // Only the nodes are disposable; the context is not. Releasing the nodes
      // keeps a long-lived context from accumulating them.
      oscillator.onended = () => {
        oscillator.disconnect();
        gain.disconnect();
      };
    }
  } catch {
    /* no device, or autoplay blocked: silence is acceptable */
  }
}
