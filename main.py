import os
import datetime
import requests
import xml.etree.ElementTree as ET
import time
import yfinance as yf
from zoneinfo import ZoneInfo
from google import genai
from google.genai import errors
from dotenv import load_dotenv


# ==========================================
# 基本設定
# ==========================================

load_dotenv(dotenv_path=".env", override=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    )
}

TIMEOUT = 15

def get_taipei_now():
    """取得台灣當前時間"""
    return datetime.datetime.now(ZoneInfo("Asia/Taipei"))


# ==========================================
# 工具：安全 GET JSON
# ==========================================

def get_json(url, params=None):
    try:
        response = requests.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=TIMEOUT
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"⚠️ API 讀取失敗：{url} -> {e}")
        return None


# ==========================================
# 工具：數字與金額格式化
# ==========================================

def format_money(value):
    try:
        value = float(value)
        if abs(value) >= 100_000_000:
            return f"{value / 100_000_000:.1f} 億"
        elif abs(value) >= 10_000:
            return f"{value / 10_000:.1f} 萬"
        return f"{value:,.0f}"
    except Exception:
        return str(value)


def format_percent(value):
    try:
        return f"{float(value):+.2f}%"
    except Exception:
        return str(value)


# ==========================================
# 1. 台股大盤 (TWSE + yfinance 備援)
# ==========================================

def fetch_twse_market():
    taipei_now = get_taipei_now()
    today_str = taipei_now.strftime("%Y%m%d")

    result = {
        "日期": taipei_now.strftime("%Y-%m-%d"),
        "是否有資料": False,
        "加權指數": "暫無資料",
        "漲跌點": "",
        "漲跌幅": "",
        "成交金額": "暫無資料",
        "上漲家數": "",
        "下跌家數": "",
        "持平家數": "",
    }

    # 1. 嘗試 TWSE Web API
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    params = {"date": today_str, "type": "ALLBUT0999", "response": "json"}
    data = get_json(url, params)

    if data and data.get("stat") == "OK":
        result["是否有資料"] = True
        for table_key in ["data1", "data2", "data3", "data4"]:
            for row in data.get(table_key, []):
                if row and "發行量加權股價指數" in str(row[0]):
                    try:
                        result["加權指數"] = row[1]
                        result["漲跌點"] = f"{row[2]}{row[3]}"
                        result["漲跌幅"] = f"{row[4]}%"
                    except Exception:
                        pass

        for table_key in data:
            rows = data.get(table_key, [])
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, list) and len(row) >= 2:
                        if "成交金額" in str(row[0]) and "股票" in str(row[0]):
                            result["成交金額"] = row[1]
                        if "上漲" in " ".join(str(x) for x in row) and "下跌" in " ".join(str(x) for x in row):
                            try:
                                result["上漲家數"] = row[1]
                                result["下跌家數"] = row[2]
                                result["持平家數"] = row[3]
                            except Exception:
                                pass
        return result

    # 2. 保底機制：使用 yfinance 抓取大盤指數
    print("ℹ️ TWSE Web API 查無資料或遭阻擋，啟用 yfinance 備援大盤...")
    try:
        twii = yf.Ticker("^TWII")
        hist = twii.history(period="5d")
        if not hist.empty and len(hist) >= 2:
            close = hist['Close'].iloc[-1]
            prev_close = hist['Close'].iloc[-2]
            change = close - prev_close
            pct = (change / prev_close) * 100

            result["加權指數"] = f"{close:,.2f}"
            result["漲跌點"] = f"{change:+.2f}"
            result["漲跌幅"] = f"{pct:+.2f}%"
            result["是否有資料"] = True
    except Exception as e:
        print(f"⚠️ yfinance 備援大盤失敗: {e}")

    return result


# ==========================================
# 2. 三大法人買賣超
# ==========================================

