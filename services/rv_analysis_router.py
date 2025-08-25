from fastapi import APIRouter, Request, HTTPException, Query, Cookie  # pyright: ignore[reportMissingImports] # ← 加 Cookie
from fastapi.responses import ( # pyright: ignore[reportMissingImports]
    JSONResponse,
    RedirectResponse,
    HTMLResponse,
)  # ← 加 RedirectResponse/HTMLResponse
from typing import Optional  # ← 加 Optional
from app_utils import get_base_context
import pandas as pd # pyright: ignore[reportMissingModuleSource]
import io
import base64
import matplotlib.pyplot as plt # pyright: ignore[reportMissingModuleSource]
import seaborn as sns # pyright: ignore[reportMissingModuleSource]
from typing import Dict, Any

# --- 常數定義 ---
DATA_FILE = "data/MSR02.xlsx"  # 指定您的資料檔案路徑


def create_rv_analysis_report(start_date: str, end_date: str) -> Dict[str, Any]:
    """
    讀取 MSR02.xlsx 檔案，根據日期區間進行住房率分析，並生成報表數據與圖表。

    Args:
        start_date (str): 開始日期 (YYYY-MM-DD)
        end_date (str): 結束日期 (YYYY-MM-DD)

    Returns:
        Dict[str, Any]: 一個包含分析結果的字典，包括：
                        - 'results': 每日詳細數據列表
                        - 'summary': 區間總結統計
                        - 'chart_image': Base64 編碼的趨勢圖
    """
    try:
        # 1. 讀取並清理資料
        # MSR02.xlsx 的格式特殊，前幾行為標頭，我們需要跳過它們
        df = pd.read_csv(DATA_FILE, skiprows=5)

        # 根據檔案格式，選取我們需要的欄位並重新命名
        # 欄位索引: 0=日期, 8=總房租, 14=可售房數, 15=售房合計
        cols_to_use = {
            df.columns[0]: "Date",
            df.columns[8]: "TotalRevenue",
            df.columns[14]: "AvailableRooms",
            df.columns[15]: "SoldRooms",
        }
        df = df[list(cols_to_use.keys())].rename(columns=cols_to_use)

        # 轉換日期格式並過濾無效資料
        df["Date"] = pd.to_datetime(
            df["Date"].astype(str), format="%Y%m%d", errors="coerce"
        )
        df.dropna(subset=["Date"], inplace=True)

        # 轉換數值欄位，錯誤的轉為 NaN
        numeric_cols = ["TotalRevenue", "AvailableRooms", "SoldRooms"]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # 過濾掉可售房數為 0 或空值的資料，避免計算錯誤
        df = df[df["AvailableRooms"] > 0].copy()
        df.dropna(subset=numeric_cols, inplace=True)

        # 2. 根據日期區間篩選
        mask = (df["Date"] >= pd.to_datetime(start_date)) & (
            df["Date"] <= pd.to_datetime(end_date)
        )
        df_filtered = df.loc[mask].copy()

        if df_filtered.empty:
            raise ValueError("在指定的日期區間內沒有找到有效的數據。")

        # 3. 計算核心指標 (KPIs)
        # 入住率 (%)
        df_filtered["OccupancyRate"] = (
            df_filtered["SoldRooms"] / df_filtered["AvailableRooms"]
        )
        # 平均房價 (ADR)
        df_filtered["ADR"] = df_filtered.apply(
            lambda row: (
                row["TotalRevenue"] / row["SoldRooms"] if row["SoldRooms"] > 0 else 0
            ),
            axis=1,
        )
        # 每間可售客房收入 (RevPAR)
        df_filtered["RevPAR"] = (
            df_filtered["TotalRevenue"] / df_filtered["AvailableRooms"]
        )

        # 4. 計算區間總結
        summary = {
            "total_revenue": df_filtered["TotalRevenue"].sum(),
            "avg_occupancy_rate": df_filtered["OccupancyRate"].mean(),
            "avg_adr": df_filtered[df_filtered["ADR"] > 0][
                "ADR"
            ].mean(),  # 只計算有售房時的 ADR
            "avg_revpar": df_filtered["RevPAR"].mean(),
            "total_available_rooms": df_filtered["AvailableRooms"].sum(),
            "total_sold_rooms": df_filtered["SoldRooms"].sum(),
        }

        # 5. 生成趨勢圖
        sns.set_theme(style="whitegrid", font="Microsoft JhengHei")
        plt.rcParams["axes.unicode_minus"] = False

        fig, axes = plt.subplots(4, 1, figsize=(12, 16), sharex=True)
        fig.suptitle(f"住房營運分析 ({start_date} to {end_date})", fontsize=20, y=0.95)

        # 繪製圖表
        sns.lineplot(
            ax=axes[0], x="Date", y="TotalRevenue", data=df_filtered, marker="o"
        ).set_title("每日總營收 (Total Revenue)", fontsize=14)
        sns.lineplot(
            ax=axes[1],
            x="Date",
            y="OccupancyRate",
            data=df_filtered,
            marker="o",
            color="g",
        ).set_title("每日入住率 (Occupancy Rate)", fontsize=14)
        sns.lineplot(
            ax=axes[2], x="Date", y="ADR", data=df_filtered, marker="o", color="r"
        ).set_title("每日平均房價 (ADR)", fontsize=14)
        sns.lineplot(
            ax=axes[3],
            x="Date",
            y="RevPAR",
            data=df_filtered,
            marker="o",
            color="purple",
        ).set_title("每日每房收益 (RevPAR)", fontsize=14)

        axes[1].yaxis.set_major_formatter(
            plt.FuncFormatter("{:.0%}".format)
        )  # 將 Y 軸格式化為百分比
        fig.autofmt_xdate()
        plt.tight_layout(rect=[0, 0, 1, 0.95])

        # 將圖表儲存到記憶體中並轉為 Base64
        buf = io.BytesIO()
        plt.savefig(buf, format="png")
        plt.close(fig)
        chart_image = base64.b64encode(buf.getvalue()).decode("utf-8")

        return {
            "results": df_filtered.to_dict("records"),
            "summary": summary,
            "chart_image": chart_image,
        }

    except FileNotFoundError:
        raise FileNotFoundError(f"資料檔案 '{DATA_FILE}' 不存在。")
    except Exception as e:
        # 向上拋出其他所有錯誤
        raise e


# ---------------- Routes ----------------
@router.get("/rv", response_class=HTMLResponse)
async def rv_overview_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    # 未登入就導去登入
    if not user:
        return RedirectResponse(url="/login")

    # 建立與其他頁面一致的共用 context（含 nav_items / 標題需要的資料）
    ctx = get_base_context(request, user, role, permissions)

    # （可選）權限保護：需有 report 權限或 admin
    if role != "admin" and not ctx["permissions"].get("report", False):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    # 用相同的 base.html 版型渲染，就會有左側選單與上方 Title
    return templates.TemplateResponse("rv_overview.html", ctx)
