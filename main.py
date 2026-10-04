import os
import datetime
import requests
import xml.etree.ElementTree as ET
import time
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
        "AppleWebKit/537.36 Chrome/140 Safari/537.36"
    )
}

TIMEOUT = 15


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
        print(f"⚠️ API 讀取失敗：{url}")
        print(f"   {e}")
        return None


# ==========================================
# 工具：數字格式
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
# 1. 台股：大盤 + 成交量 + 漲跌家數
# ==========================================

def fetch_twse_market():
    today = datetime.datetime.now()
    today_str = today.strftime("%Y%m%d")

    result = {
        "日期": today.strftime("%Y-%m-%d"),
        "是否有資料": False,
        "加權指數": "暫無資料",
        "漲跌點": "",
        "漲跌幅": "",
        "成交金額": "暫無資料",
        "上漲家數": "",
        "下跌家數": "",
        "持平家數": "",
    }

    url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"

    params = {
        "date": today_str,
        "type": "ALLBUT0999",
        "response": "json"
    }

    data = get_json(url, params)

    if not data or data.get("stat") != "OK":
        print("ℹ️ 今日可能為休市日，沒有 TWSE 盤後資料。")
        return result

    result["是否有資料"] = True

    # --------------------------------------
    # 找「發行量加權股價指數」
    # --------------------------------------

    for table_key in ["data1", "data2", "data3", "data4"]:
        rows = data.get(table_key, [])

        for row in rows:
            if not row:
                continue

            text = str(row[0])

            if "發行量加權股價指數" in text:
                try:
                    result["加權指數"] = row[1]
                    result["漲跌點"] = f"{row[2]}{row[3]}"
                    result["漲跌幅"] = f"{row[4]}%"
                except Exception:
                    pass

    # --------------------------------------
    # 找市場成交資訊
    # --------------------------------------

    for table_key in data:
        rows = data.get(table_key, [])

        if not isinstance(rows, list):
            continue

        for row in rows:
            if not isinstance(row, list) or len(row) < 2:
                continue

            text = str(row[0])

            if "成交金額" in text and "股票" in text:
                try:
                    result["成交金額"] = row[1]
                except Exception:
                    pass

    # --------------------------------------
    # 找漲跌家數
    # --------------------------------------

    for table_key in data:
        rows = data.get(table_key, [])

        if not isinstance(rows, list):
            continue

        for row in rows:
            if not isinstance(row, list):
                continue

            row_text = " ".join(str(x) for x in row)

            if "上漲" in row_text and "下跌" in row_text:
                try:
                    result["上漲家數"] = row[1]
                    result["下跌家數"] = row[2]
                    result["持平家數"] = row[3]
                except Exception:
                    pass

    return result


# ==========================================
# 2. 三大法人
# ==========================================

def fetch_institutional_data():
    today_str = datetime.datetime.now().strftime("%Y%m%d")

    url = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"

    params = {
        "dayDate": today_str,
        "type": "day",
        "response": "json"
    }

    data = get_json(url, params)

    result = {
        "外資": "暫無資料",
        "投信": "暫無資料",
        "自營商": "暫無資料",
        "三大法人合計": "暫無資料"
    }

    if not data or data.get("stat") != "OK":
        return result

    rows = data.get("data", [])

    for row in rows:
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

        elif "自營商" in name and "合計" not in name:
            # 只保留自營商自行買賣
            if "自行買賣" in name:
                result["自營商"] = format_money(net_buy)

        elif name == "合計":
            result["三大法人合計"] = format_money(net_buy)

    return result


# ==========================================
# 3. 個股行情
# ==========================================

def fetch_stock_market():
    today_str = datetime.datetime.now().strftime("%Y%m%d")

    url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"

    params = {
        "date": today_str,
        "type": "ALLBUT0999",
        "response": "json"
    }

    data = get_json(url, params)

    result = {
        "成交金額前五": [],
        "漲幅前五": [],
        "跌幅前五": []
    }

    if not data or data.get("stat") != "OK":
        return result

    stock_rows = []

    # MI_INDEX 不同時期 table key 可能不同
    # 把所有看起來像股票資料的 row 收集起來
    for key, rows in data.items():

        if not isinstance(rows, list):
            continue

        for row in rows:

            if not isinstance(row, list):
                continue

            if len(row) < 10:
                continue

            try:
                code = str(row[0]).strip()
                name = str(row[1]).strip()

                # 避免把指數資料混進來
                if not code.isdigit():
                    continue

                close = float(str(row[8]).replace(",", ""))
                change = str(row[9]).strip()

                # row[10] 通常是漲跌價差
                change_value = float(
                    str(row[10]).replace(",", "")
                )

                # 成交金額
                amount = float(
                    str(row[4]).replace(",", "")
                )

                if change == "+":
                    percent = (
                        change_value / (close - change_value) * 100
                        if close - change_value != 0
                        else 0
                    )

                elif change == "-":
                    percent = (
                        -change_value / (close + change_value) * 100
                        if close + change_value != 0
                        else 0
                    )

                else:
                    percent = 0

                stock_rows.append({
                    "代號": code,
                    "名稱": name,
                    "收盤": close,
                    "漲跌幅": percent,
                    "成交金額": amount
                })

            except Exception:
                continue

    # 去除重複股票
    unique = {}

    for stock in stock_rows:
        unique[stock["代號"]] = stock

    stocks = list(unique.values())

    # 成交金額前五
    top_volume = sorted(
        stocks,
        key=lambda x: x["成交金額"],
        reverse=True
    )[:5]

    # 漲幅前五
    top_gainers = sorted(
        stocks,
        key=lambda x: x["漲跌幅"],
        reverse=True
    )[:5]

    # 跌幅前五
    top_losers = sorted(
        stocks,
        key=lambda x: x["漲跌幅"]
    )[:5]

    for stock in top_volume:
        result["成交金額前五"].append(
            f'{stock["代號"]} {stock["名稱"]} '
            f'成交 {format_money(stock["成交金額"])}'
        )

    for stock in top_gainers:
        result["漲幅前五"].append(
            f'{stock["代號"]} {stock["名稱"]} '
            f'{format_percent(stock["漲跌幅"])}'
        )

    for stock in top_losers:
        result["跌幅前五"].append(
            f'{stock["代號"]} {stock["名稱"]} '
            f'{format_percent(stock["漲跌幅"])}'
        )

    return result


