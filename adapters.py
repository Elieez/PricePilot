import json, re
from typing import List, Dict, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
import httpx
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

def _normalize_price_to_cents(s: str) -> Optional[int]:
    s = s.strip().replace(" ", "")
    if s.count(",") > 0 and s.count(".") > 0:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return int(round(float(s) * 100))
    except:
        return None


class BaseAdapter:
    def __init__(self, cfg: Dict):
        self.cfg = cfg

    def _get_html(self, url: str) -> str:
        with httpx.Client(timeout=25, headers={"User-Agent": UA}) as client:
            response = client.get(url, follow_redirects=True)
            response.raise_for_status()
            return response.text
    
    def discover_urls(self, listing_url: str, limit: int) -> List[str]:
        raise NotImplementedError

    def fetch_offer(self, product_url: str) -> Optional[Dict]:
        raise NotImplementedError

class AsosAdapter(BaseAdapter):
    """ASOS: collect /prd/ links from listing HTML; then parse product page JSON-LD (Product→offers.price).
    Also attempts to extract brand, previous price, and rating when present."""
    BASE = "https://www.asos.com"

    def discover_urls(self, listing_url: str, limit: int) -> List[str]:
    """
    Collect up to `limit` unique product URLs.

    Control variant handling from config:
      unique_by: "product" (default) or "variant"
      keep_query_params: ["colourWayId","clr"]  # used when unique_by="variant"
    """
    unique_by = (self.cfg.get("unique_by") or "product").lower()
    keep_params = [p.lower() for p in (self.cfg.get("keep_query_params") or ["colourWayId", "clr"])]

    def canon(u: str) -> str:
        # absolutize
        if u.startswith("//"):
            u2 = "https:" + u
        elif u.startswith("/"):
            u2 = self.BASE + u
        else:
            u2 = u

        if unique_by == "product":
            return u2.split("?", 1)[0]

        # variant-aware: keep only selected query params, stable order
        parts = urlsplit(u2)
        qs = parse_qsl(parts.query, keep_blank_values=True)
        kept = [(k, v) for (k, v) in qs if k.lower() in keep_params]
        new_q = urlencode(kept, doseq=True) if kept else ""
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_q, parts.fragment))

    html = self._get_html(listing_url)
    soup = BeautifulSoup(html, "lxml")

    seen, out = set(), []
    for a in soup.select("a[href*='/prd/']"):
        href = a.get("href")
        if not href:
            continue
        url = canon(href)
        if "/prd/" not in url or url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= limit:
            break
    return out

    def fetch_offer(self, product_url: str) -> Optional[Dict]:
    html = self._get_html(product_url)
    soup = BeautifulSoup(html, "lxml")
    title = None
    brand = None
    price_cents = None
    prev_price_cents = None
    currency = None
    rating = None
    review_count = None
    image_url = None

    # --- JSON-LD blocks ---
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if node.get("@type") != "Product":
                continue

            title = title or node.get("name")

            # brand: object or string
            b = node.get("brand")
            if isinstance(b, dict):
                brand = brand or b.get("name")
            elif isinstance(b, str):
                brand = brand or b

            # image: string | list | ImageObject
            img = node.get("image")
            if isinstance(img, list):
                for v in img:
                    if isinstance(v, str) and not image_url:
                        image_url = v; break
                    if isinstance(v, dict) and not image_url:
                        image_url = v.get("url"); break
            elif isinstance(img, dict):
                image_url = image_url or img.get("url")
            elif isinstance(img, str):
                image_url = image_url or img

            # offers: support AggregateOffer and priceSpecification
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}

            price = (
                offers.get("price")
                or (offers.get("priceSpecification") or {}).get("price")
                or offers.get("lowPrice")
                or offers.get("highPrice")
            )
            currency = currency or (
                offers.get("priceCurrency")
                or (offers.get("priceSpecification") or {}).get("priceCurrency")
            )
            if price and price_cents is None:
                price_cents = _normalize_price_to_cents(str(price))

            # ratings (if present)
            ar = node.get("aggregateRating") or {}
            try:
                rating = float(ar.get("ratingValue")) if ar.get("ratingValue") else rating
            except Exception:
                pass
            try:
                review_count = int(ar.get("reviewCount")) if ar.get("reviewCount") else review_count
            except Exception:
                pass

    # Fallback: Open Graph image
    if not image_url:
        for prop in ("og:image:secure_url", "og:image"):
            og = soup.select_one(f"meta[property='{prop}']")
            if og and og.get("content"):
                image_url = og["content"]; break

    # Normalize image URL
    if image_url:
        if image_url.startswith("//"):
            image_url = "https:" + image_url
        elif image_url.startswith("/"):
            image_url = self.BASE + image_url

    # Previous/was price (DOM hints)
    prev_el = soup.select_one(
        "[data-testid='previous-price'], .previous-price, .price .previous, .rrp, .old-price, .was-price, del, s"
    )
    if prev_el:
        m = re.search(r"[0-9.,]+", prev_el.get_text(" ", strip=True))
        if m:
            prev_price_cents = _normalize_price_to_cents(m.group(0))

    # Fallback: current price from DOM if JSON-LD missed it
    if price_cents is None:
        price_candidates = soup.select(
            "[data-testid='current-price'], "
            "[data-auto-id='productPrice'], "
            "[data-auto-id='product-price'], "
            ".current-price, .price .current, [class*='currentPrice']"
        )
        for el in price_candidates:
            txt = el.get_text(" ", strip=True)
            m = re.search(r"[\d\s.,]+", txt)
            if not m:
                continue
            maybe = _normalize_price_to_cents(m.group(0))
            if maybe:
                price_cents = maybe
                low = txt.lower()
                if not currency:
                    if "sek" in low or " kr" in low:
                        currency = "SEK"
                    elif "€" in low:
                        currency = "EUR"
                    elif "$" in low:
                        currency = "USD"
                break

    # Fallbacks
    if not title:
        t = soup.select_one("h1, [data-auto-id='productTitle']")
        if t:
            title = t.get_text(strip=True)
    if not currency:
        currency = "EUR"

    if price_cents:
        return {
            "title": title or product_url,
            "brand": brand,
            "price_cents": price_cents,
            "prev_price_cents": prev_price_cents,
            "currency": currency,
            "rating": rating,
            "review_count": review_count,
            "image_url": image_url,
            "url": product_url,
        }
    return None


