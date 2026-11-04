// A minimal server-sent-events parser over a fetch ``ReadableStream`` (``EventSource`` cannot POST).
// Comment lines (``: heartbeat``) are ignored; one message per blank-line-terminated block.

export interface SseMessage {
  id?: string;
  event: string;
  data: string;
}

export class SseParser {
  private buffer = "";
  private carriageReturn = false; // a chunk ended in "\r": its "\n" may start the next chunk

  push(chunk: string): SseMessage[] {
    let text = chunk;
    if (this.carriageReturn && text.startsWith("\n")) text = text.slice(1);
    this.carriageReturn = text.endsWith("\r");
    this.buffer += text.replace(/\r\n?/g, "\n");
    const messages: SseMessage[] = [];
    let cut: number;
    while ((cut = this.buffer.indexOf("\n\n")) >= 0) {
      const block = this.buffer.slice(0, cut);
      this.buffer = this.buffer.slice(cut + 2);
      const message = parseBlock(block);
      if (message) messages.push(message);
    }
    return messages;
  }

  /** Text left after the stream ended without a terminating blank line (a truncated message). */
  get pending(): string {
    return this.buffer;
  }
}

function parseBlock(block: string): SseMessage | null {
  let event = "message";
  let id: string | undefined;
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
    else if (field === "id") id = value;
  }
  if (!data.length) return null;
  return { id, event, data: data.join("\n") };
}
