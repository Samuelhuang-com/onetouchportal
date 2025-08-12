# config.py
import os

# 專案根目錄
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 資料夾路徑
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

# 資料檔案路徑
PERMISSION_FILE = os.path.join(DATA_DIR, "Permissioncontrol.xlsx")
CONTRACTS_FILE = os.path.join(DATA_DIR, "contracts.xlsx")
# ... 其他檔案路徑