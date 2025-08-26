# Loomr TypeScript SDK

A tiny TypeScript/JavaScript client for interacting with the Loomr API.

- Package: `loomr-sdk`
- Module formats: ESM + CJS
- Types included: `dist/index.d.ts`

## Install

```bash
npm i loomr-sdk
# or
pnpm add loomr-sdk
# or
yarn add loomr-sdk
```

## Usage

### ESM (Node 18+/Bun/Deno)
```ts
import { LoomrClient } from 'loomr-sdk';

const client = new LoomrClient({
  baseUrl: process.env.LOOMR_BASE_URL || 'http://localhost:8000',
  token: process.env.LOOMR_API_TOKEN, // optional
});

// Health check
const health = await client.health();
console.log('health:', health);

// Deliver a message
await client.deliver({
  chat_id: 1234567890,
  content: 'Hello from SDK',
});
```

### CommonJS
```js
const { LoomrClient } = require('loomr-sdk');

const client = new LoomrClient({ baseUrl: 'http://localhost:8000' });
```

## API

- `new LoomrClient(options)`
  - `baseUrl`: string (default: `http://localhost:8000`)
  - `token`: string | undefined — optional Bearer token
- `client.health(): Promise<any>` — GET `/health`
- `client.deliver(payload): Promise<any>` — POST `/api/deliver`
  - `payload`: `{ chat_id: number | string; content: string }`

## Development

- Build: `npm run build`
- Publish: handled by GitHub Actions on tag `sdk-v*`

## License

See repository root `LICENSE` for details.
