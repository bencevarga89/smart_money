import os
import requests
import pandas as pd
import yfinance as yf
from collections import defaultdict
from edgar import set_identity, Company

# SEC regulations require a contact identity string
set_identity("LightyearBot trading.bot.internal@gmail.com")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Top 12 Elite Investors & their SEC CIK Codes
TOP_12_FUNDS = {
    "0001067983": "Berkshire Hathaway (Warren Buffett)",
    "0001048611": "Scion Asset Management (Michael Burry)",
    "0001336528": "Bridgewater Associates (Ray Dalio)",
    "0001007166": "Appaloosa Management (David Tepper)",
    "0001336917": "Pershing Square (Bill Ackman)",
    "0001040275": "Third Point (Dan Loeb)",
    "0001061165": "Greenlight Capital (David Einhorn)",
    "0001029160": "Soros Fund Management",
    "0001351187": "Coatue Management (Philippe Laffont)",
    "0001167483": "Tiger Global Management",
    "0001536411": "Duquesne Family Office (Stanley Druckenmiller)",
    "0001061168": "Baupost Group (Seth Klarman)"
}

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram configuration missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"Telegram error: {e}")

def scan_institutional_clusters():
    print("Scanning 13F filings for the top 12 institutional funds...")
    
    # Maps ticker -> list of fund names holding it
    ticker_ownership = defaultdict(list)

    for cik, fund_name in TOP_12_FUNDS.items():
        try:
            print(f"Querying 13F data for {fund_name}...")
            company = Company(cik)
            filings = company.get_filings(form="13F-HR")
            
            if not filings:
                continue
                
            # Pull the most recent 13F-HR filing object
            latest_filing = filings[0]
            report = latest_filing.obj()
            
            if report is None:
                continue
                
            # edgartools provides a clean consolidated holdings dataframe property (.holdings)
            df_holdings = getattr(report, "holdings", None)
            if df_holdings is None or df_holdings.empty:
                # Fallback to infotable if holdings attribute isn't directly exposed
                df_holdings = getattr(report, "infotable", None)
                
            if df_holdings is None or df_holdings.empty:
                continue
                
            # Normalize column search for ticker (edgartools uses 'Ticker' or 'tic')
            ticker_col = next((col for col in ['Ticker', 'tic', 'TICKER'] if col in df_holdings.columns), None)
            
            if ticker_col:
                tickers = df_holdings[ticker_col].dropna().unique()
                for t in tickers:
                    clean_ticker = str(t).strip().upper()
                    if clean_ticker and clean_ticker != "nan":
                        ticker_ownership[clean_ticker].append(fund_name)
                        
        except Exception as e:
            print(f"Could not process 13F for {fund_name}: {e}")

    print(f"Extracted positions across funds. Checking for clusters (3+ buyers)...")
    cluster_alerts = []
    
    # Filter for stocks owned by at least 3 of the 12 tracked funds
    for ticker, holders in ticker_ownership.items():
        unique_holders = list(set(holders)) # Deduplicate just in case
        if len(unique_holders) >= 3:
            try:
                stock = yf.Ticker(ticker)
                hist = stock.history(period="3mo")
                
                if hist.empty or len(hist) < 2:
                    continue
                    
                quarter_start_price = float(hist["Close"].iloc[0])
                current_price = float(hist["Close"].iloc[-1])
                
                price_drift = ((current_price - quarter_start_price) / quarter_start_price) * 100
                
                # Gate: Price drift must be <= 15.0% so it's not overly chased/late to buy
                if price_drift <= 15.0:
                    holders_formatted = "\n".join([f"• {h}" for h in unique_holders])
                    msg = (
                        f"🐋 **SMART MONEY CLUSTER ALERT**\n"
                        f"• **Ticker:** `{ticker}`\n"
                        f"• **Cluster Count:** `{len(unique_holders)} Elite Funds`\n"
                        f"• **Price Drift Since Filing:** `+{price_drift:.1f}%`\n"
                        f"• **Current Price:** `${current_price:.2f}`\n\n"
                        f"🏛 **Accumulating Funds:**\n{holders_formatted}\n\n"
                        f"💡 *Action:* 3+ superinvestors hold this position, and the stock has not run away yet. Worth a fundamental deep dive."
                    )
                    cluster_alerts.append(msg)
            except Exception as e:
                print(f"Error checking price for ticker {ticker}: {e}")

    # Dispatch alerts to Telegram
    if cluster_alerts:
        print(f"Found {len(cluster_alerts)} valid cluster alerts. Sending to Telegram...")
        for alert in cluster_alerts:
            send_telegram(alert)
    else:
        print("Scan complete: No 3+ fund clusters met the price drift threshold this cycle.")

if __name__ == "__main__":
    scan_institutional_clusters()
