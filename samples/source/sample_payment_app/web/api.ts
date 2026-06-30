// フロント側の決済 API クライアント（TS、解析サンプル用）

export interface ChargeRequest {
  userId: number;
  amount: number;
}

export async function postCharge(req: ChargeRequest): Promise<Response> {
  return fetch("/api/charge", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

export const formatAmount = (yen: number): string => `¥${yen.toLocaleString()}`;

export class PaymentClient {
  constructor(private readonly baseUrl: string) {}

  async charge(req: ChargeRequest): Promise<unknown> {
    const res = await postCharge(req);
    if (!res.ok) throw new Error(`charge failed: ${res.status}`);
    return res.json();
  }
}
