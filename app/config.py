"""应用配置：从 .env 读取。"""
import os

from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()

# 数据库文件放项目 data/ 目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "data", "leads.db"))

POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "10"))
DAILY_SEND_LIMIT = int(os.getenv("DAILY_SEND_LIMIT", "30"))

# 双层扫描参数（深度扫描用）
QUICK_INTERVAL_MINUTES = int(os.getenv("QUICK_INTERVAL_MINUTES", "5"))
DEEP_INTERVAL_MINUTES = int(os.getenv("DEEP_INTERVAL_MINUTES", "30"))
QUICK_VIDEOS = int(os.getenv("QUICK_VIDEOS", "5"))
DEEP_VIDEOS = int(os.getenv("DEEP_VIDEOS", "30"))
COMMENT_SCROLLS_QUICK = int(os.getenv("COMMENT_SCROLLS_QUICK", "2"))
COMMENT_SCROLLS_DEEP = int(os.getenv("COMMENT_SCROLLS_DEEP", "8"))

# 各平台扫描间隔（分钟）——按平台独立节奏
PLATFORM_SCAN_INTERVALS = {
    "douyin": int(os.getenv("SCAN_INTERVAL_DOUYIN_MINUTES", "720")),
    "xiaohongshu": int(os.getenv("SCAN_INTERVAL_XIAOHONGSHU_MINUTES", "1440")),
    "kuaishou": int(os.getenv("SCAN_INTERVAL_KUAISHOU_MINUTES", "1440")),
}

# 只保留最近多少天内的评论（太老的没价值）
COMMENT_MAX_AGE_DAYS = int(os.getenv("COMMENT_MAX_AGE_DAYS", "60"))

# AI 批量筛选：一次调用同时判断多少条评论（越大越快，20 是稳妥值）
CLASSIFY_BATCH_SIZE = int(os.getenv("CLASSIFY_BATCH_SIZE", "20"))

PORT = int(os.getenv("PORT", "8080"))
