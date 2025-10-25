from dotenv import load_dotenv
import os

load_dotenv()

OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE")
OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com")

if not OKX_API_KEY or not OKX_SECRET_KEY or not OKX_PASSPHRASE:
    raise ValueError("❌ 缺少 OKX API 配置，请检查 .env 文件。")


DEEPSEEK_API_KEY = "sk-8ba30dbdcb814f75b3ba6141ab220163"
DEEPSEEK_API_BASE = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"