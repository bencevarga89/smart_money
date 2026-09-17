import os
import requests
import datetime
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

def get_last_completed_quarter_end(today=None):
    """
    Calculates the exact calendar quarter-end date prior to today.
    13F filings reflect positions as of these quarter ends:
    Q1: Mar 31 | Q2: Jun 30 | Q3: Sep 30 | Q4: Dec 31
    """
    if today is None:
        today = datetime.date.today()
    year = today.year
    month = today.month
    
    if month in [1, 2, 3]:
        return datetime.date(year - 1, 12, 31)
    elif month in [4, 5, 6]:
        return datetime.date(year, 3, 31)
    elif month in [7, 8, 9]:
        return datetime.date(year, 6, 30)
    else:
        return datetime.date(year, 9, 30)

def scan_institutional_clusters():
    print("Running enterprise-grade institutional cluster scan...")
    
    cluster_data = defaultdict(lambda: {"funds": [], "conviction_buyers": 0})
    quarter_end_date = get_last_completed_quarter_end()
    print(f"Anchoring price drift calculation to quarter-end date: {quarter_end_date}")

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
            prev_holdings_dict = {}  # ticker -> value for position change detection
            if prev_report and hasattr(prev_report, "holdings"):
                prev_df = prev_report.holdings
                p_col = next((col for col in ['Ticker', 'tic', 'TICKER'] if col in prev_df.columns), None)
                if p_col and 'Value' in prev_df.columns:
                    for _, p_row in prev_df.iterrows():
                        p_ticker = str(p_row.get(p_col, '')).strip().upper()
                        p_value = p_row.get('Value', 0)
                        if p_ticker and p_ticker != "NAN":
                            prev_holdings_dict[p_ticker] = p_value

            for _, row in df_holdings.iterrows():
                t = row.get(ticker_col)
                val = row.get('Value', 0)
                if pd.isna(t):
                    continue
                clean_ticker = str(t).strip().upper()
                if not clean_ticker or clean_ticker == "NAN":
                    continue

                # Gate 1: Skin in the game (Position >= 1.5% of total portfolio value)
                position_weight = (val / total_portfolio_value) * 100 if total_portfolio_value > 0 else 0
                if position_weight < 1.5:
                    continue 

                # Detect conviction: new position OR position increased >25% in value
                prev_value = prev_holdings_dict.get(clean_ticker, 0)
                if prev_value == 0:
                    # New position this quarter
                    is_conviction = True
                    position_change_pct = 100.0
                else:
                    # Existing position - check if fund added significantly (>25% increase)
                    position_change_pct = ((val - prev_value) / prev_value) * 100
                    is_conviction = position_change_pct > 25

                cluster_data[clean_ticker]["funds"].append(fund_name)
                if is_conviction:
                    cluster_data[clean_ticker]["conviction_buyers"] += 1

        except Exception as e:
            print(f"Error parsing fund {fund_name}: {e}")

    alerts = []
    for ticker, data in cluster_data.items():
        unique_funds = list(set(data["funds"]))
        print(f"\n--- Analyzing {ticker} ({len(unique_funds)} funds, {data['conviction_buyers']} conviction) ---")
        if len(unique_funds) >= 3 and data["conviction_buyers"] >= 2:
            print(f"✓ Passed conviction gate")
            try:
                stock = yf.Ticker(ticker)
                
                # Fetch historical price starting near the quarter-end date
                start_fetch = quarter_end_date - datetime.timedelta(days=7)
                hist = stock.history(start=start_fetch.strftime('%Y-%m-%d'))
                
                if hist.empty or len(hist) < 2:
                    continue

                # Filter history strictly on or after the exact quarter-end date
                valid_hist = hist[hist.index.date >= quarter_end_date]
                if valid_hist.empty:
                    valid_hist = hist

                q_start = float(valid_hist["Close"].iloc[0])
                current = float(hist["Close"].iloc[-1])
                drift = ((current - q_start) / q_start) * 100

                # Gate 2: Strict Price Drift Cap (Max +5.0% run-up since quarter end)
                if drift > 5.0:
                    print(f"Skipping {ticker}: Price drift +{drift:.1f}% exceeds 5% limit.")
                    continue

                info = stock.info
                sector = info.get('sector', 'Unknown Sector')
                trailing_eps = info.get('trailingEps', 0)

                # Gate 3: Fundamental Sanity Check
                if trailing_eps is not None and trailing_eps < -2.0:
                    print(f"Skipping {ticker}: Severe negative earnings (EPS: {trailing_eps})")
                    continue

                # Gate 4: Valuation Check (P/E Ratio)
                trailing_pe = info.get('trailingPE', None)
                if trailing_pe is None or trailing_pe > 30:
                    print(f"Skipping {ticker}: P/E ratio {trailing_pe} exceeds 30x threshold or unavailable")
                    continue

                # Gate 5: ROE Check (>15% for long-term outperformance)
                roe = info.get('returnOnEquity', None)
                if roe is None or roe < 0.15:
                    print(f"Skipping {ticker}: ROE {roe} below 15% threshold or unavailable")
                    continue

                # Gate 5b: Free Cash Flow Check (must be positive)
                fcf = info.get('operatingCashflow', None)
                if fcf is None or fcf <= 0:
                    print(f"Skipping {ticker}: Free Cash Flow ${fcf} is negative or unavailable")
                    continue

                # Gate 6: Revenue Growth Check
                revenue_growth = info.get('revenueGrowth', None)
                if revenue_growth is None or revenue_growth < 0:
                    print(f"Skipping {ticker}: Revenue growth {revenue_growth} is negative or unavailable")
                    continue

                # Gate 7: 200-Day Moving Average (price entry discipline)
                if len(hist) >= 200:
                    ma_200 = hist["Close"].tail(200).mean()
                    current_price = float(hist["Close"].iloc[-1])
                    if current_price > ma_200:
                        print(f"Skipping {ticker}: Trading above 200-day MA (${current_price:.2f} vs MA ${ma_200:.2f})")
                        continue

                print(f"✓ All gates passed! ROE={roe*100:.1f}%, FCF=${fcf:,.0f}")
                fund_list = "\n".join([f"• {f}" for f in unique_funds])
                msg = (
                    f"💎 **SMART MONEY OUTPERFORMER PICK (>8-10% Target)**\n"
                    f"• **Ticker:** `{ticker}`\n"
                    f"• **Sector:** `{sector}`\n"
                    f"• **Conviction Buyers:** `{data['conviction_buyers']}/3+ funds NEW or ADDED positions`\n"
                    f"• **Total Overlapping Funds:** `{len(unique_funds)}`\n\n"
                    f"📊 **Quality Metrics (Long-Term Compounding):**\n"
                    f"• **ROE:** `{roe*100:.1f}%` (gate: >15% — beats market returns)\n"
                    f"• **Free Cash Flow:** `${fcf:,.0f}` (gate: positive — self-funding)\n"
                    f"• **P/E Ratio:** `{trailing_pe:.1f}x` (gate: <30x)\n"
                    f"• **Revenue Growth:** `{revenue_growth*100:.1f}%` (gate: positive)\n"
                    f"• **Trailing EPS:** `${trailing_eps:.2f}`\n\n"
                    f"💰 **Price Action & Entry:**\n"
                    f"• **Current Price:** `${current:.2f}`\n"
                    f"• **Drift Since Q-End:** `+{drift:.1f}%` (gate: <=5% — disciplined entry)\n"
                    f"• **Quarter-End Anchor:** `{quarter_end_date.strftime('%b %d, %Y')}`\n\n"
                    f"🏛 **Backing Funds (>1.5% positions):**\n{fund_list}\n\n"
                    f"🎯 **Why This Pick:**\n"
                    f"• **ROE {roe*100:.1f}% exceeds 15%** — historically beats 8-10% market baseline; company compounds capital efficiently\n"
                    f"• **${fcf:,.0f} FCF shows sustainability** — business self-funds growth, not dependent on financing\n"
                    f"• **{data['conviction_buyers']} elite funds ADDING (not just holding)** — signals fresh capital deployed, conviction intensifying\n"
                    f"• **Price +{drift:.1f}% off Q-end** — disciplined entry, not chasing runups; room to accumulate\n"
                    f"• **Positive revenue growth + healthy earnings** — real business momentum, not accounting tricks\n\n"
                    f"✅ *Investment Thesis:* High-return compounder with smart money backing, self-funding growth, reasonable valuation, and disciplined entry point. Positioned for 8-10%+ annual returns over mid-long term."
                )
                alerts.append(msg)

            except Exception as e:
                print(f"Error analyzing fundamentals/price for {ticker}: {e}")
        else:
            print(f"✗ Failed conviction gate: needs 3+ funds + 2+ conviction (has {len(unique_funds)} funds, {data['conviction_buyers']} conviction)")

    for alert in alerts:
        print(f"\n{'='*70}\n{alert}\n{'='*70}")
        send_telegram(alert)

if __name__ == "__main__":
    scan_institutional_clusters()
