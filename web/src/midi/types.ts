export interface MidiPortInfo {
  id: string;
  name: string;
}

/** A single outgoing message, kept around for the activity log. */
export interface OutgoingMessage {
  kind: 'cc' | 'pc' | 'sysex';
  /** 1-16. Not meaningful for 'sysex' (SysEx is channel-less) — omitted for that kind. */
  channel?: number;
  cc?: number;
  program?: number;
  value?: number;
  /** Full raw bytes (including F0/F7) for 'sysex' messages. */
  bytes?: number[];
  timestamp: number;
  /** Human-readable summary filled in by the caller, e.g. "MOD Phaser: Mix = 50". */
  description?: string;
}

export type MessageListener = (msg: OutgoingMessage) => void;

export interface MidiTransport {
  readonly kind: 'bridge' | 'test';
  readonly label: string;
  init(): Promise<void>;
  listOutputs(): MidiPortInfo[];
  onPortsChanged(cb: () => void): () => void;
  sendCC(outputId: string, channel: number, cc: number, value: number, description?: string): void;
  sendProgramChange(outputId: string, channel: number, program: number, description?: string): void;
  /** Sends raw SysEx bytes (must start with 0xF0 and end with 0xF7). See `../midi/sysex.ts` for
   * the NanoCore's own SysEx protocol (reverse-engineered from real device traffic — see
   * docs/MIDI_MAPPING_NOTES.md) and the message builders that produce these bytes. */
  sendSysEx(outputId: string, bytes: number[], description?: string): void;
  /** Subscribe to every message this transport sends (feeds the activity log). */
  onMessageSent(cb: MessageListener): () => void;
}