# ==========================================
# 4. 財經新聞 RSS
# ==========================================

def fetch_finance_news():
    queries = [
        "台股 財經",
        "台積電",
        "AI 半導體 台股",
        "美股 聯準會",
    ]

    news_list = []

    for query in queries:

        rss_url = "https://news.google.com/rss/search"

        params = {
            "q": query,
            "hl": "zh-TW",
            "gl": "TW",
            "ceid": "TW:zh-Hant"
        }

        try:
            response = requests.get(
                rss_url,
                params=params,
                headers=HEADERS,
                timeout=TIMEOUT
            )

            root = ET.fromstring(response.content)

            items = root.findall("./channel/item")[:5]

            for item in items:

                title = item.findtext("title")
                pub_date = item.findtext("pubDate")

                if title:
                    news_list.append({
                        "標題": title.strip(),
                        "時間": pub_date or ""
                    })

        except Exception as e:
            print(f"⚠️ RSS 讀取失敗：{query} / {e}")

    # 去重
    seen = set()
    clean_news = []

    for news in news_list:

        title = news["標題"]

        if title in seen:
            continue

        seen.add(title)
        clean_news.append(news)

    return clean_news[:15]


# ==========================================
# 5. 國際市場
# 使用 Yahoo Finance Chart API
# ==========================================

def fetch_global_markets():

    symbols = {
        "NASDAQ": "^IXIC",
        "S&P 500": "^GSPC",
        "費城半導體": "^SOX",
        "日經": "^N225",
        "韓國 KOSPI": "^KS11",
        "美元/台幣": "TWD=X",
        "黃金": "GC=F",
        "WTI原油": "CL=F",
    }

    # 將所有 Symbol 用逗號串接，一次發送請求
    symbol_str = ",".join(symbols.values())
    url = f"https://query1.finance.yahoo.com/v7/finance/quote"
    params = {"symbols": symbol_str}

    result = {}

    try:
        response = requests.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=TIMEOUT
        )
        data = response.json()
        quote_list = data.get("quoteResponse", {}).get("result", [])

        # 代號轉換對照表
        ticker_map = {v: k for k, v in symbols.items()}

        for quote in quote_list:
            symbol = quote.get("symbol")
            name = ticker_map.get(symbol)
            if not name:
                continue

            price = quote.get("regularMarketPrice")
            # Yahoo 直接提供的單日官方漲跌幅(%)
            change_percent = quote.get("regularMarketChangePercent", 0.0)

            result[name] = {
                "價格": price,
                "漲跌幅": change_percent
            }

    except Exception as e:
        print(f"⚠️ 國際市場資料抓取失敗: {e}")

    return result

# ==========================================
# 6. Gemini AI
# ==========================================

