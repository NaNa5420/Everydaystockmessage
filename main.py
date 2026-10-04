import os
import datetime
import requests
import xml.etree.ElementTree as ET
from google import genai
from dotenv import load_dotenv
import time
from google.genai import errors

# 強制載入同目錄下的 .env 檔案並覆蓋記憶體中的舊變數
load_dotenv(dotenv_path=".env", override=True)

# ==========================================
# 1. 抓取台股數據 (台灣證券交易所 TWSE API)
# ==========================================
def fetch_twse_data():
    today_str = datetime.datetime.now().strftime("%Y%m%d")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    stock_info = {
        "日期": today_str,
        "大盤收盤": "暫無數據（可能為非交易日或尚未開盤）",
        "漲跌": "",
        "三大法人買賣超": {}
    }
    
    # (A) 抓取加權指數與漲跌
    url_mi = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={today_str}&type=IND&response=json"
    try:
        res = requests.get(url_mi, headers=headers, timeout=10)
        data = res.json()
        if data.get("stat") == "OK":
            for row in data.get("data1", []):
                if "發行量加權股價指數" in row[0]:
                    stock_info["大盤收盤"] = row[1]
                    stock_info["漲跌"] = f"{row[2]}{row[3]} ({row[4]}%)"
                    break
        else:
            print(f"ℹ️ 今日 TWSE 狀態：{data.get('stat')} (若為週末或假日無盤後數據屬正常現象)")
    except Exception as e:
        print(f"⚠️ 抓取大盤數據失敗: {e}")

    # (B) 抓取三大法人買賣超
    url_bfi = f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?date={today_str}&response=json"
    try:
        res = requests.get(url_bfi, headers=headers, timeout=10)
        data = res.json()
        if data.get("stat") == "OK":
            for row in data.get("data", []):
                name = row[0].strip()
                net_buy = row[3].strip()  # 買賣差額 (元)
                stock_info["三大法人買賣超"][name] = net_buy
    except Exception as e:
        print(f"⚠️ 抓取三大法人數據失敗: {e}")

    return stock_info


# ==========================================
# 2. 抓取當日重要財經新聞 (RSS)
# ==========================================
def fetch_finance_news():
    rss_url = "https://news.google.com/rss/search?q=台股+財經&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    news_list = []
    try:
        res = requests.get(rss_url, timeout=10)
        root = ET.fromstring(res.content)
        # 取前 8 則即時焦點新聞
        items = root.findall("./channel/item")[:8]
        for item in items:
            title = item.find("title").text
            if title:
                news_list.append(title)
    except Exception as e:
        print(f"⚠️ 抓取新聞失敗: {e}")
    return news_list


# ==========================================
# 3. 呼叫 Gemini AI 生成「今日台股重點」
# ==========================================
def generate_ai_summary(stock_info, news_list):
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        raise ValueError("❌ 錯誤：未設定 GEMINI_API_KEY 環境變數")

    # 去除前後空格與隱形字元
    gemini_api_key = gemini_api_key.strip()

    client = genai.Client(api_key=gemini_api_key)

    prompt = f"""
你是一位專業的台股財經分析師。請根據以下今日台股盤後數據與新聞標題，整理出一份精簡、條理分明且適合在 LINE 上閱讀的「今日台股重點速報」。

【盤後數據】
- 日期：{stock_info.get('日期')}
- 加權指數收盤：{stock_info.get('大盤收盤')}
- 漲跌幅：{stock_info.get('漲跌')}
- 三大法人買賣超：{stock_info.get('三大法人買賣超')}

【今日熱門財經新聞】
{chr(10).join(['- ' + news for news in news_list])}

【排版要求】
1. 包含以下章節標題：
   📈 【今日台股表現】
   🏛️ 【三大法人動向】
   📰 【市場重點新聞摘要】
   💡 【重點總結與觀察】
2. 適當運用 Emoji，語氣專業熱情。
3. 排版請適合手機螢幕閱讀，避免過長的內文段落，總字數控制在 300~500 字以內。
4. 若數據顯示為「暫無數據」，請在回覆中溫馨提醒讀者今日為休市/非交易日，並將重點轉為新聞摘要與未來市場展望。
"""

   # ✅ 指定最新的標準模型 gemini-3.8-flash
    models_to_try = ["gemini-3.8-flash"]

    for model_name in models_to_try:
        max_retries = 5  # 增加重試次數至 5 次       
        for attempt in range(1, max_retries + 1):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                return response.text
            except errors.ServerError as e:
                print(f"⚠️ 模型 {model_name} 伺服器忙碌 (503)，等待 3 秒後重試...")
                time.sleep(3)
            except Exception as e:
                print(f"⚠️ 模型 {model_name} 呼叫失敗 ({e})，準備切換備用模型...")
                break

    raise RuntimeError("❌ 所有 Gemini 模型皆無法回應，請稍後再試。")


# ==========================================
# 4. LINE Messaging API 推播訊息
# ==========================================
def send_line_message(text):
    line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.getenv("LINE_USER_ID")  # 個人/群組 ID (選填)

    if not line_token:
        raise ValueError("❌ 錯誤：未設定 LINE_CHANNEL_ACCESS_TOKEN 環境變數")

    line_token = line_token.strip()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {line_token}"
    }

    # 防護機制：LINE 文字單則上限 2000 字
    if len(text) > 2000:
        text = text[:1995] + "\n..."

    # 如果有指定 LINE_USER_ID 就用 Push API；沒有的話改用 Broadcast API 推播給所有好友
    if user_id and user_id.strip():
        url = "https://api.line.me/v2/bot/message/push"
        payload = {
            "to": user_id.strip(),
            "messages": [{"type": "text", "text": text}]
        }
    else:
        url = "https://api.line.me/v2/bot/message/broadcast"
        payload = {
            "messages": [{"type": "text", "text": text}]
        }

    res = requests.post(url, headers=headers, json=payload)
    if res.status_code == 200:
        print("✅ LINE 訊息推播成功！")
    else:
        print(f"❌ LINE 訊息推播失敗 ({res.status_code}): {res.text}")


# ==========================================
# 主程式流程
# ==========================================
def main():
    print("🚀 開始執行台股每日推播任務...")
    
    print("1/4 抓取盤後數據...")
    stock_info = fetch_twse_data()

    print("2/4 抓取財經新聞...")
    news_list = fetch_finance_news()

    print("3/4 呼叫 Gemini AI 摘要重點...")
    ai_summary = generate_ai_summary(stock_info, news_list)
    print("\n--- [Gemini 生成內容] ---")
    print(ai_summary)
    print("------------------------\n")

    print("4/4 發送 LINE 推播...")
    send_line_message(ai_summary)

if __name__ == "__main__":
    main()