import { afterEach, describe, expect, it, vi } from "vitest";

import {
  playNotificationTone,
  setSoundEnabled,
  soundEnabled,
  soundPreferenceKey,
  toneFor,
  tones,
} from "./notificationSound";

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
    read: (key: string) => values.get(key) ?? null,
  };
}

function throwingStorage() {
  return {
    getItem: () => {
      throw new Error("storage disabled");
    },
    setItem: () => {
      throw new Error("storage disabled");
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("toneFor", () => {
  it("resolves each word in the set_agent_notify_sound vocabulary", () => {
    for (const [word, tone] of Object.entries(tones)) {
      expect(toneFor(word)).toEqual(tone);
    }
  });

  it("falls back to the default tone rather than going silent", () => {
    // An unknown word must still notify. A missed notification is worse than an
    // unspecific one, which is why this returns a tone instead of undefined.
    expect(toneFor("no-such-sound")).toEqual(tones.chime);
    expect(toneFor(null)).toEqual(tones.chime);
    expect(toneFor(undefined)).toEqual(tones.chime);
    expect(toneFor("")).toEqual(tones.chime);
  });
});

describe("the sound preference", () => {
  it("is off until it is explicitly turned on", () => {
    expect(soundEnabled(memoryStorage())).toBe(false);
    expect(soundEnabled(memoryStorage({ [soundPreferenceKey]: "off" }))).toBe(
      false,
    );
    expect(soundEnabled(memoryStorage({ [soundPreferenceKey]: "on" }))).toBe(
      true,
    );
  });

  it("round-trips through storage", () => {
    const storage = memoryStorage();
    setSoundEnabled(true, storage);
    expect(storage.read(soundPreferenceKey)).toBe("on");
    setSoundEnabled(false, storage);
    expect(storage.read(soundPreferenceKey)).toBe("off");
  });

  it("treats unusable storage as off instead of throwing", () => {
    // Private mode or a blocked origin must not take down a path whose whole
    // job is to be unobtrusive.
    const storage = throwingStorage();
    expect(() => soundEnabled(storage)).not.toThrow();
    expect(soundEnabled(storage)).toBe(false);
    expect(() => setSoundEnabled(true, storage)).not.toThrow();
  });
});

describe("playNotificationTone", () => {
  it("stays silent while the preference is off", () => {
    const AudioContextSpy = vi.fn();
    vi.stubGlobal("AudioContext", AudioContextSpy);
    const storage = memoryStorage({ [soundPreferenceKey]: "off" });

    playNotificationTone("chime", storage);

    expect(AudioContextSpy).not.toHaveBeenCalled();
  });

  it("plays the sender's tone and closes the context afterwards", () => {
    const oscillator = {
      connect: vi.fn(),
      frequency: { value: 0 },
      type: "" as OscillatorType,
      start: vi.fn(),
      stop: vi.fn(),
      onended: null as (() => void) | null,
    };
    const gain = {
      connect: vi.fn(),
      gain: {
        setValueAtTime: vi.fn(),
        exponentialRampToValueAtTime: vi.fn(),
      },
    };
    const close = vi.fn().mockResolvedValue(undefined);
    // A plain function, not an arrow: the module calls `new AudioContext()`,
    // and an arrow function is not a constructor — the resulting TypeError is
    // swallowed by the silence-is-acceptable catch, so the test would fail on a
    // zeroed oscillator with no hint as to why.
    vi.stubGlobal(
      "AudioContext",
      vi.fn(function AudioContextStub(this: unknown) {
        return {
          createOscillator: () => oscillator,
          createGain: () => gain,
          currentTime: 0,
          destination: {},
          close,
        };
      }),
    );
    const storage = memoryStorage({ [soundPreferenceKey]: "on" });

    playNotificationTone("click", storage);

    expect(oscillator.frequency.value).toBe(tones.click.hz);
    expect(oscillator.type).toBe(tones.click.wave);
    expect(oscillator.start).toHaveBeenCalledOnce();

    // Unlike the server-rendered original, this client does not reload after a
    // notification, so an unclosed context per message would accumulate.
    oscillator.onended?.();
    expect(close).toHaveBeenCalledOnce();
  });

  it("survives a browser with no audio support", () => {
    vi.stubGlobal("AudioContext", undefined);
    vi.stubGlobal("webkitAudioContext", undefined);
    const storage = memoryStorage({ [soundPreferenceKey]: "on" });

    expect(() => playNotificationTone("chime", storage)).not.toThrow();
  });

  it("survives a context that refuses to start", () => {
    vi.stubGlobal(
      "AudioContext",
      vi.fn(() => {
        throw new Error("autoplay blocked");
      }),
    );
    const storage = memoryStorage({ [soundPreferenceKey]: "on" });

    expect(() => playNotificationTone("chime", storage)).not.toThrow();
  });
});
