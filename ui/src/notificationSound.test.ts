import { afterEach, describe, expect, it, vi } from "vitest";

import {
  playNotificationTone,
  resetNotificationAudio,
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
  resetNotificationAudio();
});

interface OscillatorStub {
  connect: ReturnType<typeof vi.fn>;
  disconnect: ReturnType<typeof vi.fn>;
  frequency: { value: number };
  type: OscillatorType;
  start: ReturnType<typeof vi.fn>;
  stop: ReturnType<typeof vi.fn>;
  onended: (() => void) | null;
}

interface ContextStub {
  state: string;
  resume: ReturnType<typeof vi.fn>;
}

function audioStub() {
  const oscillators: OscillatorStub[] = [];
  const constructed: ContextStub[] = [];
  const make = () => {
    const oscillator = {
      connect: vi.fn(),
      disconnect: vi.fn(),
      frequency: { value: 0 },
      type: "" as OscillatorType,
      start: vi.fn(),
      stop: vi.fn(),
      onended: null as (() => void) | null,
    };
    oscillators.push(oscillator);
    return oscillator;
  };
  const Ctor = vi.fn(function AudioContextStub(this: unknown) {
    const ctx = {
      createOscillator: make,
      createGain: () => ({
        connect: vi.fn(),
        disconnect: vi.fn(),
        gain: {
          setValueAtTime: vi.fn(),
          exponentialRampToValueAtTime: vi.fn(),
        },
      }),
      currentTime: 0,
      destination: {},
      state: "running",
      resume: vi.fn().mockResolvedValue(undefined),
    };
    constructed.push(ctx);
    return ctx;
  });
  vi.stubGlobal("AudioContext", Ctor);
  return { oscillators, constructed };
}

describe("toneFor", () => {
  it("resolves each word in the set_agent_notify_sound vocabulary", () => {
    expect(Object.keys(tones)).toEqual([
      "chime",
      "low",
      "high",
      "soft",
      "click",
      "double",
      "rising",
      "falling",
      "knock",
      "pulse",
      "bell",
      "sparkle",
    ]);
    for (const [word, tone] of Object.entries(tones)) {
      expect(toneFor(word)).toEqual(tone);
    }
  });

  it("keeps all twelve audible patterns structurally distinct", () => {
    const signatures = Object.values(tones).map((tone) =>
      JSON.stringify(tone.notes),
    );
    expect(new Set(signatures).size).toBe(signatures.length);
    for (const tone of Object.values(tones)) {
      expect(tone.notes.length).toBeGreaterThan(0);
      for (const note of tone.notes) {
        expect(note.hz).toBeGreaterThanOrEqual(196);
        expect(note.hz).toBeLessThanOrEqual(1568);
        expect(note.duration).toBeGreaterThan(0);
        expect(note.duration).toBeLessThanOrEqual(0.55);
      }
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

  it("plays the sender's tone", () => {
    const { oscillators } = audioStub();

    playNotificationTone("click", memoryStorage({ [soundPreferenceKey]: "on" }));

    expect(oscillators).toHaveLength(1);
    expect(oscillators[0]!.frequency).toEqual({
      value: tones.click.notes[0].hz,
    });
    expect(oscillators[0]!.type).toBe(tones.click.notes[0].wave);
    expect(oscillators[0]!.start).toHaveBeenCalledOnce();
  });

  it("schedules every note in a rhythmic pattern", () => {
    const { oscillators } = audioStub();

    playNotificationTone(
      "sparkle",
      memoryStorage({ [soundPreferenceKey]: "on" }),
    );

    expect(oscillators).toHaveLength(tones.sparkle.notes.length);
    expect(oscillators.map((oscillator) => oscillator.frequency.value)).toEqual(
      tones.sparkle.notes.map((note) => note.hz),
    );
    expect(oscillators[1]!.start).toHaveBeenCalledWith(
      tones.sparkle.notes[1].start,
    );
  });

  it("reuses one context across notifications and releases only the nodes", () => {
    // The regression this guards: one context per notification hits the
    // browser's per-document cap during a burst, construction starts throwing,
    // and the silence is swallowed by the catch -- "sometimes there is no
    // sound", with nothing in the console to say why.
    const { oscillators, constructed } = audioStub();
    const storage = memoryStorage({ [soundPreferenceKey]: "on" });

    playNotificationTone("chime", storage);
    playNotificationTone("high", storage);
    playNotificationTone("soft", storage);

    expect(constructed).toHaveLength(1);
    expect(oscillators).toHaveLength(3);

    oscillators[0]!.onended?.();
    expect(oscillators[0]!.disconnect).toHaveBeenCalledOnce();
  });

  it("stays quiet when resuming the context is refused", async () => {
    // A rejected resume must not surface as an unhandled rejection: the
    // notification is already lost at that point and there is nothing to do
    // about it.
    const { constructed } = audioStub();
    const storage = memoryStorage({ [soundPreferenceKey]: "on" });

    playNotificationTone("chime", storage);
    const ctx = constructed[0]!;
    ctx.state = "suspended";
    ctx.resume.mockRejectedValueOnce(new Error("not allowed"));

    expect(() => playNotificationTone("chime", storage)).not.toThrow();
    await Promise.resolve();
  });

  it("resumes a context the browser suspended", () => {
    const { constructed } = audioStub();
    const storage = memoryStorage({ [soundPreferenceKey]: "on" });

    playNotificationTone("chime", storage);
    const ctx = constructed[0]!;
    ctx.state = "suspended";

    playNotificationTone("chime", storage);

    expect(ctx.resume).toHaveBeenCalledOnce();
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
