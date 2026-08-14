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
 * Play one short tone for `sound`. Pass nothing when the caller cannot know who
 * sent the message — that yields the default tone, which is what every caller
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
  const Ctor = audioContextConstructor();
  if (Ctor === null) {
    return;
  }
  const tone = toneFor(sound);
  try {
    const ctx = new Ctor();
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
    oscillator.onended = () => {
      // The server-rendered original reloaded the page after every ding, so a
      // leaked context never outlived one notification. This client does not
      // reload, so an unclosed context per message would accumulate until the
      // browser refuses to open more.
      void ctx.close().catch(() => undefined);
    };
  } catch {
    /* no device, or autoplay blocked: silence is acceptable */
  }
}
