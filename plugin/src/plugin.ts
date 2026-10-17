import { McpServer } from "@modelcontextprotocol/server";
import { serveStdio } from "@modelcontextprotocol/server/stdio";
import * as z from "zod/v4";

import { proposeX402, x402OutputSchema } from "./paybox.js";

const SUPPORTED_PROTOCOL_VERSIONS = ["2025-11-25", "2026-07-28"];
const WARRANTY_BASE_URL = process.env.WARRANTY_BASE_URL || "https://warranty.example.com";
const TWO_KEYS_BASE_URL = process.env.TWO_KEYS_BASE_URL || "https://two-keys.example.com";
const DEFAULT_CHAIN = process.env.WARRANTY_CHAIN || "eip155:8453";

function createServer() {
  const server = new McpServer(
    { name: "paybox-two-keys", version: "0.2.0" },
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
        host_stats: z.record(z.string(), z.unknown()),
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

  // -------------------------------------------------------------------------
  // Two Keys tools
  // -------------------------------------------------------------------------

  server.registerTool(
    "two_keys_start",
    {
      title: "Start a Two Keys mystery session",
      description: "Starts a new game session and returns the case file, suspects, evidence menu, and budget.",
      inputSchema: z.object({
        team_name: z.string().max(64).optional(),
        base_url: z.string().url().max(2048).optional(),
      }).strict(),
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: false,
      },
    },
    async ({ team_name, base_url }) => {
      const url = `${base_url ?? TWO_KEYS_BASE_URL}/v1/game/start`;
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ team_name: team_name || "Team PayBox" }),
      });
      if (!res.ok) {
        return { content: [{ type: "text", text: `Start failed: ${res.status}` }], isError: true };
      }
      const data = await res.json() as any;
      const evidenceList = (data.evidence_menu || [])
        .map((ev: any) => `  ${ev.id}. ${ev.name} - ${ev.price} - ${ev.teaser}`)
        .join("\n");
      const text = `Session: ${data.session_id}\nBudget: ${data.budget}\n\nCase File:\n${data.case_file}\n\nSuspects:\n${(data.suspects || []).map((s: any) => `  ${s.id}: ${s.name} (${s.role})`).join("\n")}\n\nEvidence Menu:\n${evidenceList}\n\nRules:\n${data.rules}`;
      return {
        content: [{ type: "text", text }],
        structuredContent: data,
      };
    },
  );

  server.registerTool(
    "two_keys_buy_evidence",
    {
      title: "Buy a Two Keys evidence clue",
      description: "Proposes the x402 payment to buy one evidence source for a session. Pass the exact price_atomic from the evidence menu to keep the charge at the listed price.",
      inputSchema: z.object({
        session_id: z.string().max(64),
        evidence_id: z.number().int().min(1).max(6),
        price_atomic: z.string().regex(/^[1-9][0-9]{0,19}$/),
        base_url: z.string().url().max(2048).optional(),
      }).strict(),
      outputSchema: x402OutputSchema,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: false,
      },
    },
    async ({ session_id, evidence_id, price_atomic, base_url }) => {
      const url = `${base_url ?? TWO_KEYS_BASE_URL}/v1/game/${session_id}/evidence/${evidence_id}`;
      return proposeX402({
        chain: DEFAULT_CHAIN,
        url,
        method: "POST",
        body: {},
        maxAmount: price_atomic,
      });
    },
  );

  server.registerTool(
    "two_keys_accuse",
    {
      title: "Make a Two Keys final accusation",
      description: "Proposes the x402 payment to accuse one suspect. This is a one-shot, paid action.",
      inputSchema: z.object({
        session_id: z.string().max(64),
        suspect: z.string().max(64),
        price_atomic: z.string().regex(/^[1-9][0-9]{0,19}$/),
        base_url: z.string().url().max(2048).optional(),
      }).strict(),
      outputSchema: x402OutputSchema,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: false,
      },
    },
    async ({ session_id, suspect, price_atomic, base_url }) => {
      const url = `${base_url ?? TWO_KEYS_BASE_URL}/v1/game/${session_id}/accuse`;
      return proposeX402({
        chain: DEFAULT_CHAIN,
        url,
        method: "POST",
        body: { suspect },
        maxAmount: price_atomic,
      });
    },
  );

  server.registerTool(
    "two_keys_receipt",
    {
      title: "Get Two Keys receipt",
      description: "Fetches the receipt for a session, showing every purchase, tx hash, duplicates, and score.",
      inputSchema: z.object({
        session_id: z.string().max(64),
        base_url: z.string().url().max(2048).optional(),
      }).strict(),
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    async ({ session_id, base_url }) => {
      const url = `${base_url ?? TWO_KEYS_BASE_URL}/v1/game/${session_id}/receipt`;
      const res = await fetch(url);
      if (!res.ok) {
        return { content: [{ type: "text", text: `Receipt fetch failed: ${res.status}` }], isError: true };
      }
      const data = await res.json() as any;
      const items = (data.purchases || [])
        .map((p: any) => `  - ${p.name}: ${p.price} (duplicate: ${p.duplicate}, helped: ${p.helped}, tx: ${p.tx_hash ?? "n/a"})`)
        .join("\n");
      const text = `Receipt for ${data.session_id}\nTeam: ${data.team_name}\nBudget: ${data.budget} | Spent: ${data.spent} | Remaining: ${data.remaining}\nAccused: ${data.accused} | Won: ${data.won}\n\nPurchases:\n${items || "  (none)"}`;
      return {
        content: [{ type: "text", text }],
        structuredContent: data,
      };
    },
  );

  return server;
}

serveStdio(createServer, { legacy: "serve" });
