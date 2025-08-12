# data_manager.py
import pandas as pd
from config import PERMISSION_FILE

def get_all_users():
    """獲取所有使用者列表"""
    try:
        df = pd.read_excel(PERMISSION_FILE, sheet_name="employeesfiles")
        return df.to_dict("records")
    except FileNotFoundError:
        return []

# ... 其他讀取資料的函式