def fetch_institutional_data():
    today_str = get_taipei_now().strftime("%Y%m%d")
    url = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
    params = {"dayDate": today_str, "type": "day", "response": "json"}

    data = get_json(url, params)

    result = {
        "外資": "暫無資料",
        "投信": "暫無資料",
        "自營商": "暫無資料",
        "三大法人合計": "暫無資料"
    }

    if not data or data.get("stat") != "OK":
        return result

    for row in data.get("data", []):
        if not row:
            continue
        name = str(row[0]).strip()
        try:
            net_buy = float(str(row[3]).replace(",", ""))
        except Exception:
            continue

        if "外資及陸資" in name:
            result["外資"] = format_money(net_buy)
        elif name == "投信":
            result["投信"] = format_money(net_buy)
        elif "自營商" in name and "自行買賣" in name:
            result["自營商"] = format_money(net_buy)
        elif name == "合計":
            result["三大法人合計"] = format_money(net_buy)

    return result


# ==========================================
# 3. 個股行情 (TWSE + yfinance 權值股熱門備援)
# ==========================================

def fetch_stock_market():
    today_str = get_taipei_now().strftime("%Y%m%d")
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    params = {"date": today_str, "type": "ALLBUT0999", "response": "json"}

    data = get_json(url, params)

    result = {
        "成交金額前五": [],
        "漲幅前五": [],
        "跌幅前五": []
    }

    stock_rows = []

    if data and data.get("stat") == "OK":
        for key, rows in data.items():
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, list) and len(row) >= 10:
                        try:
                            code = str(row[0]).strip()
                            name = str(row[1]).strip()
                            if not code.isdigit():
                                continue

                            close = float(str(row[8]).replace(",", ""))
                            change = str(row[9]).strip()
                            change_value = float(str(row[10]).replace(",", ""))
                            amount = float(str(row[4]).replace(",", ""))

                            if change == "+":
                                percent = (change_value / (close - change_value) * 100) if close != change_value else 0
                            elif change == "-":
                                percent = (-change_value / (close + change_value) * 100)
                            else:
                                percent = 0

                            stock_rows.append({
                                "代號": code, "名稱": name, "收盤": close,
                                "漲跌幅": percent, "成交金額": amount
                            })
                        except Exception:
                            continue

    # 若 TWSE 抓到資料，進行排序輸出
    if stock_rows:
        unique = {stock["代號"]: stock for stock in stock_rows}
        stocks = list(unique.values())

        top_volume = sorted(stocks, key=lambda x: x["成交金額"], reverse=True)[:5]
        top_gainers = sorted(stocks, key=lambda x: x["漲跌幅"], reverse=True)[:5]
        top_losers = sorted(stocks, key=lambda x: x["漲跌幅"])[:5]

        result["成交金額前五"] = [f'{s["代號"]} {s["名稱"]} 成交 {format_money(s["成交金額"])}' for s in top_volume]
        result["漲幅前五"] = [f'{s["代號"]} {s["名稱"]} {format_percent(s["漲跌幅"])}' for s in top_gainers]
        result["跌幅前五"] = [f'{s["代號"]} {s["名稱"]} {format_percent(s["漲跌幅"])}' for s in top_losers]
        return result

    # ----------------------------------------------------
    # 保底機制：當 TWSE 被擋 IP，改用 yfinance 抓取焦點權值股
    # ----------------------------------------------------
    print("ℹ️ TWSE 個股資料無回應，啟用 yfinance 熱門權值股備援...")
    focus_tickers = {
        "2330.TW": "台積電", "2317.TW": "鴻海", "2454.TW": "聯發科",
        "2308.TW": "台達電", "2881.TW": "富邦金", "2382.TW": "廣達",
        "3231.TW": "緯創", "2603.TW": "長榮", "3008.TW": "大立光"
    }

    try:
        tickers_data = yf.Tickers(" ".join(focus_tickers.keys()))
        yf_stocks = []

        for symbol, name in focus_tickers.items():
            hist = tickers_data.tickers[symbol].history(period="5d")
            if not hist.empty and len(hist) >= 2:
                close = hist['Close'].iloc[-1]
                prev_close = hist['Close'].iloc[-2]
                pct = ((close - prev_close) / prev_close) * 100
                yf_stocks.append({"代號": symbol.replace(".TW", ""), "名稱": name, "收盤": close, "漲跌幅": pct})

        if yf_stocks:
            sorted_gainers = sorted(yf_stocks, key=lambda x: x["漲跌幅"], reverse=True)
            result["成交金額前五"] = [f'{s["代號"]} {s["名稱"]} 收盤 {s["收盤"]:,.1f} 元 ({format_percent(s["漲跌幅"])})' for s in yf_stocks[:5]]
            result["漲幅前五"] = [f'{s["代號"]} {s["名稱"]} {format_percent(s["漲跌幅"])}' for s in sorted_gainers[:3]]
            result["跌幅前五"] = [f'{s["代號"]} {s["名稱"]} {format_percent(s["漲跌幅"])}' for s in sorted_gainers[-3:]]

    except Exception as e:
        print(f"⚠️ yfinance 個股備援失敗: {e}")

    return result


