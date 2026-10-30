import { describe, expect, it } from "vitest";
import { readSse, SseParser } from "../api/sse";
import { citationNumber, linkCitations } from "./citations";
import { pageLabel, relativeTime } from "./format";

describe("SseParser", () => {
  it("parses frames however the stream is split", () => {
    const body = 'event: delta\ndata: {"text":"Every"}\n\nevent: done\ndata: {"ok":true}\n\n';
    for (const size of [1, 3, 7, body.length]) {
      const parser = new SseParser();
      const messages = [];
      for (let i = 0; i < body.length; i += size) messages.push(...parser.push(body.slice(i, i + size)));
      expect(messages).toEqual([
        { event: "delta", data: '{"text":"Every"}' },
        { event: "done", data: '{"ok":true}' },
      ]);
    }
  });

  it("handles CRLF, comments, multi-line data and the default event name", () => {
    const parser = new SseParser();
    expect(parser.push(": keep-alive\r\n\r\ndata: a\r\ndata: b\r\n\r\n")).toEqual([{ event: "message", data: "a\nb" }]);
  });

  it("reads a byte stream with multi-byte characters split across chunks", async () => {
    const bytes = new TextEncoder().encode('event: delta\ndata: {"text":"§ 195.452 – ok"}\n\n');
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let i = 0; i < bytes.length; i += 5) controller.enqueue(bytes.slice(i, i + 5));
        controller.close();
      },
    });
    const messages = [];
    for await (const m of readSse(stream)) messages.push(m);
    expect(messages).toEqual([{ event: "delta", data: '{"text":"§ 195.452 – ok"}' }]);
  });
});

describe("citations", () => {
  it("turns [n] and [n, m] markers into citation links", () => {
    expect(linkCitations("Every 3 weeks [1]. See also [2, 4].")).toBe(
      "Every 3 weeks [1](#cite-1). See also [2](#cite-2)[4](#cite-4).",
    );
  });

  it("leaves existing links, other brackets and unfinished markers alone", () => {
    expect(linkCitations("[1](#cite-1) per 195.452(h) and [a] then [3")).toBe("[1](#cite-1) per 195.452(h) and [a] then [3");
  });

  it("reads citation numbers back from hrefs", () => {
    expect(citationNumber("#cite-12")).toBe(12);
    expect(citationNumber("https://example.com")).toBeNull();
    expect(citationNumber(undefined)).toBeNull();
  });
});

describe("format", () => {
  it("labels printed pages", () => {
    expect(pageLabel({ page_label_start: "54", page_label_end: "55" })).toBe("pp. 54–55");
    expect(pageLabel({ page_label_start: "7", page_label_end: "7" })).toBe("p. 7");
    expect(pageLabel({ page_label_start: null, page_label_end: null })).toBeNull();
  });

  it("describes recent times relatively", () => {
    const now = new Date("2026-10-29T12:00:00Z");
    expect(relativeTime("2026-10-29T11:59:30Z", now)).toBe("just now");
    expect(relativeTime("2026-10-29T11:15:00Z", now)).toBe("45 min ago");
    expect(relativeTime("2026-10-29T07:00:00Z", now)).toBe("5 h ago");
    expect(relativeTime("2026-10-27T12:00:00Z", now)).toBe("2 d ago");
  });
});