class StaticCssAdapter(BaseAdapter):
    def discover_urls(self, listing_url: str, limit: int) -> List[str]:
        html = self._get_html(listing_url)
        soup = BeautifulSoup(html, "lxml")
        sel = self.cfg["selectors"]
        cards = soup.select(sel["card"]) or []
        out = []
        for el in cards:
            a = el.select_one(sel.get("href", "a"))
            if not a:
                continue
            href = a.get("href")
            if not href:
                continue
            if self.cfg.get("absolute_urls"):
                url = href
            else:
                url = urljoin(self.cfg.get("site_base", ""), href)
            url = url.split("?")[0]
            out.append(url)
            if len(out) >= limit:
                break
        seen, dedup = set(), []
        for u in out:
            if u in seen:
                continue
            seen.add(u)
            dedup.append(u)
        return dedup

    def fetch_offer(self, product_url: str) -> Optional[Dict]:
        html = self._get_html(product_url)
        soup = BeautifulSoup(html, "lxml")
        # JSON-LD first
        title = None
        price_cents = None
        currency = None
        brand = None
        for tag in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(tag.string or "")
            except Exception:
                continue
            nodes = data if isinstance(data, list) else [data]
            for node in nodes: 
                if node.get("@type") == "Product":
                    title = title or node.get("name")
                    b = node.get("brand")
                    if isinstance(b, dict):
                        brand = brand or b.get("name")
                    elif isinstance(b, str):
                        brand = brand or b
                    offers = node.get("offers") or {}
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get("price") or (offers.get("priceSpecification") or {}).get("price")
                    currency = currency or offers.get("priceCurrency") or (offers.get("priceSpecification") or {}).get("priceCurrency")
                    if price and price_cents is None:
                        price_cents = _normalize_price_to_cents(str(price))
        if price_cents: 
            return {
                "title": title or product_url,
                "brand": brand,
                "price_cents": price_cents,
                "currency": currency or self.cfg.get("currency", "EUR"),
                "url": product_url,
            }

        # Fallback to selectors
        sel = self.cfg.get("selectors", {})
        title_el = soup.select_one(sel.get("title", ""))
        price_el = soup.select_one(sel.get("price", ""))
        price_regex = sel.get("price_regex", "[0-9.,]+")
        if title_el and price_el:
            m = re.search(price_regex, price_el.get_text(" ", strip=True))
            if m:
                cents = _normalize_price_to_cents(m.group(0))
                if cents:
                    return {
                        "title": title_el.get_text(strip=True),
                        "price_cents": cents,
                        "currency": self.cfg.get("currency", "EUR"),
                        "url": product_url,
                    }
        return None


ADAPTERS = {
"asos": AsosAdapter,
"static": StaticCssAdapter,
}