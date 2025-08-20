import json, re
from typing import List, Dict, Optional
from urllib.parse import urljoin
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
        html = self._get_html(listing_url)
        soup = BeautifulSoup(html, "lxml")
        out = []
        for a in soup.select("a[href*='/prd/']"):
            href = a.get("href")
            if not href:
                continue
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = self.BASE + href
            url = href.split("?")[0]
            if "/prd/" in url:
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
        title = None
        brand = None
        price_cents = None
        prev_price_cents = None
        currency = None
        rating = None
        review_count = None

        # JSON-LD blocks (most reliable)
        for tag in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(tag.string or "")
            except Exception:
                continue
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                if node.get("@type") == "Product":
                    title = title or node.get("name")
                    # brand may be object or string
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


                    # ratings
                    ar = node.get("aggregateRating") or {}
                    try:
                        rating = float(ar.get("ratingValue")) if ar.get("ratingValue") else rating
                    except Exception:
                        pass
                    try:
                        review_count = int(ar.get("reviewCount")) if ar.get("reviewCount") else review_count
                    except Exception:
                        pass


        # Previous/was price (DOM hints)
        prev_el = soup.select_one("[data-testid='previous-price'], .previous-price, .price .previous, .rrp")
        if prev_el:
            m = re.search("[0-9.,]+", prev_el.get_text(" ", strip=True))
            if m:
                prev_price_cents = _normalize_price_to_cents(m.group(0))


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
            if self.cfg.get("absolute:urls"):
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