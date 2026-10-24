import { test, expect } from '@playwright/test';
import { createSseParser, type SseEvent } from '../src/sse';

function run(chunks: string[]) {
  const events: SseEvent[] = [];
  const comments: string[] = [];
  const p = createSseParser((e) => events.push(e), (c) => comments.push(c));
  for (const c of chunks) p.feed(c);
  p.end();
  return { events, comments };
}

test('parses id, event and data; defaults the event name to "message"', () => {
  const { events } = run(['id: 7\nevent: delta\ndata: {"a":1}\n\ndata: x\n\n']);
  expect(events).toEqual([
    { id: '7', event: 'delta', data: '{"a":1}' },
    { id: undefined, event: 'message', data: 'x' },
  ]);
});

test('joins multiple data lines with a newline and drops one leading space only', () => {
  const { events } = run(['data:  two spaces\ndata: next\n\n']);
  expect(events[0].data).toBe(' two spaces\nnext');
});

test('a chunk may end anywhere, even between \\r and \\n or inside a field', () => {
  const whole = 'id: 1\r\nevent: a\r\ndata: hello\r\n\r\n';
  for (let cut = 1; cut < whole.length; cut++) {
    const { events } = run([whole.slice(0, cut), whole.slice(cut)]);
    expect(events, `cut at ${cut}`).toEqual([{ id: '1', event: 'a', data: 'hello' }]);
  }
});

test('comments (heartbeats) are surfaced separately and never become events', () => {
  const { events, comments } = run([': ping\n\ndata: y\n\n']);
  expect(comments).toEqual(['ping']);
  expect(events).toHaveLength(1);
});

test('an event cut off before its blank line is discarded', () => {
  const { events } = run(['id: 1\nevent: delta\ndata: {"half":']);
  expect(events).toEqual([]);
});

test('unknown fields and retry are ignored; garbage never throws', () => {
  const { events } = run(['retry: 1000\nfoo: bar\n\n\n\nnot a field\n\ndata: ok\n\n']);
  expect(events).toEqual([{ id: undefined, event: 'message', data: 'ok' }]);
});

test('events do not inherit the previous event id', () => {
  const { events } = run(['id: 1\ndata: a\n\ndata: b\n\n']);
  expect(events.map((e) => e.id)).toEqual(['1', undefined]);
});
