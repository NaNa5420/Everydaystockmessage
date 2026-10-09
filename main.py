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
# 工具：格式化函數
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
# 1. 台股大盤 + 櫃買指數 (yfinance 優先)
# ==========================================

def fetch_twse_and_otc_market():
    taipei_now = get_taipei_now()

    result = {
        "日期": taipei_now.strftime("%Y-%m-%d"),
        "加權指數": "暫無資料",
        "加權漲跌": "",
        "加權成交量": "暫無資料",
        "櫃買指數": "暫無資料",
        "櫃買漲跌": "",
        "櫃買成交量": "暫無資料",
    }

    # 1. 使用 yfinance 抓取加權指數 (^TWII) 與 櫃買指數 (^TWOII)
    try:
        tickers = yf.Tickers("^TWII ^TWOII")

        # 加權指數
        twii_hist = tickers.tickers["^TWII"].history(period="5d")
        if not twii_hist.empty and len(twii_hist) >= 2:
            close = twii_hist['Close'].iloc[-1]
            prev = twii_hist['Close'].iloc[-2]
            change = close - prev
            pct = (change / prev) * 100
            vol = twii_hist['Volume'].iloc[-1]

            result["加權指數"] = f"{close:,.2f}"
            result["加權漲跌"] = f"{change:+.2f} ({pct:+.2f}%)"
            result["加權成交量"] = f"{vol / 100000000:.1f} 億股" if vol > 0 else "已收盤"

        # 櫃買指數
        twoii_hist = tickers.tickers["^TWOII"].history(period="5d")
        if not twoii_hist.empty and len(twoii_hist) >= 2:
            close_otc = twoii_hist['Close'].iloc[-1]
            prev_otc = twoii_hist['Close'].iloc[-2]
            change_otc = close_otc - prev_otc
            pct_otc = (change_otc / prev_otc) * 100
            vol_otc = twoii_hist['Volume'].iloc[-1]

            result["櫃買指數"] = f"{close_otc:,.2f}"
            result["櫃買漲跌"] = f"{change_otc:+.2f} ({pct_otc:+.2f}%)"
            result["櫃買成交量"] = f"{vol_otc / 100000000:.1f} 億股" if vol_otc > 0 else "已收盤"

    except Exception as e:
        print(f"⚠️ yfinance 大盤/櫃買抓取失敗: {e}")

    # 2. 嘗試向 TWSE 官方 API 補充金額與進階成交金額（若成功則覆蓋金額資料）
    try:
        today_str = taipei_now.strftime("%Y%m%d")
        url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
        res = requests.get(url, params={"date": today_str, "type": "ALLBUT0999", "response": "json"}, headers=HEADERS, timeout=TIMEOUT)
        data = res.json()
        if data.get("stat") == "OK":
            for table_key in data:
                rows = data.get(table_key, [])
                if isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, list) and len(row) >= 2:
                            if "成交金額" in str(row[0]) and "股票" in str(row[0]):
                                result["加權成交量"] = f"{row[1]} 元"
    except Exception:
        pass  # 若 TWSE 阻擋 IP，沿用 yfinance 抓到的數據即可

    return result


# ==========================================
# 2. 三大法人買賣超
# ==========================================

def fetch_institutional_data():
    today_str = get_taipei_now().strftime("%Y%m%d")
    url = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
    params = {"dayDate": today_str, "type": "day", "response": "json"}

    result = {
        "外資": "暫無資料",
        "投信": "暫無資料",
        "自營商": "暫無資料",
        "三大法人合計": "暫無資料"
    }

    try:
        res = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        data = res.json()
        if data.get("stat") == "OK":
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
    except Exception as e:
        print(f"⚠️ 三大法人抓取失敗: {e}")

    return result


# ==========================================
# 3. 個股行情 (上市權值 + 櫃買熱門股)
# ==========================================

def fetch_stock_market():
    result = {
        "上市熱門股": [],
        "櫃買強勢股": []
    }

    # 使用 yfinance 抓取台股上市與上櫃指標焦點股
    tw_tickers = {
        "2330.TW": "台積電", "2317.TW": "鴻海", "2454.TW": "聯發科",
        "2382.TW": "廣達", "2881.TW": "富邦金", "3231.TW": "緯創"
    }
    otc_tickers = {
        "3293.TWO": "鈊象", "6488.TWO": "環球晶", "8069.TWO": "元太",
        "3529.TWO": "力旺", "6121.TWO": "新普", "5483.TWO": "中美晶"
    }

    all_symbols = list(tw_tickers.keys()) + list(otc_tickers.keys())

    try:
        tickers_data = yf.Tickers(" ".join(all_symbols))

        # 處理上市焦點
        for sym, name in tw_tickers.items():
            hist = tickers_data.tickers[sym].history(period="5d")
            if not hist.empty and len(hist) >= 2:
                close = hist['Close'].iloc[-1]
                prev = hist['Close'].iloc[-2]
                pct = ((close - prev) / prev) * 100
                result["上市熱門股"].append(f"{sym.replace('.TW','')} {name}: {close:,.1f}元 ({format_percent(pct)})")

        # 處理櫃買焦點
        for sym, name in otc_tickers.items():
            hist = tickers_data.tickers[sym].history(period="5d")
            if not hist.empty and len(hist) >= 2:
                close = hist['Close'].iloc[-1]
                prev = hist['Close'].iloc[-2]
                pct = ((close - prev) / prev) * 100
                result["櫃買強勢股"].append(f"{sym.replace('.TWO','')} {name}: {close:,.1f}元 ({format_percent(pct)})")

    except Exception as e:
        print(f"⚠️ 個股行情抓取失敗: {e}")

    return result


