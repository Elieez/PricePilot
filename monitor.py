import os, json, time, random, sys
from pathlib import Path
from typing import Dict, List, Optional
import yaml
import httpx
from adapters import ADAPTERS

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
STATE_DIR.mkdir(exist_ok=True)

# ---------------- FX (currency) ----------------


def _load_fx_cache() -> Optional[Dict]:
    p = STATE_DIR / "fx.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def _save_fx_cache(data: Dict):
    (STATE_DIR / "fx.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))


def fetch_fx_rates(cfg_fx: Dict) -> Optional[Dict]:
    """Fetch EUR-based rates and cache; return {'base':'EUR','rates':{...}, 'fetched_at': iso}."""
    provider = (cfg_fx or {}).get("provider", "exchangerate_host")
    symbols = ",".join((cfg_fx or {}).get("symbols", ["SEK","EUR","USD","GBP"]))
    try:
        if provider == "exchangerate_host":
            url = "https://api.exchangerate.host/latest"
            r = httpx.get(url, params={"base": "EUR", "symbols": symbols}, timeout=20)
            r.raise_for_status()
            data = r.json()
            out = {"base": "EUR", "rates": data.get("rates", {}), "fetched_at": time.time()}
            _save_fx_cache(out)
            return out
    except Exception as e:
        print("[FX] fetch failed:", e)
        return None


def get_fx(cfg: Dict) -> Optional[Dict]:
    """Return fx dict, refresh if older than refresh_hours."""
    fx_cfg = cfg.get("fx") or {}
    cache = _load_fx_cache()
    refresh_hours = int(fx_cfg.get("refresh_hours", 24))
    now = time.time()
    if cache and (now - cache.get("fetched_at", 0) < refresh_hours * 3600):
        return cache
    return fetch_fx_rates(fx_cfg)


def to_sek_cents(amount_cents: int, currency: Optional[str], fx: Optional[Dict]) -> Optional[int]:
    if amount_cents is None:
        return None
    if not currency or currency.upper() == "SEK":
        return int(amount_cents)
    if not fx or not fx.get("rates"):
        return None
    rates = fx["rates"]
    cur = currency.upper()
    if cur not in rates or "SEK" not in rates:
        return None
    # EUR-based: amount_SEK = amount * (SEK_per_EUR / CUR_per_EUR)
    sek_per_eur = float(rates["SEK"])
    cur_per_eur = float(rates[cur])
    factor = sek_per_eur / cur_per_eur
    return int(round(amount_cents * factor))


# ---------------- Config & state ----------------

def load_cfg() -> Dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state(slug: str) -> Dict:
    p = STATE_DIR / f"{slug}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"seen": []}


def save_state(slug: str, state: Dict):
    p = STATE_DIR / f"{slug}.json"
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def send_discord(webhook_url: str, content: str):
    if not webhook_url:
        print("[DRY ALERT]", content)
        return
    httpx.post(webhook_url, json={"content": content}, timeout=15)


# ---------------- Filtering ----------------

def merge_filters(global_f: Dict, monitor_f: Dict) -> Dict:
    gf = global_f or {}
    mf = monitor_f or {}
    out = {
        "include_brands": [*(gf.get("include_brands") or []), *(mf.get("include_brands") or [])],
        "exclude_brands": [*(gf.get("exclude_brands") or []), *(mf.get("exclude_brands") or [])],
        "require_discount_pct": mf.get("require_discount_pct", gf.get("require_discount_pct", 0)),
    }
    # normalize lowercase and de-dup
    out["include_brands"] = list(dict.fromkeys([b.lower() for b in out["include_brands"]]))
    out["exclude_brands"] = list(dict.fromkeys([b.lower() for b in out["exclude_brands"]]))
    return out


def brand_ok(brand: Optional[str], flt: Dict) -> bool:
    b = (brand or "").lower()
    if b in (flt.get("exclude_brands") or []):
        return False
    inc = flt.get("include_brands") or []
    if inc and b not in inc:
        return False
    return True


def discount_ok(offer: Dict, flt: Dict) -> bool:
    req = int(flt.get("require_discount_pct", 0) or 0)
    if req <= 0:
        return True

    cur = offer.get("price_cents")
    prev = offer.get("prev_price_cents")

    if cur is None:
        # No current price parsed → block (safer)
        return False

    if prev is None:
        # Fail-open when previous price is unknown (keep True).
        # If you want to require a visible discount, change this to: return False
        return True

    if prev <= cur:
        # No discount or price increased → block
        return False

    drop = (prev - cur) / prev * 100
    return drop >= req


# ---------------- Runner ----------------

def run_monitor(cfg: Dict, monitor: Dict, webhook: str, fx: Optional[Dict]):
    slug = monitor["slug"]
    Adapter = ADAPTERS[monitor["adapter"]]
    adapter = Adapter(monitor)

    state = load_state(slug)
    seen = set(state.get("seen", []))

    sample_limit = (cfg.get("run") or {}).get("sample_limit_per_run", 30)
    jitter_lo, jitter_hi = ((cfg.get("run") or {}).get("jitter_ms", [800, 1600]))

    flt = merge_filters(cfg.get("filters"), monitor.get("filters"))
    want_sek = (cfg.get("currency_output") or "SEK").upper() == "SEK"

    new_urls: List[str] = []
    for listing in monitor["listing_urls"]:
        try:
            urls = adapter.discover_urls(listing, sample_limit)
        except Exception as e:
            print(f"[{slug}] listing error: {e}")
            continue
        for url in urls:
            if url in seen:
                continue
            new_urls.append(url)

    if not new_urls:
        print(f"[{slug}] No new listings.")
        return False

    changed = False
    for url in new_urls:
        try:
            offer = adapter.fetch_offer(url) or {"title": url, "price_cents": None, "currency": ""}
            if not brand_ok(offer.get("brand"), flt):
                seen.add(url)
                changed = True
                continue
            if not discount_ok(offer, flt):
                seen.add(url)
                changed = True
                continue

            # Currency conversion (default to SEK output)
            price_cents = offer.get("price_cents")
            cur = (offer.get("currency") or "").upper()
            sek_cents = to_sek_cents(price_cents, cur, fx) if want_sek else None

            if want_sek and sek_cents is not None:
                price_str = f"{sek_cents/100:.0f} SEK (original {price_cents/100:.2f} {cur})"
            else:
                price_str = f"{price_cents/100:.2f} {cur or ''}".strip()

            brand_str = f" | {offer.get('brand')}" if offer.get("brand") else ""
            
            drop_pct = None
            prev = offer.get("prev_price_cents")
            curp = offer.get("price_cents")
            if prev and curp and prev > curp:
                drop_pct = int(round((prev - curp) / prev * 100))

            img_url = offer.get("image_url")

            embed = {
                "title": offer.get("title") or "New item",
                "url": url,
                "description": (
                    f"{offer.get('brand') or ''} • {price_str}" + (f" • ↓{drop_pct}%" if drop_pct is not None else "")
                ).strip(),
                "footer": {"text": monitor["name"]},
            }
            if img_url:
                embed["thumbnail"] = {"url": img_url}

            send_discord(webhook, embed=embed, userName="PricePilot")

            seen.add(url)
            changed = True
        except Exception as e:
            print(f"[{slug}] product error: {e}")
        time.sleep(random.uniform(jitter_lo, jitter_hi) / 1000.0)

    if changed:
        save_state(slug, {"seen": sorted(seen)})
    return changed

def main():
    cfg = load_cfg()
    webhook = os.getenv("DISCORD_WEBHOOK") or cfg.get("discord_webhook")

    fx = None
    if (cfg.get("currency_output") or "SEK").upper() == "SEK":
        fx = get_fx(cfg)

    any_changed = False
    for m in cfg["monitors"]:
        print(f"→ Running: {m['name']}")
        if run_monitor(cfg, m, webhook, fx):
            any_changed = True

    sys.exit(0 if any_changed else 78)

if __name__ == "__main__":
    main()