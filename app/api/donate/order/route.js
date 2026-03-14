import { randomUUID } from "node:crypto";
import { NextResponse } from "next/server";

export const runtime = "nodejs";

const RAZORPAY_KEY_ID = (process.env.RAZORPAY_KEY_ID || "").trim();
const RAZORPAY_KEY_SECRET = (process.env.RAZORPAY_KEY_SECRET || "").trim();
const RAZORPAY_DONATION_AMOUNT_PAISA = Number(
  process.env.RAZORPAY_DONATION_AMOUNT_PAISA || "9900"
);
const RAZORPAY_DONATION_CURRENCY = (
  process.env.RAZORPAY_DONATION_CURRENCY || "INR"
).toUpperCase();
const RAZORPAY_DONATION_NAME =
  process.env.RAZORPAY_DONATION_NAME || "Swift Convert";
const RAZORPAY_DONATION_DESCRIPTION =
  process.env.RAZORPAY_DONATION_DESCRIPTION || "Support Swift Convert";

export async function POST(request) {
  if (!RAZORPAY_KEY_ID || !RAZORPAY_KEY_SECRET) {
    return NextResponse.json(
      { error: "Razorpay is not configured on this server." },
      { status: 503 }
    );
  }

  const payload = (await request.json().catch(() => ({}))) || {};
  const rawAmount = payload.amount;
  let amount = RAZORPAY_DONATION_AMOUNT_PAISA;

  if (rawAmount !== undefined && rawAmount !== null) {
    amount = Number(rawAmount);
    if (!Number.isInteger(amount)) {
      return NextResponse.json({ error: "Invalid amount." }, { status: 400 });
    }
  }

  if (amount < 100) {
    return NextResponse.json(
      { error: "Minimum donation amount is 100 paise." },
      { status: 400 }
    );
  }

  const auth = Buffer.from(`${RAZORPAY_KEY_ID}:${RAZORPAY_KEY_SECRET}`).toString(
    "base64"
  );

  try {
    const res = await fetch("https://api.razorpay.com/v1/orders", {
      method: "POST",
      headers: {
        Authorization: `Basic ${auth}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        amount,
        currency: RAZORPAY_DONATION_CURRENCY,
        receipt: `don_${randomUUID().replace(/-/g, "").slice(0, 18)}`,
        payment_capture: 1,
      }),
      cache: "no-store",
    });

    const order = await res.json();
    if (!res.ok) {
      const message =
        order?.error?.description || "Unable to create payment order right now.";
      return NextResponse.json({ error: message }, { status: 500 });
    }

    return NextResponse.json({
      key: RAZORPAY_KEY_ID,
      order_id: order.id,
      amount: order.amount,
      currency: order.currency,
      name: RAZORPAY_DONATION_NAME,
      description: RAZORPAY_DONATION_DESCRIPTION,
    });
  } catch (error) {
    return NextResponse.json(
      { error: `Unable to create payment order right now.` },
      { status: 500 }
    );
  }
}