# ==========================================
# 4. 財經新聞 RSS
# ==========================================

def fetch_finance_news():
    queries = ["台股 財經", "櫃買市場", "台積電", "AI 半導體", "美股 聯準會"]
    news_list = []

    for query in queries:
        rss_url = "https://news.google.com/rss/search"
        params = {"q": query, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"}

        try:
            response = requests.get(rss_url, params=params, headers=HEADERS, timeout=TIMEOUT)
            root = ET.fromstring(response.content)
            for item in root.findall("./channel/item")[:3]:
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

    return clean_news[:12]


# ==========================================
# 5. 國際市場 (yfinance)
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
                    latest = hist['Close'].iloc[-1]
                    prev = hist['Close'].iloc[-2]
                    pct = ((latest - prev) / prev) * 100
                    result[name] = {"價格": f"{latest:,.2f}", "漲跌幅": pct}
                else:
                    result[name] = {"價格": "暫無資料", "漲跌幅": 0.0}
            except Exception:
                result[name] = {"價格": "暫無資料", "漲跌幅": 0.0}
    except Exception as e:
        print(f"⚠️ 國際市場抓取失敗: {e}")

    return result


# ==========================================
# 6. Gemini AI 分析生成
# ==========================================

def generate_ai_summary(market, institutions, stocks, news, global_markets):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("❌ 未設定 GEMINI_API_KEY")

    client = genai.Client(api_key=api_key.strip())

    news_text = "\n".join([f"- {item['標題']}" for item in news])
    global_text = "\n".join([f"- {name}: {info['價格']} ({format_percent(info['漲跌幅'])})" for name, info in global_markets.items()])

    prompt = f"""
你是一位專業的台股盤後分析師。請根據「真實市場資料」整理今天的台股與櫃買市場盤後情報。

重要規則：
1. 不可以捏造數據。若有數據請精準呈現。
2. 請同時分析「上市加權指數」與「上櫃櫃買指數」的表現。
3. 內容適合 LINE 閱讀，長度約 700～1000 字，使用繁體中文。

━━━━━━━━━━━━━━━━━━
【台股大盤與櫃買市場】
日期：{market.get("日期")}
加權指數：{market.get("加權指數")}
加權漲跌：{market.get("加權漲跌")}
加權成交量/金額：{market.get("加權成交量")}

櫃買指數：{market.get("櫃買指數")}
櫃買漲跌：{market.get("櫃買漲跌")}
櫃買成交量：{market.get("櫃買成交量")}

【三大法人】
外資：{institutions.get("外資")}
投信：{institutions.get("投信")}
自營商：{institutions.get("自營商")}
三大法人合計：{institutions.get("三大法人合計")}

【上市熱門股】
{chr(10).join(stocks["上市熱門股"])}

【櫃買強勢/指標股】
{chr(10).join(stocks["櫃買強勢股"])}

【國際市場】
{global_text}

【今日財經新聞】
{news_text}
━━━━━━━━━━━━━━━━━━

請嚴格使用以下格式輸出：

📊 【台股與櫃買收盤】
- 加權指數與櫃買指數表現（包含指數、漲跌幅、成交量/金額）
- 上市與上櫃市場氣氛對比（例如大型權值股 vs 中小型題材股）

🏦 【三大法人動向】
- 外資 / 投信 / 自營商 買賣超金額與籌碼流向

🔥 【個股與族群焦點】
- 上市大型權值股（台積電、鴻海等）走勢分析
- 櫃買中小型焦點股動態

🌎 【國際市場連動】
- 美股四大指數、費半與日韓股對台股的影響

📰 【今日重要新聞摘要】
- 挑選 3～5 則重點財經新聞並簡短摘要

⚠️ 【明日觀察重點】
- 美國經濟數據、重要法說與盤勢關鍵點

💡 【AI 盤後總結】
- 3 句話精準總結今日盤勢與明日策略

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
    print("🚀 台股+櫃買每日盤後情報 Bot")
    print("=" * 60)

    print("\n1/6 📈 抓取台股加權與櫃買指數...")
    market = fetch_twse_and_otc_market()
    print(market)

    print("\n2/6 🏦 抓取三大法人...")
    institutions = fetch_institutional_data()
    print(institutions)

    print("\n3/6 🔥 抓取上市櫃焦點個股...")
    stocks = fetch_stock_market()
    print(stocks)

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
