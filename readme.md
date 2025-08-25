uvicorn main:app --reload --host 172.19.88.5 --port 8000

Invoke-WebRequest "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js" `
  -OutFile "C:\OneTouch Portal\static\js\chart.4.4.3.min.js"

github：https://github.com/Samuelhuang-com/onetouchportal

cd static/sample
python -m http.server 8080

http://127.0.0.1:8080/Neumorphism%20Button.html

python -m uvicorn main:app --reload

http://127.0.0.1:8000/debug/db-check

http://127.0.0.1:8000/admin/permissions

https://finsweet.com/lottieflow/category/404

https://www.themedevhub.com/


---

如果你希望這個 HTML 是透過 FastAPI route 顯示（而不是靜態檔），可以這樣寫：

<pre class="overflow-visible!" data-start="936" data-end="1326"><div class="contain-inline-size rounded-2xl relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"><span class="" data-state="closed"></span></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-python"><span><span>from</span><span> fastapi </span><span>import</span><span> FastAPI, Request
</span><span>from</span><span> fastapi.responses </span><span>import</span><span> HTMLResponse
</span><span>from</span><span> fastapi.templating </span><span>import</span><span> Jinja2Templates

app = FastAPI()
templates = Jinja2Templates(directory=</span><span>"templates"</span><span>)

</span><span>@app.get("/neumorphism"</span><span>, response_class=HTMLResponse)
</span><span>async</span><span></span><span>def</span><span></span><span>get_button</span><span>(</span><span>request: Request</span><span>):
    </span><span>return</span><span> templates.TemplateResponse(</span><span>"Neumorphism Button.html"</span><span>, {</span><span>"request"</span><span>: request})
</span></span></code></div></div></pre>

這樣就能透過：

<pre class="overflow-visible!" data-start="1336" data-end="1377"><div class="contain-inline-size rounded-2xl relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"><span class="" data-state="closed"></span></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>http:</span><span>//127.0.0.1:8000/neumorphism</span></span></code></div></div></pre>

---

| 住房率(總房數):

| 總住房數 / 總房間數                                        |
| ---------------------------------------------------------- |
| 住房率(可售房):(總住房數-HUS-COMP)/(總房間數-OOO-HUS-COMP) |
| 平均房價(總住房數):總金額/總住房數                         |
| 平均房價(售房合計):可售總金額/(總房間數-OOO+MBK-HUS-COMP)  |
| 平均房價(售房合計):可售總金額/(總房間數-OOO-HUS-COMP)      |
| 可售總金額：總房租-HUS-COMP                                |
| 總房租：尚未入住的已訂房房價為計算，已入住的以動態房價計   |
| 房型住房率：房型總庫存數/房型的合計                        |

# 一、資料分析要點（MSR02.xlsx）

**常用指標（自動計算）**

* 住房率 (Occupancy%) = `RoomsSold / RoomsAvailable`
* 平均房價 (ADR) = `RoomRevenue / RoomsSold`
* 每可供出租客房收入 (RevPAR) = `RoomRevenue / RoomsAvailable`
* 總收入 = `RoomRevenue + FBRevenue + OtherRevenue`
* YoY/MoM：依日期（建議用「傳票日期」）做同比/環比
* 取消/No Show 率（若有欄位）：`(Cancelled + NoShow) / AllBookings`

**欄位標準化（自動對照，同義詞支援）**

以下是程式內建的模糊對照，可彈性匹配常見欄位（不需硬改 Excel 標題）：

* 日期：`date`, `trans_date`, `posting_date`, `傳票日期`
* 已售房晚：`rooms_sold`, `sold_rooms`, `間夜`, `房晚`
* 可售房晚：`rooms_avail`, `available_rooms`, `可售`, `供應房晚`
* 房租收入：`room_revenue`, `rm_rev`, `房租收入`
* 餐飲收入：`fb_revenue`, `f&b`, `beverage`, `餐飲收入`
* 其他收入：`other_revenue`, `misc_revenue`, `其他收入`
* 通路：`channel`, `ota`, `booking_source`
* 方案/市場：`rate_plan`, `market_segment`

---

# 二、建議建立的 Web Pages（依優先順序）

1. **RV Overview（收入與房務總覽）**
   * KPI：收入、Occupancy、ADR、RevPAR、YoY/MoM
   * 圖：收入/RevPAR/ADR/Occupancy 時序線、7/28 天移動平均
   * 表：依月/週聚合、Top 通路、Top 方案、Top 市場
2. **Pickup & Pace（撿房/走勢）**
   * 圖：未來 N 天的訂房累積 vs 去年同期
   * 表：逐日新增訂房量（可選日期區間）
3. **Channel Mix（通路組合）**
   * 圓餅/堆疊：Direct vs OTA，各 OTA 佔比
   * 表：通路 KPI（ADR/取消率/No Show）
4. **Rate Plan & Segment（方案與市場）**
   * 圖：各方案 ADR/RevPAR 分佈
   * 表：市場別 RoomsSold/ADR/RevPAR/取消率
5. **Cancellation / No Show（取消與未到）**
   * 圖：取消率/No Show 率時序 + 事件標註（價格調整/專案）
   * 表：原因/通路/方案交叉分析
6. **YoY / MoM Compare（同比/環比）**
   * 圖：去年同期與今年對照
   * 表：差異拆解（房晚 vs 單價 vs 組合）
7. **Calendar Heatmap（月曆熱力）**
   * 圖：天維度 Occupancy 或 RevPAR 熱力格
   * 表：同日多維指標（供營運排班/定價參考）
