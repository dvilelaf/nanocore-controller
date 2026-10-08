import type { MessageListener, MidiPortInfo, MidiTransport, OutgoingMessage } from '../midi/types';

const TEST_OUTPUT: MidiPortInfo = { id: 'test', name: 'Test output' };

/**
 * A transport that records what the editor would send and talks to nothing. The production editor
 * has one transport (the server); the tests that exercise the editor without a server register
 * this one under the kind 'test' (see setupTests.ts) and use it through `initTransport('test')`.
 */
export class FakeTransport implements MidiTransport {
  readonly kind = 'test' as const;
  readonly label = 'Test transport';

  private listeners = new Set<MessageListener>();

  async init(): Promise<void> {
    // Nothing to set up.
  }

  listOutputs(): MidiPortInfo[] {
    return [TEST_OUTPUT];
  }

  onPortsChanged(): () => void {
    return () => {};
  }

  private emit(msg: OutgoingMessage) {
    this.listeners.forEach((cb) => cb(msg));
  }

  sendCC(_outputId: string, channel: number, cc: number, value: number, description?: string): void {
    this.emit({ kind: 'cc', channel, cc, value, timestamp: performance.now(), description });
  }

  sendProgramChange(_outputId: string, channel: number, program: number, description?: string): void {
    this.emit({ kind: 'pc', channel, program, timestamp: performance.now(), description });
  }

  sendSysEx(_outputId: string, bytes: number[], description?: string): void {
    this.emit({ kind: 'sysex', bytes, timestamp: performance.now(), description });
  }

  onMessageSent(cb: MessageListener): () => void {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  }
}
