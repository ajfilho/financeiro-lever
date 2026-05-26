/**
 * BuyGoods Postback Receiver — Cloudflare Worker
 *
 * Endpoints:
 *   GET/POST /postback  — recebe webhook de venda da BuyGoods (token na query)
 *   GET      /pending   — saldo pendente desde o último payout marcado (read token)
 *   POST     /mark-payout?ts=ISO — marca um payout (read token); chamada pelo
 *                         dashboard quando detecta deposit BUYGOODS na LootRush
 *   GET      /sales     — lista últimas vendas pra debug (read token)
 *   GET      /          — health check
 *
 * Bindings necessários:
 *   - KV namespace `SALES`
 *   - Env vars (secrets) `POSTBACK_TOKEN` e `READ_TOKEN`
 */

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

async function readParams(request) {
  // Junta query string + body (form-urlencoded ou JSON) em URLSearchParams
  const url = new URL(request.url);
  const params = new URLSearchParams(url.search);
  if (request.method === "POST") {
    try {
      const ct = (request.headers.get("Content-Type") || "").toLowerCase();
      if (ct.includes("application/x-www-form-urlencoded")) {
        const body = await request.text();
        new URLSearchParams(body).forEach((v, k) => params.set(k, v));
      } else if (ct.includes("application/json")) {
        const body = await request.json();
        for (const [k, v] of Object.entries(body)) params.set(k, String(v));
      } else if (ct.includes("multipart/form-data")) {
        const fd = await request.formData();
        for (const [k, v] of fd.entries()) params.set(k, String(v));
      }
    } catch (e) { /* ignore */ }
  }
  return params;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/$/, "") || "/";

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS });
    }

    // ── 1) /postback ────────────────────────────────────────────────────────
    if (path === "/postback") {
      const params = await readParams(request);
      if (params.get("token") !== env.POSTBACK_TOKEN) {
        return json({ error: "Forbidden" }, 403);
      }

      const commission = parseFloat(
        params.get("commission") ||
        params.get("commission_amount") ||
        params.get("amount") || "0"
      );
      const orderId =
        params.get("order") ||
        params.get("orderid") ||
        params.get("orderId") ||
        params.get("transactionid") ||
        `auto-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const subId = params.get("subid") || params.get("subId") || "";
      const product = params.get("product") || params.get("productid") || "";
      const status = (params.get("status") || params.get("event") || "sale").toLowerCase();
      const ts = new Date().toISOString();

      const sale = { orderId, subId, product, commission, status, ts };
      await env.SALES.put(`sale:${orderId}`, JSON.stringify(sale));

      return json({ ok: true, recorded: sale });
    }

    // ── 2) /pending ─────────────────────────────────────────────────────────
    if (path === "/pending") {
      if (url.searchParams.get("token") !== env.READ_TOKEN) {
        return json({ error: "Forbidden" }, 403);
      }

      const lastPayoutISO = (await env.SALES.get("meta:last_payout")) || "1970-01-01T00:00:00Z";
      const lastPayoutDate = new Date(lastPayoutISO);

      // Ajuste manual (baseline)
      let manualAmount = 0;
      let manualNote = "";
      let manualUpdated = "";
      try {
        const raw = await env.SALES.get("meta:manual_adjustment");
        if (raw) {
          const a = JSON.parse(raw);
          manualAmount = a.amount || 0;
          manualNote = a.note || "";
          manualUpdated = a.updated_at || "";
        }
      } catch {}

      let salesTotal = 0;
      let count = 0;
      const recent = [];
      let cursor;
      do {
        const page = await env.SALES.list({ prefix: "sale:", cursor });
        for (const key of page.keys) {
          const raw = await env.SALES.get(key.name);
          if (!raw) continue;
          let sale;
          try { sale = JSON.parse(raw); } catch { continue; }
          if (new Date(sale.ts) > lastPayoutDate && sale.status !== "refund" && sale.status !== "chargeback") {
            salesTotal += sale.commission || 0;
            count++;
            recent.push({
              ts: sale.ts,
              orderId: sale.orderId,
              commission: sale.commission,
              product: sale.product,
              status: sale.status,
            });
          }
        }
        cursor = page.list_complete ? undefined : page.cursor;
      } while (cursor);

      recent.sort((a, b) => b.ts.localeCompare(a.ts));

      const salesUsd = Math.round(salesTotal * 100) / 100;
      const manualUsd = Math.round(manualAmount * 100) / 100;
      const totalUsd = Math.round((salesTotal + manualAmount) * 100) / 100;

      return json({
        pending_usd: totalUsd,
        sales_usd: salesUsd,
        manual_adjustment_usd: manualUsd,
        manual_adjustment_note: manualNote,
        manual_adjustment_updated: manualUpdated,
        sales_count: count,
        last_payout: lastPayoutISO,
        recent_sales: recent.slice(0, 10),
        ts: new Date().toISOString(),
      });
    }

    // ── 2b) /adjust ─────────────────────────────────────────────────────────
    if (path === "/adjust") {
      if (url.searchParams.get("token") !== env.READ_TOKEN) {
        return json({ error: "Forbidden" }, 403);
      }
      const params = await readParams(request);
      const amount = parseFloat(params.get("amount") || "0");
      if (isNaN(amount)) return json({ error: "Invalid amount" }, 400);
      const note = params.get("note") || "";
      const updated_at = new Date().toISOString();
      await env.SALES.put("meta:manual_adjustment", JSON.stringify({ amount, note, updated_at }));
      return json({ ok: true, amount, note, updated_at });
    }

    // ── 3) /mark-payout ─────────────────────────────────────────────────────
    if (path === "/mark-payout") {
      if (url.searchParams.get("token") !== env.READ_TOKEN) {
        return json({ error: "Forbidden" }, 403);
      }
      const ts = url.searchParams.get("ts") || new Date().toISOString();
      await env.SALES.put("meta:last_payout", ts);
      // Payout incluiu tudo: vendas + baseline manual. Zera o manual.
      await env.SALES.put("meta:manual_adjustment", JSON.stringify({ amount: 0, note: "", updated_at: ts }));
      return json({ ok: true, marked: ts });
    }

    // ── 4) /sales (debug) ───────────────────────────────────────────────────
    if (path === "/sales") {
      if (url.searchParams.get("token") !== env.READ_TOKEN) {
        return json({ error: "Forbidden" }, 403);
      }
      const limit = Math.min(parseInt(url.searchParams.get("limit") || "50"), 1000);
      const sales = [];
      let cursor;
      do {
        const page = await env.SALES.list({ prefix: "sale:", cursor, limit: 100 });
        for (const key of page.keys) {
          const raw = await env.SALES.get(key.name);
          if (raw) { try { sales.push(JSON.parse(raw)); } catch {} }
          if (sales.length >= limit) break;
        }
        cursor = page.list_complete || sales.length >= limit ? undefined : page.cursor;
      } while (cursor);
      sales.sort((a, b) => b.ts.localeCompare(a.ts));
      return json({ sales: sales.slice(0, limit), count: sales.length });
    }

    // ── 5) Health ───────────────────────────────────────────────────────────
    if (path === "/" || path === "/health") {
      return json({
        service: "buygoods-postback",
        endpoints: ["/postback (token)", "/pending (token)", "/mark-payout?ts=... (token)", "/sales (token)"],
      });
    }

    return json({ error: "Not Found", path }, 404);
  },
};
