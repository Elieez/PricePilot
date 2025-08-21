# PricePilot

**PricePilot** is an automated price monitoring and notification tool, primarily written in Python. It is designed to track product prices on e-commerce sites, detect price changes and discounts, and send alerts (for example, via Discord webhooks). The project is easily extensible for new shops and supports multi-currency price tracking.

## Features

- **Automated Price Monitoring:** Periodically checks prices for specified products from supported online retailers.
- **Multi-Currency Support:** Fetches and caches foreign exchange rates to normalize prices (e.g., to SEK, EUR, USD, GBP).
- **Shop Adapters:** Extensible adapter framework for scraping and parsing product listings and details from different shops (e.g., ASOS, sites with static HTML/CSS).
- **Discount & Brand Filters:** Customizable filtering to alert only on specific brands or minimum discount thresholds.
- **State Management:** Remembers seen products and price state to avoid duplicate alerts.
- **Discord Notifications:** Sends rich, embeddable notifications to Discord channels via webhooks.

## Getting Started

### Requirements

- Python 3.8+
- Dependencies: `httpx`, `PyYAML`, `beautifulsoup4`, `lxml` (see `requirements.txt` if present)

### Setup

1. **Clone the Repository**
   ```sh
   git clone https://github.com/Elieez/PricePilot.git
   cd PricePilot
   ```

2. **Install Dependencies**
   ```sh
   pip install -r requirements.txt
   ```

3. **Configure Monitors**
   - Edit or create a configuration file (YAML or JSON) specifying:
     - The shops to monitor
     - Webhook URLs
     - Filters for brands, discounts, etc.

4. **Run the Monitor**
   ```sh
   python monitor.py
   ```

## Usage

- The main entry point is `monitor.py`, which loads configuration, periodically fetches product listings, and sends notifications if price drops or new discounts are detected.
- Results are cached in the `state/` directory to avoid duplicate alerts and speed up repeated runs.

## Extending

- Add new shop support by creating an adapter in `adapters.py` that implements `discover_urls` (to find products) and `fetch_offer` (to extract price, brand, etc.).
- Adjust notification logic or add new output channels as needed.

## Technologies Used

- **Python:** Main application language
- **httpx:** For HTTP requests
- **PyYAML:** For flexible config parsing
- **BeautifulSoup & lxml:** HTML parsing
- **Discord Webhooks:** For alert notifications

---

*Repository: [Elieez/PricePilot](https://github.com/Elieez/PricePilot)*
