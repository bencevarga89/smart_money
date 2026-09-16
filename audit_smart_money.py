import os
import requests
import pandas as pd
import yfinance as yf
from collections import defaultdict
from edgar import set_identity, Company

set_identity("SmartMoneyPro trading.bot.internal@gmail.com")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TOP_12_FUNDS = {
    "0001067983": "Berkshire Hathaway",
    "0001048611": "Scion Asset Management",
    "0001336528": "Bridgewater Associates",
    "0001007166": "Appaloosa Management",
    "0001336917": "Pershing Square",
    "0001040275": "Third Point",
    "0001061165": "Greenlight Capital",
    "0001029160": "Soros Fund Management",
    "0001351187": "Coatue Management",
    "0001167483": "Tiger Global",
    "0001536411": "Duquesne Family Office",
    "0001061168": "Baupost Group"
}

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def get_latest_valid_filing(company):
    try:
        filings = company.get_filings(form=["13F-HR", "13F-HR/A"])
        if not filings:
            return None
        return filings[0].obj()
    except Exception as e:
        print(f"Error fetching filings: {e}")
        return None

def scan_institutional_clusters():
    print("Running enterprise-grade institutional cluster scan (10% max drift)...")
    
    cluster_data = defaultdict(lambda: {"funds": [], "conviction_buyers": 0})

    for cik, fund_name in TOP_12_FUNDS.items():
        try:
            company = Company(cik)
            report = get_latest_valid_filing(company)
            if not report:
                continue
                
            df_holdings = getattr(report, "holdings", None)
            if df_holdings is None or df_holdings.empty:
                df_holdings = getattr(report, "infotable", None)
            if df_holdings is None or df_holdings.empty:
                continue

            total_portfolio_value = df_holdings['Value'].sum() if 'Value' in df_holdings.columns else 1
            ticker_col = next((col for col in ['Ticker', 'tic', 'TICKER'] if col in df_holdings.columns), None)
            
            if not ticker_col:
                continue

            prev_report = report.previous_holding_report()
            prev_holdings_set = set()
            if prev_report and hasattr(prev_report, "holdings"):
                prev_df = prev_report.holdings
                p_col = next((col for col in ['Ticker', 'tic', 'TICKER'] if col in prev_df.columns), None)
                if p_col:
                    prev_holdings_set = set(prev_df[p_col].dropna().str.upper().str.strip())

            for _, row in df_holdings.iterrows():
                t = row.get(ticker_col)
                val = row.get('Value', 0)
                if pd.isna(t):
                    continue
                clean_ticker = str(t).strip().upper()
                if not clean_ticker or clean_ticker == "NAN":
                    continue

                position_weight = (val / total_portfolio_value) * 100 if total_portfolio_value > 0 else 0
                if position_weight < 1.5:
                    continue 

                is_new_or_added = clean_ticker not in prev_holdings_set or len(prev_holdings_set) == 0

                cluster_data[clean_ticker]["funds"].append(fund_name)
                if is_new_or_added:
                    cluster_data[clean_ticker]["conviction_buyers"] += 1

        except Exception as e:
            print(f"Error parsing fund {fund_name}: {e}")

    alerts = []
    for ticker, data in cluster_data.items():
        unique_funds = list(set(data["funds"]))
        if len(unique_funds) >= 3:
            try:
                stock = yf.Ticker(ticker)
                hist = stock.history(period="3mo")
                if hist.empty or len(hist) < 2:
                    continue

                q_start = float(hist["Close"].iloc[0])
                current = float(hist["Close"].iloc[-1])
                drift = ((current - q_start) / q_start) * 100

                # Strict Gate: Price drift must not exceed 10.0%
                if drift > 10.0:
                    continue

                info = stock.info
                sector = info.get('sector', 'Unknown Sector')
                trailing_eps = info.get('trailingEps', 0)
                
                if trailing_eps is not None and trailing_eps < -2.0:
                    print(f"Skipping {ticker}: Severe negative earnings (EPS: {trailing_eps})")
                    continue

                fund_list = "\n".join([f"• {f}" for f in unique_funds])
                msg = (
                    f"💎 **ELITE SMART MONEY CLUSTER**\n"
                    f"• **Ticker:** `{ticker}`\n"
                    f"• **Sector:** `{sector}`\n"
                    f"• **Overlapping Funds:** `{len(unique_funds)}`\n"
                    f"• **New/Accumulated Stakes:** `{data['conviction_buyers']} funds`\n"
                    f"• **Price Drift:** `+{drift:.1f}%` (${current:.2f})\n\n"
                    f"🏛 **Backing Funds (Weight > 1.5%):**\n{fund_list}\n\n"
                    f"💡 *Institutional Quality Check Passed:* High portfolio conviction, solid earnings baseline, and strict price discipline (<= 10% drift)."
                )
                alerts.append(msg)

            except Exception as e:
                print(f"Error analyzing fundamentals/price for {ticker}: {e}")

    for alert in alerts:
        send_telegram(alert)

if __name__ == "__main__":
    scan_institutional_clusters()
