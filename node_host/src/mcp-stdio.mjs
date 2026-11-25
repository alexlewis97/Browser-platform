/** Tiny MCP stdio server using Content-Length framing. */

export function startMcpServer({ tools, name = "browser-platform-pomerado-host" }) {
  let buffer = Buffer.alloc(0);
  process.stdin.on("data", (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    while (true) {
      const extracted = extract(buffer);
      if (!extracted) break;
      buffer = extracted.rest;
      handle(extracted.message, tools, name).catch((err) => {
        if (extracted.message?.id != null) {
          send({ jsonrpc: "2.0", id: extracted.message.id, error: { code: -32000, message: String(err) } });
        }
      });
    }
  });
  process.stdin.on("end", () => process.exit(0));
}

function extract(buf) {
  const text = buf.toString("utf8");
  if (/^content-length:/i.test(text) || text.includes("Content-Length:")) {
    const sep = text.includes("\r\n\r\n") ? "\r\n\r\n" : "\n\n";
    const idx = buf.indexOf(sep === "\r\n\r\n" ? "\r\n\r\n" : "\n\n");
    if (idx < 0) return null;
    const header = buf.slice(0, idx).toString("utf8");
    const match = header.match(/content-length:\s*(\d+)/i);
    if (!match) return null;
    const length = Number(match[1]);
    const start = idx + sep.length;
    if (buf.length < start + length) return null;
    const raw = buf.slice(start, start + length).toString("utf8");
    return { message: JSON.parse(raw), rest: buf.slice(start + length) };
  }
  const nl = buf.indexOf("\n");
  if (nl < 0) return null;
  const line = buf.slice(0, nl).toString("utf8").trim();
  const rest = buf.slice(nl + 1);
  if (!line) return { message: null, rest };
  return { message: JSON.parse(line), rest };
}

function send(message) {
  const raw = Buffer.from(JSON.stringify(message), "utf8");
  const header = Buffer.from(`Content-Length: ${raw.length}\r\n\r\n`, "ascii");
  process.stdout.write(Buffer.concat([header, raw]));
}

async function handle(message, tools, name) {
  if (!message) return;
  if (message.method === "initialize") {
    send({
      jsonrpc: "2.0",
      id: message.id,
      result: {
        protocolVersion: "2024-11-05",
        capabilities: { tools: {} },
        serverInfo: { name, version: "0.1.0" },
      },
    });
    return;
  }
  if (message.method === "notifications/initialized" || message.method?.startsWith("notifications/")) {
    return;
  }
  if (message.method === "tools/list") {
    send({
      jsonrpc: "2.0",
      id: message.id,
      result: {
        tools: tools.map((t) => ({
          name: t.name,
          description: t.description,
          inputSchema: t.inputSchema || { type: "object", properties: {} },
        })),
      },
    });
    return;
  }
  if (message.method === "tools/call") {
    const name_ = message.params?.name;
    const args = message.params?.arguments || {};
    const tool = tools.find((t) => t.name === name_);
    if (!tool) {
      send({ jsonrpc: "2.0", id: message.id, error: { code: -32601, message: `Unknown tool ${name_}` } });
      return;
    }
    const output = await tool.handler(args);
    send({
      jsonrpc: "2.0",
      id: message.id,
      result: {
        content: [{ type: "text", text: JSON.stringify(output) }],
        structuredContent: output,
      },
    });
    return;
  }
  if (message.id != null) {
    send({ jsonrpc: "2.0", id: message.id, error: { code: -32601, message: `Unknown method ${message.method}` } });
  }
}
