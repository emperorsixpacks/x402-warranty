import { McpServer } from "@modelcontextprotocol/server";
import { serveStdio } from "@modelcontextprotocol/server/stdio";
import * as z from "zod/v4";

import { proposeX402, x402OutputSchema } from "./paybox.js";

const SUPPORTED_PROTOCOL_VERSIONS = ["2025-11-25", "2026-07-28"];
const WARRANTY_BASE_URL = process.env.WARRANTY_BASE_URL || "https://warranty.example.com";
const DEFAULT_CHAIN = process.env.WARRANTY_CHAIN || "eip155:8453";

function createServer() {
  const server = new McpServer(
    { name: "x402-warranty", version: "0.1.0" },
    { supportedProtocolVersions: SUPPORTED_PROTOCOL_VERSIONS },
  );

  server.registerTool(
    "quote_warranty",
    {
      title: "Quote warranty call",
      description: "Get the warranty tier and price for a target upstream URL without paying.",
      inputSchema: z.object({
        url: z.string().url().max(2048),
        method: z.enum(["GET", "POST"]).optional(),
      }).strict(),
      outputSchema: z.object({
        tier: z.string(),
        network: z.string(),
        warranted: z.boolean(),
        upstream_price_atomic: z.string(),
        premium_atomic: z.string(),
        warranty_price_atomic: z.string(),
        upstream_url: z.string(),
        call_url: z.string(),
        host: z.string(),
        host_stats: z.record(z.unknown()),
        reason: z.string().nullable().optional(),
      }).strict(),
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    async ({ url, method }) => {
      const target = `${WARRANTY_BASE_URL}/v1/warranty/quote?url=${encodeURIComponent(url)}&method=${method || "GET"}`;
      const res = await fetch(target, { method: "GET" });
      if (!res.ok) {
        return { content: [{ type: "text", text: `Quote failed: ${res.status}` }], isError: true };
      }
      const quote = await res.json() as { tier: string; upstream_price_atomic: string; warranty_price_atomic: string };
      return {
        content: [{ type: "text", text: `Tier: ${quote.tier}, upstream: ${quote.upstream_price_atomic}, warranty: ${quote.warranty_price_atomic}` }],
        structuredContent: quote,
      };
    },
  );

  server.registerTool(
    "call_warranty",
    {
      title: "Call an upstream API through x402 Warranty",
      description: "Proposes a paid x402 request to the warranty proxy, which calls the upstream and refunds if the response is junk.",
      inputSchema: z.object({
        url: z.string().url().max(2048),
        method: z.enum(["GET", "POST"]).optional(),
        body: z.unknown().optional(),
        headers: z.record(z.string(), z.string()).optional(),
        expect: z.object({
          json_schema: z.record(z.string(), z.unknown()).optional(),
          must_contain: z.array(z.string()).optional(),
          content_type: z.string().optional(),
          min_bytes: z.number().int().optional(),
        }).strict().optional(),
        max_price_usd: z.string().optional(),
        tier: z.enum(["tier1", "tier2", "tier3"]).optional(),
      }).strict(),
      outputSchema: x402OutputSchema,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    async (input) => {
      const method = input.method ?? "GET";
      const query = `?url=${encodeURIComponent(input.url)}&method=${method}`;
      let callUrl: string;
      let maxAmount: string;
      let chain: string;
      if (input.tier) {
        // Tier fallback avoids a network round-trip (keeps the conformance
        // sample offline). The ceiling is 1.5x the tier cap to cover the
        // maximum dynamic premium (38% of the upstream cap).
        callUrl = `${WARRANTY_BASE_URL}/v1/warranty/call/${input.tier}${query}`;
        maxAmount = input.tier === "tier1" ? "75000" : input.tier === "tier2" ? "375000" : "1500000";
        chain = DEFAULT_CHAIN;
      } else {
        const quoteRes = await fetch(`${WARRANTY_BASE_URL}/v1/warranty/quote?url=${encodeURIComponent(input.url)}&method=${method}`);
        const quote = await quoteRes.json() as { call_url?: string; warranty_price_atomic?: string; network?: string; warranted?: boolean; reason?: string };
        if (!quote.call_url || !quote.warranty_price_atomic || !quote.network) {
          return { content: [{ type: "text", text: `Quote failed: ${JSON.stringify(quote)}` }], isError: true };
        }
        if (!quote.warranted) {
          return { content: [{ type: "text", text: `Warranty not offered: ${quote.reason ?? "unknown reason"}` }], isError: true };
        }
        // call_url already carries ?url=...&method=... for the price callback.
        callUrl = quote.call_url;
        maxAmount = quote.warranty_price_atomic;
        chain = quote.network;
      }
      const payload: Record<string, unknown> = {
        url: input.url,
        method,
        headers: input.headers ?? {},
        expect: input.expect ?? {},
        max_price_usd: input.max_price_usd ?? "1.00",
      };
      if (input.body !== undefined) payload.body = input.body;
      return proposeX402({
        chain,
        url: callUrl,
        method: "POST",
        body: payload as any,
        maxAmount,
      });
    },
  );

  return server;
}

serveStdio(createServer, { legacy: "serve" });