def generate_ai_summary(
    market,
    institutions,
    stocks,
    news,
    global_markets
):

    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        raise ValueError(
            "❌ 未設定 GEMINI_API_KEY"
        )

    client = genai.Client(
        api_key=api_key.strip()
    )

    news_text = "\n".join(
        [
            f"- {item['標題']}"
            for item in news
        ]
    )

    global_text = "\n".join(
        [
            f"- {name}: "
            f"{info['價格']} "
            f"({format_percent(info['漲跌幅'])})"
            for name, info in global_markets.items()
        ]
    )

    prompt = f"""
你是一位專業的台股盤後分析師。

請根據「真實市場資料」整理今天的台股盤後情報。

重要規則：

1. 不可以自行捏造數字。
2. 如果資料是「暫無資料」，必須明確寫出。
3. 不要把新聞標題當成已確認的事實。
4. 不要提供買進、賣出或個股投資建議。
5. 內容要適合 LINE 閱讀。
6. 總長度約 700～1000 字。
7. 使用繁體中文。
8. 優先講「今天市場真正重要的事情」。

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

請使用以下格式：

📊 【台股收盤】

說明今天大盤：
- 指數
- 漲跌幅
- 成交量/成交金額
- 上漲下跌家數
- 今天市場氣氛

🏦 【三大法人】

簡單說明：
- 外資
- 投信
- 自營商
- 三大法人合計

並指出今天法人資金方向。

🔥 【個股焦點】

列出：
- 成交金額最大的股票
- 今日強勢股票
- 今日弱勢股票

不要只是列數字，要簡短說明值得注意的地方。

🌎 【國際市場】

整理：
- Nasdaq
- S&P 500
- 費半
- 日經
- 韓國
- 美元/台幣
- 黃金
- 原油

只挑真正可能影響台股的因素說明。

📰 【今日重要新聞】

從新聞中挑 3～5 則最重要的。

每則：
• 標題
• 一句話摘要

⚠️ 【明日市場焦點】

列出明天最值得注意的：
- 美國經濟數據
- Fed / 利率
- 公司法說
- 財報
- 台股重要事件
- 國際事件

如果資料不足，就寫「目前沒有足夠資料確認」。

💡 【AI 盤後觀察】

用 3～5 句話總結：

「今天台股為什麼漲/跌？」

「資金主要流向哪裡？」

「明天最值得注意什麼？」

最後加上：

⚠️ 本內容為市場資訊整理，不構成投資建議。
"""

    models_to_try = [
        "gemini-3.8-flash"
    ]

    for model_name in models_to_try:

        for attempt in range(1, 6):

            try:

                print(
                    f"🤖 使用 Gemini 模型："
                    f"{model_name} "
                    f"(第 {attempt} 次)"
                )

                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )

                if response.text:
                    return response.text.strip()

            except errors.ServerError as e:

                print(
                    f"⚠️ Gemini 伺服器忙碌：{e}"
                )

                time.sleep(3)

            except Exception as e:

                print(
                    f"⚠️ Gemini 呼叫失敗：{e}"
                )

                break

    raise RuntimeError(
        "❌ 所有 Gemini 模型皆無法回應"
    )


# ==========================================
# 7. LINE 推播
# ==========================================

def send_line_message(text):

    token = os.getenv(
        "LINE_CHANNEL_ACCESS_TOKEN"
    )

    user_id = os.getenv("LINE_USER_ID")

    if not token:
        raise ValueError(
            "❌ 未設定 LINE_CHANNEL_ACCESS_TOKEN"
        )

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token.strip()}"
    }

    # LINE 單則文字訊息限制
    if len(text) > 4900:
        text = text[:4890] + "\n..."

    if user_id and user_id.strip():

        url = (
            "https://api.line.me/v2/bot/message/push"
        )

        payload = {
            "to": user_id.strip(),
            "messages": [
                {
                    "type": "text",
                    "text": text
                }
            ]
        }

    else:

        url = (
            "https://api.line.me/v2/bot/message/broadcast"
        )

        payload = {
            "messages": [
                {
                    "type": "text",
                    "text": text
                }
            ]
        }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=TIMEOUT
    )

    if response.status_code == 200:

        print("✅ LINE 推播成功")

    else:

        print(
            f"❌ LINE 推播失敗 "
            f"{response.status_code}: "
            f"{response.text}"
        )


# ==========================================
# 8. 主程式
# ==========================================

def main():

    print("=" * 60)
    print("🚀 台股每日盤後情報 Bot")
    print("=" * 60)

    # --------------------------------------
    # 1. 台股
    # --------------------------------------

    print("\n1/6 📈 抓取台股大盤...")

    market = fetch_twse_market()

    print(market)

    # --------------------------------------
    # 2. 法人
    # --------------------------------------

    print("\n2/6 🏦 抓取三大法人...")

    institutions = fetch_institutional_data()

    print(institutions)

    # --------------------------------------
    # 3. 個股
    # --------------------------------------

    print("\n3/6 🔥 抓取個股排行...")

    stocks = fetch_stock_market()

    print(stocks)

    # --------------------------------------
    # 4. 新聞
    # --------------------------------------

    print("\n4/6 📰 抓取財經新聞...")

    news = fetch_finance_news()

    print(
        f"取得 {len(news)} 則新聞"
    )

    # --------------------------------------
    # 5. 國際
    # --------------------------------------

    print("\n5/6 🌎 抓取國際市場...")

    global_markets = fetch_global_markets()

    print(global_markets)

    # --------------------------------------
    # 6. Gemini
    # --------------------------------------

    print("\n6/6 🤖 產生 AI 盤後分析...")

    summary = generate_ai_summary(
        market,
        institutions,
        stocks,
        news,
        global_markets
    )

    print("\n" + "=" * 60)
    print(summary)
    print("=" * 60)

    # --------------------------------------
    # LINE
    # --------------------------------------

    print("\n📱 發送 LINE...")

    send_line_message(summary)

    print("\n🎉 今日任務完成！")


if __name__ == "__main__":
    main()
