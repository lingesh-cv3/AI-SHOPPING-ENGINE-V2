/**
 * A Server-Sent Events parser for `fetch` + `ReadableStream` (browser `EventSource` cannot POST or send an Authorization header, which the chat stream needs).
 *
 * Pure and DOM-free so it can be tested directly. Follows the SSE field rules: events end at a blank line; `data:` lines of one event are joined with "\n"; `id` and `event`
 * are kept; lines starting with ":" are comments (heartbeats) and surface only through `onComment`; `retry` and unknown fields are ignored; `\r\n`, `\n` and a lone `\r`
 * all end a line; one leading space after the colon is dropped; a chunk may end anywhere, including inside a line or between the `\r` and `\n` of a line ending.
 *
 * It never throws on malformed input: it just keeps reading. Deciding what an event MEANS (JSON, ids, duplicates) is the caller's job.
 */

export interface SseEvent {
  /** The `id:` of the event, if any (a string, as sent). */
  id: string | undefined;
  /** The `event:` name; "message" when none was sent, per the SSE spec. */
  event: string;
  /** The joined `data:` lines. */
  data: string;
}

export interface SseParser {
  /** Feed the next decoded text chunk. */
  feed(chunk: string): void;
  /** The stream ended: an event still being built is discarded (per the SSE spec an event is only valid once its blank line has arrived). */
  end(): void;
}

export function createSseParser(onEvent: (event: SseEvent) => void, onComment?: (text: string) => void): SseParser {
  let buffer = "";
  let pendingCr = false; // the previous chunk ended with "\r": a following "\n" belongs to the same line ending
  let id: string | undefined;
  let eventName = "";
  let dataLines: string[] = [];
  let hasData = false;

  function dispatch() {
    if (hasData) {
      onEvent({ id, event: eventName || "message", data: dataLines.join("\n") });
    }
    // `id` persists across events in a real EventSource; here every event we care about carries its own, so reset to keep events independent.
    id = undefined;
    eventName = "";
    dataLines = [];
    hasData = false;
  }

  function line(text: string) {
    if (text === "") {
      dispatch();
      return;
    }
    if (text.startsWith(":")) {
      onComment?.(text.slice(1).trimStart());
      return;
    }
    const colon = text.indexOf(":");
    const field = colon === -1 ? text : text.slice(0, colon);
    let value = colon === -1 ? "" : text.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") {
      dataLines.push(value);
      hasData = true;
    } else if (field === "event") {
      eventName = value;
    } else if (field === "id") {
      if (!value.includes("\0")) id = value;
    }
    // "retry" and anything else: ignored
  }

  return {
    feed(chunk: string) {
      let text = chunk;
      if (pendingCr) {
        pendingCr = false;
        if (text.startsWith("\n")) text = text.slice(1);
      }
      buffer += text;
      let start = 0;
      for (let i = 0; i < buffer.length; i++) {
        const ch = buffer[i];
        if (ch === "\n" || ch === "\r") {
          line(buffer.slice(start, i));
          if (ch === "\r") {
            if (i + 1 < buffer.length) {
              if (buffer[i + 1] === "\n") i++;
            } else {
              pendingCr = true;
            }
          }
          start = i + 1;
        }
      }
      buffer = buffer.slice(start);
    },
    end() {
      buffer = "";
      id = undefined;
      eventName = "";
      dataLines = [];
      hasData = false;
    },
  };
}