# ==========================================
# 4. 財經新聞 RSS
# ==========================================

def fetch_finance_news():
    queries = ["台股 財經", "台積電", "AI 半導體 台股", "美股 聯準會"]
    news_list = []

    for query in queries:
        rss_url = "https://news.google.com/rss/search"
        params = {"q": query, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"}

        try:
            response = requests.get(rss_url, params=params, headers=HEADERS, timeout=TIMEOUT)
            root = ET.fromstring(response.content)
            for item in root.findall("./channel/item")[:5]:
                title = item.findtext("title")
                pub_date = item.findtext("pubDate")
                if title:
                    news_list.append({"標題": title.strip(), "時間": pub_date or ""})
        except Exception as e:
            print(f"⚠️ RSS 讀取失敗：{query} / {e}")

    seen = set()
    clean_news = []
    for news in news_list:
        if news["標題"] not in seen:
            seen.add(news["標題"])
            clean_news.append(news)

    return clean_news[:15]


# ==========================================
# 5. 國際市場 (yfinance 穩定抓取)
# ==========================================

def fetch_global_markets():
    symbols = {
        "NASDAQ": "^IXIC", "S&P 500": "^GSPC", "費城半導體": "^SOX",
        "日經": "^N225", "韓國 KOSPI": "^KS11", "美元/台幣": "TWD=X",
        "黃金": "GC=F", "WTI原油": "CL=F",
    }

    result = {}
    try:
        tickers = yf.Tickers(" ".join(symbols.values()))
        for name, symbol in symbols.items():
            try:
                hist = tickers.tickers[symbol].history(period="5d")
                if not hist.empty and len(hist) >= 2:
                    latest_price = hist['Close'].iloc[-1]
                    prev_price = hist['Close'].iloc[-2]
                    change_pct = ((latest_price - prev_price) / prev_price) * 100
                    result[name] = {"價格": f"{latest_price:,.2f}", "漲跌幅": change_pct}
                else:
                    result[name] = {"價格": "暫無資料", "漲跌幅": 0.0}
            except Exception:
                result[name] = {"價格": "暫無資料", "漲跌幅": 0.0}
    except Exception as e:
        print(f"⚠️ 國際市場抓取失敗: {e}")

    return result


# ==========================================
# 6. Gemini AI 分析
# ==========================================

def generate_ai_summary(market, institutions, stocks, news, global_markets):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("❌ 未設定 GEMINI_API_KEY")

    client = genai.Client(api_key=api_key.strip())

    news_text = "\n".join([f"- {item['標題']}" for item in news])
    global_text = "\n".join([f"- {name}: {info['價格']} ({format_percent(info['漲跌幅'])})" for name, info in global_markets.items()])

    prompt = f"""
你是一位專業的台股盤後分析師。請根據「真實市場資料」整理今天的台股盤後情報。

重要規則：
1. 不可以自行捏造數字。
2. 如果資料是「暫無資料」，必須明確寫出。
3. 不要把新聞標題當成已確認的事實。
4. 不要提供買進、賣出或個股投資建議。
5. 內容適合 LINE 閱讀，長度約 700～1000 字，使用繁體中文。

━━━━━━━━━━━━━━━━━━
【台股大盤】
日期：{market.get("日期")}
加權指數：{market.get("加權指數")}
漲跌點：{market.get("漲跌點")}
漲跌幅：{market.get("漲跌幅")}
成交金額：{market.get("成交金額")}
上漲家數：{market.get("上漲家數")}
下跌家數：{market.get("下跌家數")}
持平家數：{market.get("持平家數")}

【三大法人】
外資：{institutions.get("外資")}
投信：{institutions.get("投信")}
自營商：{institutions.get("自營商")}
三大法人合計：{institutions.get("三大法人合計")}

【成交金額前五】
{chr(10).join(stocks["成交金額前五"])}

【漲幅前五】
{chr(10).join(stocks["漲幅前五"])}

【跌幅前五】
{chr(10).join(stocks["跌幅前五"])}

【國際市場】
{global_text}

【今日財經新聞】
{news_text}
━━━━━━━━━━━━━━━━━━

請使用以下格式輸出：

📊 【台股收盤】
- 指數 / 漲跌幅 / 成交金額 / 市場氣氛

🏦 【三大法人】
- 外資 / 投信 / 自營商 / 合計與資金方向

🔥 【個股焦點】
- 指標權值股與焦點強弱勢股說明

🌎 【國際市場】
- 主要指數與匯率商品動態

📰 【今日重要新聞】
- 3~5 則重點新聞摘要

⚠️ 【明日市場焦點】
- 觀察重點與事件

💡 【AI 盤後觀察】
- 3 句話總結今日走勢與明日關鍵

⚠️ 本內容為市場資訊整理，不構成投資建議。
"""

    models_to_try = ["gemini-3.8-flash", "gemini-1.5-flash"]

    for model_name in models_to_try:
        for attempt in range(1, 4):
            try:
                print(f"🤖 使用 Gemini 模型：{model_name} (第 {attempt} 次)")
                response = client.models.generate_content(model=model_name, contents=prompt)
                if response.text:
                    return response.text.strip()
            except errors.ServerError as e:
                print(f"⚠️ Gemini 伺服器忙碌：{e}")
                time.sleep(3)
            except Exception as e:
                print(f"⚠️ Gemini 呼叫失敗：{e}")
                break

    raise RuntimeError("❌ 所有 Gemini 模型皆無法回應")


# ==========================================
# 7. LINE 推播
# ==========================================

def send_line_message(text):
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.getenv("LINE_USER_ID")

    if not token:
        raise ValueError("❌ 未設定 LINE_CHANNEL_ACCESS_TOKEN")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token.strip()}"
    }

    if len(text) > 4900:
        text = text[:4890] + "\n..."

    url = "https://api.line.me/v2/bot/message/push" if user_id and user_id.strip() else "https://api.line.me/v2/bot/message/broadcast"
    payload = {"to": user_id.strip(), "messages": [{"type": "text", "text": text}]} if user_id and user_id.strip() else {"messages": [{"type": "text", "text": text}]}

    response = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)

    if response.status_code == 200:
        print("✅ LINE 推播成功")
    else:
        print(f"❌ LINE 推播失敗 {response.status_code}: {response.text}")


# ==========================================
# 8. 主程式
# ==========================================

def main():
    print("=" * 60)
    print("🚀 台股每日盤後情報 Bot (台灣時間整合版)")
    print("=" * 60)

    print("\n1/6 📈 抓取台股大盤...")
    market = fetch_twse_market()
    print(market)
    time.sleep(3)

    print("\n2/6 🏦 抓取三大法人...")
    institutions = fetch_institutional_data()
    print(institutions)
    time.sleep(3)

    print("\n3/6 🔥 抓取個股排行...")
    stocks = fetch_stock_market()
    print(stocks)
    time.sleep(3)

    print("\n4/6 📰 抓取財經新聞...")
    news = fetch_finance_news()
    print(f"取得 {len(news)} 則新聞")

    print("\n5/6 🌎 抓取國際市場...")
    global_markets = fetch_global_markets()
    print(global_markets)

    print("\n6/6 🤖 產生 AI 盤後分析...")
    summary = generate_ai_summary(market, institutions, stocks, news, global_markets)

    print("\n" + "=" * 60)
    print(summary)
    print("=" * 60)

    print("\n📱 發送 LINE...")
    send_line_message(summary)
    print("\n🎉 今日任務完成！")


if __name__ == "__main__":
    main()
