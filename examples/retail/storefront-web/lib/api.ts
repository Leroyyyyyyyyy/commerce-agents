// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import { AgentApi } from "web-shared";
import type { CartPayload, Product, ProductDetails } from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export const api = new AgentApi(API_URL, "/api");

export const UNREACHABLE =
  "Couldn't reach the retail API on port 8000. Start it with " +
  "`uvicorn retail.api.main:app --app-dir examples --port 8000` and try again.";

export async function fetchProducts(): Promise<Product[] | null> {
  const data = await api.get<{ products: Product[] }>("/products", { limit: "100" });
  return data?.products ?? null;
}

export function fetchProduct(productId: string): Promise<ProductDetails | null> {
  return api.get<ProductDetails>(`/products/${encodeURIComponent(productId)}`);
}

export type CartAddResult = { cart: CartPayload | null; retryable: boolean };
export type CartAddIntent = { session: string | null; productId: string; operationId: string; retryable: boolean };
export type ProductAdd = (product: Product, intent: CartAddIntent) => boolean | void | Promise<boolean | void>;

/** A new intent gets a new ID; callers retain and pass it after an ambiguous failure. */
export async function addToCart(productId: string, quantity = 1, operationId = crypto.randomUUID()): Promise<CartAddResult> {
  try {
    const response = await fetch(`${api.base}/cart/add`, {
      method: "POST",
      headers: api.headers(true),
      body: JSON.stringify({ product_id: productId, quantity, operation_id: operationId }),
    });
    if (!response.ok) return { cart: null, retryable: response.status >= 500 };
    const data = await response.json() as { cart?: CartPayload };
    return { cart: data.cart ?? null, retryable: !data.cart };
  } catch {
    // A transport/parse failure does not say whether the server committed.
    return { cart: null, retryable: true };
  }
}
