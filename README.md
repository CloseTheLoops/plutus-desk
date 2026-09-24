# Plutus

Market-making **analysis** for a token you issue. Paste a contract address; it discovers the
venues, supply, launchpad, burn address and quote asset, proposes a class for every address
holding meaningful supply, measures that token's own execution cost from a live quote ladder,
and keeps a reconciled picture of who holds what — with a "what do you want?" box that turns an
intent into a costed plan.

**It holds no private key and cannot trade.** Every source it touches is read-only, and
`sources/gmgn.py` raises on any command that would need a key. That is enforced in code, not
promised in a README.

---

## Quick start

```bash
uv sync                                   # or: pip install -e .
python -m plutus.cli serve                # http://127.0.0.1:8800
```

Open `/add`, paste a chain and a contract address, paste your wallets, submit. Or from the CLI:

```bash
cp tokens/example.toml tokens/mytoken.toml   # edit it
python -m plutus.cli onboard mytoken         # discover + classify + calibrate the fee
python -m plutus.cli tick   mytoken --full --census
python -m plutus.cli ledger mytoken
```

Requires Python 3.11+ and [`gmgn-cli`](https://www.npmjs.com/package/gmgn-cli) on PATH with an
API key configured. GeckoTerminal is used for venue discovery and the trade tape, and needs no
key at all.

---

## Why it exists

Most token dashboards answer "what is the price". If you hold a large share of your own token,
the questions that actually matter are different:

- **How much supply is genuinely tradeable?** Not circulating supply — the float, after your own
  holdings, the pool, anything locked, and anything burnt are removed.
- **Is that buying real?** If a fifth of the tape is your own wallets, the chart is reading your
  own money back to you as demand.
- **What does a trade actually cost here?** Not the pool's advertised fee — the measured, all-in
  cost on this specific venue.

## Six ideas the design turns on

**Float is the residual, never a sum.** It is computed as `effective − ours − pool − locked`, so
the classes are forced to reconcile against supply. If an address is misclassified the error
lands *visibly in float* instead of silently vanishing. Computing float by adding up third-party
holders instead produces a number that quietly disagrees with supply and nobody notices.

**Burnt supply leaves the denominator.** `effective = nominal − burnt`. Burnt is not a class that
holds, it is a class that *removes*. Launchpad bonding curves commonly perma-burn at graduation;
counting that as supply understates your position and misprices every target you derive.

**Our holdings come from direct per-wallet queries.** Never from a ranked holder sweep. A ranked
sweep reaches roughly the top 100 rows per slice, so on a real wallet set it found barely a third
of them; querying each wallet by name found all of them. We know our own wallets' names, so we
ask by name.

**The tape is split ours/third-party at ingest**, not at display time. Any flow figure computed
on unsplit tape is a bug, and a test asserts it.

**The execution fee is measured per token, never assumed.** A pool can advertise a zero fee and
still cost several percent per trade via a hook. Onboarding quotes at seven sizes spanning 100x
and fits `total = 1 − (1 − fee)(1 − impact)`. A constant multiplicative fee shows up as a tight
band; anything else is flagged so the engine prices from live quotes instead.

**The model states its own accuracy envelope.** Calibration reports the largest fill size it
predicts within half a percent. Above that boundary, advice comes back flagged `needs_quote` and
the caller must take a live quote rather than extrapolate a model that was never that good out
there.

## What it computes

| | |
|---|---|
| **Ledger** | `burnt · ours · pool · locked · float`, reconciled to supply, with anything unexplained shown rather than absorbed |
| **Curve** | constant product × the measured fee. Two identities run everything, both linear in `Q`: cost to move price by `m` is `Q(√m − 1)/(1−fee)`; proceeds for a drawdown `d` are `Q(1 − √(1−d))(1−fee)` |
| **Flow** | third-party-only buy/sell over several windows, realised volatility, sell-flow trend, maker concentration, capture rate |
| **Composition** | who holds the float: cost basis, never-bought vs underwater, vendor tags, concentration, and the portion the census could not reach |
| **Advice** | *acquire* a supply share, *push* FDV to a level in a window, or run an *event* — each costed, with the paths compared and their assumptions stated |

## Layout

```
plutus/
  config.py     chain profiles · token config · address-vs-pool-id · paste extraction
  db.py         SQLite (WAL) · token-keyed · append-only observations
  onboard.py    discovery · the classification worksheet · fee calibration
  sources/      gmgn (subprocess, read-only) · gecko (free HTTP)
  track/        pool · tape · inventory · census, each with its own cadence
  analyze/      curve · ledger · flow · composition · advice
  web/          FastAPI + one page
tests/          curve math vs real quotes · spreadsheet paste shapes · template JS
```

```bash
python tests/test_curve.py      # arithmetic checked against real venue quotes
python tests/test_paste.py      # 16 shapes a spreadsheet paste actually arrives in
python tests/test_templates.py  # every template renders and its JS parses
```

## A note on chains

Only one chain has been run end to end. The others should work — same vendor surface, same
discovery — but **wallet attribution in the trade tape must be re-verified per chain**. If your
own trades do not appear with a signer address, capture-rate silently measures nothing rather
than failing. Onboarding runs that check and tells you. Solana addresses are base58 and
case-sensitive; lowercasing them 404s every lookup and the lane looks dead rather than broken.

## Scope

Analysis only. Execution is deliberately a separate concern: when it is added it will be its own
process, reading intents this one writes, holding the keys and its own caps — so a compromise of
the web layer yields a read-only dashboard and nothing more.

MIT licensed.

## Deploying behind a reverse proxy

The admin password guards every state-changing endpoint. Reading stays open, so anyone with the
URL can watch a campaign but cannot delete a token, stop a run or spend API budget.

**The password is per-deployment.** It lives in `data/admin.json` as a scrypt hash and is
gitignored, so it is never copied by `git pull`. Set one on the server before the port is
reachable:

```bash
python -m plutus.cli setpassword          # prompts, or reads PLUTUS_ADMIN_PASSWORD
```

Until a password is set, the server lets a caller **on the machine itself** through so a fresh
install is usable. That bootstrap is refused for any request carrying a forwarding header
(`X-Forwarded-For`, `X-Real-IP`, `Forwarded`, `X-Forwarded-Host`), because a proxied app is
normally bound to `127.0.0.1` — which is also exactly what makes every visitor look local. Set
the password first and the question never arises: once one exists, loopback earns nothing.

Run the app on loopback and let the proxy reach it:

```bash
python -m plutus.cli serve --host 127.0.0.1 --port 8800
```

Pass the client address through, or every visitor shares one rate-limit bucket:

```nginx
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header Host              $host;
```

`X-Forwarded-Proto` matters: it is how the app knows to mark the session cookie `secure`. The
forwarded address is used **only** to bucket the login rate limit, never to decide who the
operator is — it is attacker-controlled, so a global cap bounds guessing regardless of how many
addresses an attacker claims.

If you run uvicorn directly rather than through `plutus.cli serve`, set `PLUTUS_BIND_HOST`
yourself so the app knows what it bound to, and pass `--proxy-headers`.

### If your proxy already authenticates

Some deployments sit behind a gate that authenticates every request before it arrives — an SSO
proxy, a password gate, a private network. A second password there is redundant. Say so
explicitly:

```bash
PLUTUS_TRUST_PROXY_AUTH=1
```

Every request arriving from loopback is then treated as the operator. It is honoured only when
the *peer* is loopback, so it cannot be switched on by a header from outside, and the server logs
a warning at startup naming the assumption.

Use this **instead of** relying on the no-password bootstrap. Bootstrap is a first-run
convenience, not a security model: it is refused for proxied requests precisely so that an
unconfigured server cannot be claimed by a passer-by. Depending on it for a live deployment means
depending on behaviour that is meant to stop working.

The assumption it encodes is real: if that gate is ever removed, misconfigured, or bypassed by a
route that skips it, every write here is open. Setting an admin password as well costs one extra
login and removes that single point of failure.

**Rate limits are shared per API key, not per process.** The pacer's clock lives in the database,
so the service and any CLI command coordinate automatically — but only if they use the same
`data/` directory. Two checkouts with separate databases are two independent budgets.
