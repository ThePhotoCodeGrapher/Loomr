export type LoomrClientOptions = {
  baseUrl?: string;
  token?: string; // Bearer token for protected endpoints
  fetchImpl?: typeof fetch;
};

export class LoomrClient {
  private baseUrl: string;
  private token?: string;
  private fetchImpl: typeof fetch;

  constructor(opts: LoomrClientOptions = {}) {
    this.baseUrl = (opts.baseUrl || 'http://127.0.0.1:8090').replace(/\/$/, '');
    this.token = opts.token;
    this.fetchImpl = opts.fetchImpl || fetch;
  }

  private headers(): HeadersInit {
    const h: HeadersInit = { 'Content-Type': 'application/json' };
    if (this.token) h['Authorization'] = `Bearer ${this.token}`;
    return h;
  }

  async health(): Promise<{ status: string }> {
    const res = await this.fetchImpl(`${this.baseUrl}/`, { headers: this.headers() });
    if (!res.ok) throw new Error(`Health check failed: ${res.status}`);
    return res.json();
  }

  async deliver(body: unknown): Promise<unknown> {
    const res = await this.fetchImpl(`${this.baseUrl}/deliver`, {
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`Deliver failed: ${res.status}`);
    return res.json();
  }
}

export default LoomrClient;
