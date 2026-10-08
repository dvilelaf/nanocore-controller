import '@testing-library/jest-dom/vitest';
import { registerTestTransport } from './store/patchStore';
import { FakeTransport } from './test/fakeTransport';

// Tests that exercise the editor without a server use initTransport('test').
registerTestTransport(new FakeTransport());
