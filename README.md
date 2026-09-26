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

Until a password is set, the server treats anything arriving on **loopback** as the operator, so
a fresh install is usable immediately.

Know what that means behind a proxy: the proxy is what connects, so every visitor it forwards
arrives on loopback and is trusted. That is correct when the proxy authenticates them — an SSO
proxy, a password gate, a private network — and open to anyone when it does not. The server
cannot tell which from the inside, so it prints a loud warning at startup and leaves the call to
you.

Setting a password ends the ambiguity. After that, loopback earns nothing and every write needs
the password, whatever is in front.

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

An unconfigured server behaves the same way, so this flag changes nothing on its own. It is
worth setting anyway: it records the assumption in the unit file where the next person to read it
will see it, and it keeps working if a password is ever added for other reasons.

The assumption is real either way: if that gate is removed, misconfigured, or bypassed by a route
that skips it, every write here is open. An admin password costs one extra login and removes that
single point of failure.

**Rate limits are shared per API key, not per process.** The pacer's clock lives in the database,
so the service and any CLI command coordinate automatically — but only if they use the same
`data/` directory. Two checkouts with separate databases are two independent budgets.

## Exact balances from the transfer ledger (Etherscan)

With an Etherscan API key, Plutus keeps the token's **complete transfer history** (every ERC-20
`Transfer` event, via `getLogs`) and derives every address's balance from it, exactly, as
integers. That covers what per-wallet reads miss between reads: transfers between your own
wallets, staking, other venues, and every holder the GMGN census never ranks. The ledger is
checked against the token's total supply hourly and rebuilt if it ever disagrees. While it is
synced (within 15 minutes) and verified, per-wallet GMGN balance reads stop entirely; GMGN is kept
for what only it has — cost basis, PnL, tags, and pool checks. If the ledger falls behind, the desk
falls back to GMGN reads on its own.

**Key:** put it on one line in `data/etherscan.key` (gitignored), or set `PLUTUS_ETHERSCAN_KEY`.
Never commit it or paste it anywhere shared. `python -m plutus.cli doctor <token>` checks the key,
the chain on your plan, and that the ledger sums to supply — without printing the key.

**Limits** (researched 2026-09-26 — re-verify monthly):

| Plan | Price | Rate | Daily | Robinhood chain |
|---|---|---|---|---|
| Free | $0 | 3/s | 100,000 | until 2026-10-15 only |
| Lite | $49/mo | 5/s | 100,000 | required from 2026-10-16 |

Measured by a simulated week: **~4,300 Etherscan calls a day per token** (a sync every 30s plus
the hourly supply check), and GMGN falls from ~110 calls an hour to ~1. Settings:
`PLUTUS_ETHERSCAN_RPS` (default 2.5; raise to 4.5 on Lite), `PLUTUS_ETHERSCAN_DAILY` (default
90,000). All processes share one pacer through the database.

GeckoTerminal (free trade feed and trusted pool prices, 30 calls/min public) is likewise paced
across every process through the database, at one call per 2.1s, and a 429 pauses every caller.

## Staying inside the API budget at scale

GMGN calls are capped per hour (`PLUTUS_MAX_CALLS_HOUR`, default 900). Three things keep a large
campaign — measured with 500 wallets — well inside it:

- **Your position is rolled forward from your own fills.** Each of your wallets is its last real
  balance read plus the fills the free trade feed has seen since, counted by block so nothing is
  double-counted. Wallets are no longer re-read just because they traded: an hour of tracking
  after a 500-wallet buy costs about a dozen calls instead of several thousand. Real reads still
  happen on first sight, after a gap in the trade feed (the feed returns a fixed window; a burst
  faster than one poll can outrun it, and the page says so), and at the hourly reconciliation that
  catches what fills cannot see — transfers, other venues, staking.
- **Pool prices come from GeckoTerminal where that is proven safe.** Each token is checked against
  GMGN on first use and hourly; it reads the free source only while the two agree within 3%.
  Full-range pools pass. Concentrated-liquidity pools do not — one measured 150–240% off — and
  stay on GMGN. Once trusted, a token's prices stay live even when the hour's GMGN budget is spent.
- **Sweeps read what the budget allows, most important first.** A sweep that does not fit is not
  refused: it reads the pool first, then never-read wallets, then the stalest, keeps 50 calls in
  reserve for prices and quotes, and picks up the rest as the hourly window frees. Onboarding no
  longer reads every wallet twice.

**Background work has its own ceiling.** Everything the tracker loop does on its own stops at
`PLUTUS_BG_HOUR_SHARE` (default 0.5) of the hourly cap and `PLUTUS_BG_DAY_SHARE` (default 0.6) of
the daily one. The rest is kept for what the operator does — a full pull, onboarding, a live
campaign — which background work can never starve. When background work cannot afford its next
step it logs **one line** saying when calls free up and pauses its GMGN work until then; the trade
feed and trusted free pool prices carry on meanwhile.

**Wallets are re-read on a slow rolling schedule.** Every 5 minutes the loop re-reads only the
few stalest wallets, sized so each is re-read within `PLUTUS_RECONCILE_S` (default 12h). A wallet
at zero with no fills since its last read is re-read at most daily. Full re-reads of every wallet
happen only when the operator asks (full pull, onboarding).

**The holder census comes first** and is never replaced by a partial one: a census the budget
cuts short is discarded and the previous complete one stays current. The analysis page always
states the census's coverage and age, and it and campaign advice warn when the latest census is
partial.

**Rate limits are handled once, for everyone.** A 429 pauses every worker in every process on the
key, through the shared database, and the request is never repeated on the CLI.
`PLUTUS_BALANCE_WORKERS` (default 4) sets how many balance reads run at once.

**One full read per token at a time, across processes.** A CLI `tick --full` and the server's
own full reads share a lock in the database, so they cannot read the same wallets twice.

Measured by simulation with 458 wallets and no users: **~97 GMGN calls an hour** in steady state —
36 for the hourly census, 48 for the rolling re-reads, 12 for the pool contract — with no wallet
more than ~9.5h since a real read. Each token you track adds its own share. A token whose pool
fails the free-source check reads its pool from GMGN, which adds up to ~30 an hour.
